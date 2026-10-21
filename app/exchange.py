"""Scambio del piano con un assistente esterno (es. Claude su claude.ai).

Esporta: un testo autosufficiente con istruzioni, profili, note, valori degli esami, piano attuale e
lo schema JSON della risposta. Importa: legge il JSON restituito, lo valida e lo salva come nuovo piano.
"""
import json
import re
from datetime import date, timedelta

from pydantic import ValidationError

from . import db, labs
from .assistant import MEALS, PLAN_SCHEMA, PLAN_SYSTEM, PROFILE_LABELS, Plan

ALIASES = ["Persona A", "Persona B"]
WEEKDAYS = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


def _name_map(anonymize: bool) -> dict[str, str]:
    """nome reale -> nome usato nell'export."""
    users = db.users()
    return {u["name"]: (ALIASES[i] if anonymize else u["name"]) for i, u in enumerate(users)}


def _replace(text: str, mapping: dict[str, str]) -> str:
    for src, dst in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        if src != dst:
            text = re.sub(rf"\b{re.escape(src)}\b", dst, text)
    return text


def day_labels(week_start: str) -> list[str]:
    start = date.fromisoformat(week_start)
    return [f"{WEEKDAYS[(start + timedelta(days=i)).weekday()]} {(start + timedelta(days=i)):%d/%m}" for i in range(7)]


def export_prompt(plan: dict | None, week_start: str, anonymize: bool, include_labs: bool,
                  request: str = "", meals: list[str] | None = None) -> str:
    mapping = _name_map(anonymize)
    users = db.users()
    meals = meals or ["colazione", "pranzo", "cena"]
    parts = [
        "# Piano alimentare settimanale per una coppia",
        PLAN_SYSTEM,
        "## Profili",
    ]
    for u in users:
        lines = [f"- {PROFILE_LABELS[k]}: {v}" for k, v in u["profile"].items() if k in PROFILE_LABELS and v]
        parts.append(f"### {u['name']}\n" + ("\n".join(lines) or "(non compilato)"))

    notes = db.notes_for()
    if notes:
        parts.append("## Note\n" + "\n".join(f"- ({n['user_name'] or 'entrambi'}) {n['content']}" for n in notes))

    favs = db.favorites()
    if favs:
        parts.append("## Piatti preferiti (riproponine qualcuno)\n"
                     + "\n".join(f"- {f['meal'] or 'pasto'}: {f['title']}" for f in favs))

    if include_labs:
        # Solo i valori strutturati (fuori range con storico, gli altri per nome); mai il testo del referto.
        labs_text = labs.summary_text(raw_limit=0)
        if labs_text:
            parts.append("## Esami del sangue\n" + labs_text)

    labels = day_labels(week_start)
    if plan:
        parts.append("## Piano attuale (da migliorare)\n```json\n"
                     + json.dumps(plan["data"], ensure_ascii=False, indent=1) + "\n```")
        task = "Rivedi e migliora il piano attuale"
    else:
        task = "Crea il piano"
    task += (f" per 7 giorni, dal {labels[0]} al {labels[-1]}. Usa esattamente questi nomi per i giorni: "
             f"{', '.join(labels)}. Pasti da includere: {', '.join(meals)}.")
    if request.strip():
        task += f"\nRichieste per questa settimana: {request.strip()}"
    names = ", ".join(mapping.values())
    parts += [
        "## Cosa ti chiedo",
        task,
        f"Nei campi `person` usa solo questi nomi: {names}.",
        "Puoi prima ragionare e discutere con me il piano. Quando è pronto, rispondi con UN SOLO blocco "
        "```json``` che rispetti questo JSON Schema (valori ammessi per `meal`: "
        f"{', '.join(MEALS)}; quantità = totale per entrambi, in g/ml/pz):",
        "```json\n" + json.dumps(PLAN_SCHEMA, ensure_ascii=False) + "\n```",
        "Io copierò quel blocco nella mia app per generare automaticamente la lista della spesa.",
    ]
    return _replace("\n\n".join(parts), mapping)


class ImportError_(ValueError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = blocks[::-1] or []
    if "{" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise ImportError_("Non trovo un JSON valido nel testo incollato. Copia tutto il blocco ```json``` della risposta.")


def parse_import(text: str) -> dict:
    """Testo incollato -> dati del piano validati, con i nomi reali ripristinati."""
    reverse = {alias: real for real, alias in _name_map(True).items()}
    data = _extract_json(_replace(text, reverse))
    if "days" not in data and isinstance(data.get("plan"), dict):
        data = data["plan"]
    try:
        plan = Plan.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = " → ".join(str(x) for x in first["loc"])
        raise ImportError_(f"Il JSON non ha la forma attesa ({where}: {first['msg']}).") from e
    if not plan.days:
        raise ImportError_("Il piano importato non contiene giorni.")
    return plan.model_dump()
