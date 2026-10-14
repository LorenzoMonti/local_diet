"""Backend per API compatibili OpenAI: Gemini, Groq, OpenRouter, Mistral, Ollama, OpenAI…

Configurazione (.env):
  LLM_BASE_URL   es. https://openrouter.ai/api/v1
  LLM_API_KEY    chiave del provider
  LLM_MODEL      modello di default (opzionale: si sceglie anche da Impostazioni)
"""
import json
import os
import re
import time
from typing import AsyncIterator

import openai

from .. import assistant as a
from .. import db

BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "")
IS_OPENROUTER = "openrouter.ai" in BASE_URL
# Opzionali: non tutti i provider accettano questi parametri.
REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT") or None
MAX_TOKENS = int(os.environ["LLM_MAX_TOKENS"]) if os.environ.get("LLM_MAX_TOKENS") else None
# Un piano settimanale su un modello locale (es. Jetson) può richiedere parecchi minuti.
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "1800"))

_client: openai.AsyncOpenAI | None = None


def client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(
            base_url=BASE_URL, api_key=os.environ.get("LLM_API_KEY", "") or "none", timeout=TIMEOUT, max_retries=1,
            # Header facoltativi usati da OpenRouter per identificare l'app.
            default_headers={"X-Title": "Dieta di casa"} if IS_OPENROUTER else None,
        )
    return _client


def current_model(kind: str = "chat") -> str:
    """kind: 'chat' oppure 'structured' (piano ed esami). Impostazioni > .env."""
    chat = db.get_setting("model_chat", DEFAULT_MODEL)
    model = db.get_setting("model_structured", chat) if kind == "structured" else chat
    if not model:
        raise a.LLMError("Nessun modello selezionato: sceglilo nella pagina Impostazioni.")
    return model


_models_cache: tuple[float, list[dict]] | None = None
# Modelli non adatti alla chat (immagini, audio, video, embedding…), esclusi dall'elenco.
NON_CHAT = re.compile(r"embed|image|imagen|veo|tts|audio|live|aqa|robotics|lyria|transcri|whisper|moderation", re.I)


async def list_models() -> list[dict]:
    """Modelli disponibili presso il provider (GET /models), con capacità quando il provider le espone."""
    global _models_cache
    if _models_cache and time.time() - _models_cache[0] < 600:
        return _models_cache[1]
    try:
        page = await client().models.list(extra_query={"limit": 1000} if IS_OPENROUTER else None)
    except openai.APIError as e:
        raise _friendly(e) from e
    models = []
    for m in page.data:
        d = m.to_dict()
        d["id"] = d["id"].removeprefix("models/")  # Gemini restituisce "models/gemini-…"
        if NON_CHAT.search(d["id"]):
            continue
        pricing = d.get("pricing") or {}
        params = d.get("supported_parameters")
        try:
            free = float(pricing.get("prompt", "nan")) == 0 and float(pricing.get("completion", "nan")) == 0
        except (TypeError, ValueError):
            free = None  # il provider non espone i prezzi
        models.append({
            "id": d["id"],
            "name": d.get("name") or d["id"],
            "free": free if pricing else None,
            "context": d.get("context_length"),
            # None = informazione non disponibile per questo provider
            "tools": ("tools" in params) if params is not None else None,
            "json": bool({"structured_outputs", "response_format"} & set(params)) if params is not None else None,
        })
    models.sort(key=lambda m: m["name"].lower())
    _models_cache = (time.time(), models)
    return models


async def ping(model: str) -> str:
    try:
        resp = await client().chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Rispondi solo con: OK"}], max_tokens=20)
    except openai.APIError as e:
        raise _friendly(e) from e
    return (resp.choices[0].message.content or "").strip()


TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
    for t in a.TOOLS
]


def _opts() -> dict:
    o = {}
    if REASONING_EFFORT:
        o["reasoning_effort"] = REASONING_EFFORT
    if MAX_TOKENS:
        o["max_tokens"] = MAX_TOKENS
    return o


