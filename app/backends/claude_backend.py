"""Backend Claude API (SDK Anthropic)."""
import json
import os
from typing import AsyncIterator

import anthropic

from .. import assistant as a

MODEL = os.environ.get("LLM_MODEL") or os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
# Se un classificatore di sicurezza rifiuta la richiesta, l'API la ripete su un modello di fallback.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

_client: anthropic.AsyncAnthropic | None = None


def current_model(kind: str = "chat") -> str:
    return MODEL


async def list_models() -> list[dict] | None:
    return None  # con Claude il modello si configura da .env


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


TOOLS = [
    {"name": t["name"], "description": t["description"], "input_schema": t["schema"],
     "strict": True, "eager_input_streaming": True}
    for t in a.TOOLS
]


def _last_text(msg) -> str:
    return next(b.text for b in reversed(msg.content) if b.type == "text")


async def structured(system: str, prompt: str, schema: dict, name: str, effort: str = "medium") -> dict:
    try:
        async with client().beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            system=system,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
            **FALLBACK,
        ) as stream:
            msg = await stream.get_final_message()
    except anthropic.APIError as e:
        raise a.LLMError(f"Errore Claude API: {getattr(e, 'message', e)}") from e
    if msg.stop_reason == "refusal":
        raise a.LLMError("Claude ha rifiutato la richiesta.")
    if msg.stop_reason == "max_tokens":
        raise a.LLMError("Risposta troncata (max_tokens raggiunto).")
    return json.loads(_last_text(msg))


def _sanitize_fallback(content: list[dict]) -> list[dict]:
    """Dopo un fallback a metà risposta, i blocchi interni prima del marcatore non vanno rimandati."""
    idx = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if idx < 0:
        return content
    drop = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}
    return [b for i, b in enumerate(content) if i > idx or b.get("type") not in drop]


async def chat_turn(history: list[dict], last_ctx_hash: str | None, uid: int, text: str) -> AsyncIterator[dict]:
    history = list(history)
    new: list[tuple[dict, str | None]] = []

    def push(message: dict, display: str | None):
        history.append(message)
        new.append((message, display))

    push({"role": "user", "content": text}, text)
    # Il system prompt resta fisso; il contesto entra come messaggio di sistema solo quando cambia
    # (cronologia append-only: compatibile con prompt caching e thinking preservato).
    ctx = a.build_context(uid)
    ctx_hash = a.context_hash(ctx)
    if ctx_hash != last_ctx_hash:
        push({"role": "system", "content": ctx}, None)

    json_retries = 0
    while True:
        try:
            async with client().beta.messages.stream(
                model=MODEL,
                max_tokens=32000,
                system=a.CHAT_SYSTEM,
                tools=TOOLS,
                messages=history,
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                **FALLBACK,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield {"type": "text", "text": event.text}
                msg = await stream.get_final_message()
        except ValueError:
            # JSON di un input strumento non parsabile: si ripete il round (limitato).
            json_retries += 1
            if json_retries > 2:
                raise
            continue
        except anthropic.APIError as e:
            raise a.LLMError(f"Errore Claude API: {getattr(e, 'message', e)}") from e
        json_retries = 0

        content = _sanitize_fallback(msg.to_dict()["content"])
        texts = "\n\n".join(b["text"] for b in content if b.get("type") == "text").strip()
        tool_uses = [b for b in content if b.get("type") == "tool_use"]

        if msg.stop_reason == "refusal" or not tool_uses or msg.stop_reason == "max_tokens":
            if msg.stop_reason == "refusal":
                texts += "\n\n_(La richiesta è stata rifiutata dai filtri di sicurezza.)_"
            elif msg.stop_reason == "max_tokens":
                texts += "\n\n_(Risposta interrotta: troppo lunga.)_"
            push({"role": "assistant", "content": content}, texts or None)
            break

        results, events = [], []
        for b in tool_uses:
            ok, out = a.try_tool(b["name"], b["input"], uid)
            result = {"type": "tool_result", "tool_use_id": b["id"], "content": out}
            if not ok:
                result["is_error"] = True
            else:
                label = a.TOOL_LABELS.get(b["name"], b["name"])
                events.append(f"{label}: {out}")
                yield {"type": "tool", "label": label, "detail": out}
            results.append(result)
        display = "\n\n".join([texts] + [f"_{e}_" for e in events]).strip() or None
        push({"role": "assistant", "content": content}, display)
        push({"role": "user", "content": results}, None)
        yield {"type": "break"}

    yield {"type": "_save", "messages": new, "context_hash": ctx_hash}
