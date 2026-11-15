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


class OcrFinto:
    """Legge una parola se il suo riquadro contiene ancora pixel neri."""

    def __init__(self, truth, spostamento=0.0):
        self.truth, self.dx, self.calls = truth, spostamento, 0

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
            words = [{"text": t, "box": [b[0] + self.dx, b[1], b[2], b[3]]}
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
