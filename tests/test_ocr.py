# -*- coding: utf-8 -*-
"""OCR delle pagine scansionate (src/app/ocr.py + pdf_export + /preview, /pdf).

Vision gira solo su macOS: qui il motore e' un FINTO che fa quello che serve ai
test, e lo fa sui pixel veri. Conosce le parole della pagina e i loro riquadri, e
"legge" una parola solo se nel suo riquadro dell'immagine ci sono ancora pixel
neri. Cosi' la prima lettura trova il testo della scansione e la seconda (la
verifica sul PDF redatto) non trova le parole coperte: se la copertura e' sbagliata
il test se ne accorge, come se ne accorgerebbe Vision.

Il motore vero (rizzo-ocr + Vision) e' provato da tests/macos/, in CI su macOS.

Tutti i valori sono SINTETICI.
"""

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

import ocr  # noqa: E402
import pdf_text  # noqa: E402
from test_ora import APP, client  # noqa: E402

try:
    import pymupdf as fitz
    import pdf_export
except ImportError:                      # pragma: no cover
    fitz = pdf_export = None

IBAN = "IT60X0542811101000000123456"
EMAIL = "mario.rossi@example.com"
RIGHE = [
    "TRIBUNALE ORDINARIO DI TORINO - Atto di citazione",
    "Il sottoscritto avvocato, per conto della parte attrice, chiede che il",
    f"pagamento sia fatto sul conto IBAN {IBAN} intestato alla ditta.",
    f"Per le comunicazioni scrivere a {EMAIL} entro il termine di legge.",
    "Con osservanza, si allegano i documenti indicati nella presente.",
]

ITALIANO = " ".join(RIGHE) * 2
# lo stesso testo con le lettere spostate di 3: quello che esce da un font con la
# codifica rovinata (a video si legge, estratto e' senza senso)
SPOSTATO = "".join(chr((ord(c) - 97 + 3) % 26 + 97) if "a" <= c <= "z" else c
                   for c in ITALIANO.lower())
SIMBOLI = "#$%&'()*+,-./:;<=>?@[\\]^_`{|}~" * 6


class Qualita(unittest.TestCase):
    def test_testo_vero_leggibile(self):
        self.assertFalse(ocr.is_unreadable(ITALIANO))

    def test_modulo_coi_puntini_leggibile(self):
        t = ("Il sottoscritto ................................ nato a ............ "
             "il ........ residente in via ....................... n. .... "
             "codice fiscale ................................ dichiara di essere "
             "a conoscenza delle sanzioni previste per le dichiarazioni mendaci "
             "e della decadenza dai benefici ottenuti.")
        self.assertFalse(ocr.is_unreadable(t))

    def test_elenco_di_nomi_leggibile(self):
        t = ("Mario Rossi, via Roma 12, Torino; Luca Bianchi, corso Francia 5, "
             "Milano; Giulia Verdi, piazza Castello 3, Genova; Paolo Neri, viale "
             "Monza 44, Bergamo; Anna Russo, via Po 8, Asti; Marco Gallo, Cuneo")
        self.assertFalse(ocr.is_unreadable(t))

    def test_tedesco_leggibile(self):
        t = ("Der Vertrag wird zwischen den Parteien geschlossen und unterliegt dem "
             "deutschen Recht. Der Käufer zahlt den Kaufpreis innerhalb von dreißig "
             "Tagen nach der Lieferung der Ware an die angegebene Adresse.")
        self.assertFalse(ocr.is_unreadable(t))

    def test_inglese_leggibile(self):
        t = ("This agreement is made between the parties and is governed by the "
             "laws of Italy. The buyer shall pay the price within thirty days of "
             "the delivery of the goods to the address indicated in the order.")
        self.assertFalse(ocr.is_unreadable(t))

    def test_lettere_spostate_illeggibili(self):
        self.assertTrue(ocr.is_unreadable(SPOSTATO))

    def test_simboli_illeggibili(self):
        self.assertTrue(ocr.is_unreadable(SIMBOLI))

    def test_glifi_senza_mappa_illeggibili(self):
        self.assertTrue(ocr.is_unreadable("Atto ��� di �� " * 10))
        self.assertTrue(ocr.is_unreadable(" abc " * 20))

    def test_vuoto_non_e_illeggibile(self):
        self.assertFalse(ocr.is_unreadable(""))


class _Pag:
    def __init__(self, testo, immagini=0, invisibile=0.0):
        self._t, self._i, self._inv = testo, immagini, invisibile

    def get_text(self):
        return self._t

    def get_images(self, full=False):
        return [(1,)] * self._i

    def get_texttrace(self):
        n = len(self._t)
        k = int(n * self._inv)
        return [{"type": 3, "chars": [0] * k}, {"type": 0, "chars": [0] * (n - k)}]


