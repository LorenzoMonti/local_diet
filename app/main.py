import asyncio
import json
import uuid
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import markdown as md
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import anonymize, assistant, db, exchange, pii, shopping

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    # Piani rimasti a metà per un riavvio del server.
    with db.tx() as c:
        c.execute("UPDATE plans SET status='error', error='Interrotto dal riavvio del server' WHERE status='generating'")
    yield


app = FastAPI(title="Dieta", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["md"] = lambda t: md.markdown((t or "").replace("<", "&lt;"), extensions=["tables", "sane_lists"])
templates.env.filters["qty"] = shopping.fmt_qty
templates.env.filters["itdate"] = lambda v, short=False: _itdate(v, short)
templates.env.globals["icon"] = templates.env.get_template("_icons.html").module.icon

_background: set[asyncio.Task] = set()


def _itdate(value: str | None, short: bool = False) -> str:
    """'2026-10-02[ 12:00:00]' -> '02/10/2026' (o '02/10' con short)."""
    if not value or len(value) < 10 or value[4] != "-":
        return value or ""
    y, m, d = value[:4], value[5:7], value[8:10]
    return f"{d}/{m}" if short else f"{d}/{m}/{y}"
PROFILE_FIELDS = ["diet", "conditions", "allergies", "dislikes", "likes", "goals", "sex", "age",
                  "height_cm", "weight_kg", "activity", "notes", "redact_terms"]


@app.middleware("http")
async def require_user(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/static", "/chi-sei")):
        return await call_next(request)
    uid = request.cookies.get("uid")
    if not uid or not uid.isdigit() or not db.user(int(uid)):
        return RedirectResponse("/chi-sei", 303)
    request.state.user = db.user(int(uid))
    return await call_next(request)


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"me": getattr(request.state, "user", None),
                                                      "users": db.users(), "page": name.split(".")[0],
                                                      "model_label": assistant.model_label(),
                                                      "today": date.today().strftime("%d/%m"), **ctx})


def back(url: str):
    return RedirectResponse(url, 303)


# --- utente ------------------------------------------------------------------

@app.get("/chi-sei", response_class=HTMLResponse)
def who(request: Request):
    return render(request, "who.html")


@app.post("/chi-sei/{uid}")
def set_user(uid: int):
    if not db.user(uid):
        raise HTTPException(404)
    r = back("/")
    r.set_cookie("uid", str(uid), max_age=60 * 60 * 24 * 365, samesite="lax")
    return r


@app.get("/")
def home():
    return back("/piano")


@app.get("/profilo", response_class=HTMLResponse)
def profile(request: Request):
    return render(request, "profile.html", notes=db.notes_for())


@app.post("/profilo")
async def save_profile(request: Request):
    form = await request.form()
    profile = {k: str(form.get(k, "")).strip() for k in PROFILE_FIELDS}
    with db.tx() as c:
        c.execute("UPDATE users SET name=?, profile=? WHERE id=?",
                  (str(form.get("name") or request.state.user["name"]).strip(), json.dumps(profile),
                   request.state.user["id"]))
    return back("/profilo?msg=Profilo+salvato")


@app.post("/note")
def add_note(request: Request, content: str = Form(...), about: str = Form("me")):
    uid = request.state.user["id"] if about == "me" else None
    with db.tx() as c:
        c.execute("INSERT INTO notes (user_id, content) VALUES (?, ?)", (uid, content.strip()))
    return back("/profilo#note")


@app.post("/note/{nid}/elimina")
def delete_note(nid: int):
    with db.tx() as c:
        c.execute("DELETE FROM notes WHERE id=?", (nid,))
    return back("/profilo#note")


# --- piano -------------------------------------------------------------------

@app.get("/piano", response_class=HTMLResponse)
@app.get("/piano/{pid:int}", response_class=HTMLResponse)
def plan_page(request: Request, pid: int | None = None):
    with db.tx() as c:
        plans = [dict(r) for r in c.execute(
            "SELECT id, week_start, created_at, status, error, source FROM plans ORDER BY id DESC LIMIT 20")]
        row = (c.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone() if pid
               else c.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone())
    plan = {**dict(row), "data": json.loads(row["data"]) if row["data"] else None} if row else None
    active = db.active_plan()
    next_monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7)
    return render(request, "plan.html", plan=plan, plans=plans, active_id=active and active["id"],
                  next_monday=next_monday.isoformat(), meals=assistant.MEALS)


