# -*- coding: utf-8 -*-
"""
Generazione del PDF anonimizzato (issue #7, punto 1).

Due modalita', entrambe 100% in locale e senza dipendenze dal modello: il modulo
lavora solo su bytes + dizionario {placeholder -> valore}, quindi e' testabile in
isolamento (nessun import di torch/transformers).

  redact_pdf(pdf_bytes, mapping)
      Redazione VERA del PDF originale: per ogni valore del dizionario cerca le
      occorrenze nelle pagine, RIMUOVE il testo dal content stream
      (page.apply_redactions(), non un rettangolo disegnato sopra) e scrive il
      placeholder al suo posto. Layout conservato.

      Il matching e' CHAR-PRECISO con CONFINI DI PAROLA: la pagina viene
      indicizzata carattere per carattere (get_text("rawdict"), ogni glifo con il
      suo bbox) e i valori sono cercati con regex ancorate (?<!\\w)...(?!\\w).
      Cosi' "DE" NON viene redatto dentro "CORDELLA" e una cifra non cancella i
      numeri di un referto: si redige il token/la sequenza esatta, ovunque ma
      intera. Il pattern tollera la SILLABAZIONE a fine riga ("Fran-\\ncesco") e
      gli spazi flessibili tra i token (anche a cavallo di riga).

  text_to_pdf(text)
      PDF "ricostruito" solo testo, impaginato da zero a partire dal testo gia'
      anonimizzato (input incollato oppure file .md/.txt).

Cosa viene ripulito oltre al testo di pagina (tutti posti dove si nasconde PII e
che apply_redactions() da solo NON tocca):
  - metadati classici + XMP;
  - contenuto delle ANNOTAZIONI (commenti, FreeText, note);
  - valore dei CAMPI MODULO (widget AcroForm);
  - titoli dei SEGNALIBRI (outline/TOC);
  - FIRME che non sono testo (immagini, tratti a mano libera, timbri, campi /Sig);
  - ALLEGATI incorporati (embedded files), rimossi in blocco.

Rete di sicurezza finale: `report["residual"]` elenca i placeholder il cui valore
e' ANCORA leggibile nell'output (testo di pagina + annotazioni + widget + TOC).
Deve essere vuota; l'UI avvisa se non lo e'.

Pagine scansionate (`ocr_pages`, solo macOS, vedi ocr.py): i valori si cercano
anche nel testo letto dall'OCR e si coprono i riquadri delle parole; i pixel
dell'immagine sotto vengono sbiancati da apply_redactions(). Se la pagina ha
ancora testo VISIBILE (illeggibile, per questo e' andata all'OCR) prima diventa
un'immagine: vedi _flatten_to_image. Poi le pagine si RILEGGONO con l'OCR
(`ocr_verify`): un valore ancora leggibile e' un residuo.

Limiti noti (= punti 2-4 della issue #7):
  - testo dentro immagini raster SENZA OCR (Windows/Linux, o OCR fallito): non
    esiste nel layer testuale, quindi non puo' essere trovato ne' redatto. Se in
    tutto il PDF non si trova NESSUNA occorrenza, il chiamante deve rifiutare:
    meglio un errore che un PDF "anonimizzato" che non lo e';
  - valori troppo corti/ambigui (< 2 caratteri alfanumerici, o 2 sole cifre) NON
    vengono redatti: cercarli ovunque devasterebbe il documento. Finiscono in
    `report["skipped"]` e l'UI DEVE avvisare, perche' restano in chiaro.
"""

import re
import unicodedata

import pymupdf as fitz  # PyMuPDF (il nome "fitz" e' deprecato)
import pdf_text


class PdfError(ValueError):
    """Errore d'uso (PDF non valido, protetto, dizionario vuoto...)."""


# --------------------------------------------------------------------------- #
# Utility comuni
# --------------------------------------------------------------------------- #
# caratteri tipografici frequenti nei PDF estratti -> equivalenti Latin-1
# (il font base "helv" copre Latin-1: cosi' la sostituzione e' deterministica)
_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "€": "EUR",
    " ": " ", "•": "-", "ﬁ": "fi", "ﬂ": "fl",
})


def _norm(s):
    """Spazi compressi + casefold: stessa normalizzazione usata in app.py."""
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _fit_fontsize(text, rect, max_fs=10.0, min_fs=4.0):
    """Corpo del testo per far stare `text` dentro `rect` (0 = non ci sta:
    meglio nessuna etichetta che un'etichetta illeggibile o troncata)."""
    try:
        w10 = fitz.get_text_length(text, fontname="helv", fontsize=10.0)
    except Exception:
        return 0
    if w10 <= 0:
        return 0
    # la casella colorata col segnaposto sta dentro `rect` di REDACT_STROKE_HALF
    h = 2 * REDACT_STROKE_HALF
    fs = min(max_fs, max(rect.height - h, 0.0) * 0.82,
             10.0 * max(rect.width - h - 2.0, 0.0) / w10)
    return round(fs, 1) if fs >= min_fs else 0


def _covered(rect, taken, thr=0.85):
    """True se `rect` e' gia' (quasi) tutto dentro una redazione precedente:
    evita doppioni quando un valore e' contenuto in un altro (es. "Rossi"
    dentro "Mario Rossi", redatto prima perche' piu' lungo)."""
    area = rect.get_area()
    if area <= 0:
        return True
    for t in taken:
        inter = fitz.Rect(rect)
        inter.intersect(t)
        if not inter.is_empty and inter.get_area() / area >= thr:
            return True
    return False


