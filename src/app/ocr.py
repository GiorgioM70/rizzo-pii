# -*- coding: utf-8 -*-
"""OCR delle pagine scansionate: macOS Vision, in locale, nessuna installazione.

Perche' serve: l'anonimizzazione lavora sul layer testuale del PDF. In un atto
scansionato quel layer manca, oppure c'e' ma e' quello messo dallo scanner, spesso
INVISIBILE (sotto l'immagine) o con una codifica rovinata: a video escono caratteri
senza senso, il modello non trova niente e le PII restano leggibili nell'immagine.

Cosa fa questo modulo (nessun import di fitz/torch: si testa con oggetti finti):
  - `text_quality` / `is_unreadable`: controllo di qualita' del testo estratto;
  - `plan_pages`: quali pagine leggere con l'OCR e perche';
  - `MacVisionEngine`: chiama l'eseguibile Swift `rizzo-ocr` (framework Vision,
    livello accurato, italiano + inglese) con il PNG della pagina sullo stdin e
    legge un JSON con righe, parole e riquadri;
  - `ocr_pages`: renderizza le pagine con PyMuPDF, le passa al motore e porta i
    riquadri nelle coordinate della pagina, quelle di `add_redact_annot`.

Perche' un eseguibile Swift e non pyobjc: Vision e' un framework di sistema, un
binario di 100 KB compilato con gli strumenti di Xcode gia' richiesti dalla build
non porta dipendenze Python da bloccare, non passa dagli hook di PyInstaller per
pyobjc (decine di estensioni .so da firmare e notarizzare) e lascia un confine
netto (PNG in ingresso, JSON in uscita) facile da sostituire con un finto nei test.

Su Windows e Linux il motore non c'e': nessun errore, le pagine da leggere restano
segnalate all'utente come prima (pages_no_text). Variabile PII_OCR=0 per spegnerlo.
"""

import json
import os
import re
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Controllo di qualita' del testo estratto
# --------------------------------------------------------------------------- #
# Sotto questa soglia (spazi esclusi) il layer testuale e' "quasi vuoto": stessa
# soglia di pdf_text.MIN_TEXT_CHARS (PR upstream #105).
MIN_TEXT_CHARS = 40

# Il testo e' ILLEGGIBILE se vale almeno una di queste (caratteri senza spazi,
# dopo aver compresso le ripetizioni tipo "........" dei moduli da compilare):
BAD_MAX = 0.10        # > 10% di caratteri impossibili: U+FFFD, uso privato,
#                       non assegnati, di controllo (i glifi senza mappa Unicode)
ALNUM_MIN = 0.55      # < 55% di lettere e cifre: un testo vero sta sopra l'80%;
#                       i font con codifica spostata danno soprattutto simboli
WORDS_MIN = 20        # le due prove sulle parole valgono da 20 parole latine in su
VOWEL_MIN = 0.28      # < 28% di vocali tra le lettere: italiano ~45%, inglese e
#                       tedesco ~34-36%, elenchi di nomi e indirizzi ~48%; il testo
#                       con le lettere spostate (font con codifica rovinata) 14-23%
STOP_MIN = 0.03       # < 3% di parole comuni (di, il, che, the, and...): in un atto
#                       italiano sono il 30-40%, in un elenco di nomi ~5%; nel
#                       testo con le lettere spostate 0-6% (coincidenze)
WEIRD_MAX = 0.35      # > 35% di parole impossibili (senza vocali, maiuscole in
#                       mezzo, 5 consonanti di fila)
# Pagina con immagini e testo quasi tutto invisibile = scansione con l'OCR dello
# scanner sotto: si rilegge con Vision, i cui riquadri vengono dall'immagine stessa.
INVISIBLE_MIN = 0.50

STOPWORDS = frozenset("""
di a da in con su per tra fra il lo la i gli le un uno una e ed o che non si del
dello della dei degli delle al allo alla ai agli alle dal dalla dai dalle nel nello
nella nei negli nelle sul sulla sui sulle come anche ma più sono essere ha hanno era
questo questa quale quali suo sua suoi sue cui ove nato nata via art sig dott avv
the of and to in is for on with as by at from that this be are or an it not
de la le les des du et en un une pour dans sur par est au aux que qui
el los las del y en por con para una es lo
der die das und ist mit von zu den des im fur
het een van en is op te
""".split())

