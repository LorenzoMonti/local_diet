"""Estrazione testo da PDF e anonimizzazione best-effort dei referti.

L'anonimizzazione automatica non è perfetta: il risultato viene sempre mostrato
all'utente, che può correggerlo e deve approvarlo prima che venga usato con Claude.
"""
import re
from pathlib import Path

from pypdf import PdfReader

REDACTED = "[RIMOSSO]"

# Righe che contengono quasi sempre dati identificativi: si oscura il valore dopo l'etichetta.
LABEL_RE = re.compile(
    r"(?im)^(?P<label>\s*(?:paziente|assistito|cognome(?:\s+e\s+nome)?|nome(?:\s+e\s+cognome)?|"
    r"nat[oa](?:\s+(?:il|a))?|data\s+(?:di\s+)?nascita|luogo\s+(?:di\s+)?nascita|"
    r"indirizzo|residen(?:za|te)|domicilio|comune|c\.?\s?f\.?|codice\s+fiscale|"
    r"tessera\s+sanitaria|n\.?\s*tessera|medico(?:\s+curante|\s+richiedente)?|"
    r"telefono|tel\.?|cell(?:ulare)?\.?|e-?mail|n\.?\s*(?:richiesta|accettazione|campione|referto)|"
    r"id\s+paziente|cod(?:ice)?\.?\s+paziente|id\.?\s*paz|sig(?:\.?ra|nora|nor)?|nosologico|"
    r"doc(?:umento)?\.?\s*n|richiesta)\b\s*[:.\-]?)(?P<value>.*)$"
)
CF_RE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPRST]\d{2}[A-Z]\d{3}[A-Z]\b", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+39[\s.]?)?(?:3\d{2}|0\d{1,3})[\s./-]?\d{5,8}(?!\d)")
ADDRESS_RE = re.compile(
    r"\b(?:via|viale|v\.le|piazza|p\.za|piazzale|corso|c\.so|largo|vicolo|strada)[ \t]+"
    r"[A-ZÀ-Ü][\wÀ-ü' .]{2,40}?,?[ \t]*\d+[a-z]?\b",
    re.I,
)
# CAP + città + (provincia): la provincia è obbligatoria per non colpire valori numerici a 5 cifre.
CAP_CITY_RE = re.compile(r"\b\d{5}[ \t]+[A-ZÀ-Ü][A-Za-zÀ-ü' ]{1,30}\s*\([A-Z]{2}\)")
DOCTOR_RE = re.compile(r"\b(?i:dott\.?ssa|dott\.?|dr\.?ssa|dr\.?|prof\.?)[ \t]+[A-ZÀ-Ü][\wÀ-ü']+(?:[ \t]+[A-ZÀ-Ü][\wÀ-ü']+)?")


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def anonymize(text: str, extra_terms: list[str]) -> str:
    out = LABEL_RE.sub(lambda m: f"{m.group('label')} {REDACTED}", text)
    for rx in (CF_RE, EMAIL_RE, ADDRESS_RE, CAP_CITY_RE, DOCTOR_RE, PHONE_RE):
        out = rx.sub(REDACTED, out)
    # Nomi/cognomi indicati nel profilo (e loro parti), case-insensitive.
    terms = set()
    for t in extra_terms:
        t = t.strip()
        if len(t) >= 3:
            terms.add(t)
            terms.update(p for p in t.split() if len(p) >= 3)
    for t in sorted(terms, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(t)}\b", REDACTED, out, flags=re.I)
    return out
