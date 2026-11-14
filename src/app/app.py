# -*- coding: utf-8 -*-
"""
App locale per l'anonimizzazione reversibile di documenti con il modello PII.

Flusso d'uso:
  1) ANONIMIZZA  - incolli testo o carichi un PDF; il modello + una rete regex/checksum
     trovano le PII. Ogni entita' riceve un ID univoco e reversibile: [FULLNAME_1],
     [IBAN_1], ... (valori uguali condividono lo stesso ID).
  2) COPIA       - copi il testo anonimizzato e lo incolli in ChatGPT/altro LLM.
  3) RIPRISTINA  - incolli la risposta dell'LLM (che contiene i placeholder) e l'app
     rimette i valori veri usando il dizionario locale.

Tutto in locale: il testo e il dizionario {placeholder -> valore} non lasciano la macchina.

Il modello e' affiancato da una rete REGEX + CHECKSUM (EMAIL, TELEFONO, IBAN, CF, PIVA,
carta di credito, importi, targhe, URL, IP). Le entita' validate matematicamente (IBAN/CF/
PIVA/carta) hanno priorita' sul modello in caso di sovrapposizione.

Il dizionario di ripristino si puo' DISATTIVARE (switch nell'UI, --no-mapping, PII_MAPPING=0):
l'anonimizzazione diventa definitiva, nessuna chiave placeholder->valore viene costruita.

Endpoint HTTP:
  GET  /health    liveness/readiness senza inference (200 = modello caricato, 503 = no)
  POST /analyze   {"text": ...} oppure multipart con un file (.pdf/.md/.txt); campi
                  opzionali "exclude_tags" e "include_mapping" per override per-richiesta
  POST /pdf       stesso input di /analyze -> scarica il PDF ANONIMIZZATO (redazione
                  vera del PDF caricato, oppure PDF ricostruito dal testo)
  POST /preview   multipart con un .pdf -> lo tiene in memoria per l'ANTEPRIMA a video e
                  ritorna {doc_id, n_pages, text}
  POST /pdf/preview  come /pdf, ma invece del binario ritorna {doc_id, n_pages, ...}:
                  il PDF anonimizzato resta in memoria e si guarda pagina per pagina
  GET  /doc/<id>/page/<n>.png   pagina renderizzata (anteprima a video)
  GET  /doc/<id>/file.pdf       download del documento tenuto in memoria
  GET  /settings  legenda dei 23 tag, tag esclusi, stato del dizionario reversibile
  POST /settings  {"excluded_tags": [...], "mapping_enabled": bool} -> salva in prefs.json
                  (/tags e' un alias storico degli stessi due endpoint)
  GET  /config, POST /config, GET /port-check   host/porta del server

Avvio:  python app.py   ->   http://127.0.0.1:5005
Configurazione host/porta (precedenza): CLI --host/--port > env PII_HOST/PII_PORT >
  config.json (vedi server_config.py) > default 127.0.0.1:5005
Preferenze di anonimizzazione (precedenza): campo nella richiesta > CLI --exclude-tags/
  --no-mapping > env PII_EXCLUDE_TAGS/PII_MAPPING > prefs.json > default
"""

import bisect
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf as fitz  # PyMuPDF (il nome "fitz" e' deprecato)
from flask import (Flask, jsonify, render_template_string, request,
                   send_from_directory)

import local_guard
import pdf_export
import pdf_text
import server_config
# Rete REGEX + CHECKSUM: modulo a parte, senza dipendenze dal modello. I nomi
# restano importabili da qui (`app.detect_regex`) per non rompere chi li usa.
from detectors import (DETECTORS, SOFT_REGEX_LABELS, cf_ok,  # noqa: F401
                       complete_time, detect_iban, detect_regex, iban_ok, luhn_ok,
                       piva_ok)