_VOWELS = set("aeiouyàáâäèéêëìíîïòóôöùúûü")
_LATIN_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")
_REPEAT = re.compile(r"(.)\1{2,}")


def _is_bad(c):
    if c == "�":
        return True
    cat = unicodedata.category(c)
    return cat in ("Co", "Cn", "Cs") or (cat == "Cc" and c not in "\t\n\r")


def _weird(word):
    w = word.lower()
    if len(w) < 4:
        return False
    if not any(ch in _VOWELS for ch in w):
        return True
    if re.search(r"[^aeiouyàáâäèéêëìíîïòóôöùúûü]{5}", w):
        return True
    # maiuscola dopo una minuscola ("vrWwr"), ammessi i casi tipo "McDonald"
    return bool(re.search(r"[a-zà-ÿ][A-ZÀ-Ö].*[A-ZÀ-Ö]", word))


def text_quality(txt):
    """Misure del testo di una pagina. Vedi le soglie qui sopra."""
    s = _REPEAT.sub(r"\1", "".join(c for c in (txt or "") if not c.isspace()))
    n = len(s)
    bad = sum(1 for c in s if _is_bad(c))
    alnum = sum(1 for c in s if c.isalnum())
    words = _LATIN_WORD.findall(txt or "")
    long_words = [w for w in words if len(w) >= 2]
    stop = sum(1 for w in long_words if w.lower() in STOPWORDS)
    weird = sum(1 for w in long_words if _weird(w))
    k = len(long_words)
    letters = [c for w in long_words for c in w.lower()]
    vowels = sum(1 for c in letters if c in _VOWELS and c != "y")
    return {
        "vowel_ratio": vowels / len(letters) if letters else 0.0,
        "chars": n,
        "bad_ratio": bad / n if n else 0.0,
        "alnum_ratio": alnum / n if n else 0.0,
        "words": k,
        "stop_ratio": stop / k if k else 0.0,
        "weird_ratio": weird / k if k else 0.0,
    }


def is_unreadable(txt):
    """True se il testo estratto NON e' testo: glifi senza mappa, codifica
    spostata, sequenze di simboli. In dubbio si preferisce dire "illeggibile":
    l'OCR in piu' costa qualche secondo, un falso "leggibile" lascia le PII
    nell'immagine."""
    q = text_quality(txt)
    if q["chars"] == 0:
        return False
    if q["bad_ratio"] > BAD_MAX or q["alnum_ratio"] < ALNUM_MIN:
        return True
    if q["words"] >= WORDS_MIN and (q["vowel_ratio"] < VOWEL_MIN
                                    or q["stop_ratio"] < STOP_MIN
                                    or q["weird_ratio"] > WEIRD_MAX):
        return True
    return False


def _invisible_share(page):
    """Quota di caratteri con render mode 3 (invisibili). 0 se non si sa."""
    try:
        spans = page.get_texttrace()
    except Exception:
        return 0.0
    tot = inv = 0
    for sp in spans or ():
        k = len(sp.get("chars") or ())
        tot += k
        if sp.get("type") == 3:
            inv += k
    return inv / tot if tot else 0.0


# motivi per cui una pagina va letta con l'OCR
EMPTY, UNREADABLE, INVISIBLE = "vuota", "illeggibile", "invisibile"
# quelli in cui, SENZA OCR, le PII della pagina restano sicuramente in chiaro:
# per "invisibile" il layer dello scanner e' leggibile e la redazione normale
# (che sbianca anche i pixel dell'immagine sotto il testo) lavora gia'.
NEEDS_WARNING = (EMPTY, UNREADABLE)


def plan_pages(doc, min_chars=MIN_TEXT_CHARS):
    """{indice di pagina da 0: motivo} delle pagine da leggere con l'OCR.

    - "vuota":       immagini e meno di `min_chars` caratteri (scansione pura);
    - "illeggibile": testo che non supera il controllo di qualita' (anche senza
                     immagini: un PDF digitale con i font rovinati si vede bene ma
                     si estrae male, e l'OCR legge quello che si vede);
    - "invisibile":  immagini e testo quasi tutto invisibile (scansione con l'OCR
                     dello scanner sotto).
    Le pagine bianche (niente testo, niente immagini) non contano."""
    plan = {}
    for i, page in enumerate(doc):
        try:
            txt = page.get_text() or ""
        except Exception:
            txt = ""
        try:
            has_images = bool(page.get_images(full=False))
        except Exception:
            has_images = False
        if sum(1 for c in txt if not c.isspace()) < min_chars:
            if has_images:
                plan[i] = EMPTY
            continue
        if is_unreadable(txt):
            plan[i] = UNREADABLE
        elif has_images and _invisible_share(page) >= INVISIBLE_MIN:
            plan[i] = INVISIBLE
    return plan