# --------------------------------------------------------------------------- #
# Indice char-preciso della pagina + ricerca con confini di parola
# --------------------------------------------------------------------------- #
def _page_char_index(page):
    """(testo, [bbox per carattere]) dalla pagina: ogni carattere del layer
    testuale con il suo rettangolo (None per i newline di fine riga e per gli
    spazi ricostruiti). Le lettere spaziate sono riunite in parole, come nel testo
    che ha visto il modello (pdf_text.raw_lines)."""
    chars, boxes = [], []
    for line in pdf_text.raw_lines(page) or []:
        for c, bb in line:
            chars.append(c)
            boxes.append(fitz.Rect(bb) if bb is not None else None)
        chars.append("\n")
        boxes.append(None)
    return "".join(chars), boxes


# Sillabazione a fine riga: "Fran-\ncesco", "Fran­\ncesco" o anche solo un
# a-capo dentro la parola. Ammesso TRA due caratteri qualsiasi del valore, ma solo
# se c'e' davvero un newline: senza, il gruppo non consuma nulla e il matching
# resta char-per-char (niente tolleranza allo spacing, che aprirebbe a
# sovra-redazioni tipo "MI" dentro "M I L A N O").
_HYPHEN_BREAK = r"(?:[­‐-]?[ \t]*\n[ \t]*)?"


# Sotto questa soglia di caratteri alfanumerici un frammento NON puo' rinunciare
# ai confini di parola: ".it" senza ancore matcherebbe "it" dentro ogni parola.
_UNANCHORED_MIN_ALNUM = 4


def _value_pattern(value):
    """Regex del valore: caratteri esatti con CONFINI DI PAROLA agli estremi,
    sillabazione a fine riga tollerata, whitespace flessibile tra i token.
    Niente match di sottostringhe dentro altre parole.

    Eccezione: quando il valore ha punteggiatura ai bordi il modello ha tagliato
    a meta' una parola spezzata a fine riga (tipico: "Fran-\\ncesco Cordella" ->
    il modello etichetta "-\\ncesco Cordella"). Li' il confine di parola su quel
    lato e' proprio cio' che impedisce di trovare il valore, che resterebbe in
    chiaro: si toglie la punteggiatura e con essa l'ancora, ma solo se quel che
    resta e' abbastanza lungo da non trasformarsi in una sottostringa qualunque."""
    v = _norm(value)
    core = re.sub(r"^[^\w]+", "", v)
    core = re.sub(r"[^\w]+$", "", core)
    toks = [t for t in core.split(" ") if t]
    if not toks:
        return None
    if len(re.sub(r"[\W_]+", "", core)) >= _UNANCHORED_MIN_ALNUM:
        left = "" if re.match(r"^[^\w]", v) else r"(?<!\w)"
        right = "" if re.search(r"[^\w]$", v) else r"(?!\w)"
    else:                                  # troppo corto: ancore obbligatorie
        toks = [t for t in v.split(" ") if t]
        left, right = r"(?<!\w)", r"(?!\w)"
    body = r"\s*".join(_HYPHEN_BREAK.join(re.escape(c) for c in tok) for tok in toks)
    return re.compile(left + body + right, re.IGNORECASE)


def _match_rects(boxes, m):
    """Bbox dei caratteri del match, uniti per riga (overlap verticale)."""
    rects, cur = [], None
    for i in range(m.start(), m.end()):
        b = boxes[i]
        if b is None or b.is_empty:
            continue
        if cur is None:
            cur = fitz.Rect(b)
        elif b.y0 < cur.y1 and b.y1 > cur.y0:      # stessa riga
            cur |= b
        else:                                       # riga nuova
            rects.append(cur)
            cur = fitz.Rect(b)
    if cur is not None and not cur.is_empty:
        rects.append(cur)
    return rects


def _too_noisy(value):
    """Valori non localizzabili in modo sicuro in un PDF: frammenti con meno di 2
    caratteri alfanumerici, o di 2 sole cifre (es. "1", "C", "05", prodotti a
    volte dal modello su testi tabellari). Cercarli ovunque cancellerebbe pezzi di
    documento non-PII: si saltano, MA il chiamante deve avvisare l'utente (restano
    in chiaro)."""
    alnum = re.sub(r"[\W_]+", "", _norm(value))
    return len(alnum) < 2 or (len(alnum) == 2 and alnum.isdigit())


# --------------------------------------------------------------------------- #
# Pulizia di tutto cio' che apply_redactions() non tocca
# --------------------------------------------------------------------------- #
def _scrub_metadata(doc):
    """Azzera i metadati classici e l'XMP: possono contenere PII (autore...)."""
    try:
        # set_metadata aggiorna SOLO le chiavi passate: vanno azzerate tutte
        # esplicitamente, altrimenti autore/titolo originali restano nel file
        doc.set_metadata({
            "title": "", "author": "", "subject": "", "keywords": "",
            "creationDate": "", "modDate": "", "trapped": "",
            "creator": "rizzo-pii", "producer": "rizzo-pii",
        })
    except Exception:
        pass
    f = getattr(doc, "del_xml_metadata", None) or getattr(doc, "delXmlMetadata", None)
    if f:
        try:
            f()
        except Exception:
            pass


def _sub_all(patterns, s):
    """Applica tutte le (regex, placeholder) alla stringa. Ritorna (nuova, n)."""
    n = 0
    for pat, ph in patterns:
        s, k = pat.subn(ph, s)
        n += k
    return s, n


def _scrub_annots(page, patterns):
    """Contenuto e titolo delle annotazioni (commenti, FreeText, note adesive):
    testo vero e proprio, invisibile a get_text() e non toccato dalle redazioni."""
    done = 0
    try:
        annots = list(page.annots())
    except Exception:
        return 0
    for a in annots:
        try:
            if a.type[0] == fitz.PDF_ANNOT_REDACT:      # le nostre, gia' gestite
                continue
            info = a.info
            new = dict(info)
            hit = 0
            for k in ("content", "subject", "title"):
                if info.get(k):
                    new[k], k_hit = _sub_all(patterns, info[k])
                    hit += k_hit
            if hit:
                a.set_info(new)
                a.update()
                done += hit
        except Exception:
            continue
    return done


