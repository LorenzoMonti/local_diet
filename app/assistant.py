"""Logica comune a tutti i modelli: schemi, contesto, prompt, strumenti della chat.

Il modello vero e proprio sta in app/backends/ e si sceglie con LLM_PROVIDER:
  - "claude": Claude API (SDK Anthropic)
  - "openai": qualunque API compatibile OpenAI (Gemini, Groq, OpenRouter, Mistral, Ollama…)
"""
import hashlib
import json
import os
import re
from datetime import date
from typing import AsyncIterator, Literal

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from . import db, shopping

PROVIDER = os.environ.get("LLM_PROVIDER", "openai").strip().lower()

MEALS = ["colazione", "spuntino", "pranzo", "merenda", "cena"]
CATEGORIES = [
    "Frutta e verdura", "Legumi", "Cereali, pane e pasta", "Proteine vegetali (tofu, tempeh, seitan)",
    "Latticini e uova", "Frutta secca e semi", "Dispensa e condimenti", "Surgelati", "Bevande", "Altro",
]
UNITS = ["g", "ml", "pz", "cucchiai", "cucchiaini", "q.b."]


class LLMError(Exception):
    """Errore del modello, con un messaggio leggibile per la UI."""


# --- schemi -----------------------------------------------------------------

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


INGREDIENT_SCHEMA = _obj({
    "name": {"type": "string", "description": "Nome generico al singolare, es. 'lenticchie rosse'"},
    "quantity": {"type": "number", "description": "Quantità totale per entrambe le persone; 0 se q.b."},
    "unit": {"type": "string", "enum": UNITS},
    "category": {"type": "string", "enum": CATEGORIES},
})
MEAL_SCHEMA = _obj({
    "meal": {"type": "string", "enum": MEALS},
    "title": {"type": "string"},
    "description": {"type": "string", "description": "Preparazione sintetica (2-4 frasi)"},
    "prep_minutes": {"type": "integer"},
    "ingredients": {"type": "array", "items": INGREDIENT_SCHEMA},
    "per_person": {
        "type": "array",
        "description": "Porzione/variante per ciascuna persona",
        "items": _obj({"person": {"type": "string"}, "note": {"type": "string"}}),
    },
})
PLAN_SCHEMA = _obj({
    "summary": {"type": "string", "description": "Logica del piano in 3-5 frasi"},
    "guidelines": {
        "type": "array",
        "description": "Consigli pratici per la settimana, con la persona a cui si riferiscono",
        "items": _obj({"person": {"type": "string"}, "tip": {"type": "string"}}),
    },
    "days": {
        "type": "array",
        "items": _obj({"day": {"type": "string"}, "meals": {"type": "array", "items": MEAL_SCHEMA}}),
    },
})
LABS_SCHEMA = _obj({
    "exam_date": {"type": "string", "description": "Data del prelievo YYYY-MM-DD, stringa vuota se assente"},
    "results": {
        "type": "array",
        "items": _obj({
            "name": {"type": "string"},
            "value": {"type": "string"},
            "unit": {"type": "string"},
            "reference_range": {"type": "string"},
            "flag": {"type": "string", "enum": ["alto", "basso", "normale", "non indicato"]},
        }),
    },
})


UNIT_ALIASES = {"gr": "g", "gr.": "g", "grammi": "g", "grammo": "g", "ml.": "ml", "millilitri": "ml",
                "pezzi": "pz", "pezzo": "pz", "pz.": "pz", "unità": "pz", "unita": "pz",
                "cucchiaio": "cucchiai", "cucchiaino": "cucchiaini", "qb": "q.b.", "q.b": "q.b.",
                "quanto basta": "q.b.", "chilogrammi": "kg", "litro": "l", "litri": "l"}
_QTY_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([a-zA-Zà-ù.]+)?\s*$")
# Ingrediente scritto come frase: "200 g di lenticchie", "lenticchie 200 g", "olio q.b."
_ING_LEAD = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(kg|g|gr\.?|grammi|ml|l|litri|pz|pezzi|cucchiai[on]?|cucchiaini?)?\.?\s+(?:di\s+|d')?(.+)$", re.I)
_ING_TRAIL = re.compile(r"^(.+?)[\s:,-]+(\d+(?:[.,]\d+)?)\s*(kg|g|gr\.?|grammi|ml|l|litri|pz|pezzi|cucchiai[on]?|cucchiaini?)?\.?\s*$", re.I)


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:[.,]\d+)?", str(v or ""))
    return float(m.group().replace(",", ".")) if m else 0.0