def _resource_path(rel):
    """Percorso risorsa valido sia in sviluppo sia dentro l'exe PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# MODELLO usato dall'app. Accetta tre forme, provate in quest'ordine:
#   "1.2.0"  -> models/rizzo-pii-0.3B-v1.2.0/   (versione di un run di training)
#   "main"   -> models/rizzo-pii-0.3B-main/     (revision scaricata da HF, senza numero)
#   percorso -> usato tal quale (assoluto o relativo alla root della repo)
# Metti None per usare AUTOMATICAMENTE l'ultima versione `-v*` disponibile.
APP_MODEL_VERSION = "1.5.0"

# Offline sempre, come nel Docker (HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE): il
# modello e' una cartella locale e i documenti non devono mai causare traffico
# verso Hugging Face (controllo versioni, telemetria, file mancanti). Va fissato
# PRIMA di importare transformers/huggingface_hub, che leggono la variabile al
# caricamento: qui e' in tempo perche' load_model() li importa pigramente.
# setdefault: chi vuole davvero la rete lo dichiara con HF_HUB_OFFLINE=0.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Dentro l'exe il modello e' impacchettato come "pii_model" (vedi build.spec).
# In sviluppo: pin sopra -> auto-ultima versione -> vecchio non versionato -> legacy.
# Override puntuale a runtime: env PII_MODEL_DIR.
if getattr(sys, "_MEIPASS", None):
    MODEL_DIR = _resource_path("pii_model")
elif os.environ.get("PII_MODEL_DIR"):
    MODEL_DIR = os.environ["PII_MODEL_DIR"]
else:
    import re
    _models = Path(__file__).resolve().parents[2] / "models"
    _cands = []
    if APP_MODEL_VERSION:
        _cands = [_models / f"rizzo-pii-0.3B-v{APP_MODEL_VERSION}",
                  _models / f"rizzo-pii-0.3B-{APP_MODEL_VERSION}"]
        if os.sep in APP_MODEL_VERSION or "/" in APP_MODEL_VERSION:   # e' un percorso
            _cands += [Path(APP_MODEL_VERSION), _models.parent / APP_MODEL_VERSION]
    _pinned = next((p for p in _cands if p.is_dir()), None)
    _versioned = [p for p in _models.glob("rizzo-pii-0.3B-v*") if p.is_dir()]
    if _pinned:
        MODEL_DIR = str(_pinned)
    elif _versioned:
        MODEL_DIR = str(max(_versioned, key=lambda p: tuple(
            int(x) for x in re.search(r"-v([0-9][0-9.]*)$", p.name).group(1).split("."))))
    else:
        _prod = _models / "rizzo-pii-0.3B"
        MODEL_DIR = str(_prod if _prod.exists() else _models / "pii_model_legacy")

# Se la cartella non c'e', dirlo QUI e con il comando giusto: senza questo controllo si
# arriva a from_pretrained() con un path inventato (pii_model_legacy, ultimo fallback) e
# l'errore parla di una cartella che l'utente non ha mai sentito nominare.
if not getattr(sys, "_MEIPASS", None) and not os.path.isdir(MODEL_DIR):
    _v = APP_MODEL_VERSION or "1.5.0"
    print(f"ERRORE: modello non trovato in {MODEL_DIR}\n"
          f"Scaricalo con (la revision e la cartella devono combaciare):\n"
          f"  hf download rizzoaiacademy/rizzo-pii-0.3B --revision v{_v} "
          f"--local-dir models/rizzo-pii-0.3B-v{_v}\n"
          f"Oppure indica una cartella tua con la variabile PII_MODEL_DIR.",
          file=sys.stderr)
    sys.exit(2)

ASSETS_DIR = _resource_path("assets")   # mascotte / icone (servite su /assets/<file>)
APP_VERSION = "2.0.0"                    # versione mostrata nell'UI (allineata a tauri.conf.json)
MAX_WORDS = 120      # parole per chunk (~180 subword, sotto i 512 del training)
OVERLAP = 20         # parole di sovrapposizione tra chunk consecutivi

# --------------------------------------------------------------------------- #
# Caricamento del modello: PIGRO e una volta sola.
#
# torch e transformers si importano solo qui dentro: `import app` costa millisecondi
# e non richiede torch (i test della parte HTTP girano anche senza). Gli entry point
# (python app.py, serve.py, desktop_app.py) chiamano load_model() PRIMA di app.run(),
# quindi all'avvio il comportamento e' quello di sempre: il server risponde solo a
# modello caricato. Sotto gunicorn (Docker) lo fa PII_PRELOAD_MODEL=1 all'import.
# Chi arriva prima del caricamento (es. import senza entry point) lo innesca alla
# prima inferenza; le richieste concorrenti aspettano lo stesso caricamento.
# --------------------------------------------------------------------------- #
nlp = None                 # pipeline HF; None finche' non e' caricata
device = -1                # 0 = GPU, -1 = CPU (valorizzato da load_model)
_NLP_LOCK = threading.Lock()


def load_model():
    """Carica la pipeline del modello se non c'e' gia'. Idempotente e thread-safe."""
    global nlp, device
    if nlp is not None:
        return nlp
    with _NLP_LOCK:
        if nlp is None:
            import torch
            from transformers import pipeline
            device = 0 if torch.cuda.is_available() else -1
            print(f"Carico il modello da {MODEL_DIR} su {'GPU' if device == 0 else 'CPU'}...")
            nlp = pipeline(
                "token-classification",
                model=MODEL_DIR,
                tokenizer=MODEL_DIR,
                aggregation_strategy="simple",
                device=device,
            )
            print("Modello pronto.")
    return nlp


# L'inferenza gira sempre sullo STESSO thread, qualunque thread serva la richiesta
# (issue upstream #139, PR #141). torch crea il suo pool OpenMP
# (torch.get_num_threads() - 1 thread) per ogni thread che lo chiama, e su Windows non
# lo libera quando quel thread finisce: con app.run(threaded=True), che usa un thread
# nuovo per richiesta, ogni /analyze lasciava un pool intero, senza limite. Con un
# worker fisso il pool nasce una volta sola. Le richieste in parallelo fanno la fila
# qui, ma sulla CPU torch usa gia' tutti i core.
_INFERENZA = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inferenza")


if os.environ.get("PII_PRELOAD_MODEL", "").strip().lower() in ("1", "true", "yes", "on"):
    load_model()

STATIC_DIR = _resource_path("static")   # index.html + app.css + app.js (la UI)
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

# Estensioni accettate dall'upload (il PDF passa da PyMuPDF, il resto e' testo puro).
TEXT_EXTS = {".md", ".markdown", ".txt", ".text"}

# --------------------------------------------------------------------------- #
# Anteprima a video dei PDF (documento caricato a sinistra, anonimizzato a destra)
#
# I PDF vengono renderizzati QUI, con PyMuPDF, e serviti come PNG per pagina: non
# serve un viewer PDF nel browser (WebView2/WKWebView dentro Tauri non lo hanno in
# modo affidabile) ne' una libreria JS esterna (l'app e' offline, niente CDN).
#
# I documenti restano in memoria (mai su disco: valgono quanto il documento stesso)
# in una LRU piccola, e muoiono con il processo. Il render di una pagina e' pigro e
# poi tenuto in cache: aprire un PDF di 200 pagine non costa 200 render.
# --------------------------------------------------------------------------- #
PREVIEW_DPI = 110          # ~1150 px di larghezza su un A4: leggibile senza pesare
MAX_DOCS = 6               # documenti tenuti in memoria (LRU, i piu' vecchi cadono)

_DOCS = OrderedDict()      # doc_id -> {"pdf": bytes, "pages": {n: png}, "n_pages", "name"}
_DOCS_LOCK = threading.Lock()


def _safe_name(name, default="documento.pdf"):
    """Nome file pulito per Content-Disposition (ASCII, niente separatori)."""
    base = os.path.basename(name or "").strip()
    base = re.sub(r"[^A-Za-z0-9._\- ]+", "_", base).strip(" ._")
    return base or default


def _store_doc(data, name="documento.pdf"):
    """Mette un PDF nello store dell'anteprima. Ritorna (doc_id, n_pages)."""
    with fitz.open(stream=data, filetype="pdf") as doc:
        n_pages = doc.page_count
    doc_id = secrets.token_urlsafe(12)
    with _DOCS_LOCK:
        _DOCS[doc_id] = {"pdf": data, "pages": {}, "n_pages": n_pages,
                         "name": _safe_name(name)}
        while len(_DOCS) > MAX_DOCS:
            _DOCS.popitem(last=False)
    return doc_id, n_pages


def _get_doc(doc_id):
    with _DOCS_LOCK:
        d = _DOCS.get(doc_id)
        if d is not None:
            _DOCS.move_to_end(doc_id)          # LRU: l'uso lo tiene in vita
    return d