class Piano(unittest.TestCase):
    def test_motivi(self):
        doc = [_Pag(ITALIANO),                  # 0 testo vero: niente OCR
               _Pag("", 1),                     # 1 scansione pura
               _Pag("", 0),                     # 2 pagina bianca
               _Pag("  12  ", 2),               # 3 numero di pagina sopra la scansione
               _Pag(SPOSTATO, 1),               # 4 scansione col testo rovinato
               _Pag(SPOSTATO, 0),               # 5 PDF digitale coi font rovinati
               _Pag(ITALIANO, 1, invisibile=0.9),   # 6 OCR dello scanner, invisibile
               _Pag(ITALIANO, 1, invisibile=0.1)]   # 7 testo vero + logo
        self.assertEqual({1: ocr.EMPTY, 3: ocr.EMPTY, 4: ocr.UNREADABLE,
                          5: ocr.UNREADABLE, 6: ocr.INVISIBLE}, ocr.plan_pages(doc))

    def test_avviso_senza_motore(self):
        res = ocr.OcrResult(plan={1: ocr.EMPTY, 4: ocr.UNREADABLE, 6: ocr.INVISIBLE})
        # "invisibile" senza OCR: il layer dello scanner si redige come prima
        self.assertEqual([2, 5], res.warn_pages())
        res.pages = {1: ocr.OcrPage()}
        self.assertEqual([5], res.warn_pages())

    def test_vecchia_funzione_invariata(self):
        doc = [_Pag("", 1), _Pag("x" * 60, 1)]
        self.assertEqual([1], pdf_text.pages_without_text(doc))


class Riquadri(unittest.TestCase):
    RAW = {"lines": [{"text": "Mario Rossi", "box": [0.1, 0.2, 0.4, 0.05],
                      "words": [{"text": "Mario", "box": [0.1, 0.2, 0.15, 0.05]},
                                {"text": "Rossi", "box": [0.3, 0.2, 0.2, 0.05]}]},
                     {"text": "via Roma", "box": [0.1, 0.3, 0.3, 0.05], "words": []}]}

    def test_coordinate_pagina(self):
        p = ocr.parse_result(self.RAW, (0, 0, 600, 800))
        (m, r), riga2 = p.lines
        self.assertEqual("Mario", m.text)
        for a, b in zip(m.box, (60, 160, 150, 200)):
            self.assertAlmostEqual(a, b)
        # riga senza riquadri per parola: si usa quello della riga
        self.assertEqual(["via", "Roma"], [w.text for w in riga2])
        self.assertEqual(riga2[0].box, riga2[1].box)

    def test_indice_caratteri(self):
        p = ocr.parse_result(self.RAW, (0, 0, 600, 800))
        text, boxes = p.char_index()
        self.assertEqual("Mario Rossi\nvia Roma\n", text)
        self.assertEqual(len(text), len(boxes))
        self.assertIsNone(boxes[5])                    # spazio
        self.assertEqual(boxes[6], p.lines[0][1].box)  # "R" di Rossi
        self.assertEqual(text, p.text)

    @unittest.skipIf(fitz is None, "PyMuPDF non disponibile")
    def test_pagina_ruotata(self):
        """La matematica della rotazione contro PyMuPDF vero: il riquadro del
        testo visto nell'immagine renderizzata deve tornare quello del rawdict."""
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((100, 100), "MARIO ROSSI", fontsize=20)
        page.set_rotation(90)
        ws = page.get_text("words")             # "MARIO", "ROSSI"
        span = (min(w[0] for w in ws), min(w[1] for w in ws),
                max(w[2] for w in ws), max(w[3] for w in ws))
        pix = page.get_pixmap(dpi=72)
        dark = [(x, y) for y in range(pix.height) for x in range(0, pix.width)
                if pix.pixel(x, y)[0] < 128]
        xs, ys = [d[0] for d in dark], [d[1] for d in dark]
        box = [min(xs) / pix.width, min(ys) / pix.height,
               (max(xs) - min(xs) + 1) / pix.width, (max(ys) - min(ys) + 1) / pix.height]
        raw = {"lines": [{"text": "X", "box": box, "words": [{"text": "X", "box": box}]}]}
        got = ocr.parse_result(raw, tuple(page.rect), tuple(page.derotation_matrix))
        w = got.lines[0][0].box
        # i pixel neri stanno dentro il riquadro del testo (piu' stretti del font)
        self.assertGreaterEqual(w[0], span[0] - 2)
        self.assertLessEqual(w[2], span[2] + 2)
        self.assertGreaterEqual(w[1], span[1] - 2)
        self.assertLessEqual(w[3], span[3] + 2)
        doc.close()


class _Doc:
    """Documento finto per ocr_pages: tre pagine, la 1 non si renderizza."""
    class P:
        def __init__(self, i):
            self.i = i
            self.rect = (0, 0, 100, 100)
            self.derotation_matrix = (1, 0, 0, 1, 0, 0)

        def get_pixmap(self, dpi):
            if self.i == 1:
                raise RuntimeError("pagina rotta")

            class Pix:
                def tobytes(_s, fmt):
                    return b"png%d" % self.i
            return Pix()

    def load_page(self, i):
        return self.P(i)