def _scrub_widgets(page, patterns):
    """Valore dei campi modulo AcroForm: sopravvive a qualsiasi redazione del
    content stream ed e' leggibile aprendo il PDF."""
    done = 0
    try:
        widgets = list(page.widgets())
    except Exception:
        return 0
    for w in widgets:
        try:
            val = w.field_value
            if not isinstance(val, str) or not val:
                continue
            new, hit = _sub_all(patterns, val)
            if hit:
                w.field_value = new
                w.update()
                done += hit
        except Exception:
            continue
    return done


def _scrub_toc(doc, patterns):
    """Titoli dei segnalibri: spesso ricalcano intestazioni con nomi e numeri."""
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        return 0
    if not toc:
        return 0
    done, new_toc = 0, []
    for entry in toc:
        lvl, title, pg = entry[0], entry[1], entry[2]
        title, hit = _sub_all(patterns, title or "")
        done += hit
        new_toc.append([lvl, title, pg])
    if done:
        try:
            doc.set_toc(new_toc)
        except Exception:
            return 0
    return done


# --------------------------------------------------------------------------- #
# FIRME: tutto cio' che firma ma non e' testo
#
# Il nome sotto "F.to" lo trova il modello come ogni altro nome; la FIRMA vera no:
# e' un'immagine raster (scansione ritagliata, "Fill & Sign"), un tratto a mano
# libera (annotazione Ink), un timbro (Stamp), un campo firma digitale (/Sig: nome del
# firmatario nell'aspetto, nel dizionario /V e nel certificato) o un tracciato
# vettoriale di curve. Nessuno e' nel layer testuale, quindi `apply_redactions()` su
# un valore non li tocca, e `_verify_residuals` non puo' vederli.
#
# Per questo si rimuovono PER FORMA, non per valore:
#   - campi di firma, annotazioni Ink/Stamp: via sempre (+ casella "[FIRMA]");
#   - immagini piccole e curve a mano libera: solo se stanno vicino a una dicitura
#     BREVE di firma ("Firma:", "F.to", "Il Venditore"...) o, per le immagini, sopra un
#     nome gia' redatto. Senza questo vincolo sparirebbero i loghi o i pulsanti di un
#     modulo accanto a "Il sottoscritto Mario Rossi dichiara che...";
#   - i dati della firma DIGITALE (/V col nome, /DSS, /Perms) a livello di documento:
#     vedi _scrub_signature_data.
# Due fasi per pagina: i tracciati vettoriali escono solo con graphics=TOUCHED, che e'
# un parametro dell'INTERA pagina e si porterebbe via i bordi di tabella toccati dalle
# caselle dei nomi: si applicano da soli, per primi (`_redact_signature_shapes`). Le
# immagini vengono dopo i nomi (`_redact_signature_images`), cosi' "sopra un nome
# redatto" ha un significato.
# Limite: una firma dentro una pagina SCANSIONATA e' un pixel fra gli altri (l'OCR
# non legge la calligrafia): non c'e' modo affidabile di distinguerla; l'UI lo dice.
# --------------------------------------------------------------------------- #
SIG_LABEL = "[FIRMA]"
_SIG_CUES = re.compile(
    r"\b(?:firma\w*|f[./]\s?to|sottoscritt\w*|in\s+fede|timbro|signature|signed|"
    r"sign\s+here|il\s+dichiarante|il\s+venditore|il\s+compratore|il\s+locatore|"
    r"il\s+conduttore|il\s+committente|l['\u2019]appaltatore|il\s+cedente|"
    r"il\s+cessionario|il\s+mandante|il\s+mandatario|il\s+notaio|per\s+accettazione|"
    r"letto\s+confermato)\b", re.I)
SIG_CUE_MAX_CHARS = 40     # una dicitura di firma e' una riga corta, non prosa
SIG_NEAR_X = 40.0          # pt: quanto una firma puo' stare lontana dalla dicitura
SIG_NEAR_Y = 70.0
SIG_MAX_AREA = 0.15        # frazione di pagina: oltre e' una scansione/sfondo, non una firma
SIG_MAX_W = 0.5
SIG_MAX_H = 0.25
SIG_MIN_CURVES = 5         # un cerchio (pulsante radio) ne ha 4


def _cue_rects(page):
    """Riquadri delle righe BREVI che contengono una dicitura di firma."""
    out = []
    try:
        blocks = page.get_text("dict")["blocks"]
    except Exception:
        return out
    for b in blocks:
        for ln in b.get("lines", []):
            txt = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
            if len(txt) <= SIG_CUE_MAX_CHARS and _SIG_CUES.search(txt):
                out.append(fitz.Rect(ln["bbox"]))
    return out


def _near(rect, cues, taken=()):
    r = fitz.Rect(rect)
    grown = fitz.Rect(r.x0 - SIG_NEAR_X, r.y0 - SIG_NEAR_Y, r.x1 + SIG_NEAR_X, r.y1 + SIG_NEAR_Y)
    return (any(grown.intersects(c) for c in cues)
            or any(r.intersects(t) for t in taken))


def _sig_box(page, rect, taken, fill, text_color, labels=True):
    r = fitz.Rect(rect) + (-1, -1, 1, 1)
    r &= page.rect
    if r.is_empty or r.width < 2 or r.height < 2:
        return 0
    fs = _fit_fontsize(SIG_LABEL, r) if labels else 0
    _add_redact_annot(page, r, SIG_LABEL if fs else None, fs or 6, fill, text_color)
    taken.append(fitz.Rect(r))
    return 1