class Ingredient(BaseModel):
    name: str
    quantity: float = 0
    unit: str = "q.b."
    category: str = "Altro"

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v):
        # I modelli più piccoli scrivono gli ingredienti in molti modi diversi: li riportiamo a un oggetto.
        if isinstance(v, str):
            text = v.strip().rstrip(".")
            if m := _ING_LEAD.match(text):
                return {"name": m.group(3), "quantity": m.group(1), "unit": m.group(2) or "pz"}
            if m := _ING_TRAIL.match(text):
                return {"name": m.group(1), "quantity": m.group(2), "unit": m.group(3) or "pz"}
            return {"name": re.sub(r"\s*q\.?\s*b\.?$", "", text, flags=re.I), "quantity": 0, "unit": "q.b."}
        if isinstance(v, dict):
            qty = _pick(v, "quantity", "quantita", "quantità", "qty", "amount", "grams", "grammi", default=0)
            unit = _pick(v, "unit", "unita", "unità", "unit_of_measure", "misura", default="")
            if isinstance(qty, str) and not unit and (m := _QTY_RE.match(qty)):
                qty, unit = m.group(1), m.group(2) or ""
            if not unit:
                unit = "g" if _pick(v, "grams", "grammi") is not None else ("q.b." if not _num(qty) else "pz")
            return {"name": _pick(v, "name", "nome", "ingredient", "ingrediente", "item", default="?"),
                    "quantity": _num(qty), "unit": str(unit),
                    "category": _pick(v, "category", "categoria", "reparto", default="Altro")}
        return v

    @model_validator(mode="after")
    def _normalize(self):
        # Unità e reparti non sempre coincidono con i nostri.
        u = UNIT_ALIASES.get(self.unit.strip().lower(), self.unit.strip().lower())
        if u == "kg":
            u, self.quantity = "g", self.quantity * 1000
        elif u in ("l", "lt"):
            u, self.quantity = "ml", self.quantity * 1000
        self.unit = u if u in UNITS else (u or "q.b.")
        if self.category not in CATEGORIES or self.category == "Altro":
            self.category = _guess_category(self.name)
        return self


_CATEGORY_HINTS = [
    ("Legumi", "lenticch ceci fagiol pisell fave edamame lupin"),
    ("Proteine vegetali (tofu, tempeh, seitan)", "tofu tempeh seitan"),
    ("Cereali, pane e pasta", "pasta riso pane avena farro orzo quinoa couscous cracker gallett farina bulgur miglio grano"),
    ("Latticini e uova", "latte yogurt formagg uova uovo ricotta mozzarell parmigian burro"),
    ("Frutta secca e semi", "noci mandorl nocciol semi pistacch anacard arachid"),
    ("Dispensa e condimenti", "olio aceto sale pepe spezie curcuma cumino salsa sugo passata dado lievito zucchero miele tahin senape"),
    ("Bevande", "bevanda acqua succo tè te caffè"),
    ("Surgelati", "surgelat"),
    ("Frutta e verdura", "mela pera banana arancia limone kiwi frutti bacche spinac insalata rucola pomodor zucch carot cipoll aglio broccol cavol peperon melanzan patat funghi zucca sedano finocch lattuga verdur frutta avocado"),
]


def _guess_category(name: str) -> str:
    n = name.lower()
    for cat, hints in _CATEGORY_HINTS:
        if any(h in n for h in hints.split()):
            return cat
    return "Altro"


class PersonNote(BaseModel):
    person: str
    note: str

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, dict):
            return {"person": _pick(v, "person", "persona", "nome", "name", default="?"),
                    "note": _pick(v, "note", "nota", "portion", "porzione", "variant", "variante", default="")}
        return v


_MEAL_KEYS = [("colaz", "colazione"), ("breakfast", "colazione"), ("pranz", "pranzo"), ("lunch", "pranzo"),
              ("cen", "cena"), ("dinner", "cena"), ("merend", "merenda"), ("spunt", "spuntino"), ("snack", "spuntino")]


