# 🥗 Dieta di casa

Mini piattaforma self-hosted per due persone: piano alimentare settimanale personalizzato, lista della spesa
generata dal piano, archivio esami del sangue anonimizzati e chat con un assistente che ricorda le informazioni utili.

## Avvio

```bash
cp .env.example .env        # inserisci la chiave del modello e i due nomi
docker compose up -d --build                 # senza rizzo-pii
docker compose --profile pii up -d --build   # con rizzo-pii (PII_MODE=rizzo)
```

Apri `http://<ip-del-server>:8000` (con Tailscale: `http://<nome-macchina>:8000`).
Il primo build scarica anche rizzo-pii e il suo modello (~1.2 GB): può richiedere 10-20 minuti.
Tutti i dati (database SQLite e PDF) stanno in `./data`: per il backup basta copiare questa cartella.

> ⚠️ Non c'è login (solo selettore utente): non esporre la porta su Internet. Con Tailscale è raggiungibile
> solo dai vostri dispositivi della tailnet; puoi anche limitare la porta all'IP Tailscale in `docker-compose.yml`
> (es. `"100.x.y.z:8000:8000"`).

## Sul telefono

L'interfaccia è pensata per lo smartphone. Per usarla come un'app (icona sulla Home, schermo intero):

- **iPhone (Safari):** Condividi → *Aggiungi alla schermata Home*
- **Android (Chrome):** menu ⋮ → *Aggiungi a schermata Home* / *Installa app*

Con Tailscale conviene servirla in **HTTPS** (`tailscale serve --bg 8000` sull'AGX, poi
`https://<nome-macchina>.<tailnet>.ts.net`): Android la installa come vera app solo in HTTPS, e la
condivisione nativa della lista della spesa funziona solo in contesto sicuro.

## Modello

Il provider si sceglie nel `.env`:

| `LLM_PROVIDER` | Cosa usa |
|---|---|
| `openai` (default) | Qualunque API compatibile OpenAI: **OpenRouter** (preconfigurato), Gemini, Groq, Mistral, Ollama… Configura `LLM_BASE_URL` e `LLM_API_KEY`. |
| `claude` | Claude API (a pagamento): `ANTHROPIC_API_KEY`, modello da `LLM_MODEL` (default `claude-opus-5-5`). |

Il **modello** si sceglie dalla pagina **⚙️ Impostazioni**: elenca i modelli del provider (di default solo i
gratuiti), con il supporto a *strumenti* (necessario per la chat) e *output JSON* (utile per piano ed esami).
Si può usare un modello per la chat e uno diverso per piano/esami, e provarli con "Prova" prima di salvare.

Modelli gratuiti di OpenRouter (`:free`): 20 richieste/minuto e 50/giorno, che diventano 1000/giorno dopo aver
acquistato almeno 10 crediti. Un messaggio in chat che usa strumenti consuma più richieste.
Alcuni provider dei modelli gratuiti possono registrare i contenuti: controlla le impostazioni privacy del tuo
account OpenRouter. Per questo gli esami passano sempre da rizzo-pii e dalla tua revisione.
Le chat sono legate al provider con cui sono nate: se cambi provider, aprine una nuova.

## Piano con Claude (esporta / importa)

Da **Piano → ⇄** si esporta un testo con profili, note, valori degli esami, piano attuale e lo schema della
risposta. Lo incolli in una chat con Claude (o un altro assistente), ne discuti, e alla fine incolli la sua
risposta in *Importa*: il piano diventa quello attivo e la lista della spesa si ricalcola.
Di default i nomi escono come «Persona A/B» (ripristinati all'import) e degli esami escono solo i valori
estratti, mai il testo del referto.

## Le 4 aree

| Area | Cosa fa |
|---|---|
| **Piano** | Il modello genera 7 giorni di pasti condivisi, con varianti/porzioni per ciascuno, usando profili, note ed esami approvati. Storico dei piani precedenti. |
| **Spesa** | Somma automatica degli ingredienti del piano attivo (nessuna chiamata al modello), raggruppati per reparto. Spunte, voci extra, copia negli appunti. |
| **Esami** | Upload PDF per utente → anonimizzazione con **rizzo-pii** + regole locali → **revisione e approvazione manuale** → estrazione dei valori. |
| **Chat** | Chat per utente. L'assistente può salvare/eliminare note, sostituire un pasto del piano (la spesa si aggiorna) e aggiungere voci alla spesa. |

Il **Profilo** contiene regime alimentare, condizioni, allergie, gusti, obiettivi e le note salvate (a mano o dalla chat).

## Privacy degli esami

- Il PDF originale resta solo sul server (`data/uploads/`).
- [rizzo-pii](https://github.com/Rizzo-AI-Academy/rizzo-pii) gira come container locale, senza porte esposte:
  riconosce nomi, date, indirizzi, codice fiscale, telefoni, email, documenti… e li sostituisce con segnaposto
  (`[FULLNAME_1]`, `[DATE_1]`). L'anonimizzazione è definitiva (nessun dizionario per ricostruire i dati).
  Età, sesso e valori numerici restano in chiaro (`PII_KEEP_TAGS`), perché servono per interpretare gli esami.
- Dopo rizzo-pii passano anche le regole locali e i termini indicati in *Profilo → Termini da oscurare*
  (che non vengono mai inviati al modello).
- Se rizzo-pii non risponde, il file viene anonimizzato con le sole regole e la pagina lo segnala:
  puoi rifare l'anonimizzazione più tardi.
- **Nulla viene inviato al modello finché non approvi il testo**, che puoi modificare a mano. Puoi revocare
  l'approvazione in qualsiasi momento.
- I PDF scansionati (immagini) non hanno testo estraibile: puoi trascrivere i valori a mano nella stessa pagina.

## Sviluppo senza Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
export DATA_DIR=./data PII_SERVICE_URL=http://127.0.0.1:5005   # rizzo-pii avviato a parte, o vuoto per le sole regole
.venv/bin/uvicorn app.main:app --reload
```

## Struttura

- `app/assistant.py`: logica comune (contesto, prompt, schemi JSON, strumenti della chat)
- `app/backends/`: `openai_backend.py` (API compatibili OpenAI) e `claude_backend.py` (Claude API)
- `app/pii.py`: client rizzo-pii; `app/anonymize.py`: regole locali
- `app/shopping.py`: aggregazione della lista della spesa
- `app/main.py`: pagine e azioni (FastAPI + Jinja2, SQLite)
