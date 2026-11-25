# -*- coding: utf-8 -*-
"""Testo leggibile di un PDF: content stream + annotazioni + campi modulo + TOC.

Come `detectors` e `pdf_export`, questo modulo lavora solo su oggetti duck-typed:
nessun import di torch/transformers/fitz, quindi e' testabile in isolamento.
`app._text_from_bytes` e `pdf_export._readable_text` lo usano sullo stesso
documento aperto da PyMuPDF.

Nei PDF fillable (AcroForm) il valore sta nel widget, non nel content stream:
`page.get_text()` torna vuoto e /analyze rispondeva 400 (issue #85).
"""

# PyMuPDF: fitz.PDF_ANNOT_REDACT. Le annotazioni di redazione non sono testo
# del documento, e in verifica residui andrebbero contate due volte.
REDACT_ANNOT = 12


def collect_readable_text(doc, redact_annot_type=REDACT_ANNOT, page_texts=None):
    """TUTTO il testo leggibile: pagine + annotazioni + campi modulo + segnalibri.

    `doc` e' un iterabile di pagine con `get_text()`, `annots()`, `widgets()`
    e (sul documento) `get_toc(simple=True)`: il contratto di PyMuPDF, senza
    importarlo. Da solo non vede il testo dentro le immagini raster.

    `page_texts` ({indice da 0: testo}) sostituisce il testo di pagina con quello
    letto dall'OCR (ocr.py): sulle pagine scansionate il layer dello scanner e'
    vuoto, invisibile o con caratteri senza senso, e l'analisi deve lavorare su
    cio' che si vede.
    """
    parts = []
    page_texts = page_texts or {}
    for i, page in enumerate(doc):
        if i in page_texts:
            parts.append(page_texts[i] or "")
        else:
            parts.append(page_text(page))
        try:
            for a in page.annots() or ():
                if a.type[0] == redact_annot_type:
                    continue
                info = a.info or {}
                parts.extend(str(info.get(k) or "") for k in ("content", "subject", "title"))
        except Exception:
            pass
        try:
            for w in page.widgets() or ():
                val = w.field_value
                if isinstance(val, str) and val:
                    parts.append(val)
        except Exception:
            pass
    try:
        parts.extend(str(e[1] or "") for e in (doc.get_toc(simple=True) or ()))
    except Exception:
        pass
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Testo con le lettere SPAZIATE ("S T U D I O  L E G A L E", "A V V . M A R I O ...")
#
# Le intestazioni degli studi legali sono spesso scritte con la spaziatura tra i
# caratteri (Tc). PyMuPDF la restituisce come un carattere di spazio fra ogni lettera
# e perde i confini di parola: "AVV. MARIO ROSSI" diventa "A V V . M A R I O R O S S I".
# Il modello non riconosce un nome fatto di lettere isolate, quindi il nome
# dell'avvocato restava in chiaro. Qui si ricostruiscono le parole dalla GEOMETRIA: tra
# due lettere della stessa parola lo scarto e' quello della spaziatura, tra due parole
# e' circa il doppio (spaziatura + spazio vero + spaziatura).
# La stessa ricostruzione serve all'analisi (page_text) e alla redazione
# (pdf_export._page_char_index): cosi' il valore trovato dal modello combacia con i
# caratteri della pagina.
# --------------------------------------------------------------------------- #
LETTERSPACED_MIN_RUN = 4        # lettere isolate di fila per parlare di spaziatura
_WORD_GAP_FACTOR = 1.5          # scarto > 1,5 x quello mediano = confine di parola


def looks_letterspaced(text):
    """Prefiltro economico sul testo di get_text(): quattro o piu' lettere isolate
    di fila, separate da un solo spazio."""
    import re
    return re.search(r"(?<!\S)(?:[^\W\d_] ){%d,}[^\W\d_](?!\S)" % (LETTERSPACED_MIN_RUN - 1),
                     text) is not None