class Meal(BaseModel):
    meal: Literal["colazione", "spuntino", "pranzo", "merenda", "cena"]
    title: str
    description: str = ""
    prep_minutes: int = 0
    ingredients: list[Ingredient] = []
    per_person: list[PersonNote] = []

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v):
        if not isinstance(v, dict):
            return v
        meal = str(_pick(v, "meal", "pasto", "tipo", "type", default="")).strip().lower()
        meal = next((m for key, m in _MEAL_KEYS if key in meal), meal)
        return {"meal": meal,
                "title": _pick(v, "title", "titolo", "nome", "name", "piatto", "dish", default="—"),
                "description": _pick(v, "description", "descrizione", "preparazione", "recipe", default=""),
                "prep_minutes": int(_num(_pick(v, "prep_minutes", "tempo", "minuti", "prep_time", default=0))),
                "ingredients": _pick(v, "ingredients", "ingredienti", default=[]) or [],
                "per_person": _pick(v, "per_person", "per_persona", "porzioni", default=[]) or []}


class Plan(BaseModel):
    class Guideline(BaseModel):
        person: str
        tip: str

        @model_validator(mode="before")
        @classmethod
        def _coerce(cls, v):
            if isinstance(v, str):
                return {"person": "entrambi", "tip": v}
            if isinstance(v, dict):
                return {"person": _pick(v, "person", "persona", default="entrambi"),
                        "tip": _pick(v, "tip", "consiglio", "text", default="")}
            return v

    class Day(BaseModel):
        day: str
        meals: list[Meal]

        @model_validator(mode="before")
        @classmethod
        def _coerce(cls, v):
            if isinstance(v, dict):
                return {"day": _pick(v, "day", "giorno", "name", default="?"),
                        "meals": _pick(v, "meals", "pasti", default=[]) or []}
            return v

    summary: str = ""
    guidelines: list[Guideline] = []
    days: list[Day]

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, dict):
            v = {**v, "days": _pick(v, "days", "giorni", default=v.get("days")),
                 "summary": _pick(v, "summary", "sintesi", "riepilogo", default=""),
                 "guidelines": _pick(v, "guidelines", "consigli", default=[]) or []}
        return v

    def ingredient_count(self) -> int:
        return sum(len(m.ingredients) for d in self.days for m in d.meals)


class SaveNote(BaseModel):
    content: str
    about: Literal["me", "partner", "both"]


class DeleteNote(BaseModel):
    note_id: int


class ReplaceMeal(BaseModel):
    day_index: int
    meal: Meal


class ShoppingAdd(BaseModel):
    items: list[Ingredient]


# Strumenti della chat in forma neutra: ogni backend li converte nel proprio formato.
TOOLS = [
    {
        "name": "save_note",
        "description": (
            "Salva un'informazione stabile e utile per la dieta (preferenza, intolleranza, obiettivo, "
            "reazione a un piatto, indicazione del medico). Non salvare cose già presenti nelle note."
        ),
        "schema": _obj({
            "content": {"type": "string", "description": "Nota breve e autosufficiente"},
            "about": {"type": "string", "enum": ["me", "partner", "both"],
                      "description": "A chi si riferisce rispetto a chi sta scrivendo"},
        }),
    },
    {
        "name": "delete_note",
        "description": "Elimina una nota non più valida, usando il suo id dal contesto.",
        "schema": _obj({"note_id": {"type": "integer"}}),
    },
    {
        "name": "replace_meal",
        "description": (
            "Sostituisce un pasto nel piano settimanale attivo (la lista della spesa si aggiorna da sola). "
            "Usalo solo se l'utente chiede di cambiare un pasto. day_index parte da 0 (primo giorno del piano)."
        ),
        "schema": _obj({"day_index": {"type": "integer"}, "meal": MEAL_SCHEMA}),
    },
    {
        "name": "add_shopping_items",
        "description": "Aggiunge voci extra alla lista della spesa.",
        "schema": _obj({"items": {"type": "array", "items": INGREDIENT_SCHEMA}}),
    },
]
TOOL_LABELS = {"save_note": "📝 Nota salvata", "delete_note": "🗑️ Nota eliminata",
               "replace_meal": "🍽️ Piano aggiornato", "add_shopping_items": "🛒 Lista della spesa aggiornata"}


# --- prompt -------------------------------------------------------------------

