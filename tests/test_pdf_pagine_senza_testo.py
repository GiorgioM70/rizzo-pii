# -*- coding: utf-8 -*-
"""PDF misti: le pagine scansionate (immagini senza testo) vanno segnalate.

L'anonimizzazione lavora sul layer testuale e, senza OCR (Windows/Linux, o OCR
spento), le PII dentro una pagina immagine restano visibili anche nel PDF redatto. /preview, /analyze, /pdf/preview e
/pdf lo dicono (pages_no_text / X-PII-NoText-Pages) e l'UI mostra l'avviso.
Soglia (40 caratteri) ripresa dalla PR upstream #105.

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

import pdf_text  # noqa: E402
from test_ora import APP, client  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:                      # pragma: no cover
    fitz = None


class _Pag:
    def __init__(self, testo, immagini):
        self._t, self._i = testo, immagini

    def get_text(self):
        return self._t

    def get_images(self, full=False):
        return [(1,)] * self._i


class PagineSenzaTesto(unittest.TestCase):
    def test_duck_typed(self):
        doc = [_Pag("Contratto tra Mario Rossi e la ditta Alfa, firmato a Milano.", 0),
               _Pag("", 1),               # scansione
               _Pag("", 0),               # pagina bianca: niente da nascondere
               _Pag("  12  ", 2),         # solo il numero di pagina sopra la scansione
               _Pag("x" * 60, 1)]         # testo vero + logo
        self.assertEqual([2, 4], pdf_text.pages_without_text(doc))


def _pdf_misto():
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Il Sig. Mario Rossi, IBAN IT60X0542811101000000123456.")
    p2 = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
    pix.clear_with(200)
    p2.insert_image(fitz.Rect(72, 72, 300, 300), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


@unittest.skipIf(APP is None or fitz is None, "app.py/PyMuPDF non disponibili")
class PdfMistoHttp(unittest.TestCase):
    def setUp(self):
        self.c = client()
        self.p = patch.object(APP, "nlp", lambda chunks: [[] for _ in chunks])
        self.p.start()
        # qui si prova l'avviso SENZA OCR (Windows/Linux): su macOS il motore vero
        # leggerebbe la pagina. L'OCR ha i suoi test in test_ocr.py.
        self.po = patch.object(APP, "OCR_ENGINE", False)
        self.po.start()
        APP._OCR_CACHE.clear()
        self.data = _pdf_misto()

    def tearDown(self):
        self.p.stop()
        self.po.stop()
        APP._OCR_CACHE.clear()

    def _file(self):
        return {"file": (io.BytesIO(self.data), "misto.pdf")}

    def test_preview_e_analyze(self):
        for route in ("/preview", "/analyze", "/pdf/preview"):
            with self.subTest(route):
                r = self.c.post(route, data=self._file(), content_type="multipart/form-data")
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertEqual([2], r.get_json()["pages_no_text"])

    def test_pdf_header(self):
        r = self.c.post("/pdf", data=self._file(), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertEqual("2", r.headers["X-PII-NoText-Pages"])

    def test_testo_incollato_niente_pagine(self):
        r = self.c.post("/analyze", json={"text": "IBAN IT60X0542811101000000123456"})
        self.assertEqual([], r.get_json()["pages_no_text"])


if __name__ == "__main__":
    unittest.main()
