"""Riassunto degli esami per persona: valori fuori range con il loro storico, il resto solo per nome.

Al modello serve sapere cosa non va e come sta cambiando, non l'elenco completo di ogni referto:
così il contesto resta piccolo anche con molti referti caricati.
"""
import re

from . import db

_PREFIX = re.compile(r"^(?:[A-Z]{1,2}/)?[A-Z]{1,2}-")  # "S-", "S/P-", "U-" dei referti
_NUM = re.compile(r"^\s*[<>]?\s*(\d+(?:[.,]\d+)?)\s*$")
ABNORMAL = ("alto", "basso")


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", _PREFIX.sub("", name.strip()).replace(" - reflex", "")).strip()


def _num(value) -> float | None:
    m = _NUM.match(str(value))
    return float(m.group(1).replace(",", ".")) if m else None


def _date(f: dict) -> str:
    return f["exam_date"] or f["uploaded_at"][:10]


def _trend(history: list[dict]) -> str | None:
    """Direzione dell'ultimo cambiamento, letta rispetto al problema (alto che scende = miglioramento)."""
    nums = [h for h in history if _num(h["value"]) is not None]
    if len(nums) < 2:
        return None
    prev, last = _num(nums[-2]["value"]), _num(nums[-1]["value"])
    if prev == 0 or abs(last - prev) / abs(prev) < 0.03:
        return "stabile"
    problem = next((h["flag"] for h in reversed(history) if h["flag"] in ABNORMAL), None)
    if problem is None:
        return "in aumento" if last > prev else "in calo"
    better = last < prev if problem == "alto" else last > prev
    return "in miglioramento" if better else "in peggioramento"


def trends(user_id: int | None = None) -> list[dict]:
    """Per ogni persona: valori da tenere d'occhio (con storico), nomi dei valori nella norma, referti senza valori."""
    people: dict[str, dict] = {}
    for f in db.approved_labs():  # già ordinati per persona e data
        if user_id is not None and f["user_id"] != user_id:
            continue
        p = people.setdefault(f["user_name"], {"name": f["user_name"], "last_date": "", "series": {}, "raw": []})
        if not (f["labs"] and f["labs"].get("results")):
            p["raw"].append(f)
            continue
        p["last_date"] = max(p["last_date"], _date(f))
        for r in f["labs"]["results"]:
            name = _clean(r["name"])
            # Il tipo di valore fa parte della chiave: "Emoglobina" nel sangue (16.1) e nelle urine (Negativo).
            key = f"{name.lower()}|{_num(r['value']) is not None}"
            s = p["series"].setdefault(key, {"name": name, "history": []})
            s.update(unit=r.get("unit", ""), ref=r.get("reference_range", ""))
            s["history"].append({"date": _date(f), "value": r["value"], "flag": r.get("flag", "")})

    out = []
    for p in people.values():
        watch, normal = [], []
        for s in p["series"].values():
            s["history"].sort(key=lambda h: h["date"])
            if any(h["flag"] in ABNORMAL for h in s["history"]):
                s["latest"] = s["history"][-1]
                s["trend"] = _trend(s["history"])
                watch.append(s)
            elif _num(s["history"][-1]["value"]) is not None:  # i qualitativi nella norma ("Negativo") si omettono
                normal.append(s["name"])
        out.append({"name": p["name"], "last_date": p["last_date"], "watch": watch, "normal": normal, "raw": p["raw"]})
    return out


def _itdate(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if len(iso) >= 10 and iso[4] == "-" else iso


def summary_text(raw_limit: int = 4000) -> str:
    """Testo per il modello (piano, chat, export)."""
    chunks = []
    for p in trends():
        lines = [f"### {p['name']}" + (f" — ultimo referto del {_itdate(p['last_date'])}" if p["last_date"] else "")]
        if p["watch"]:
            lines.append("Valori fuori range (almeno una volta), dal più vecchio al più recente:")
            for s in p["watch"]:
                hist = " → ".join(f"{h['value']} {h['flag']} ({_itdate(h['date'])})" for h in s["history"])
                ref = f"rif. {s['ref']}" if s["ref"] else "rif. n/d"
                lines.append(f"- {s['name']} [{s['unit']}, {ref}]: {hist}" + (f" · {s['trend']}" if s["trend"] else ""))
        if p["normal"]:
            lines.append("Nella norma: " + ", ".join(p["normal"]) + ".")
        for f in p["raw"] if raw_limit else []:  # referti approvati di cui non sono ancora stati estratti i valori
            lines.append(f"Referto del {_itdate(_date(f))} (testo, valori non estratti):\n{(f['anon_text'] or '')[:raw_limit]}")
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)