@app.post("/piano/genera")
async def plan_generate(request: Request):
    form = await request.form()
    week_start = str(form.get("week_start") or date.today().isoformat())
    req = str(form.get("request") or "")
    meals = form.getlist("meals") or ["colazione", "pranzo", "cena"]
    with db.tx() as c:
        try:
            source = assistant.backend().current_model("structured")
        except assistant.LLMError:
            source = None
        pid = c.execute("INSERT INTO plans (week_start, request, source) VALUES (?, ?, ?)",
                        (week_start, req, source)).lastrowid
    task = asyncio.create_task(assistant.generate_plan(pid, week_start, req, meals))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return back(f"/piano/{pid}")


@app.get("/piano/scambia", response_class=HTMLResponse)
def plan_exchange(request: Request, pid: int | None = None, week_start: str = "", anon: int = 1, labs: int = 1,
                  base: int = 1, req: str = "", errore: str = "", testo: str = ""):
    q = request.query_params
    if "opt" in q:  # form inviato: le checkbox assenti valgono "no"
        anon, labs = int(bool(q.get("anon"))), int(bool(q.get("labs")))
    meals = q.getlist("meals") or ["colazione", "pranzo", "cena"]
    plan = None
    if base:
        if pid:
            with db.tx() as c:
                r = c.execute("SELECT * FROM plans WHERE id=? AND status='ready'", (pid,)).fetchone()
            plan = {**dict(r), "data": json.loads(r["data"])} if r else None
        else:
            plan = db.active_plan()
    if not week_start:
        week_start = plan["week_start"] if plan else (date.today() + timedelta(days=(7 - date.today().weekday()) % 7)).isoformat()
    prompt = exchange.export_prompt(plan, week_start, bool(anon), bool(labs), req, meals)
    return render(request, "exchange.html", prompt=prompt, plan=plan, week_start=week_start, anon=anon, labs=labs,
                  base=base, req=req, sel_meals=meals, meals=assistant.MEALS, error=errore, pasted=testo,
                  has_plan=db.active_plan() is not None)


@app.post("/piano/importa")
def plan_import(text: str = Form(...), week_start: str = Form(...), source: str = Form("Claude")):
    try:
        data = exchange.parse_import(text)
    except exchange.ImportError_ as e:
        from urllib.parse import urlencode
        return back("/piano/scambia?" + urlencode({"errore": str(e), "week_start": week_start}) + "#importa")
    with db.tx() as c:
        pid = c.execute("INSERT INTO plans (week_start, status, request, data, source) VALUES (?, 'ready', ?, ?, ?)",
                        (week_start, "Importato da assistente esterno", json.dumps(data),
                         f"{source.strip() or 'esterno'} (importato)")).lastrowid
    shopping.rebuild(pid)
    db.activate_plan(pid)
    return back(f"/piano/{pid}?msg=Piano+importato:+lista+della+spesa+aggiornata")


@app.post("/piano/{pid}/ingredienti")
async def plan_fill_ingredients(pid: int):
    with db.tx() as c:
        r = c.execute("SELECT data FROM plans WHERE id=? AND status='ready'", (pid,)).fetchone()
    if not r:
        raise HTTPException(404)
    try:
        data = await assistant.fill_ingredients(json.loads(r["data"]))
    except (assistant.LLMError, ValueError) as e:
        return back(f"/piano/{pid}?msg=" + quote(f"Non sono riuscito a ricavare gli ingredienti: {e}"[:200]))
    with db.tx() as c:
        c.execute("UPDATE plans SET data=? WHERE id=?", (json.dumps(data), pid))
    shopping.rebuild(pid)
    return back(f"/piano/{pid}?msg=Ingredienti+ricavati:+lista+della+spesa+aggiornata")