def _page_png(d, n, dpi=PREVIEW_DPI):
    """PNG della pagina n (0-based) del documento, con cache. None se fuori range."""
    if not (0 <= n < d["n_pages"]):
        return None
    key = (n, dpi)
    with _DOCS_LOCK:
        png = d["pages"].get(key)
    if png is None:
        with fitz.open(stream=d["pdf"], filetype="pdf") as doc:
            png = doc.load_page(n).get_pixmap(dpi=dpi).tobytes("png")
        with _DOCS_LOCK:
            d["pages"][key] = png
    return png


# --------------------------------------------------------------------------- #
# Legenda dei tag + tag esclusi dall'anonimizzazione
#
# I 22 tag del modello (docs/TASSONOMIA_TAG.md) + URL e IPADDR, che sono solo-regex:
# il modello non e' stato addestrato su di essi, li trova la rete regex.
# L'utente puo' DESELEZIONARE un tag: le entita' di quel tipo vengono rilevate ma non
# sostituite (restano in chiaro). Serve a chi deve confrontare gli importi (AMOUNT) o
# tenere eta'/sesso in un caso clinico.
# --------------------------------------------------------------------------- #
TAGS = [
    ("FULLNAME", "Nome di persona (anche ruoli legali: giudice, avvocato, parti, teste)",
     "Person name (legal roles included: judge, lawyer, parties, witness)", "Mario Rossi"),
    ("AGE", "Età", "Age", "45 anni"),
    ("GENDER", "Sesso / genere", "Sex / gender", "Femmina"),
    ("DATE", "Data di calendario", "Calendar date", "12/06/1985"),
    ("TIME", "Ora", "Time of day", "ore 15:30"),
    ("STREET", "Via / piazza / corso", "Street / square", "Via Garibaldi"),
    ("BUILDINGNUM", "Numero civico", "Building number", "24"),
    ("ZIPCODE", "CAP", "ZIP / postal code", "00185"),
    ("CITY", "Città", "City", "Milano"),
    ("PROVINCE", "Sigla della provincia", "Province code", "MI"),
    ("EMAIL", "Email, PEC inclusa", "Email, certified mail included", "m.rossi@studio.it"),
    ("TELEPHONENUM", "Numero di telefono", "Phone number", "+39 333 1234567"),
    ("CF", "Codice fiscale (checksum verificato)", "Italian tax code (checksum verified)",
     "RSSMRA85H12F205Y"),
    ("PIVA", "Partita IVA (checksum verificato)", "VAT number (checksum verified)", "12345678903"),
    ("ID_DOC", "Numero di documento d'identità (carta, passaporto, patente)",
     "Identity document number (ID card, passport, driving licence)", "CA12345AB"),
    ("IBAN", "IBAN / numero di conto (checksum verificato)",
     "IBAN / account number (checksum verified)", "IT60X0542811101000000123456"),
    ("CREDITCARDNUMBER", "Numero di carta di credito (Luhn verificato)",
     "Credit card number (Luhn verified)", "4111 1111 1111 1111"),
    ("AMOUNT", "Importo in denaro", "Money amount", "€ 12.500,00"),
    ("TARGA", "Targa di veicolo", "Vehicle plate", "AB 123 CD"),
    ("ORG", "Ragione sociale privata: società, studio legale, banca",
     "Private organization: company, law firm, bank", "Edilnord S.r.l."),
    ("DOCID", "Codice di un atto: ruolo generale, protocollo, repertorio, sentenza",
     "Document code: case number, protocol, repertoire, judgment", "1234/2024"),
    ("CATASTO", "Dati catastali: foglio, particella, subalterno",
     "Land registry data: sheet, parcel, subordinate", "Foglio 12, part. 345, sub. 6"),
    ("URL", "Indirizzo web (rilevato solo dalla rete regex, non dal modello)",
     "Web address (regex net only, not from the model)", "https://www.studiorossi.it"),
    ("IPADDR", "Indirizzo IP, anche subnet CIDR o range (rilevato solo dalla rete regex)",
     "IP address, including CIDR subnet or range (regex net only)", "192.168.1.1"),
]
TAG_NAMES = [t[0] for t in TAGS]

# Preferenze di default del server (env > prefs.json). Gli argomenti CLI le sovrascrivono
# all'avvio; ogni singola richiesta puo' comunque passare le proprie.
#
# MAPPING_ENABLED = False -> l'anonimizzazione e' DEFINITIVA: nessun dizionario
# placeholder->valore viene costruito, restituito o salvato, e la risposta non contiene
# piu' il testo originale delle entita' (nemmeno dentro `segments`). Serve a chi non
# vuole che una chiave di ripristino esista, da nessuna parte.
_prefs = server_config.load_prefs()
EXCLUDED_TAGS = _prefs["excluded_tags"]
MAPPING_ENABLED = _prefs["mapping_enabled"]
_PREFS_LOCK = threading.Lock()   # protegge la lettura/scrittura ATOMICA della coppia sopra


def _get_prefs():
    """Snapshot coerente di (excluded_tags, mapping_enabled): mai una POST /settings a meta'."""
    with _PREFS_LOCK:
        return EXCLUDED_TAGS, MAPPING_ENABLED


# --------------------------------------------------------------------------- #
# Chunking word-safe + inferenza del modello su tutto il documento
# --------------------------------------------------------------------------- #
def chunk_text(text, max_words=MAX_WORDS, overlap=OVERLAP):
    """Ritorna [(sottostringa, offset_char_globale), ...] senza tagliare parole."""
    words = list(re.finditer(r"\S+", text))
    if not words:
        return []
    chunks, i = [], 0
    step = max(1, max_words - overlap)
    while i < len(words):
        block = words[i:i + max_words]
        start, end = block[0].start(), block[-1].end()
        chunks.append((text[start:end], start))      # slice esatto -> offset diretti
        if i + max_words >= len(words):
            break
        i += step
    return chunks


def detect_model(text):
    """Entita' trovate dal modello mmBERT su tutti i chunk, su offset globali."""
    chunks = chunk_text(text)
    ents = []
    if chunks:
        results = _INFERENZA.submit(load_model(), [c for c, _ in chunks]).result()
        if isinstance(results, dict):                 # singolo chunk -> normalizza
            results = [results]
        for (_, off), res in zip(chunks, results):
            for e in res:
                ents.append({
                    "label": e["entity_group"],
                    "start": int(e["start"]) + off,
                    "end": int(e["end"]) + off,
                    "score": float(e["score"]),
                    "validated": False,
                    "source": "modello",
                })
    return ents, len(chunks)