class Motore(unittest.TestCase):
    def test_errori_per_pagina(self):
        def eng(png):
            if png == b"png2":
                raise ocr.OcrError("Vision ha fallito")
            return {"lines": [{"text": "ok", "box": [0, 0, 1, 1],
                               "words": [{"text": "ok", "box": [0, 0, 1, 1]}]}]}
        pages, failed = ocr.ocr_pages(_Doc(), [0, 1, 2, 3], eng, workers=2)
        self.assertEqual([0, 3], sorted(pages))
        self.assertEqual([1, 2], sorted(failed))
        self.assertEqual("ok\n", pages[0].text)

    def test_senza_motore(self):
        pages, failed = ocr.ocr_pages(_Doc(), [0, 2], None)
        self.assertEqual({}, pages)
        self.assertEqual([0, 2], sorted(failed))

    def test_niente_motore_fuori_da_macos(self):
        with patch.object(ocr.sys, "platform", "linux"):
            self.assertIsNone(ocr.default_engine())
        with patch.object(ocr.sys, "platform", "darwin"), \
                patch.dict(ocr.os.environ, {"PII_OCR": "0"}):
            self.assertIsNone(ocr.default_engine())

    def test_helper_chiamato_con_il_png(self):
        class R:
            returncode, stdout, stderr = 0, b'{"lines": []}', b""
        with patch.object(ocr.subprocess, "run", return_value=R()) as run:
            out = ocr.MacVisionEngine("/x/rizzo-ocr")(b"PNG")
        self.assertEqual({"lines": []}, out)
        self.assertEqual(b"PNG", run.call_args.kwargs["input"])

        class Rotto:
            returncode, stdout, stderr = 3, b"", b"riconoscimento fallito"
        with patch.object(ocr.subprocess, "run", return_value=Rotto()):
            with self.assertRaises(ocr.OcrError):
                ocr.MacVisionEngine("/x/rizzo-ocr")(b"PNG")


# --------------------------------------------------------------------------- #
# Scansioni vere (PyMuPDF) + OCR finto che guarda i pixel
# --------------------------------------------------------------------------- #
def _sorgente():
    """Pagina di testo da cui si "scansiona": serve anche a sapere dove sono le
    parole (la verita' del motore finto)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 100
    for r in RIGHE:
        page.insert_text((60, y), r, fontsize=10)
        y += 22
    return doc


def _verita(page):
    """Righe di parole con i riquadri normalizzati sull'immagine della pagina."""
    W, H = page.rect.width, page.rect.height
    righe = {}
    for x0, y0, x1, y1, w, b, ln, _n in page.get_text("words"):
        righe.setdefault((b, ln), []).append(
            (w, [x0 / W, y0 / H, (x1 - x0) / W, (y1 - y0) / H]))
    return [righe[k] for k in sorted(righe)]


def scansione(strato=None):
    """PDF con la pagina come IMMAGINE (nessun testo), opzionalmente con un layer
    invisibile tipo scanner. Ritorna (bytes, verita')."""
    src = _sorgente()
    sp = src[0]
    pix = sp.get_pixmap(dpi=150)
    out = fitz.open()
    p = out.new_page(width=sp.rect.width, height=sp.rect.height)
    p.insert_image(p.rect, pixmap=pix)
    if strato:
        p.insert_text((60, 100), strato, fontsize=6, render_mode=3)
    data = out.tobytes()
    truth = _verita(sp)
    out.close()
    src.close()
    return data, truth


def scansione_ruotata():
    """Come capita davvero: l'immagine e' salvata di traverso e la pagina ha
    /Rotate 90, cosi' a video (e a Vision) l'atto si vede DRITTO. La verita'
    resta quella della pagina come si vede."""
    data, truth = scansione()
    with fitz.open(stream=data, filetype="pdf") as d:
        pix = d[0].get_pixmap(dpi=150)
        w, h = d[0].rect.width, d[0].rect.height
    out = fitz.open()
    p = out.new_page(width=h, height=w)
    p.set_rotation(90)
    # rect nelle coordinate NON ruotate: cosi' l'immagine riempie la pagina
    p.insert_image(p.rect * p.derotation_matrix, pixmap=pix, rotate=90)
    data = out.tobytes()
    out.close()
    return data, truth


def rect_non_ruotato(pdf_bytes, rect_visto):
    """Riquadro della pagina come si vede -> coordinate non ruotate."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        return fitz.Rect(rect_visto) * d[0].derotation_matrix


class OcrFinto:
    """Legge una parola se il suo riquadro contiene ancora pixel neri.
    `allarga` (frazione della larghezza della pagina, per lato) restituisce
    riquadri piu' larghi della parola, come fa Vision."""

    def __init__(self, truth, spostamento=0.0, allarga=0.0):
        self.truth, self.dx, self.calls = truth, spostamento, 0
        self.allarga = allarga

    def __call__(self, png):
        self.calls += 1
        pix = fitz.Pixmap(png)
        W, H, n, s = pix.width, pix.height, pix.n, pix.samples

        def nero(box):
            x0, y0 = int(box[0] * W), int(box[1] * H)
            x1, y1 = int((box[0] + box[2]) * W) + 1, int((box[1] + box[3]) * H) + 1
            for y in range(max(0, y0), min(H, y1)):
                row = y * W * n
                for x in range(max(0, x0), min(W, x1)):
                    i = row + x * n
                    if max(s[i:i + 3]) < 80:
                        return True
            return False

        lines = []
        for riga in self.truth:
            a = self.allarga
            words = [{"text": t, "box": [b[0] + self.dx - a, b[1], b[2] + 2 * a, b[3]]}
                     for t, b in riga if nero(b)]
            if words:
                lines.append({"text": " ".join(w["text"] for w in words),
                              "box": words[0]["box"], "words": words})
        return {"lines": lines}