def _redact_signature_shapes(page, taken, fill, text_color, labels=True):
    """Fase 1 (prima dei nomi): campi di firma, Ink, Stamp, tracciati a mano libera.
    Applica subito le redazioni; ritorna quante firme ha tolto."""
    n = 0
    pr = page.rect
    # 1) campi firma digitale: si cancellano (la firma e' comunque invalidata da
    #    qualunque modifica) e con loro l'aspetto che mostra il nome del firmatario
    try:
        widgets = [w for w in page.widgets()
                   if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE]
    except Exception:
        widgets = []
    for w in widgets:
        rect = fitz.Rect(w.rect)
        try:
            page.delete_widget(w)
        except Exception:
            continue
        n += _sig_box(page, rect, taken, fill, text_color, labels)
    # 2) tratti a mano libera e timbri
    try:
        annots = [a for a in page.annots()
                  if a.type[0] in (fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_STAMP)]
    except Exception:
        annots = []
    for a in annots:
        rect = fitz.Rect(a.rect)
        try:
            page.delete_annot(a)
        except Exception:
            continue
        n += _sig_box(page, rect, taken, fill, text_color, labels)
    # 3) tracciati vettoriali a mano libera: molte curve, quasi nessun segmento dritto,
    #    non un'ellisse chiusa (pulsante radio, timbro tondo vuoto)
    cues = _cue_rects(page)
    vector = False
    try:
        drawings = page.get_drawings() if cues else []
    except Exception:
        drawings = []
    for d in drawings:
        items = d.get("items", [])
        curves = sum(1 for it in items if it[0] == "c")
        lines = sum(1 for it in items if it[0] in ("l", "re", "qu"))
        r = fitz.Rect(d["rect"])
        if (curves < SIG_MIN_CURVES or lines * 2 > curves or r.is_empty
                or r.width > SIG_MAX_W * pr.width or r.height > SIG_MAX_H * pr.height):
            continue
        if d.get("closePath") and r.height and 0.6 <= r.width / r.height <= 1.7:
            continue
        if _near(r, cues):
            n += _sig_box(page, r, taken, fill, text_color, labels)
            vector = True
    if n:
        # un tracciato non viene rimosso da graphics="se coperto" (le curve escono dal
        # riquadro coi punti di controllo): serve "se toccato". In questo momento le sole
        # caselle sulla pagina sono quelle delle firme: i nomi si redigono dopo.
        page.apply_redactions(
            graphics=(getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED", 2)
                      if vector else 1))
    return n


def _redact_signature_images(page, taken, fill, text_color, labels=True):
    """Fase 2 (dopo i nomi, prima di apply_redactions): immagini piccole vicino a una
    dicitura di firma o sopra un nome appena redatto. Le caselle le applica il chiamante."""
    n = 0
    pr = page.rect
    page_area = max(pr.width * pr.height, 1.0)
    cues = _cue_rects(page)
    try:
        images = page.get_image_info()
    except Exception:
        images = []
    for im in images:
        r = fitz.Rect(im["bbox"])
        if (r.is_empty or r.width * r.height > SIG_MAX_AREA * page_area
                or r.width > SIG_MAX_W * pr.width or r.height > SIG_MAX_H * pr.height):
            continue
        if _near(r, cues, taken):
            n += _sig_box(page, r, taken, fill, text_color, labels)
    return n


def _scrub_signature_data(doc):
    """Dati della firma DIGITALE che sopravvivono alla cancellazione del widget: il
    dizionario /V (nome, motivo, luogo, contatto, certificato in /Contents, spesso col
    codice fiscale del firmatario), il campo padre che lo regge, /DSS (certificati e
    OCSP) e /Perms. Ritorna quanti dizionari di firma ha svuotato."""
    done = 0
    try:
        cat = doc.pdf_catalog()
        for k in ("DSS", "Perms"):
            if doc.xref_get_key(cat, k)[0] != "null":
                doc.xref_set_key(cat, k, "null")
    except Exception:
        pass
    try:
        n_x = doc.xref_length()
    except Exception:
        return 0
    for x in range(1, n_x):
        try:
            typ = doc.xref_get_key(x, "Type")[1]
            if typ in ("/XRef", "/ObjStm"):
                continue
            ft = doc.xref_get_key(x, "FT")[1]
            if ft == "/Sig":                       # campo (padre o widget): via il valore
                for k in ("V", "Lock", "SV"):
                    if doc.xref_get_key(x, k)[0] != "null":
                        doc.xref_set_key(x, k, "null")
            if typ == "/Sig" or doc.xref_get_key(x, "ByteRange")[0] != "null":
                doc.update_object(x, "<<>>")       # il dizionario di firma in se'
                done += 1
        except Exception:
            continue
    return done


def _strip_embedded(doc):
    """Allegati incorporati: non ispezionabili in modo affidabile (possono essere
    di qualunque formato), quindi si rimuovono tutti."""
    removed = 0
    try:
        names = list(doc.embfile_names())
    except Exception:
        return 0
    for name in names:
        try:
            doc.embfile_del(name)
            removed += 1
        except Exception:
            continue
    return removed


def _readable_text(doc):
    """TUTTO il testo leggibile del documento: pagine + annotazioni + campi
    modulo + segnalibri. E' la base della verifica dei residui: se un valore
    compare qui, l'anonimizzazione NON e' completa.
    NB: non vede il testo dentro immagini raster (loghi, firme, scansioni)."""
    return pdf_text.collect_readable_text(doc, redact_annot_type=fitz.PDF_ANNOT_REDACT)