# --------------------------------------------------------------------------- #
# Fusione modello + regex, ID reversibili, testo anonimizzato
# --------------------------------------------------------------------------- #
def _is_word(ch):
    """Carattere interno a una parola (lettere accentate e cifre incluse)."""
    return ch.isalnum() or ch == "_"


def _merge(cands, text):
    """Greedy senza overlap. Priorita': checksum-valido > regex (non soft) > score
    > lunghezza.
    La rete regex copre campi a forma molto specifica: per quegli span e' piu' affidabile
    del modello (evita la frammentazione di CF/IBAN/carta in piu' pezzi). Fanno eccezione
    i tag in SOFT_REGEX_LABELS, dove la forma non ha un checksum a confermarla."""
    order = sorted(
        cands,
        key=lambda e: (1 if e["validated"] else 0,
                       1 if (e["source"] == "regex"
                             and e["label"] not in SOFT_REGEX_LABELS) else 0,
                       e["score"], e["end"] - e["start"]),
        reverse=True,
    )
    kept = []
    for e in order:
        # kept resta ordinata per start e senza sovrapposizioni: allora un candidato puo'
        # accavallarsi solo con i due vicini, che la ricerca binaria trova subito. Il
        # confronto con TUTTA kept era O(n^2): su un documento con 40.000 entita' (una chat
        # esportata di qualche MB) erano 111 s spesi qui, dopo l'inferenza.
        i = bisect.bisect_right(kept, e["start"], key=lambda k: k["start"])
        if (i and kept[i - 1]["end"] > e["start"]) or \
           (i < len(kept) and kept[i]["start"] < e["end"]):
            continue
        kept.insert(i, e)
    # niente spazi inglobati nei placeholder (il modello a volte include lo spazio iniziale)
    for e in kept:
        while e["start"] < e["end"] and text[e["start"]].isspace():
            e["start"] += 1
        while e["end"] > e["start"] and text[e["end"] - 1].isspace():
            e["end"] -= 1
    kept = [e for e in kept if e["end"] > e["start"]]

    # Allineamento ai confini di parola. Il modello etichetta i sotto-token e a volte ne
    # copre solo una parte ("No" di "Novara"): sostituendo la span cosi' com'e' resterebbe
    # "[CITY_1]vara", cioe' un valore ancora ricostruibile. Se una span taglia una parola
    # a meta', la si estende fino a coprirla. Nel dubbio si maschera un carattere in piu':
    # per un anonimizzatore l'errore per eccesso e' l'unico accettabile.
    for e in kept:
        while (e["start"] > 0
               and _is_word(text[e["start"] - 1]) and _is_word(text[e["start"]])):
            e["start"] -= 1
        while (e["end"] < len(text)
               and _is_word(text[e["end"]]) and _is_word(text[e["end"] - 1])):
            e["end"] += 1

    # L'estensione puo' rendere due span sovrapposte o adiacenti: si fondono, altrimenti
    # una stessa parola verrebbe sostituita due volte ("[CATASTO_1][CATASTO_2]").
    kept.sort(key=lambda e: (e["start"], -(e["end"] - e["start"])))
    merged = []
    for e in kept:
        if merged and e["start"] < merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], e["end"])
            continue
        if merged and e["start"] == merged[-1]["end"] and e["label"] == merged[-1]["label"]:
            merged[-1]["end"] = e["end"]
            continue
        merged.append(e)
    return merged


def _norm(s):
    return re.sub(r"\s+", " ", s.strip()).casefold()


# --------------------------------------------------------------------------- #
# Sessioni di numerazione dei segnaposto
#
# Senza sessione ogni analisi ricomincia da [FULLNAME_1]: due documenti analizzati
# uno dopo l'altro davano lo stesso [FULLNAME_1] a due persone diverse, e la UI
# sostituiva il dizionario del primo con quello del secondo (il ripristino del
# primo testo non era piu' possibile). Con un session_id la numerazione CONTINUA:
# un valore gia' visto nella sessione riprende il suo segnaposto, uno nuovo prende
# il numero successivo. /pdf e /pdf/preview usano la stessa sessione, quindi il
# PDF censurato ha gli stessi segnaposto del testo.
#
# Lo stato sta in memoria (muore col processo) e NON contiene i valori: la chiave
# e' un HMAC di (tag, valore normalizzato) con un segreto del processo, cosi' anche
# con il dizionario disattivato nessuna PII resta nel server tra una richiesta e
# l'altra. Senza session_id il comportamento e' quello di sempre (API invariata).
# --------------------------------------------------------------------------- #
MAX_SESSIONS = 32
_SESSIONS = OrderedDict()          # session_id -> {"seen": {hmac: ph}, "counters": {}}
_SESSIONS_LOCK = threading.Lock()
_SESSION_SECRET = secrets.token_bytes(32)
_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _session_key(label, value):
    msg = f"{label}\0{_norm(value)}".encode("utf-8")
    return hmac.new(_SESSION_SECRET, msg, hashlib.sha256).hexdigest()


def valid_session_id(sid):
    return isinstance(sid, str) and bool(_SESSION_RE.fullmatch(sid))


def _session_state(sid):
    """Stato della sessione (creato se manca), in cima alla LRU. Va usato col lock."""
    st = _SESSIONS.get(sid)
    if st is None:
        st = {"seen": {}, "counters": {}}
        _SESSIONS[sid] = st
        while len(_SESSIONS) > MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    _SESSIONS.move_to_end(sid)
    return st


def reset_session(sid):
    with _SESSIONS_LOCK:
        return _SESSIONS.pop(sid, None) is not None