def _pixel_neri(pdf_bytes, rect):
    """Pixel quasi neri dentro rect (coordinate NON ruotate della pagina, quelle
    delle redazioni) nel PDF renderizzato come si vede."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        page = d[0]
        vis = fitz.Rect(rect) * page.rotation_matrix      # -> pagina come si vede
        pix = page.get_pixmap(dpi=72)
    s, n, W = pix.samples, pix.n, pix.width
    tot = 0
    for y in range(max(0, int(vis.y0)), min(pix.height, int(vis.y1) + 1)):
        for x in range(max(0, int(vis.x0)), min(W, int(vis.x1) + 1)):
            i = (y * W + x) * n
            if max(s[i:i + 3]) < 80:
                tot += 1
    return tot


def _riga(pdf_bytes, rect, dpi=300):
    """Colonne (in pt) della fascia centrale della riga di `rect`, con i pixel
    RGB di ogni colonna. Pagina non ruotata."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as d:
        pix = d[0].get_pixmap(dpi=dpi)
    k, W, n, s = dpi / 72, pix.width, pix.n, pix.samples
    y0, y1 = int((rect.y0 + rect.height * 0.3) * k), int((rect.y1 - rect.height * 0.3) * k)
    x0, x1 = int((rect.x0 - 40) * k), int((rect.x1 + 40) * k)
    for x in range(max(0, x0), min(W, x1)):
        yield x / k, [tuple(s[(y * W + x) * n:(y * W + x) * n + 3]) for y in range(y0, y1)]


def _inchiostro(pdf_bytes, rect):
    """(fine dell'inchiostro prima di rect, (inizio, fine) dell'inchiostro dentro
    rect, inizio dell'inchiostro dopo), in pt."""
    nere = [x for x, px in _riga(pdf_bytes, rect) if any(max(p) < 100 for p in px)]
    passo = 72 / 300
    dentro = [x for x in nere if rect.x0 - 1 <= x <= rect.x1 + 1]
    prima = max((x for x in nere if x < rect.x0 - 1), default=None)
    dopo = min((x for x in nere if x > rect.x1 + 1), default=None)
    return (None if prima is None else prima + passo,
            (dentro[0], dentro[-1] + passo) if dentro else None, dopo)


def _casella(pdf_bytes, rect):
    """(inizio, fine) in pt della casella viola sulla riga di rect."""
    fill = tuple(round(c * 255) for c in pdf_export.REDACT_FILL)
    viola = [x for x, px in _riga(pdf_bytes, rect)
             if any(max(abs(a - b) for a, b in zip(p, fill)) < 25 for p in px)]
    return viola[0], viola[-1] + 72 / 300


def ruota_verita(truth, page):
    """Verita' del motore finto per la pagina RUOTATA: i riquadri vanno
    nell'immagine come si vede (quella che riceve Vision)."""
    W0, H0 = page.mediabox.width, page.mediabox.height
    m, vr = page.rotation_matrix, page.rect
    out = []
    for riga in truth:
        nuova = []
        for t, b in riga:
            r = fitz.Rect(b[0] * W0, b[1] * H0, (b[0] + b[2]) * W0, (b[1] + b[3]) * H0) * m
            nuova.append((t, [r.x0 / vr.width, r.y0 / vr.height,
                              r.width / vr.width, r.height / vr.height]))
        out.append(nuova)
    return out