def _verify_residuals(pdf_bytes, items):
    """Placeholder il cui valore e' ANCORA leggibile nell'output. Usa lo STESSO
    pattern della redazione (sillabazione inclusa), altrimenti dichiarerebbe
    "0 residui" proprio nei casi che il matcher non sa gestire."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        text = _readable_text(doc)
    residual = []
    for ph, val in items:
        pat = _value_pattern(val)
        if pat and pat.search(text):
            residual.append(ph)
    return residual


def _verify_ocr(out_bytes, items, indices, ocr_verify):
    """Rilegge con l'OCR le pagine scansionate del PDF IN USCITA: un valore che
    l'OCR legge ancora e' un residuo, esattamente come nel testo. Ritorna
    (placeholder residui, pagine da 1 non rilette)."""
    if ocr_verify is None:
        return [], [i + 1 for i in indices]
    try:
        again = ocr_verify(out_bytes, indices) or {}
    except Exception:
        again = {}
    unverified = [i + 1 for i in indices if i not in again]
    text = "\n".join(again[i].text for i in indices if i in again)
    residual = []
    for ph, val in items:
        pat = _value_pattern(val)
        if pat and pat.search(text):
            residual.append(ph)
    return residual, unverified


# --------------------------------------------------------------------------- #
# API pubblica
# --------------------------------------------------------------------------- #
# Colore della casella di redazione: il viola del brand (#7c3a9e) pieno, con il
# placeholder in bianco sopra. Deve SALTARE ALL'OCCHIO scorrendo il documento —
# una casella tenue si confonde con un'evidenziazione qualsiasi, e chi rilegge il
# PDF prima di mandarlo fuori deve vedere subito cosa e' stato coperto.
REDACT_FILL = (0.486, 0.227, 0.620)
REDACT_TEXT = (1.0, 1.0, 1.0)


def _ocr_char_index(ocr_page):
    """Indice char-preciso di una pagina letta con l'OCR (ocr.OcrPage): stesso
    formato di _page_char_index, riquadri delle parole al posto dei glifi."""
    text, boxes = ocr_page.char_index()
    return text, [fitz.Rect(b) if b is not None else None for b in boxes]


# Margine attorno ai riquadri dell'OCR, in frazione dell'altezza della riga: i
# riquadri di Vision stanno stretti sui glifi, e un pixel di asta o di grazia
# rimasto fuori basta a rileggere una cifra.
OCR_PAD = 0.25


def _pad(rect, k=OCR_PAD):
    dy = max(1.0, rect.height * k)
    dx = max(1.0, rect.height * k * 0.6)
    return fitz.Rect(rect.x0 - dx, rect.y0 - dy, rect.x1 + dx, rect.y1 + dy)


# Bordi delle caselle sulle pagine OCR allineati all'INCHIOSTRO vero: i riquadri
# di Vision sono larghi e il margine OCR_PAD li allarga ancora, quindi la casella
# finiva sopra l'ultima lettera della parola prima ("Signor" -> "Signo").
# La pagina si renderizza in grigi una volta; per ogni casella si cercano le
# colonne con inchiostro sulla riga: il dato sono i tratti col centro dentro il
# riquadro di Vision, e la casella parte dal primo tratto del dato e finisce
# sull'ultimo: tra la parola prima e la casella resta bianco tutto lo
# spazio-lettera, e lo stesso verso la parola dopo. Verso il bianco libero
# (inizio o fine riga) il margine resta OCR_PAD.
INK_DPI = 300
INK_LEVEL = 140          # grigio (0-255) sotto cui un pixel e' inchiostro
INK_SAME_WORD = 0.12     # spazio < 12% dell'altezza della riga: stessa parola


class _Ink:
    """Pagina come si vede (rotazione compresa), in grigi, per misurare dove
    sta l'inchiostro attorno a un riquadro dell'OCR."""

    def __init__(self, page):
        self.pix = page.get_pixmap(dpi=INK_DPI, colorspace=fitz.csGRAY, annots=False)
        self.k = INK_DPI / 72.0
        self.to_vis = page.rotation_matrix
        self.from_vis = page.derotation_matrix

    def _ink_cols(self, x0, x1, y0, y1):
        W, H, s = self.pix.width, self.pix.height, self.pix.samples
        x0, x1 = max(0, x0), min(W, x1)
        y0, y1 = max(0, y0), min(H, y1)
        cols = []
        for x in range(x0, x1):
            cols.append(any(s[y * W + x] < INK_LEVEL for y in range(y0, y1)))
        return x0, cols

    def snap(self, rect, k=OCR_PAD):
        """Riquadro dell'OCR (coordinate non ruotate) -> casella con margine,
        allineata all'inchiostro del dato e staccata dalle parole vicine."""
        vis = fitz.Rect(rect) * self.to_vis
        h = max(vis.height, 1.0)
        dx, dy = max(1.0, h * k * 0.6), max(1.0, h * k)
        padded = fitz.Rect(vis.x0 - dx, vis.y0 - dy, vis.x1 + dx, vis.y1 + dy)
        q = self.k
        bx0, bx1 = vis.x0 * q, vis.x1 * q
        start, cols = self._ink_cols(int((vis.x0 - 3 * h) * q), int((vis.x1 + 3 * h) * q) + 1,
                                     int(vis.y0 * q), int(vis.y1 * q) + 1)
        runs, i = [], 0
        while i < len(cols):                       # tratti di inchiostro contigui
            if cols[i]:
                j = i
                while j + 1 < len(cols) and cols[j + 1]:
                    j += 1
                runs.append([start + i, start + j + 1])
                i = j + 1
            else:
                i += 1
        val = [n for n, (a, b) in enumerate(runs) if bx0 <= (a + b) / 2 <= bx1]
        if not val:
            return _pad(rect, k)
        lo, hi = val[0], val[-1]
        same = INK_SAME_WORD * h * q
        while lo > 0 and runs[lo][0] - runs[lo - 1][1] < same:          # lettera tagliata
            lo -= 1
        while hi + 1 < len(runs) and runs[hi + 1][0] - runs[hi][1] < same:
            hi += 1
        vx0, vx1 = runs[lo][0] / q, runs[hi][1] / q
        # verso una parola vicina la casella si ferma sul bordo dell'inchiostro
        # del dato: tutto lo spazio-lettera resta bianco. Verso il bianco
        # libero (inizio o fine riga) resta il margine OCR_PAD.
        left = vx0 if lo > 0 else vx0 - dx
        right = vx1 if hi + 1 < len(runs) else vx1 + dx
        box = fitz.Rect(left, padded.y0, right, padded.y1) * self.from_vis
        box.normalize()
        return box