@app.post("/piano/{pid}/attiva")
def plan_activate(pid: int):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM plans WHERE id=? AND status='ready'", (pid,)).fetchone():
            raise HTTPException(404)
        has_items = c.execute("SELECT 1 FROM shopping_items WHERE plan_id=? AND manual=0", (pid,)).fetchone()
    db.activate_plan(pid)
    if not has_items:
        shopping.rebuild(pid)
    return back(f"/piano/{pid}?msg=Piano+attivo:+lista+della+spesa+aggiornata")


@app.get("/piano/{pid}/stato")
def plan_status(pid: int):
    with db.tx() as c:
        r = c.execute("SELECT status FROM plans WHERE id=?", (pid,)).fetchone()
    return {"status": r["status"] if r else "missing"}


@app.post("/piano/{pid}/elimina")
def plan_delete(pid: int):
    with db.tx() as c:
        c.execute("DELETE FROM shopping_items WHERE plan_id=? AND manual=0", (pid,))
        c.execute("UPDATE shopping_items SET plan_id=NULL WHERE plan_id=?", (pid,))  # le voci extra restano
        c.execute("DELETE FROM plans WHERE id=?", (pid,))
    return back("/piano")


# --- spesa -------------------------------------------------------------------

@app.get("/spesa", response_class=HTMLResponse)
def shopping_page(request: Request):
    plan = db.active_plan()
    return render(request, "shopping.html", plan=plan, groups=shopping.grouped(plan and plan["id"]),
                  categories=assistant.CATEGORIES)


@app.post("/spesa/{iid}/toggle")
def shopping_toggle(iid: int):
    with db.tx() as c:
        c.execute("UPDATE shopping_items SET checked = 1 - checked WHERE id=?", (iid,))
    return {"ok": True}


@app.post("/spesa/aggiungi")
def shopping_add(name: str = Form(...), quantity: str = Form(""), unit: str = Form(""),
                 category: str = Form("Altro")):
    try:
        q = float(quantity.replace(",", ".")) if quantity.strip() else None
    except ValueError:
        q = None
    shopping.add_manual([{"name": name.strip(), "quantity": q, "unit": unit.strip(), "category": category}])
    return back("/spesa?msg=Aggiunto+alla+lista")


@app.post("/spesa/{iid}/elimina")
def shopping_delete(iid: int):
    with db.tx() as c:
        c.execute("DELETE FROM shopping_items WHERE id=?", (iid,))
    return back("/spesa")


@app.post("/spesa/rigenera")
def shopping_rebuild():
    plan = db.active_plan()
    if plan:
        shopping.rebuild(plan["id"])
    return back("/spesa")


@app.post("/spesa/svuota-spuntati")
def shopping_clear_checked():
    with db.tx() as c:
        c.execute("DELETE FROM shopping_items WHERE checked=1 AND manual=1")
    return back("/spesa")


# --- file / esami ---------------------------------------------------------------

def _own_file(request: Request, fid: int) -> dict:
    with db.tx() as c:
        r = c.execute("SELECT * FROM files WHERE id=? AND user_id=?", (fid, request.state.user["id"])).fetchone()
    if not r:
        raise HTTPException(404)
    return {**dict(r), "labs": json.loads(r["labs"]) if r["labs"] else None}


@app.get("/file", response_class=HTMLResponse)
def files_page(request: Request):
    with db.tx() as c:
        files = [dict(r) for r in c.execute(
            "SELECT id, filename, uploaded_at, exam_date, approved, labs IS NOT NULL AS has_labs "
            "FROM files WHERE user_id=? ORDER BY id DESC", (request.state.user["id"],))]
    return render(request, "files.html", files=files)