@unittest.skipIf(fitz is None, "PyMuPDF non disponibile")
class RedazioneScansione(unittest.TestCase):
    def setUp(self):
        self.data, self.truth = scansione()
        with fitz.open(stream=self.data, filetype="pdf") as d:
            self.res = ocr.analyze_doc(d, OcrFinto(self.truth))

    def _rect_parola(self, parola):
        for riga in self.truth:
            for t, b in riga:
                if t == parola:
                    return fitz.Rect(b[0] * 595, b[1] * 842,
                                     (b[0] + b[2]) * 595, (b[1] + b[3]) * 842)
        raise AssertionError(parola)

    def test_la_scansione_viene_letta(self):
        self.assertEqual({0: ocr.EMPTY}, self.res.plan)
        self.assertIn(IBAN, self.res.pages[0].text)
        self.assertEqual([], self.res.warn_pages())

    def test_iban_coperto_e_verificato(self):
        verifiche = OcrFinto(self.truth)

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, verifiche)[0]
        mapping = {"[IBAN_1]": IBAN, "[EMAIL_1]": EMAIL}
        out, rep = pdf_export.redact_pdf(self.data, mapping, ocr_pages=self.res.pages,
                                         ocr_verify=rileggi)
        self.assertEqual(2, rep["occurrences"])
        self.assertEqual([], rep["residual"])
        self.assertEqual([1], rep["ocr_pages"])
        self.assertEqual([], rep["ocr_unverified"])
        self.assertEqual(1, verifiche.calls)
        # nell'immagine in uscita IBAN ed email non hanno piu' pixel neri...
        self.assertEqual(0, _pixel_neri(out, self._rect_parola(IBAN)))
        self.assertEqual(0, _pixel_neri(out, self._rect_parola(EMAIL)))
        # ...mentre il resto della scansione e' intatto
        self.assertGreater(_pixel_neri(out, self._rect_parola("TRIBUNALE")), 0)

    def test_copertura_sbagliata_diventa_residuo(self):
        """Riquadri spostati di mezza pagina: la redazione copre il vuoto, la
        rilettura trova ancora l'IBAN e lo segnala. Mai un PDF che sembra
        anonimizzato e non lo e'."""
        with fitz.open(stream=self.data, filetype="pdf") as d:
            storto = ocr.analyze_doc(d, OcrFinto(self.truth, spostamento=0.5))

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, OcrFinto(self.truth))[0]
        out, rep = pdf_export.redact_pdf(self.data, {"[IBAN_1]": IBAN},
                                         ocr_pages=storto.pages, ocr_verify=rileggi)
        self.assertEqual(["[IBAN_1]"], rep["residual"])

    def test_pagina_ruotata(self):
        """Pagina con /Rotate 90: Vision vede l'immagine ruotata, le redazioni
        vanno nelle coordinate non ruotate. Le due cose devono combaciare."""
        with fitz.open(stream=self.data, filetype="pdf") as d:
            d[0].set_rotation(90)
            data = d.tobytes()
        with fitz.open(stream=data, filetype="pdf") as d:
            truth = ruota_verita(self.truth, d[0])
            res = ocr.analyze_doc(d, OcrFinto(truth))
        self.assertIn(IBAN, res.pages[0].text)

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, OcrFinto(truth))[0]
        out, rep = pdf_export.redact_pdf(data, {"[IBAN_1]": IBAN},
                                         ocr_pages=res.pages, ocr_verify=rileggi)
        self.assertEqual([], rep["residual"])
        self.assertEqual(0, _pixel_neri(out, self._rect_parola(IBAN)))
        self.assertGreater(_pixel_neri(out, self._rect_parola("TRIBUNALE")), 0)

    def test_scansione_ruotata_dritta(self):
        """Il caso reale: /Rotate 90 con l'immagine salvata di traverso, l'atto
        si vede dritto. Le redazioni devono finire sopra la parola giusta."""
        data, truth = scansione_ruotata()
        with fitz.open(stream=data, filetype="pdf") as d:
            self.assertEqual(90, d[0].rotation)
            res = ocr.analyze_doc(d, OcrFinto(truth))
        self.assertIn(IBAN, res.pages[0].text)

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, OcrFinto(truth))[0]
        out, rep = pdf_export.redact_pdf(data, {"[IBAN_1]": IBAN},
                                         ocr_pages=res.pages, ocr_verify=rileggi)
        self.assertEqual(1, rep["occurrences"])
        self.assertEqual([], rep["residual"])
        iban = rect_non_ruotato(data, self._rect_parola(IBAN))
        tribunale = rect_non_ruotato(data, self._rect_parola("TRIBUNALE"))
        self.assertGreater(_pixel_neri(data, iban), 0)
        self.assertEqual(0, _pixel_neri(out, iban))
        self.assertGreater(_pixel_neri(out, tribunale), 0)

    def test_senza_verifica_si_dice(self):
        out, rep = pdf_export.redact_pdf(self.data, {"[IBAN_1]": IBAN},
                                         ocr_pages=self.res.pages)
        self.assertEqual([1], rep["ocr_unverified"])

        def rotto(out, idx):
            raise ocr.OcrError("Vision non risponde")
        out, rep = pdf_export.redact_pdf(self.data, {"[IBAN_1]": IBAN},
                                         ocr_pages=self.res.pages, ocr_verify=rotto)
        self.assertEqual([1], rep["ocr_unverified"])

    def test_strato_dello_scanner_rovinato(self):
        """Il caso dell'atto: immagine + testo invisibile con caratteri senza senso.
        La pagina va all'OCR e il testo rovinato non entra nell'analisi."""
        data, truth = scansione(strato=SPOSTATO[:400])
        with fitz.open(stream=data, filetype="pdf") as d:
            res = ocr.analyze_doc(d, OcrFinto(truth))
            testo = pdf_text.collect_readable_text(d, page_texts=res.texts())
        self.assertEqual({0: ocr.UNREADABLE}, res.plan)
        self.assertIn(IBAN, testo)
        self.assertNotIn(SPOSTATO[:30], testo)
        # nel PDF in uscita il testo invisibile dello scanner sparisce: con le
        # lettere spostate le cifre dell'IBAN erano rimaste uguali
        self.assertIn("0542811101000000123456", SPOSTATO)
        out, rep = pdf_export.redact_pdf(data, {"[IBAN_1]": IBAN},
                                         ocr_pages=res.pages)
        with fitz.open(stream=out, filetype="pdf") as d:
            txt = d[0].get_text()
        self.assertNotIn("0542811101000000123456", txt)
        self.assertIn("[IBAN_1]", txt)                  # l'etichetta resta
        self.assertGreater(rep["invisible_chars"], 300)
        self.assertEqual(0, _pixel_neri(out, fitz.Rect(*self._box(truth, IBAN))))

    def test_testo_visibile_non_si_tocca(self):
        """Una pagina digitale coi font rovinati (testo VISIBILE) non perde il
        testo: si tolgono solo i caratteri coperti dalle caselle."""
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((72, 72), SPOSTATO[:80], fontsize=9)
        self.assertEqual(0, pdf_export._strip_invisible_text(p))
        self.assertIn(SPOSTATO[:20], p.get_text())
        doc.close()

    @staticmethod
    def _box(truth, parola):
        for riga in truth:
            for t, b in riga:
                if t == parola:
                    return (b[0] * 595, b[1] * 842, (b[0] + b[2]) * 595, (b[1] + b[3]) * 842)
        raise AssertionError(parola)