LABS_SYSTEM = (
    "Estrai i valori di laboratorio da un referto di esami del sangue già anonimizzato. "
    "Riporta ogni analita una sola volta, con valore, unità e intervallo di riferimento come scritti. "
    "Il flag indica se il valore è fuori dall'intervallo di riferimento. Le parti tra parentesi quadre "
    "(es. [FULLNAME_1], [RIMOSSO]) sono dati oscurati: ignorale."
)

PLAN_SYSTEM = """Sei un nutrizionista che prepara il piano alimentare settimanale per una coppia che mangia insieme.
Costruisci pasti condivisi e indica in per_person le differenze di porzione o le varianti per ciascuno, \
in modo che una sola lista della spesa copra entrambi.

Criteri:
- Rispetta rigorosamente regime alimentare, allergie e cibi non graditi di ciascuno.
- Usa esami del sangue e condizioni di salute per orientare le scelte (es. ferro basso: legumi, semi, \
verdure a foglia con fonti di vitamina C, tè/caffè lontano dai pasti; colesterolo alto: fibre solubili, \
grassi insaturi, pochi grassi saturi). Non fare diagnosi.
- Varietà nella settimana, ingredienti di stagione, riuso intelligente degli avanzi e preparazioni in batch \
per ridurre sprechi e tempi.
- Le quantità degli ingredienti sono il totale per entrambe le persone; usa grammi crudi/secchi.
- Usa nomi di ingredienti coerenti in tutta la settimana (stesso nome per lo stesso prodotto) così la lista \
della spesa si somma correttamente.
- Scrivi in italiano."""

CHAT_SYSTEM = """Sei l'assistente nutrizionale di una coppia, integrato nella loro piattaforma per dieta e spesa.
Il contesto (profili, note salvate, esami del sangue anonimizzati e piano settimanale attivo) ti viene \
fornito in un messaggio di sistema; se ne arriva uno più recente, fai riferimento a quello.

- Rispondi in italiano, in modo concreto e conciso; usa markdown leggero.
- Quando emergono informazioni stabili utili per la dieta, salvale con save_note, senza chiedere conferma, \
e dillo in una riga. Se una nota diventa falsa, eliminala.
- Se l'utente chiede di cambiare un pasto del piano, usa replace_meal mantenendo coerenza con gli esami e i profili.
- Non sei un medico: per valori molto fuori range o sintomi, suggerisci di parlarne con il medico, senza allarmismi."""


# --- contesto ---------------------------------------------------------------

PROFILE_LABELS = {
    "diet": "Regime alimentare", "conditions": "Condizioni/esigenze di salute", "allergies": "Allergie/intolleranze",
    "dislikes": "Cibi non graditi", "likes": "Cibi preferiti", "goals": "Obiettivi", "sex": "Sesso",
    "age": "Età", "height_cm": "Altezza (cm)", "weight_kg": "Peso (kg)", "activity": "Attività fisica",
    "notes": "Altro",
}  # 'redact_terms' resta escluso di proposito: serve solo all'anonimizzazione.


def build_context(current_uid: int | None = None) -> str:
    users = db.users()
    parts = [f"Data di oggi: {date.today().isoformat()}"]
    if current_uid:
        me = next(u for u in users if u["id"] == current_uid)
        parts.append(f"Stai parlando con: {me['name']}")

    for u in users:
        lines = [f"- {PROFILE_LABELS[k]}: {v}" for k, v in u["profile"].items() if k in PROFILE_LABELS and v]
        parts.append(f"## Profilo di {u['name']}\n" + ("\n".join(lines) or "(non compilato)"))

    notes = db.notes_for()
    if notes:
        parts.append("## Note salvate\n" + "\n".join(
            f"- [id {n['id']}] ({n['user_name'] or 'entrambi'}) {n['content']}" for n in notes))

    labs = db.approved_labs()
    if labs:
        chunks = []
        for f in labs:
            head = f"### Esami di {f['user_name']} — {f['exam_date'] or f['uploaded_at'][:10]}"
            if f["labs"] and f["labs"].get("results"):
                body = "\n".join(
                    f"- {r['name']}: {r['value']} {r['unit']} (rif. {r['reference_range'] or 'n/d'}; {r['flag']})"
                    for r in f["labs"]["results"])
            else:
                body = (f["anon_text"] or "")[:6000]
            chunks.append(f"{head}\n{body}")
        parts.append("## Esami del sangue (anonimizzati)\n" + "\n\n".join(chunks))

    plan = db.active_plan()
    if plan:
        lines = []
        for i, d in enumerate(plan["data"]["days"]):
            meals = "; ".join(f"{m['meal']}: {m['title']}" for m in d["meals"])
            lines.append(f"- [{i}] {d['day']} — {meals}")
        parts.append(f"## Piano attivo (settimana dal {plan['week_start']})\n" + "\n".join(lines))
    else:
        parts.append("## Piano attivo\nNessun piano generato.")
    return "\n\n".join(parts)