async def _anonymize_file(me: dict, path: Path, filename: str) -> tuple[str, str]:
    """(testo anonimizzato, metodo). rizzo-pii + regole; solo regole se il servizio non risponde."""
    terms = [me["name"]] + me["profile"].get("redact_terms", "").split(",")
    if pii.MODE in ("off", "rules"):
        try:
            text = anonymize.extract_pdf_text(path)
        except Exception:  # noqa: BLE001 - PDF illeggibile
            text = ""
        if not text:
            return "", "nessun testo estratto"
        if pii.MODE == "off":
            return text, "nessuna (modello in locale)"
        return anonymize.anonymize(text, terms), "solo regole locali"
    try:
        text = await pii.anonymize_pdf(path, filename)
        method = "rizzo-pii + regole"
    except pii.PIIUnavailable as e:
        try:
            text = anonymize.extract_pdf_text(path)
        except Exception:  # noqa: BLE001 - PDF illeggibile
            text = ""
        method = f"solo regole ({e})" if text else "nessun testo estratto"
    # Le regole locali passano comunque: secondo filtro + termini personali del profilo.
    return (anonymize.anonymize(text, terms) if text else ""), method


@app.post("/file/carica")
async def file_upload(request: Request, upload: UploadFile = File(...)):
    me = request.state.user
    if not (upload.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Sono supportati solo PDF")
    dest_dir = db.UPLOAD_DIR / str(me["id"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}.pdf"
    dest.write_bytes(await upload.read())
    anon, method = await _anonymize_file(me, dest, upload.filename)
    with db.tx() as c:
        fid = c.execute("INSERT INTO files (user_id, filename, stored_path, anon_text, anon_method) VALUES (?,?,?,?,?)",
                        (me["id"], upload.filename, str(dest), anon, method)).lastrowid
    return back(f"/file/{fid}")


@app.post("/file/{fid}/rianonimizza")
async def file_reanonymize(request: Request, fid: int):
    f = _own_file(request, fid)
    anon, method = await _anonymize_file(request.state.user, Path(f["stored_path"]), f["filename"])
    with db.tx() as c:
        c.execute("UPDATE files SET anon_text=?, anon_method=?, approved=0 WHERE id=?", (anon, method, fid))
    return back(f"/file/{fid}")


@app.get("/file/{fid}", response_class=HTMLResponse)
def file_detail(request: Request, fid: int, errore: str = ""):
    return render(request, "file_detail.html", f=_own_file(request, fid), error=errore)


@app.get("/file/{fid}/pdf")
def file_pdf(request: Request, fid: int):
    f = _own_file(request, fid)
    return FileResponse(f["stored_path"], media_type="application/pdf", filename=f["filename"])


@app.post("/file/{fid}/approva")
def file_approve(request: Request, fid: int, anon_text: str = Form(...), exam_date: str = Form("")):
    _own_file(request, fid)
    with db.tx() as c:
        c.execute("UPDATE files SET anon_text=?, exam_date=?, approved=1 WHERE id=?",
                  (anon_text, exam_date or None, fid))
    return back(f"/file/{fid}?msg=Testo+approvato")


@app.post("/file/{fid}/revoca")
def file_revoke(request: Request, fid: int):
    _own_file(request, fid)
    with db.tx() as c:
        c.execute("UPDATE files SET approved=0 WHERE id=?", (fid,))
    return back(f"/file/{fid}")


@app.post("/file/{fid}/estrai")
async def file_extract(request: Request, fid: int):
    f = _own_file(request, fid)
    if not f["approved"]:
        raise HTTPException(400, "Approva prima il testo anonimizzato")
    try:
        labs = await assistant.extract_labs(f["anon_text"])
    except (assistant.LLMError, ValueError) as e:
        return back(f"/file/{fid}?errore={str(e)[:200]}")
    with db.tx() as c:
        c.execute("UPDATE files SET labs=?, exam_date=COALESCE(NULLIF(?, ''), exam_date) WHERE id=?",
                  (json.dumps(labs), labs.get("exam_date", ""), fid))
    return back(f"/file/{fid}")


@app.post("/file/{fid}/elimina")
def file_delete(request: Request, fid: int):
    f = _own_file(request, fid)
    Path(f["stored_path"]).unlink(missing_ok=True)
    with db.tx() as c:
        c.execute("DELETE FROM files WHERE id=?", (fid,))
    return back("/file")


# --- impostazioni -----------------------------------------------------------------

@app.get("/impostazioni", response_class=HTMLResponse)
async def settings_page(request: Request, tutti: int = 0, msg: str = "", errore: str = ""):
    b = assistant.backend()
    models, error = None, errore
    try:
        models = await b.list_models()
    except assistant.LLMError as e:
        error = error or str(e)
    if models and not tutti and any(m["free"] is not None for m in models):
        models = [m for m in models if m["free"]]
    chat = db.get_setting("model_chat") or ""
    return render(request, "settings.html", models=models, show_all=bool(tutti), msg=msg, error=error,
                  provider=assistant.PROVIDER, base_url=getattr(b, "BASE_URL", ""),
                  model_chat=chat, model_structured=db.get_setting("model_structured") or "",
                  default_model=getattr(b, "DEFAULT_MODEL", "") or getattr(b, "MODEL", ""))


@app.post("/impostazioni")
def settings_save(model_chat: str = Form(""), model_structured: str = Form(""), custom_chat: str = Form("")):
    chat = (custom_chat or model_chat).strip()
    if chat:  # nessuna scelta = si tiene quello attuale
        db.set_setting("model_chat", chat)
    db.set_setting("model_structured", "" if model_structured == "same" else model_structured.strip())
    return back("/impostazioni?msg=Modelli+salvati")


@app.post("/impostazioni/prova")
async def settings_test(model: str = Form(...)):
    b = assistant.backend()
    t0 = asyncio.get_running_loop().time()
    try:
        reply = await b.ping(model)
    except assistant.LLMError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "message": f"Risposta: «{reply[:80]}» in {asyncio.get_running_loop().time() - t0:.1f}s"}


# --- chat --------------------------------------------------------------------

def _own_conv(request: Request, cid: int) -> dict:
    with db.tx() as c:
        r = c.execute("SELECT * FROM conversations WHERE id=? AND user_id=?",
                      (cid, request.state.user["id"])).fetchone()
    if not r:
        raise HTTPException(404)
    return dict(r)


@app.get("/chat", response_class=HTMLResponse)
@app.get("/chat/{cid}", response_class=HTMLResponse)
def chat_page(request: Request, cid: int | None = None):
    uid = request.state.user["id"]
    with db.tx() as c:
        convs = [dict(r) for r in c.execute(
            "SELECT id, title, created_at FROM conversations WHERE user_id=? ORDER BY id DESC", (uid,))]
    if cid is None and convs:
        return back(f"/chat/{convs[0]['id']}")
    conv, msgs = None, []
    if cid is not None:
        conv = _own_conv(request, cid)
        with db.tx() as c:
            msgs = [dict(r) for r in c.execute(
                "SELECT role, display FROM messages WHERE conversation_id=? AND display IS NOT NULL ORDER BY id",
                (cid,))]
    stale = bool(conv and conv["provider"] and conv["provider"] != assistant.PROVIDER)
    return render(request, "chat.html", convs=convs, conv=conv, msgs=msgs, stale=stale)


@app.post("/chat/nuova")
def chat_new(request: Request):
    with db.tx() as c:
        cid = c.execute("INSERT INTO conversations (user_id) VALUES (?)", (request.state.user["id"],)).lastrowid
    return back(f"/chat/{cid}")


@app.post("/chat/{cid}/elimina")
def chat_delete(request: Request, cid: int):
    _own_conv(request, cid)
    with db.tx() as c:
        c.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return back("/chat")


@app.post("/chat/{cid}/invia")
async def chat_send(request: Request, cid: int):
    _own_conv(request, cid)
    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400)
    uid = request.state.user["id"]

    async def events():
        def sse(obj):
            return f"data: {json.dumps(obj)}\n\n"
        try:
            async for ev in assistant.chat_turn(cid, uid, text):
                yield sse(ev)
            yield sse({"type": "done"})
        except assistant.LLMError as e:
            yield sse({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            yield sse({"type": "error", "message": f"Errore: {e}"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/anteprima-md")
async def md_preview(request: Request):
    body = await request.json()
    return HTMLResponse(templates.env.filters["md"](body.get("text", "")))