def _friendly(e: openai.APIError) -> a.LLMError:
    if isinstance(e, openai.RateLimitError):
        return a.LLMError("Limite di richieste del piano gratuito raggiunto: riprova tra qualche minuto.")
    if isinstance(e, openai.AuthenticationError):
        return a.LLMError("Chiave API non valida (controlla LLM_API_KEY nel file .env).")
    if isinstance(e, openai.NotFoundError):
        return a.LLMError(f"Modello non trovato o non più disponibile: {getattr(e, 'message', e)}")
    if isinstance(e, openai.APIConnectionError):
        return a.LLMError(f"Impossibile contattare {BASE_URL}.")
    return a.LLMError(f"Errore del modello: {getattr(e, 'message', e)}")


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


async def structured(system: str, prompt: str, schema: dict, name: str, effort: str = "medium") -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    try:
        try:
            resp = await client().chat.completions.create(
                model=current_model("structured"), messages=messages,
                response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
                **_opts(),
            )
        except openai.BadRequestError:
            # Provider/modello senza json_schema: si chiede JSON semplice con lo schema nel prompt.
            messages[0]["content"] += ("\n\nRispondi SOLO con un oggetto JSON valido conforme a questo JSON Schema:\n"
                                       + json.dumps(schema, ensure_ascii=False))
            resp = await client().chat.completions.create(
                model=current_model("structured"), messages=messages, response_format={"type": "json_object"}, **_opts())
    except openai.APIError as e:
        raise _friendly(e) from e
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise a.LLMError("Risposta troncata: il modello ha raggiunto il limite di lunghezza.")
    try:
        return _parse_json(choice.message.content)
    except json.JSONDecodeError as e:
        raise a.LLMError("Il modello non ha restituito JSON valido: riprova.") from e


def _assistant_dict(message) -> dict:
    """Messaggio assistant da rimandare al provider. Si conservano i campi extra (es. le firme
    di ragionamento di Gemini in extra_content), si scartano quelli nulli."""
    d = message.to_dict()
    out = {k: v for k, v in d.items() if v not in (None, [], {}) and k not in ("refusal", "annotations", "audio")}
    out["role"] = "assistant"
    out.setdefault("content", "")
    return out


async def chat_turn(history: list[dict], last_ctx_hash: str | None, uid: int, text: str) -> AsyncIterator[dict]:
    # Qui il contesto aggiornato va direttamente nel system prompt a ogni richiesta.
    ctx = a.build_context(uid)
    system = {"role": "system", "content": f"{a.CHAT_SYSTEM}\n\n# Contesto aggiornato\n\n{ctx}"}
    msgs = [h["content"] for h in history]
    new: list[tuple[dict, str | None]] = []

    def push(message: dict, display: str | None):
        msgs.append(message)
        new.append(({"role": message["role"], "content": message}, display))

    push({"role": "user", "content": text}, text)

    for _ in range(8):  # limite ai giri di strumenti per turno
        try:
            resp = await client().chat.completions.create(
                model=current_model("chat"), messages=[system] + msgs, tools=TOOLS, tool_choice="auto", **_opts())
        except openai.APIError as e:
            raise _friendly(e) from e
        choice = resp.choices[0]
        msg = _assistant_dict(choice.message)
        content = (choice.message.content or "").strip()
        calls = choice.message.tool_calls or []

        if content:
            yield {"type": "text", "text": content}
        if not calls:
            if choice.finish_reason == "length":
                content += "\n\n_(Risposta interrotta: troppo lunga.)_"
            push(msg, content or None)
            break

        results, events = [], []
        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            ok, out = a.try_tool(call.function.name, args, uid)
            if ok:
                label = a.TOOL_LABELS.get(call.function.name, call.function.name)
                events.append(f"{label}: {out}")
                yield {"type": "tool", "label": label, "detail": out}
            results.append({"role": "tool", "tool_call_id": call.id, "content": out})
        display = "\n\n".join([content] + [f"_{e}_" for e in events]).strip() or None
        push(msg, display)
        for r in results:
            push(r, None)
        yield {"type": "break"}
    else:
        yield {"type": "text", "text": "_(Troppi passaggi con gli strumenti: mi fermo qui.)_"}

    yield {"type": "_save", "messages": new, "context_hash": a.context_hash(ctx)}