# Pagina scansionata col testo dello scanner: se questa quota dei caratteri e'
# INVISIBILE (render mode 3) il layer e' quello dello scanner e si toglie tutto.
INVISIBLE_LAYER_MIN = 0.5


def _strip_invisible_text(page):
    """Toglie il testo INVISIBILE di una pagina scansionata (l'OCR dello scanner
    sotto l'immagine). Sulle pagine lette con il nostro OCR quel layer non serve a
    niente e puo' tenere le PII in chiaro anche quando e' "rovinato": in un font
    con le lettere spostate le cifre di un IBAN restano spesso le stesse, e
    copia-incolla le restituisce. Si tolgono solo i caratteri invisibili (riquadri
    dal texttrace), immagini e grafica intatte. Ritorna i caratteri tolti."""
    try:
        spans = page.get_texttrace()
    except Exception:
        return 0
    tot = sum(len(sp.get("chars") or ()) for sp in spans)
    inv = [sp for sp in spans if sp.get("type") == 3]
    n = sum(len(sp.get("chars") or ()) for sp in inv)
    if not tot or n / tot < INVISIBLE_LAYER_MIN:
        return 0
    for sp in inv:
        r = fitz.Rect(sp["bbox"])
        if not r.is_empty:
            page.add_redact_annot(r, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    return n


# Risoluzione dell'immagine che prende il posto di una pagina col testo visibile
# illeggibile: la stessa con cui la legge l'OCR (ocr.OCR_DPI).
FLATTEN_DPI = 300


def _visible_chars(page):
    try:
        spans = page.get_texttrace()
    except Exception:
        return 0
    return sum(len(sp.get("chars") or ()) for sp in spans if sp.get("type") != 3)


def _flatten_to_image(page):
    """Pagina letta con l'OCR che ha ancora testo VISIBILE (illeggibile: font
    senza mappa Unicode, lettere spostate) -> la stessa pagina come immagine.

    Perche': in quel testo i caratteri hanno spesso riquadri molto piu' larghi
    del segno vero (nell'atto: font Type 3 con ogni carattere largo quanto il
    corpo, ~10 pt, e le lettere avvicinate a ~3 pt con la crenatura).
    apply_redactions() toglie OGNI carattere il cui riquadro tocca la casella,
    quindi la casella sopra un dato si portava via anche 2-3 lettere prima e
    dopo ("numero" -> "nume", "Fiscale" -> "Fisca"). Su un'immagine invece si
    sbiancano solo i pixel dentro la casella. In piu' il testo illeggibile esce
    dal file: non tiene piu' in chiaro nemmeno le cifre di un IBAN.

    Si renderizza senza annotazioni (ripulite a parte), si svuotano contenuto e
    risorse della pagina e si mette l'immagine. Ritorna la pagina ricaricata, o
    None se non c'era testo visibile."""
    if not _visible_chars(page):
        return None
    doc, rot = page.parent, page.rotation
    if rot:
        page.set_rotation(0)        # immagine e redazioni nelle coordinate non ruotate
    pix = page.get_pixmap(dpi=FLATTEN_DPI, annots=False)
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, b"")
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    doc.xref_set_key(page.xref, "Resources", "<<>>")
    page = doc.reload_page(page)
    page.insert_image(page.rect, pixmap=pix)
    if rot:
        page.set_rotation(rot)
    return page