# --------------------------------------------------------------------------- #
# Testo VISIBILE con riquadri dei caratteri piu' larghi del segno (l'atto)
# --------------------------------------------------------------------------- #
PAROLE_T3 = ["Avanti", "BETA", "numero"]
T3_X0, T3_PASSO, T3_LETTERE, T3_Y = 72.0, 19.0, 4, 700.0   # y del PDF (dal basso)


def atto_type3(rotazione=0):
    """Come l'atto vero: testo VISIBILE in un font Type 3 senza mappa Unicode,
    ogni carattere largo quanto il corpo (10 pt) e le lettere riavvicinate a
    4 pt con la crenatura del TJ. Il segno di ogni lettera e' un rettangolo pieno
    di 3,5 x 7 pt. Le parole stanno a 3,5 pt l'una dall'altra, quindi il
    riquadro dell'ultima lettera di una parola sborda sulla successiva.
    Ritorna (bytes, verita' per OcrFinto, {parola: [rect di ogni lettera]})."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    def oggetto(testo, stream=None):
        x = doc.get_new_xref()
        doc.update_object(x, testo)
        if stream is not None:
            doc.update_stream(x, stream)
        return x
    glifo = oggetto("<<>>", b"1 0 0 0 0.35 0.7 d1 0 0 0.35 0.7 re f")
    font = oggetto("<</Type/Font/Subtype/Type3/FontBBox[0 0 1 1]"
                   "/FontMatrix[1 0 0 1 0 0]/CharProcs<</g %d 0 R>>"
                   "/Encoding<</Type/Encoding/Differences[97/g]>>"
                   "/FirstChar 97/LastChar 97/Widths[1]>>" % glifo)
    ops, lettere = [], {}
    for i, parola in enumerate(PAROLE_T3):
        x = T3_X0 + i * T3_PASSO
        tj = " 600 ".join(["(a)"] * T3_LETTERE)
        ops.append("BT /T3 10 Tf 1 0 0 1 %.2f %.2f Tm [%s] TJ ET" % (x, T3_Y, tj))
        lettere[parola] = [fitz.Rect(x + k * 4, 842 - T3_Y - 7, x + k * 4 + 3.5, 842 - T3_Y)
                           for k in range(T3_LETTERE)]
    contenuto = oggetto("<<>>", " ".join(ops).encode())
    doc.xref_set_key(page.xref, "Contents", "%d 0 R" % contenuto)
    doc.xref_set_key(page.xref, "Resources", "<</Font<</T3 %d 0 R>>>>" % font)
    W, H = 595.0, 842.0
    truth = [[(p, [r[0].x0 / W, r[0].y0 / H, (r[-1].x1 - r[0].x0) / W, 7 / H])
              for p, r in lettere.items()]]
    if rotazione:
        page.set_rotation(rotazione)
        truth = ruota_verita(truth, page)
    data = doc.tobytes()
    doc.close()
    return data, truth, lettere


def testo_largo(righe=RIGHE, corpo=10.0):
    """Lettere VERE in un font Type 3 come quello dell'atto: ogni glifo disegna
    la lettera Helvetica, /Widths di 1 em per tutti, la crenatura del TJ le
    riporta alla distanza giusta e nessuna mappa Unicode. A video e' lo stesso
    testo di _sorgente() (stesse posizioni, quindi stessa verita' per
    OcrFinto), estratto e' senza senso, e per MuPDF ogni carattere e' largo
    quanto il corpo. Lo legge anche Vision (tests/macos)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    def oggetto(testo, stream=None):
        x = doc.get_new_xref()
        doc.update_object(x, testo)
        if stream is not None:
            doc.update_stream(x, stream)
        return x
    helv = oggetto("<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>")
    # codici 1, 2, 3... nell'ordine in cui compaiono, come nell'atto: niente
    # mappa Unicode, estratto e' una fila di caratteri di controllo
    lettere = sorted({c for r in righe for c in r})
    codice = {c: i + 1 for i, c in enumerate(lettere)}
    codici = sorted(codice.values())
    procs = {}
    for c in lettere:
        e = c.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        procs[codice[c]] = oggetto("<<>>", ("1 0 0 -0.3 1 1 d1 BT /H 1 Tf 0 0 Td (%s) Tj ET"
                                            % e).encode("latin-1"))
    font = oggetto(
        "<</Type/Font/Subtype/Type3/FontBBox[0 0 1 1]/FontMatrix[1 0 0 1 0 0]"
        "/CharProcs<<%s>>/Encoding<</Type/Encoding/Differences[%s]>>"
        "/FirstChar %d/LastChar %d/Widths[%s]/Resources<</Font<</H %d 0 R>>>>>>"
        % (" ".join("/g%d %d 0 R" % (k, x) for k, x in procs.items()),
           " ".join("%d/g%d" % (k, k) for k in codici),
           codici[0], codici[-1], " ".join(["1"] * (codici[-1] - codici[0] + 1)), helv))
    ops, y = [], 842 - 100
    for r in righe:
        parti = []
        for c in r:
            vero = fitz.get_text_length(c, fontname="helv", fontsize=1) * 1000
            parti.append("<%02x> %.1f" % (codice[c], 1000 - vero))
        ops.append("BT /T3 %.1f Tf 1 0 0 1 60 %.1f Tm [%s] TJ ET" % (corpo, y, " ".join(parti)))
        y -= 22
    contenuto = oggetto("<<>>", "\n".join(ops).encode("latin-1"))
    doc.xref_set_key(page.xref, "Contents", "%d 0 R" % contenuto)
    doc.xref_set_key(page.xref, "Resources", "<</Font<</T3 %d 0 R>>>>" % font)
    data = doc.tobytes()
    doc.close()
    return data