def context_hash(ctx: str) -> str:
    return hashlib.sha256(ctx.encode()).hexdigest()


# --- backend -------------------------------------------------------------------

def backend():
    if PROVIDER == "claude":
        from .backends import claude_backend as b
    elif PROVIDER == "openai":
        from .backends import openai_backend as b
    else:
        raise LLMError(f"LLM_PROVIDER non valido: {PROVIDER!r} (usa 'openai' o 'claude')")
    return b


def model_label() -> str:
    b = backend()
    try:
        chat, structured = b.current_model("chat"), b.current_model("structured")
    except LLMError:
        return f"{PROVIDER} · nessun modello selezionato"
    return f"{PROVIDER} · {chat}" + (f" (piano/esami: {structured})" if structured != chat else "")


# --- operazioni -------------------------------------------------------------------

async def extract_labs(anon_text: str) -> dict:
    return await backend().structured(LABS_SYSTEM, anon_text, LABS_SCHEMA, "labs", effort="medium")


async def generate_plan(plan_id: int, week_start: str, request: str, meals: list[str]):
    try:
        prompt = (
            f"{build_context()}\n\n---\nGenera il piano per 7 giorni a partire da {week_start} "
            f"(nomina i giorni come 'Lunedì 20/10'). Pasti da includere: {', '.join(meals)}."
        )
        if request.strip():
            prompt += f"\nRichieste specifiche per questa settimana: {request.strip()}"
        data = await backend().structured(PLAN_SYSTEM, prompt, PLAN_SCHEMA, "weekly_plan", effort="high")
        plan = Plan.model_validate(data)  # i modelli più piccoli non sempre rispettano lo schema
        data = plan.model_dump()
        if plan.ingredient_count() == 0:
            # Piano senza ingredienti (succede con i modelli più piccoli): li ricaviamo con una seconda richiesta.
            try:
                data = await fill_ingredients(data)
            except Exception:  # noqa: BLE001 - il piano resta valido; dalla pagina si può riprovare
                pass
        with db.tx() as c:
            c.execute("UPDATE plans SET status='ready', data=? WHERE id=?", (json.dumps(data), plan_id))
        shopping.rebuild(plan_id)
        db.activate_plan(plan_id)
    except Exception as e:  # noqa: BLE001 - l'errore va mostrato nella pagina del piano
        with db.tx() as c:
            c.execute("UPDATE plans SET status='error', error=? WHERE id=?", (str(e)[:1000], plan_id))


FILL_SCHEMA = _obj({
    "meals": {"type": "array", "items": _obj({
        "day_index": {"type": "integer"},
        "meal": {"type": "string", "enum": MEALS},
        "ingredients": {"type": "array", "items": INGREDIENT_SCHEMA},
    })},
})


async def fill_ingredients(data: dict) -> dict:
    """Ricava gli ingredienti (per due persone) dei pasti che ne sono privi."""
    missing = [(i, m) for i, d in enumerate(data["days"]) for m in d["meals"] if not m["ingredients"]]
    if not missing:
        return data
    lines = [f"- day_index {i} · {data['days'][i]['day']} · {m['meal']}: {m['title']}"
             + (f" — {m['description']}" if m["description"] else "") for i, m in missing]
    prompt = ("Per ciascuno di questi pasti elenca gli ingredienti necessari, con le quantità TOTALI per due "
              "persone (grammi crudi/secchi, ml o pezzi; 'q.b.' con quantità 0 per sale, spezie e olio). "
              "Usa nomi generici e coerenti tra i pasti, così la lista della spesa si somma bene.\n\n" + "\n".join(lines))
    out = await backend().structured(
        "Sei un nutrizionista che ricava la lista degli ingredienti dai pasti di un piano settimanale.",
        prompt, FILL_SCHEMA, "meal_ingredients", effort="low")
    for item in out.get("meals", []):
        try:
            day = data["days"][int(item["day_index"])]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        meal = next((m for m in day["meals"] if m["meal"] == item.get("meal") and not m["ingredients"]), None)
        if meal is not None:
            meal["ingredients"] = [Ingredient.model_validate(x).model_dump() for x in item.get("ingredients", [])]
    return data