# --------------------------------------------------------------------------- #
# Risultato dell'OCR nelle coordinate della pagina
# --------------------------------------------------------------------------- #
@dataclass
class OcrWord:
    text: str
    box: tuple          # (x0, y0, x1, y1) nelle coordinate NON ruotate della pagina


@dataclass
class OcrPage:
    lines: list = field(default_factory=list)     # [[OcrWord, ...], ...]

    @property
    def text(self):
        """Il testo che va all'analisi: parole separate da uno spazio, righe da
        un a-capo. `char_index` lo ricostruisce identico, carattere per carattere."""
        return self.char_index()[0]

    def char_index(self):
        """(testo, [riquadro per carattere]) come pdf_export._page_char_index:
        ogni carattere di una parola ha il riquadro della parola, spazi e a-capo
        None. Granularita' a parola: una PII dentro una parola copre la parola."""
        chars, boxes = [], []
        for line in self.lines:
            words = [w for w in line if w.text]
            if not words:
                continue
            for k, w in enumerate(words):
                if k:
                    chars.append(" ")
                    boxes.append(None)
                chars.extend(w.text)
                boxes.extend([w.box] * len(w.text))
            chars.append("\n")
            boxes.append(None)
        return "".join(chars), boxes


def _transform(pt, m):
    """Punto per matrice PyMuPDF (a, b, c, d, e, f): come fitz.Point * Matrix."""
    a, b, c, d, e, f = m
    x, y = pt
    return a * x + c * y + e, b * x + d * y + f


def parse_result(raw, page_rect, derotation=(1, 0, 0, 1, 0, 0)):
    """JSON del motore -> OcrPage nelle coordinate della pagina.

    raw: {"lines": [{"text", "box", "words": [{"text", "box"}]}]} con box
    = [x, y, larghezza, altezza] normalizzati 0..1, origine in ALTO a sinistra,
    riferiti all'immagine renderizzata, cioe' a `page_rect` (la pagina come si
    vede, rotazione compresa). `derotation` (page.derotation_matrix) li riporta
    nelle coordinate non ruotate, le stesse di get_text("rawdict") e
    add_redact_annot."""
    x0, y0, x1, y1 = page_rect
    W, H = x1 - x0, y1 - y0
    m = tuple(derotation)

    def to_page(box):
        bx, by, bw, bh = (float(v) for v in box)
        pts = [(x0 + bx * W, y0 + by * H), (x0 + (bx + bw) * W, y0 + by * H),
               (x0 + bx * W, y0 + (by + bh) * H), (x0 + (bx + bw) * W, y0 + (by + bh) * H)]
        pts = [_transform(p, m) for p in pts]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    lines = []
    for ln in (raw or {}).get("lines") or ():
        words = []
        for w in ln.get("words") or ():
            t = (w.get("text") or "").strip()
            box = w.get("box") or ln.get("box")
            if t and box and len(box) == 4:
                words.append(OcrWord(t, to_page(box)))
        if not words and (ln.get("text") or "").strip() and ln.get("box"):
            # motore senza riquadri per parola: la riga intera, parola per parola
            lb = to_page(ln["box"])
            words = [OcrWord(t, lb) for t in ln["text"].split()]
        if words:
            lines.append(words)
    return OcrPage(lines)


# --------------------------------------------------------------------------- #
# Motore macOS: eseguibile Swift con il framework Vision
# --------------------------------------------------------------------------- #
HELPER_NAME = "rizzo-ocr"
OCR_DPI = 300          # una scansione A4 a 300 dpi: corpo 8 ancora leggibile
OCR_TIMEOUT = 120      # secondi per pagina
OCR_WORKERS = 4        # pagine in parallelo (Vision usa Neural Engine/GPU)


class OcrError(RuntimeError):
    pass