@unittest.skipIf(fitz is None, "PyMuPDF non disponibile")
class TestoVisibileIllegibile(unittest.TestCase):
    def _redigi(self, rotazione=0):
        data, truth, lettere = atto_type3(rotazione)
        with fitz.open(stream=data, filetype="pdf") as d:
            # il riquadro di un carattere e' largo quanto il corpo, non il segno
            ch = d[0].get_texttrace()[0]["chars"][0]
            self.assertGreater(ch[3][2] - ch[3][0], 9)
            # (poche lettere e nessuna immagine: il piano non la sceglierebbe,
            # nell'atto la sceglie la qualita' del testo; qui la si legge e basta)
            pagine = ocr.ocr_pages(d, [0], OcrFinto(truth))[0]
        self.assertIn("BETA", pagine[0].text)

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, OcrFinto(truth))[0]
        out, rep = pdf_export.redact_pdf(data, {"[ID_1]": "BETA"},
                                         ocr_pages=pagine, ocr_verify=rileggi)
        return data, out, rep, lettere

    def _controlla(self, data, out, rep, lettere):
        self.assertEqual(1, rep["occurrences"])
        self.assertEqual([], rep["residual"])
        for r in lettere["BETA"]:
            self.assertEqual(0, _pixel_neri(out, r))
        # TUTTE le lettere delle parole vicine restano, comprese l'ultima prima
        # della casella e la prima dopo ("Avanti me" non diventa "Avanti m")
        for parola in ("Avanti", "numero"):
            for k, r in enumerate(lettere[parola]):
                self.assertGreater(_pixel_neri(data, r), 0)
                self.assertGreater(_pixel_neri(out, r), 0, "%s, lettera %d" % (parola, k + 1))
        with fitz.open(stream=out, filetype="pdf") as d:
            self.assertEqual("[ID_1]", d[0].get_text().strip())   # niente testo illeggibile
        self.assertEqual([1], rep["flattened"])

    def test_casella_non_mangia_le_lettere_vicine(self):
        self._controlla(*self._redigi())

    def test_casella_non_mangia_le_lettere_vicine_ruotata(self):
        data, out, rep, lettere = self._redigi(rotazione=90)
        with fitz.open(stream=out, filetype="pdf") as d:
            self.assertEqual(90, d[0].rotation)
        self._controlla(data, out, rep, lettere)

    def test_lettere_vere_come_l_atto(self):
        """Stesso documento di _sorgente() ma in un Type 3 come quello dell'atto:
        il piano lo manda all'OCR, le caselle coprono IBAN ed email e le parole
        accanto restano intere ("IBAN" non diventa "IBA")."""
        data = testo_largo()
        src = _sorgente()
        truth = _verita(src[0])
        lettere = {}
        for b in src[0].get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    for ch in sp["chars"]:
                        lettere.setdefault(ch["c"], []).append(fitz.Rect(ch["bbox"]))
        parole = {w[4]: fitz.Rect(w[:4]) for w in src[0].get_text("words")}
        src.close()
        with fitz.open(stream=data, filetype="pdf") as d:
            res = ocr.analyze_doc(d, OcrFinto(truth))
        self.assertEqual({0: ocr.UNREADABLE}, res.plan)

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, OcrFinto(truth))[0]
        out, rep = pdf_export.redact_pdf(data, {"[IBAN_1]": IBAN, "[EMAIL_1]": EMAIL},
                                         ocr_pages=res.pages, ocr_verify=rileggi)
        self.assertEqual([], rep["residual"])
        self.assertEqual(0, _pixel_neri(out, parole[IBAN]))
        self.assertEqual(0, _pixel_neri(out, parole[EMAIL]))
        # l'ultima lettera prima di ogni casella: la "N" di IBAN, la "a" di "a"
        n = [r for r in lettere["N"] if parole["IBAN"].contains(r)][0]
        a = [r for r in lettere["a"] if r.y0 > parole[EMAIL].y0 - 2
             and r.x1 <= parole[EMAIL].x0][-1]
        for r in (n, a):
            self.assertGreater(_pixel_neri(data, r), 0)
            self.assertGreater(_pixel_neri(out, r), 0)
        with fitz.open(stream=out, filetype="pdf") as d:
            txt = d[0].get_text()
        self.assertIn("[IBAN_1]", txt)
        self.assertNotIn("\x01", txt)                 # il testo illeggibile e' uscito
        self.assertEqual([1], rep["flattened"])

    def test_casella_staccata_di_uno_spazio(self):
        """Riquadri dell'OCR larghi 3 pt in piu' per lato, come quelli di Vision:
        tra l'ultima lettera prima del dato e la casella deve restare bianco
        tutto lo spazio-lettera che c'era, e lo stesso tra la casella e la
        parola dopo. La casella copre comunque tutto il dato."""
        data = testo_largo()
        src = _sorgente()
        truth = _verita(src[0])
        parole = {w[4]: fitz.Rect(w[:4]) for w in src[0].get_text("words")}
        src.close()
        with fitz.open(stream=data, filetype="pdf") as d:
            res = ocr.analyze_doc(d, OcrFinto(truth, allarga=3 / 595))

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, OcrFinto(truth))[0]
        out, rep = pdf_export.redact_pdf(data, {"[IBAN_1]": IBAN, "[EMAIL_1]": EMAIL},
                                         ocr_pages=res.pages, ocr_verify=rileggi)
        self.assertEqual([], rep["residual"])
        for valore in (IBAN, EMAIL):
            w = parole[valore]
            prima, dato, dopo = _inchiostro(data, w)
            c0, c1 = _casella(out, w)
            spazio_sx, spazio_dx = dato[0] - prima, dopo - dato[1]
            self.assertGreater(spazio_sx, 2.0)          # c'e' davvero uno spazio
            # bianco tra la lettera prima e la casella: tutto lo spazio (0,3 pt
            # di tolleranza per i pixel) e la casella copre il dato
            self.assertGreaterEqual(c0 - prima, spazio_sx - 0.3, valore)
            self.assertGreaterEqual(dopo - c1, spazio_dx - 0.3, valore)
            self.assertLessEqual(c0, dato[0] + 0.3, valore)
            self.assertGreaterEqual(c1, dato[1] - 0.3, valore)
            # e la lettera prima e' intera
            self.assertEqual(_inchiostro(out, w)[0], prima)

    def test_senza_immagine_le_lettere_sparivano(self):
        """La causa, per memoria: redazione diretta sul testo Type 3."""
        data, truth, lettere = atto_type3()
        with fitz.open(stream=data, filetype="pdf") as d:
            p = d[0]
            beta = fitz.Rect(lettere["BETA"][0].x0 - 1, 0, lettere["BETA"][-1].x1 + 1, 842)
            beta = fitz.Rect(beta.x0, lettere["BETA"][0].y0 - 1, beta.x1, lettere["BETA"][0].y1 + 1)
            p.add_redact_annot(beta, fill=False)
            p.apply_redactions()
            out = d.tobytes()
        self.assertEqual(0, _pixel_neri(out, lettere["Avanti"][-1]))