def redact_pdf(pdf_bytes, mapping, fill=REDACT_FILL, text_color=REDACT_TEXT,
               ocr_pages=None, ocr_verify=None, labels=True):
    """PDF originale -> PDF con redazione vera + placeholder al posto delle PII.

    mapping: {"[FULLNAME_1]": "Mario Rossi", ...} (il dizionario di analyze()).

    ocr_pages: {indice di pagina da 0: ocr.OcrPage} per le pagine SCANSIONATE.
        Oltre al layer testuale si cercano i valori nel testo letto dall'OCR e si
        copre il riquadro delle parole: apply_redactions() sbianca i pixel
        dell'immagine sotto (images=PDF_REDACT_IMAGE_PIXELS, il default) e la
        casella viola ci va sopra. Senza questo, nel PDF in uscita la scansione
        resterebbe com'era.
    labels: True = su ogni casella il suo segnaposto ([DATE_1], [FIRMA]...); False =
        solo il rettangolo colorato, senza testo (impostazione "etichette sul PDF").
        Non cambia cosa viene rimosso, solo cosa si vede al suo posto.
    ocr_verify: funzione (pdf_bytes_in_uscita, [indici]) -> {indice: OcrPage}
        che RILEGGE con l'OCR le pagine redatte. Un valore ancora leggibile finisce
        in "residual" come per il testo; una pagina che non si riesce a rileggere
        finisce in "ocr_unverified" (l'UI avvisa: copertura non verificata).
    Ritorna (bytes, report) con report = {
        "occurrences":    occorrenze redatte nel testo di pagina,
        "by_placeholder": {placeholder: n_occorrenze},
        "not_found":      placeholder cercati ma senza occorrenze di pagina,
        "skipped":        placeholder NON cercati perche' troppo corti/ambigui
                          (restano in chiaro: va segnalato all'utente),
        "residual":       placeholder ancora leggibili nell'output (deve essere []),
        "ocr_pages":      pagine (da 1) redatte con i riquadri dell'OCR,
        "ocr_unverified": pagine (da 1) OCR la cui copertura non e' stata riletta,
        "invisible_chars": caratteri del testo invisibile dello scanner tolti,
        "flattened":      pagine (da 1) OCR col testo visibile illeggibile,
                          trasformate in immagine prima di coprire,
        "annots":         sostituzioni nelle annotazioni,
        "widgets":        sostituzioni nei campi modulo,
        "signatures":     firme rimosse (campi /Sig, Ink, timbri, immagini e tratti
                          vicino alle diciture di firma): vedi _redact_signature_*,
        "signature_data": dizionari di firma digitale (/V, /Sig) svuotati,
        "toc":            sostituzioni nei segnalibri,
        "embedded":       allegati rimossi,
    }
    """
    if not isinstance(mapping, dict) or not mapping:
        raise PdfError("Dizionario vuoto: anonimizza prima il documento.")
    items = [(ph, v) for ph, v in mapping.items()
             if isinstance(ph, str) and isinstance(v, str) and v.strip()]
    if not items:
        raise PdfError("Dizionario non valido.")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise PdfError("File PDF non valido o danneggiato.")
    if doc.needs_pass:
        doc.close()
        raise PdfError("PDF protetto da password: rimuovi la protezione e riprova.")

    # valori lunghi per primi: cosi' "Rossi" non spezza la redazione di
    # "Mario Rossi" (i rect gia' coperti vengono saltati da _covered)
    items.sort(key=lambda kv: -len(kv[1]))

    skipped, usable = [], []
    for ph, val in items:
        if _too_noisy(val):
            skipped.append(ph)
            continue
        pat = _value_pattern(val)
        if pat:
            usable.append((ph, val, pat))
        else:
            skipped.append(ph)

    by_ph = {ph: 0 for ph, _, _ in usable}
    patterns = [(pat, ph) for ph, _, pat in usable]   # per annot/widget/TOC
    total = n_annots = n_widgets = n_sig = 0

    ocr_pages = {i: p for i, p in (ocr_pages or {}).items()
                 if 0 <= i < doc.page_count}

    def _redact_text(page, text, boxes, taken, pad, ink=None):
        n = 0
        if not text.strip():
            return 0
        for ph, val, pat in usable:
            for m in pat.finditer(text):
                rects = _match_rects(boxes, m)
                if pad:
                    rects = [ink.snap(r) if ink else _pad(r) for r in rects]
                placed_any, labeled = False, False
                for r in rects:
                    if _covered(r, taken):
                        continue
                    fs = 0 if (labeled or not labels) else _fit_fontsize(ph, r)
                    _add_redact_annot(page, r, ph if fs else None,
                                      fs or 6, fill, text_color)
                    taken.append(fitz.Rect(r))
                    placed_any = True
                    labeled = labeled or bool(fs)
                if placed_any:
                    by_ph[ph] += 1
                    n += 1
        return n

    n_invisible = 0
    flattened = []
    for pno in range(doc.page_count):
        page = doc[pno]
        taken = []
        # 0) pagina scansionata: via il testo invisibile dello scanner, PRIMA di
        #    disegnare le caselle (dopo, la sua rimozione toglierebbe anche le
        #    etichette dei segnaposto che ci stanno sopra)
        if pno in ocr_pages:
            n_invisible += _strip_invisible_text(page)
            # 0b) testo VISIBILE illeggibile: la pagina diventa un'immagine,
            #     cosi' le caselle dell'OCR non si portano via le lettere vicine
            flat = _flatten_to_image(page)
            if flat is not None:
                page = flat
                flattened.append(pno + 1)
        # 0c) firme (immagini, tratti, timbri, campi /Sig): per forma, non per valore.
        #     PRIMA delle caselle dei nomi: i tracciati vettoriali si tolgono solo con
        #     graphics=TOUCHED, che altrimenti si porterebbe via anche i bordi di tabella
        #     toccati dalle altre caselle della pagina.
        n_sig += _redact_signature_shapes(page, taken, fill, text_color, labels)
        # 1) layer testuale (anche sulle scansioni: il testo nascosto dello
        #    scanner va tolto dove contiene i valori)
        text, boxes = _page_char_index(page)
        total += _redact_text(page, text, boxes, taken, pad=False)
        # 2) testo letto dall'OCR: copre quello che si VEDE nell'immagine
        if pno in ocr_pages:
            o_text, o_boxes = _ocr_char_index(ocr_pages[pno])
            try:
                ink = _Ink(page)
            except Exception:
                ink = None
            total += _redact_text(page, o_text, o_boxes, taken, pad=True, ink=ink)
        # 3) immagini-firma: dopo i nomi, cosi' "sopra un nome redatto" vale qualcosa
        n_sig += _redact_signature_images(page, taken, fill, text_color, labels)
        if taken:
            page.apply_redactions()   # rimozione VERA dal content stream
        # dopo le redazioni: annotazioni e campi modulo non sono content stream
        n_annots += _scrub_annots(page, patterns)
        n_widgets += _scrub_widgets(page, patterns)

    n_sigdata = _scrub_signature_data(doc)
    n_toc = _scrub_toc(doc, patterns)
    n_emb = _strip_embedded(doc)
    _scrub_metadata(doc)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()

    checked = [(ph, v) for ph, v in items if ph in by_ph]
    residual = _verify_residuals(out, checked)
    unverified = []
    if ocr_pages:
        res_ocr, unverified = _verify_ocr(out, checked, sorted(ocr_pages), ocr_verify)
        residual += [ph for ph in res_ocr if ph not in residual]
    return out, {
        "occurrences": total,
        "by_placeholder": by_ph,
        "not_found": [ph for ph, n in by_ph.items() if n == 0],
        "skipped": skipped,
        "residual": residual,
        "ocr_pages": [i + 1 for i in sorted(ocr_pages)],
        "invisible_chars": n_invisible,
        "flattened": flattened,
        "ocr_unverified": unverified,
        "annots": n_annots,
        "widgets": n_widgets,
        "signatures": n_sig,
        "signature_data": n_sigdata,
        "toc": n_toc,
        "embedded": n_emb,
    }