def helper_path():
    """Dove sta `rizzo-ocr`: PII_OCR_HELPER, dentro il bundle PyInstaller
    (_internal/ocr/), oppure build/ocr/ della repo (sviluppo). None se manca."""
    cands = []
    env = os.environ.get("PII_OCR_HELPER")
    if env:
        cands.append(env)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "ocr", HELPER_NAME))
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "..", "..", "build", "ocr", HELPER_NAME))
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return None


class MacVisionEngine:
    """PNG -> dict. Un processo per pagina: niente stato condiviso, un crash
    di Vision su una pagina non porta giu' il backend."""

    def __init__(self, path, timeout=OCR_TIMEOUT):
        self.path, self.timeout = path, timeout

    def __call__(self, png):
        try:
            r = subprocess.run([self.path], input=png, capture_output=True,
                               timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            raise OcrError("OCR: tempo scaduto")
        except OSError as e:
            raise OcrError(f"OCR non avviabile: {e}")
        if r.returncode != 0:
            msg = (r.stderr or b"").decode("utf-8", "replace").strip()
            raise OcrError(f"OCR fallito ({r.returncode}): {msg[:200]}")
        try:
            return json.loads(r.stdout.decode("utf-8"))
        except ValueError:
            raise OcrError("OCR: risposta non valida")


def ocr_enabled():
    return os.environ.get("PII_OCR", "1").strip().lower() not in ("0", "false", "no", "off")


def default_engine():
    """Il motore della piattaforma, o None (Windows/Linux, helper mancante,
    PII_OCR=0). None non e' un errore: vuol dire "niente OCR"."""
    if sys.platform != "darwin" or not ocr_enabled():
        return None
    p = helper_path()
    return MacVisionEngine(p) if p else None


@dataclass
class OcrResult:
    plan: dict = field(default_factory=dict)       # {indice: motivo}
    pages: dict = field(default_factory=dict)      # {indice: OcrPage}
    failed: dict = field(default_factory=dict)     # {indice: messaggio}

    def warn_pages(self):
        """Pagine (da 1) le cui PII restano in chiaro perche' l'OCR non c'e' o e'
        fallito: e' il vecchio pages_no_text, con in piu' il testo illeggibile."""
        return sorted(i + 1 for i, why in self.plan.items()
                      if why in NEEDS_WARNING and i not in self.pages)

    def ocr_pages_1(self):
        return sorted(i + 1 for i in self.pages)

    def texts(self):
        return {i: p.text for i, p in self.pages.items()}


def _render(page, dpi):
    return page.get_pixmap(dpi=dpi).tobytes("png")


def ocr_pages(doc, indices, engine, dpi=None, workers=None):
    """Legge con `engine` le pagine `indices` (da 0) di `doc` (aperto con
    PyMuPDF, o un finto con load_page/get_pixmap/rect/derotation_matrix).
    Ritorna ({indice: OcrPage}, {indice: errore}). Il render e' sequenziale
    (un documento PyMuPDF non va usato da piu' thread), l'OCR a gruppi in
    parallelo: in memoria ci sono al massimo `workers` immagini alla volta."""
    dpi = dpi or OCR_DPI
    workers = workers or OCR_WORKERS
    pages, failed = {}, {}
    idx = sorted(set(indices))
    if engine is None:
        return pages, {i: "OCR non disponibile" for i in idx}
    if not idx:
        return pages, failed
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for start in range(0, len(idx), max(1, workers)):
            batch = []
            for i in idx[start:start + max(1, workers)]:
                try:
                    page = doc.load_page(i)
                    rect = tuple(page.rect)
                    derot = tuple(page.derotation_matrix)
                    png = _render(page, dpi)
                except Exception as e:
                    failed[i] = f"render: {e}"
                    continue
                batch.append((i, rect, derot, pool.submit(engine, png)))
            for i, rect, derot, fut in batch:
                try:
                    pages[i] = parse_result(fut.result(), rect, derot)
                except Exception as e:
                    failed[i] = str(e)
    return pages, failed


def analyze_doc(doc, engine=None, min_chars=MIN_TEXT_CHARS):
    """Piano + OCR di tutte le pagine del piano. Senza motore: solo il piano."""
    plan = plan_pages(doc, min_chars=min_chars)
    res = OcrResult(plan=plan)
    if engine is not None and plan:
        res.pages, res.failed = ocr_pages(doc, list(plan), engine)
    return res