@unittest.skipIf(APP is None or fitz is None, "app.py/PyMuPDF non disponibili")
class ScansioneHttp(unittest.TestCase):
    def setUp(self):
        self.data, self.truth = scansione()
        self.c = client()
        self.patches = [patch.object(APP, "nlp", lambda chunks: [[] for _ in chunks]),
                        patch.object(APP, "OCR_ENGINE", OcrFinto(self.truth))]
        for p in self.patches:
            p.start()
        APP._OCR_CACHE.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        APP._OCR_CACHE.clear()

    def _file(self):
        return {"file": (io.BytesIO(self.data), "atto_scansionato.pdf")}

    def test_preview_e_analyze_leggono_la_scansione(self):
        r = self.c.post("/preview", data=self._file(), content_type="multipart/form-data")
        d = r.get_json()
        self.assertEqual(200, r.status_code, d)
        self.assertIn(IBAN, d["text"])
        self.assertEqual([1], d["pages_ocr"])
        self.assertEqual([], d["pages_no_text"])
        r = self.c.post("/analyze", data=self._file(), content_type="multipart/form-data")
        d = r.get_json()
        self.assertEqual([1], d["pages_ocr"])
        self.assertIn("[IBAN_1]", d["anonymized_text"])
        self.assertNotIn(IBAN, d["anonymized_text"])

    def test_ocr_una_volta_sola_per_documento(self):
        eng = APP.OCR_ENGINE
        for route in ("/preview", "/analyze"):
            self.c.post(route, data=self._file(), content_type="multipart/form-data")
        self.assertEqual(1, eng.calls)

    def test_pdf_anonimizzato(self):
        r = self.c.post("/pdf", data=self._file(), content_type="multipart/form-data")
        self.assertEqual(200, r.status_code, r.data[:300])
        self.assertEqual("0", r.headers["X-PII-Residual"])
        self.assertEqual("1", r.headers["X-PII-OCR-Pages"])
        self.assertEqual("", r.headers["X-PII-OCR-Unverified"])
        self.assertEqual("", r.headers["X-PII-NoText-Pages"])
        with fitz.open(stream=r.data, filetype="pdf") as d:
            self.assertIn("[IBAN_1]", d[0].get_text())
        r = self.c.post("/pdf/preview", data=self._file(), content_type="multipart/form-data")
        d = r.get_json()
        self.assertEqual(0, d["residual"])
        self.assertEqual([1], d["pages_ocr"])
        self.assertEqual([], d["ocr_unverified"])

    def test_senza_ocr_resta_l_avviso(self):
        """Windows/Linux: nessun errore, la pagina e' segnalata come prima."""
        with patch.object(APP, "OCR_ENGINE", False):
            APP._OCR_CACHE.clear()
            r = self.c.post("/preview", data=self._file(), content_type="multipart/form-data")
            d = r.get_json()
            self.assertEqual(200, r.status_code)
            self.assertEqual([1], d["pages_no_text"])
            self.assertEqual([], d["pages_ocr"])
            r = self.c.post("/pdf", data=self._file(), content_type="multipart/form-data")
            self.assertEqual(400, r.status_code)     # nessun testo da anonimizzare


if __name__ == "__main__":
    unittest.main()