# apply_redactions() colora la casella con riempimento E bordo dello stesso
# colore, spesso 1 pt: la casella visibile e' mezzo punto piu' larga del
# rettangolo da redigere su ogni lato. Accanto a una parola quel mezzo punto
# copriva meta' dell'ultima lettera ("Signor" -> "Signo").
REDACT_STROKE_HALF = 0.5


def _add_redact_annot(page, rect, text, fontsize, fill, text_color):
    """add_redact_annot con cross_out disattivato dove il binding lo supporta
    (le X diagonali sporcano il placeholder); fallback per le versioni vecchie.

    La casella colorata deve coincidere col rettangolo: una redazione senza
    colore su tutto `rect` toglie testo e pixel, una seconda col colore e il
    segnaposto sta dentro di mezzo punto, cosi' col bordo arriva proprio al
    bordo di `rect` e non oltre."""
    rect = fitz.Rect(rect)
    h = REDACT_STROKE_HALF
    inner = fitz.Rect(rect.x0 + h, rect.y0 + h, rect.x1 - h, rect.y1 - h)
    kw = dict(text=text, fontname="helv", fontsize=fontsize,
              align=fitz.TEXT_ALIGN_CENTER, fill=fill, text_color=text_color)

    def add(r, **k):
        try:
            return page.add_redact_annot(r, cross_out=False, **k)
        except TypeError:
            return page.add_redact_annot(r, **k)

    if not fill or inner.width < 1 or inner.height < 1:
        return add(rect, **kw)
    add(rect)                       # solo togliere, nessun colore
    return add(inner, **kw)


def text_to_pdf(text, margin=56.0, fontsize=10.5, leading=15.5):
    """Testo (gia' anonimizzato) -> PDF A4 solo testo, impaginato da zero.
    Nessun contenuto del documento originale finisce nell'output."""
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE)
    # helv copre Latin-1: sostituzione esplicita dei caratteri fuori codifica
    text = text.encode("latin-1", "replace").decode("latin-1")
    if not text.strip():
        raise PdfError("Nessun testo da impaginare.")

    a4 = fitz.paper_rect("a4")
    width = a4.width - 2 * margin

    def tl(s):
        return fitz.get_text_length(s, fontname="helv", fontsize=fontsize)

    # In una riga larga `width` non entrano piu' di `cap` caratteri, qualunque siano:
    # oltre quel punto non c'e' niente da misurare. Serve perche' hard_split misurava
    # e ricopiava TUTTA la stringa residua a ogni giro -> O(n^2): una "parola" senza
    # spazi (JSON incollato, base64, riga di tabella coi soli tab) da 16.000 caratteri
    # costava 15 s, da 128.000 un quarto d'ora.
    # Il conto va fatto sul glifo PIU' STRETTO, non su "l": il testo e' gia' forzato in
    # latin-1 poche righe sopra, quindi il repertorio e' chiuso e il minimo si misura.
    # In helv e' l'apostrofo (2,0055 pt a corpo 10,5) contro i 2,331 di "l": con "l" cap
    # varrebbe 209 dove in colonna ne entrano 240, la finestra ci starebbe, il ciclo
    # uscirebbe subito e la coda finirebbe tutta su una riga larga oltre il bordo pagina.
    stretto = min(tl(chr(c)) for c in list(range(32, 127)) + list(range(160, 256)))
    cap = max(2, int(width / max(stretto, 0.1)) + 2)   # >= 2: con margini assurdi width <= 0

    def hard_split(line):
        """Spezza le 'parole' piu' larghe della riga (IBAN, URL...)."""
        if tl(line) <= width:          # caso normale: la riga ci sta gia', una sola misura
            return [line]
        out, i = [], 0
        while len(line) - i > 1:
            head = line[i:i + cap]
            if tl(head) <= width:
                break
            k = max(1, int(len(head) * width / tl(head)))
            while k > 1 and tl(head[:k]) > width:
                k -= 1
            while k < len(head) and tl(head[:k + 1]) <= width:
                k += 1
            out.append(head[:k])
            i += k
        out.append(line[i:])
        return out

    def wrap(par):
        if not par:
            return [""]
        lines, cur = [], ""
        for w in par.split(" "):
            cand = (cur + " " + w) if cur else w
            if not cur or tl(cand) <= width:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        out = []
        for ln in lines:
            out.extend(hard_split(ln))
        return out

    doc = fitz.open()
    page = doc.new_page(width=a4.width, height=a4.height)
    y = margin + fontsize
    for par in text.split("\n"):
        for ln in wrap(par.rstrip()):
            if y > a4.height - margin:
                page = doc.new_page(width=a4.width, height=a4.height)
                y = margin + fontsize
            if ln:
                page.insert_text((margin, y), ln, fontname="helv",
                                 fontsize=fontsize)
            y += leading

    _scrub_metadata(doc)
    out = doc.tobytes(deflate=True)
    doc.close()
    return out