def run_tool(name: str, raw: dict, uid: int) -> str:
    """Esegue uno strumento della chat. Solleva ValueError/ValidationError per input non validi."""
    users = db.users()
    if name == "save_note":
        a = SaveNote.model_validate(raw)
        partner = next((u["id"] for u in users if u["id"] != uid), None)
        target = {"me": uid, "partner": partner, "both": None}[a.about]
        with db.tx() as c:
            nid = c.execute("INSERT INTO notes (user_id, content, source) VALUES (?, ?, 'chat')",
                            (target, a.content)).lastrowid
        return f"Nota salvata (id {nid})."
    if name == "delete_note":
        a = DeleteNote.model_validate(raw)
        with db.tx() as c:
            n = c.execute("DELETE FROM notes WHERE id=?", (a.note_id,)).rowcount
        return "Nota eliminata." if n else f"Nessuna nota con id {a.note_id}."
    if name == "replace_meal":
        a = ReplaceMeal.model_validate(raw)
        plan = db.active_plan()
        if not plan:
            raise ValueError("Non c'è un piano attivo.")
        days = plan["data"]["days"]
        if not 0 <= a.day_index < len(days):
            raise ValueError(f"day_index deve essere tra 0 e {len(days) - 1}.")
        meals = days[a.day_index]["meals"]
        new = a.meal.model_dump()
        pos = next((i for i, m in enumerate(meals) if m["meal"] == new["meal"]), None)
        if pos is None:
            meals.append(new)
        else:
            meals[pos] = new
        with db.tx() as c:
            c.execute("UPDATE plans SET data=? WHERE id=?", (json.dumps(plan["data"]), plan["id"]))
        shopping.rebuild(plan["id"])
        return f"Pasto sostituito: {days[a.day_index]['day']} / {new['meal']} → {new['title']}. Lista spesa aggiornata."
    if name == "add_shopping_items":
        a = ShoppingAdd.model_validate(raw)
        shopping.add_manual([i.model_dump() for i in a.items])
        return f"Aggiunte {len(a.items)} voci alla lista della spesa."
    raise ValueError(f"Strumento sconosciuto: {name}")


def try_tool(name: str, raw, uid: int) -> tuple[bool, str]:
    """(ok, testo del risultato) — gli errori tornano al modello invece di interrompere la chat."""
    try:
        if not isinstance(raw, dict):
            raise ValueError("Argomenti non validi (atteso un oggetto JSON).")
        return True, run_tool(name, raw, uid)
    except (ValidationError, ValueError) as e:
        return False, f"Errore: {e}"


async def chat_turn(conv_id: int, uid: int, text: str) -> AsyncIterator[dict]:
    """Un turno di chat: eventi per la UI; il turno viene salvato solo se completato."""
    with db.tx() as c:
        conv = dict(c.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone())
        rows = c.execute("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id",
                         (conv_id,)).fetchall()
    if conv["provider"] and conv["provider"] != PROVIDER:
        raise LLMError("Questa chat è stata creata con un altro modello: aprine una nuova.")
    history = [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    new, ctx_hash = [], conv["context_hash"]
    async for ev in backend().chat_turn(history, conv["context_hash"], uid, text):
        if ev["type"] == "_save":
            new, ctx_hash = ev["messages"], ev["context_hash"]
        else:
            yield ev

    with db.tx() as c:
        for m, disp in new:
            c.execute("INSERT INTO messages (conversation_id, role, content, display) VALUES (?, ?, ?, ?)",
                      (conv_id, m["role"], json.dumps(m["content"]), disp))
        c.execute("UPDATE conversations SET context_hash=?, provider=? WHERE id=?", (ctx_hash, PROVIDER, conv_id))
        if conv["title"] == "Nuova chat":
            c.execute("UPDATE conversations SET title=? WHERE id=?", (text[:60], conv_id))
