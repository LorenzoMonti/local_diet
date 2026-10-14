"""Client per rizzo-pii (https://github.com/Rizzo-AI-Academy/rizzo-pii), che gira come servizio locale.

Il PDF viene inviato solo al container rizzo-pii sulla rete Docker interna: nulla esce dal server.
Il servizio restituisce il testo con segnaposto tipo [FULLNAME_1], [CF_1], [STREET_1].
"""
import os
from pathlib import Path

import httpx

PII_URL = os.environ.get("PII_SERVICE_URL", "http://rizzo-pii:5005").rstrip("/")
# "rizzo" = rizzo-pii + regole locali; "rules" = solo regole; "off" = nessuna anonimizzazione
# (sensato solo se anche il modello gira in locale, es. Ollama sull'AGX).
MODE = os.environ.get("PII_MODE", "rizzo").strip().lower()
# Tag da lasciare in chiaro: età e sesso servono a interpretare i range di riferimento, e i valori
# numerici degli esami non devono essere scambiati per importi.
KEEP_TAGS = os.environ.get("PII_KEEP_TAGS", "AGE,GENDER,AMOUNT")


class PIIUnavailable(Exception):
    pass


async def anonymize_pdf(path: Path, filename: str) -> str:
    if not PII_URL:
        raise PIIUnavailable("PII_SERVICE_URL non configurato")
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            with path.open("rb") as fh:
                r = await client.post(
                    f"{PII_URL}/analyze",
                    files={"file": (filename, fh, "application/pdf")},
                    # include_mapping=false: anonimizzazione definitiva, nessun dizionario per ricostruire.
                    data={"exclude_tags": KEEP_TAGS, "include_mapping": "false"},
                )
    except httpx.HTTPError as e:
        raise PIIUnavailable(f"rizzo-pii non raggiungibile: {e}") from e
    if r.status_code != 200:
        try:
            msg = r.json().get("error", r.text)
        except ValueError:
            msg = r.text
        raise PIIUnavailable(f"rizzo-pii ha risposto {r.status_code}: {msg[:200]}")
    return r.json()["anonymized_text"]