def despace_line(items):
    """items = [(carattere, (x0, y0, x1, y1) o None)] di UNA riga.
    Ritorna la stessa forma con le lettere spaziate riunite in parole: via gli spazi
    sintetici fra le lettere, un solo spazio (senza riquadro) fra le parole.
    Le righe senza almeno LETTERSPACED_MIN_RUN lettere isolate restano come sono."""
    # token = gruppi di caratteri separati da spazi
    toks, cur = [], []
    for it in items:
        if it[0].isspace():
            if cur:
                toks.append(cur)
                cur = []
        else:
            cur.append(it)
    if cur:
        toks.append(cur)
    if len(toks) < LETTERSPACED_MIN_RUN:
        return items

    def single_letter(t):
        return len(t) == 1 and t[0][0].isalpha() or (len(t) == 1 and t[0][0] in ".,'")

    # le sequenze di token a lettera singola (con almeno una lettera vera ogni due)
    out, i, n = [], 0, len(toks)
    changed = False
    while i < n:
        if not single_letter(toks[i]):
            out.append(toks[i])
            i += 1
            continue
        j = i
        while j < n and single_letter(toks[j]):
            j += 1
        run = toks[i:j]
        letters = sum(1 for t in run if t[0][0].isalpha())
        boxes = [t[0][1] for t in run]
        # la sequenza deve essere (quasi) tutta la riga: "e a b c d" dentro una frase
        # normale e' un elenco, non una spaziatura. Il solo scarto fra le lettere non
        # distingue (la spaziatura e' spesso simile a uno spazio vero).
        if (len(run) < LETTERSPACED_MIN_RUN + 1 or letters * 10 < len(run) * 7
                or len(run) * 10 < len(toks) * 8
                or any(b is None for b in boxes)):
            out.extend(run)
            i = j
            continue
        gaps = [boxes[k + 1][0] - boxes[k][2] for k in range(len(run) - 1)]
        pos = sorted(g for g in gaps if g > 0.05)
        if not pos:
            out.extend(run)
            i = j
            continue
        med = pos[len(pos) // 2]
        word = []
        for k, t in enumerate(run):
            word.append(t[0])
            if k < len(gaps) and gaps[k] > _WORD_GAP_FACTOR * med:
                out.append(word)
                word = []
        if word:
            out.append(word)
        changed = True
        i = j
    if not changed:
        return items
    res = []
    for k, tok in enumerate(out):
        if k:
            res.append((" ", None))
        res.extend(tok)
    return res


def raw_lines(page):
    """Le righe del layer testuale come liste di (carattere, bbox), dal rawdict di
    PyMuPDF, con le lettere spaziate gia' riunite. Una lista per riga, nell'ordine
    del documento. Ritorna None se la pagina non offre il rawdict."""
    try:
        raw = page.get_text("rawdict")
    except Exception:
        return None
    lines = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            items = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    for cc in (ch.get("c") or ""):      # ligature -> piu' caratteri
                        items.append((cc, tuple(ch["bbox"])))
            lines.append(despace_line(items))
    return lines


def page_text(page):
    """Testo della pagina per l'ANALISI. Come get_text(), ma con le lettere spaziate
    riunite in parole. Solo se il testo ne ha l'aria si passa dal rawdict: il caso
    normale resta get_text() identico a prima."""
    txt = page.get_text() or ""
    if not looks_letterspaced(txt):
        return txt
    lines = raw_lines(page)
    if not lines:
        return txt
    return "\n".join("".join(c for c, _ in ln) for ln in lines) + "\n"


# Sotto questa soglia (spazi esclusi) il layer testuale di una pagina e' "quasi
# vuoto". Valore ripreso dalla PR upstream #105 (OCR_MIN_CHARS), che con questa
# stessa soglia decideva quali pagine mandare all'OCR.
MIN_TEXT_CHARS = 40


def pages_without_text(doc, min_chars=MIN_TEXT_CHARS):
    """Numeri di pagina (da 1) che hanno IMMAGINI ma quasi nessun testo estraibile:
    tipicamente pagine scansionate dentro un PDF misto. L'anonimizzazione lavora sul
    layer testuale, quindi le PII dentro quelle immagini restano visibili anche nel
    PDF redatto: l'UI lo deve dire. Una pagina bianca (niente testo, niente
    immagini) non conta: non c'e' niente da nascondere."""
    out = []
    for i, page in enumerate(doc, start=1):
        try:
            txt = page.get_text() or ""
        except Exception:
            txt = ""
        if sum(1 for c in txt if not c.isspace()) >= min_chars:
            continue
        try:
            has_images = bool(page.get_images(full=False))
        except Exception:
            has_images = False
        if has_images:
            out.append(i)
    return out