def analyze(text, excluded=None, mapping_enabled=True, session_id=None):
    """excluded = tag da NON anonimizzare: le entita' di quel tipo vengono scartate
    prima della fusione, quindi il valore resta in chiaro nel testo di output.

    mapping_enabled=False -> anonimizzazione DEFINITIVA: nessun dizionario
    placeholder->valore, e i segmenti-entita' non riportano il testo originale
    (che altrimenti permetterebbe di ricostruire il dizionario dalla risposta).
    La numerazione dei placeholder resta: dice che due occorrenze sono lo stesso
    soggetto, ma da sola non fa risalire al valore.

    session_id -> numerazione continua tra piu' analisi (vedi "Sessioni" sopra).
    `mapping` contiene comunque solo i segnaposto presenti in QUESTO testo."""
    excluded = set(excluded or ())
    model_ents, n_chunks = detect_model(text)
    # il modello taglia i minuti delle ore: la regex dell'ora completa SOLO le sue span TIME
    complete_time(model_ents, text)
    cands = model_ents + detect_regex(text)
    if excluded:
        cands = [e for e in cands if e["label"] not in excluded]
    kept = _merge(cands, text)

    # ID reversibili: stesso (label, valore-normalizzato) -> stesso placeholder.
    # Con una sessione i contatori e i valori gia' visti vengono da li' (sotto lock:
    # due richieste concorrenti della stessa sessione non devono darsi lo stesso numero).
    lock = _SESSIONS_LOCK if session_id else threading.Lock()
    mapping, used = {}, set()
    with lock:
        st = _session_state(session_id) if session_id else {"seen": {}, "counters": {}}
        counters, seen = st["counters"], st["seen"]
        for e in kept:
            val = text[e["start"]:e["end"]]
            key = _session_key(e["label"], val)
            if key not in seen:
                counters[e["label"]] = counters.get(e["label"], 0) + 1
                seen[key] = f"[{e['label']}_{counters[e['label']]}]"
            ph = seen[key]
            e["ph"] = ph
            used.add(ph)
            if mapping_enabled and ph not in mapping:
                mapping[ph] = val

    # segmenti per la preview + testo anonimizzato + statistiche
    segments, anon, by_label, by_source, pos = [], [], {}, {}, 0
    for e in kept:
        if e["start"] > pos:
            segments.append({"t": text[pos:e["start"]]})
            anon.append(text[pos:e["start"]])
        seg = {
            "label": e["label"],
            "ph": e["ph"],
            "src": e["source"],
            "validated": e["validated"],
        }
        if mapping_enabled:                       # senza dizionario niente valore originale
            seg["t"] = text[e["start"]:e["end"]]
        segments.append(seg)
        anon.append(e["ph"])
        by_label[e["label"]] = by_label.get(e["label"], 0) + 1
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
        pos = e["end"]
    if pos < len(text):
        segments.append({"t": text[pos:]})
        anon.append(text[pos:])

    return {
        "segments": segments,
        "anonymized_text": "".join(anon),
        "mapping": mapping,
        "mapping_enabled": mapping_enabled,
        "n_chunks": n_chunks,
        "n_chars": len(text),
        "n_entities": len(kept),
        "n_unique": len(used),
        "by_label": dict(sorted(by_label.items(), key=lambda x: -x[1])),
        "by_source": by_source,
        "excluded_tags": sorted(excluded),
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Protezione del server locale (Host/Origin + token di sessione): vedi local_guard.
# BIND_HOST e' l'host su cui il server ascolta; gli entry point lo riallineano
# dopo aver letto la CLI. Serve a sapere quali Host ammettere oltre al loopback.
# --------------------------------------------------------------------------- #
BIND_HOST = server_config.resolve()[0]


@app.before_request
def _guard():
    err = local_guard.check(request.method, request.path, request.headers,
                            request.args, BIND_HOST)
    if err is not None:
        status, msg = err
        return jsonify({"error": msg}), status
    return None


@app.after_request
def _security_headers(resp):
    for k, v in local_guard.SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


# --------------------------------------------------------------------------- #
# UI (single page): HTML, CSS e JS stanno in static/ (index.html, app.css, app.js).
# index.html e' letto una volta all'avvio; __VERSION__ viene sostituito al volo.
# --------------------------------------------------------------------------- #
with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as _f:
    PAGE = _f.read()


def _page():
    # il token di sessione arriva alla UI solo dentro la pagina (vedi local_guard)
    return (PAGE.replace("__VERSION__", APP_VERSION)
                .replace("__TOKEN__", local_guard.API_TOKEN))


@app.route("/")
def index():
    return _page()


@app.route("/assets/<path:fn>")
def assets(fn):
    if os.path.isfile(os.path.join(ASSETS_DIR, fn)):
        return send_from_directory(ASSETS_DIR, fn)
    return ("", 404)


@app.route("/favicon.ico")
def favicon():
    if os.path.isfile(os.path.join(ASSETS_DIR, "mascot_shield.png")):
        return send_from_directory(ASSETS_DIR, "mascot_shield.png")
    return ("", 204)


@app.errorhandler(404)
def not_found(_e):
    return _page()


@app.route("/health")
@app.route("/healthz")
def health():
    """Liveness/readiness SENZA inference: sonda economica per orchestratori e sidecar.
    200 = modello caricato e pronto; 503 = server su ma modello non disponibile."""
    ready = nlp is not None
    excl, mapping = _get_prefs()
    body = {
        "status": "ok" if ready else "loading",
        "model_loaded": ready,
        "model": os.path.basename(str(MODEL_DIR).rstrip("/\\")),
        "model_version": APP_MODEL_VERSION,
        "app_version": APP_VERSION,
        "device": "cuda" if device == 0 else "cpu",
        "tags": len(TAG_NAMES),
        "excluded_tags": excl,
        "mapping_enabled": mapping,
    }
    return jsonify(body), (200 if ready else 503)


def _is_pdf(name, data):
    return os.path.splitext((name or "").lower())[1] == ".pdf" or data[:5] == b"%PDF-"


def _text_from_bytes(name, data):
    """Testo dai bytes di un upload: PDF via PyMuPDF, .md/.txt come testo puro.

    Per i PDF non basta `page.get_text()`: nei moduli AcroForm il valore sta
    nei widget (issue #85). `pdf_text.collect_readable_text` allinea
    l'estrazione a quanto `pdf_export` gia' ispezionava per i residui.
    """
    name = (name or "").lower()
    ext = os.path.splitext(name)[1]
    if _is_pdf(name, data):
        with fitz.open(stream=data, filetype="pdf") as doc:
            # senza, l'utente leggeva l'errore grezzo di PyMuPDF: "document closed or
            # encrypted". Un PDF con la sola password del proprietario si apre: needs_pass
            # e' falso e si prosegue.
            if doc.needs_pass:
                raise PdfLocked("PDF protetto da password: rimuovi la protezione e riprova.")
            # Non solo page.get_text(): nei PDF fillable il valore sta nei
            # widget AcroForm (issue #85). Stesso contratto di pdf_export.
            return pdf_text.collect_readable_text(doc)
    if ext in TEXT_EXTS or not ext:
        # utf-16 solo col BOM: senza, il codec accetta qualunque sequenza di lunghezza pari
        # e si mangia i .txt a 8 bit. cp1252 prima di latin-1: e' quello che scrivono Word
        # e il Blocco note italiani, e i due differiscono su euro e virgolette tipografiche.
        primo = ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ()
        for enc in primo + ("utf-8-sig", "cp1252", "latin-1"):
            try:
                # Via i byte nulli. Un utf-16 SENZA BOM finisce per forza su una codifica a
                # 8 bit e il testo esce "M\0a\0r\0i\0o": a schermo non si vede niente (in
                # HTML il NUL e' invisibile), quindi l'utente legge il documento intero,
                # non trova segnaposti e conclude che non ci fosse nulla da anonimizzare -
                # mentre i rilevatori, che lavorano sul testo vero, non trovano niente.
                # In un utf-16 latino il byte alto e' proprio quello nullo: tolto, il testo
                # torna esatto e le PII tornano trovabili. Riconoscere l'utf-16 contando i
                # byte nulli, invece, sbaglia in entrambe le direzioni.
                return data.decode(enc).replace("\x00", "")
            except UnicodeDecodeError:
                continue
    raise ValueError(
        f"Formato non supportato: {ext or name or 'sconosciuto'}. "
        "Accetto .pdf, .md, .txt oppure testo incollato."
    )


def _no_text_pages(name, data):
    """Pagine (da 1) con immagini ma senza testo estraibile: le PII li' dentro NON
    vengono anonimizzate (niente OCR). Vuota se non e' un PDF o non si apre."""
    if not data or not _is_pdf(name, data):
        return []
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                return []
            return pdf_text.pages_without_text(doc)
    except Exception:
        return []


class PdfLocked(ValueError):
    """PDF con password di apertura: /preview lo segnala con `code`, cosi' l'UI lo
    dice nella sua lingua invece di "Anteprima del PDF non disponibile"."""


def _extract_upload(fs):
    """Testo da un file caricato (consuma lo stream)."""
    return _text_from_bytes(fs.filename, fs.read())


def _uploaded_file():
    """Il file dell'upload, se c'e'. "pdf" e' il nome storico del campo (l'UI lo
    usa ancora), "file" e' l'alias nuovo."""
    return next((request.files[k] for k in ("pdf", "file")
                 if k in request.files and request.files[k].filename), None)


@app.route("/analyze", methods=["POST"])
def analyze_route():
    up = _uploaded_file()
    no_text = []
    if up is not None:
        try:
            name, data = up.filename, up.read()
            text = _text_from_bytes(name, data)
            no_text = _no_text_pages(name, data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:                       # PDF corrotto / protetto
            return jsonify({"error": f"Impossibile leggere il file: {e}"}), 400
        raw_excl = request.form.get("exclude_tags")
        raw_map = request.form.get("include_mapping")
        raw_sid = request.form.get("session_id")
    else:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text", "")
        raw_excl = payload.get("exclude_tags")
        raw_map = payload.get("include_mapping")
        raw_sid = payload.get("session_id")
    if raw_sid is not None and not valid_session_id(raw_sid):
        return jsonify({"error": "session_id non valido."}), 400

    text = (text or "").strip()
    if not text:
        return jsonify({"error": "Nessun testo da analizzare."}), 400

    # override per-richiesta; senza override vale la configurazione del server
    # (snapshot atomico: una POST /settings concorrente non deve far leggere
    # un tag escluso aggiornato insieme a un mapping_enabled non ancora aggiornato, o viceversa)
    default_excl, default_map = _get_prefs()
    excl = server_config.parse_tag_list(raw_excl) if raw_excl is not None else default_excl
    keep_map = (server_config.parse_bool(raw_map, default_map)
                if raw_map is not None else default_map)
    out = analyze(text, excl, keep_map, session_id=raw_sid)
    # Il testo originale torna solo col dizionario attivo (l'UI lo rimette nella casella
    # dopo un upload). In modalita' definitiva la risposta non deve contenere nessun
    # valore in chiaro: la chiave manca del tutto, come il valore nei segmenti (#118).
    if keep_map:
        out["source_text"] = text
    out["pages_no_text"] = no_text
    return jsonify(out)


# --------------------------------------------------------------------------- #
# PDF anonimizzato da scaricare (issue #7, punto 1)
# --------------------------------------------------------------------------- #
def _pdf_response(data, report, redactions, filename="documento_anonimizzato.pdf"):
    """Risposta binaria + intestazioni diagnostiche. X-PII-Residual e
    X-PII-Skipped sono le due che l'UI trasforma in AVVISO: valori del documento
    che, per motivi diversi, sono rimasti in chiaro."""
    resp = app.response_class(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    resp.headers["X-PII-Redactions"] = str(redactions)
    resp.headers["X-PII-Residual"] = str(len(report.get("residual", [])))
    resp.headers["X-PII-Skipped"] = str(len(report.get("skipped", [])))
    resp.headers["X-PII-Notfound"] = str(len(report.get("not_found", [])))
    # pagine scansionate (immagini senza testo): restano come sono, PII comprese
    resp.headers["X-PII-NoText-Pages"] = ",".join(map(str, report.get("no_text_pages", [])))
    return resp


class _ReqError(Exception):
    """Errore da rendere al client come {"error": ...} con uno status preciso."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def _build_anonymized_pdf():
    """Costruisce il PDF ANONIMIZZATO dall'input della richiesta. Stesso input di
    /analyze:

    - multipart con un file .pdf  -> REDAZIONE VERA del documento originale: le
      PII sono rimosse dal content stream e sostituite dai placeholder, il layout
      resta quello di partenza (piu' metadati/annotazioni/campi modulo/segnalibri
      ripuliti e allegati rimossi);
    - multipart con .md/.txt oppure {"text": ...} -> PDF ricostruito impaginando
      da zero il solo testo anonimizzato.

    Il dizionario placeholder->valore NON viaggia sulla rete in nessuna delle due
    direzioni: viene ricostruito qui, serve solo a localizzare le PII nel PDF e
    muore con la richiesta. Per questo funziona identico anche con il dizionario
    reversibile disattivato (MAPPING_ENABLED=False), dove il client non ne ha
    nessuno.

    Ritorna (bytes, report, n_redazioni, nome_file). Solleva _ReqError sugli errori.
    """
    up = _uploaded_file()
    if up is not None:
        name, data = up.filename, up.read()
        try:
            text = _text_from_bytes(name, data)
        except ValueError as e:
            raise _ReqError(str(e))
        except Exception as e:                       # PDF corrotto / protetto
            raise _ReqError(f"Impossibile leggere il file: {e}")
        raw_excl = request.form.get("exclude_tags")
        raw_sid = request.form.get("session_id")
    else:
        payload = request.get_json(silent=True) or {}
        name, data, text = "", b"", payload.get("text", "")
        raw_excl = payload.get("exclude_tags")
        raw_sid = payload.get("session_id")
    if raw_sid is not None and not valid_session_id(raw_sid):
        raise _ReqError("session_id non valido.")

    text = (text or "").strip()
    if not text:
        raise _ReqError("Nessun testo da anonimizzare.")

    stem = os.path.splitext(_safe_name(name, "documento.pdf"))[0] or "documento"
    out_name = f"{stem}_anonimizzato.pdf"

    excl = (server_config.parse_tag_list(raw_excl) if raw_excl is not None
            else _get_prefs()[0])
    # mapping_enabled=True e' interno: il risultato non esce da questa funzione.
    # stessa sessione dell'analisi a video -> stessi segnaposto nel PDF
    res = analyze(text, excl, mapping_enabled=True, session_id=raw_sid)
    if not res["mapping"]:
        raise _ReqError("Nessuna PII trovata: non c'e' niente da "
                        "anonimizzare in questo documento.", 422)

    if data and _is_pdf(name, data):
        try:
            out, report = pdf_export.redact_pdf(data, res["mapping"])
        except pdf_export.PdfError as e:
            raise _ReqError(str(e))
        report["no_text_pages"] = _no_text_pages(name, data)
        if report["occurrences"] == 0:
            raise _ReqError("Nessuna occorrenza trovata nel PDF: se il documento "
                            "e' una scansione (testo dentro un'immagine) la "
                            "redazione del layer testuale non puo' agire.", 422)
        return out, report, report["occurrences"], out_name

    try:
        out = pdf_export.text_to_pdf(res["anonymized_text"])
    except pdf_export.PdfError as e:
        raise _ReqError(str(e))
    # testo reimpaginato da zero: nell'output c'e' solo il testo anonimizzato,
    # quindi niente residui e niente valori saltati da segnalare.
    return out, {}, res["n_entities"], out_name


@app.route("/pdf", methods=["POST"])
def pdf_route():
    """Scarica il documento ANONIMIZZATO in PDF (vedi _build_anonymized_pdf)."""
    try:
        out, report, redactions, name = _build_anonymized_pdf()
    except _ReqError as e:
        return jsonify({"error": e.msg}), e.status
    return _pdf_response(out, report, redactions, name)


@app.route("/pdf/preview", methods=["POST"])
def pdf_preview_route():
    """Come /pdf, ma il binario resta in memoria e si guarda a video: la risposta
    e' il descrittore del documento ({doc_id, n_pages, ...}), le pagine si prendono
    poi da /doc/<id>/page/<n>.png e il file da /doc/<id>/file.pdf.

    Serve a non anonimizzare due volte: l'anteprima a destra e il download del PDF
    usano lo STESSO documento, generato una volta sola."""
    try:
        out, report, redactions, name = _build_anonymized_pdf()
    except _ReqError as e:
        return jsonify({"error": e.msg}), e.status
    doc_id, n_pages = _store_doc(out, name)
    return jsonify({
        "doc_id": doc_id,
        "n_pages": n_pages,
        "filename": name,
        "redactions": redactions,
        "residual": len(report.get("residual", [])),
        "skipped": len(report.get("skipped", [])),
        "not_found": len(report.get("not_found", [])),
        "pages_no_text": report.get("no_text_pages", []),
    })


# --------------------------------------------------------------------------- #
# Anteprima a video del documento CARICATO + pagine renderizzate
# --------------------------------------------------------------------------- #
@app.route("/preview", methods=["POST"])
def preview_route():
    """Tiene in memoria il PDF appena caricato per mostrarlo a sinistra e ne
    ritorna anche il testo estratto (cosi' l'UI puo' offrire "PDF" o "Testo"
    senza una seconda estrazione)."""
    up = _uploaded_file()
    if up is None:
        return jsonify({"error": "Nessun file caricato."}), 400
    name, data = up.filename, up.read()
    if not _is_pdf(name, data):
        return jsonify({"error": "L'anteprima renderizzata vale solo per i PDF."}), 400
    try:
        text = _text_from_bytes(name, data)
        doc_id, n_pages = _store_doc(data, name)
    except PdfLocked as e:
        return jsonify({"error": f"Impossibile leggere il file: {e}", "code": "pdf_locked"}), 400
    except Exception as e:                           # PDF corrotto / protetto
        return jsonify({"error": f"Impossibile leggere il file: {e}"}), 400
    return jsonify({"doc_id": doc_id, "n_pages": n_pages,
                    "filename": _safe_name(name), "text": text,
                    "pages_no_text": _no_text_pages(name, data)})


@app.route("/doc/<doc_id>/page/<int:n>.png")
def doc_page(doc_id, n):
    """Pagina n (0-based) renderizzata in PNG. 404 se il documento e' scaduto
    dalla LRU: l'UI in quel caso rifa' l'upload."""
    d = _get_doc(doc_id)
    if d is None:
        return ("", 404)
    png = _page_png(d, n)
    if png is None:
        return ("", 404)
    resp = app.response_class(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@app.route("/doc/<doc_id>/file.pdf")
def doc_file(doc_id):
    """Download del PDF tenuto in memoria (quello gia' anonimizzato da
    /pdf/preview): non ricalcola niente."""
    d = _get_doc(doc_id)
    if d is None:
        return jsonify({"error": "Documento non piu' disponibile."}), 404
    resp = app.response_class(d["pdf"], mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f"attachment; filename={d['name']}"
    return resp


@app.route("/session/reset", methods=["POST"])
def session_reset():
    """Dimentica la numerazione di una sessione (la UI lo chiama con "Pulisci")."""
    sid = (request.get_json(silent=True) or {}).get("session_id")
    if not valid_session_id(sid):
        return jsonify({"error": "session_id non valido."}), 400
    return jsonify({"ok": True, "existed": reset_session(sid)})


# --------------------------------------------------------------------------- #
# Impostazioni: legenda dei tag, tag esclusi, dizionario reversibile on/off
# (/tags resta come alias storico dello stesso endpoint)
# --------------------------------------------------------------------------- #
@app.route("/settings", methods=["GET"])
@app.route("/tags", methods=["GET"])
def settings_get():
    excl, mapping = _get_prefs()
    return jsonify({
        "tags": [{"tag": t, "it": it, "en": en, "example": ex} for t, it, en, ex in TAGS],
        "excluded_tags": excl,
        "mapping_enabled": mapping,
        "config_path": str(server_config.prefs_path()),
        "env_override": "PII_EXCLUDE_TAGS" in os.environ or "PII_MAPPING" in os.environ,
    })


@app.route("/settings", methods=["POST"])
@app.route("/tags", methods=["POST"])
def settings_post():
    global EXCLUDED_TAGS, MAPPING_ENABLED
    data = request.get_json(silent=True) or {}
    tags = None
    if "excluded_tags" in data:
        tags = server_config.parse_tag_list(data["excluded_tags"])
        unknown = [t for t in tags if t not in TAG_NAMES]
        if unknown:
            return jsonify({"error": f"Tag sconosciuti: {', '.join(unknown)}"}), 400
    mapping = data.get("mapping_enabled")
    if tags is None and mapping is None:
        return jsonify({"error": "Niente da salvare: passa excluded_tags e/o mapping_enabled."}), 400
    saved = server_config.save_prefs(excluded_tags=tags, mapping_enabled=mapping)
    with _PREFS_LOCK:
        EXCLUDED_TAGS = saved["excluded_tags"]
        MAPPING_ENABLED = saved["mapping_enabled"]
    return jsonify({"ok": True, "excluded_tags": EXCLUDED_TAGS,
                    "mapping_enabled": MAPPING_ENABLED})


# --------------------------------------------------------------------------- #
# Config host/porta (GET = leggi, POST = salva per il prossimo avvio)
# --------------------------------------------------------------------------- #
@app.route("/config", methods=["GET"])
def config_get():
    cfg = server_config.load_config()
    return jsonify({
        "host": cfg.get("host", server_config.DEFAULT_HOST),
        "port": cfg.get("port", server_config.DEFAULT_PORT),
        "config_path": str(server_config.config_path()),
    })


@app.route("/config", methods=["POST"])
def config_post():
    data = request.get_json(silent=True) or {}
    host = str(data.get("host", server_config.DEFAULT_HOST)).strip()
    try:
        port = int(data.get("port", server_config.DEFAULT_PORT))
    except (ValueError, TypeError):
        return jsonify({"error": "Porta non valida."}), 400
    if not (1024 <= port <= 65535):
        return jsonify({"error": "La porta deve essere tra 1024 e 65535."}), 400
    if not server_config.valid_host(host):
        return jsonify({"error": "Indirizzo non valido."}), 400
    # Un host diverso dal loopback (0.0.0.0, un IP di LAN) espone i documenti alla
    # rete, in HTTP in chiaro: si salva solo con una conferma ESPLICITA nella richiesta.
    if not local_guard.is_loopback(host) and data.get("confirm_expose") is not True:
        return jsonify({"error": "Esporre il server fuori da questo computer richiede "
                                 "una conferma esplicita.",
                        "code": "confirm_expose"}), 409
    server_config.save_config(host, port)
    return jsonify({"ok": True, "host": host, "port": port})


@app.route("/port-check")
def port_check():
    host = request.args.get("host", server_config.DEFAULT_HOST).strip()
    try:
        port = int(request.args.get("port", server_config.DEFAULT_PORT))
    except (ValueError, TypeError):
        return jsonify({"available": False})
    # solo gli indirizzi su cui questo server potrebbe ascoltare: niente sonda di
    # porte su indirizzi arbitrari
    if not (local_guard.is_loopback(host) or local_guard.is_wildcard(host)
            or host == BIND_HOST):
        return jsonify({"error": "Indirizzo non ammesso per la verifica."}), 400
    return jsonify({"available": server_config.port_available(host, port)})


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser(description="Rizzo PII — server locale di anonimizzazione.")
    _p.add_argument("--host", default=None, help="indirizzo su cui ascoltare (default da config/env/127.0.0.1)")
    _p.add_argument("--port", type=int, default=None, help="porta su cui ascoltare (default da config/env/5005)")
    _p.add_argument("--exclude-tags", default=None, metavar="TAG,TAG",
                    help="tag PII da NON anonimizzare, es. AGE,GENDER (default da env/prefs.json)")
    _p.add_argument("--no-mapping", action="store_true",
                    help="anonimizzazione definitiva: non costruire il dizionario di ripristino")
    _args = _p.parse_args()

    if _args.exclude_tags is not None:
        EXCLUDED_TAGS = server_config.parse_tag_list(_args.exclude_tags)
        _unknown = [t for t in EXCLUDED_TAGS if t not in TAG_NAMES]
        if _unknown:
            print(f"ERRORE: tag sconosciuti in --exclude-tags: {', '.join(_unknown)}")
            print(f"Tag validi: {', '.join(TAG_NAMES)}")
            sys.exit(2)
    if _args.no_mapping:
        MAPPING_ENABLED = False
    if EXCLUDED_TAGS:
        print(f"Tag esclusi dall'anonimizzazione: {', '.join(EXCLUDED_TAGS)}")
    print("Dizionario reversibile: " +
          ("ATTIVO (si puo' ripristinare)" if MAPPING_ENABLED
           else "DISATTIVO (anonimizzazione definitiva)"))

    _host, _port = server_config.resolve(cli_host=_args.host, cli_port=_args.port)

    if not server_config.port_available(_host, _port):
        print(f"ERRORE: porta {_port} occupata su {_host}")
        sys.exit(server_config.EXIT_PORT_CONFLICT)

    BIND_HOST = _host
    load_model()   # come prima del lazy loading: il server parte a modello caricato
    print(f"Server su http://{_host}:{_port}")
    try:
        app.run(host=_host, port=_port, threaded=True)
    except OSError as e:
        print(f"ERRORE bind: {e}")
        sys.exit(server_config.EXIT_PORT_CONFLICT)
