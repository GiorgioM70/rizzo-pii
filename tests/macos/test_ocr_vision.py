# -*- coding: utf-8 -*-
"""OCR VERO con Vision (solo macOS, con build/ocr/rizzo-ocr compilato).

Non sta nella discovery normale (cartella senza __init__.py): su Linux/Windows
risulterebbe "saltato" e la CI lo vieta. Lo lancia il workflow ocr-macos:
    swiftc -O -target "$(uname -m)-apple-macos11" -o build/ocr/rizzo-ocr src/app/ocr_macos/main.swift
    python tests/macos/test_ocr_vision.py

Stessa scansione sintetica di tests/test_ocr.py, ma a leggerla e' Vision: il
testo deve uscire giusto, l'IBAN e l'email coperti e la rilettura pulita.
Tutti i valori sono SINTETICI.
"""

import io
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

import pymupdf as fitz  # noqa: E402

import ocr  # noqa: E402
import pdf_export  # noqa: E402
from test_ocr import (EMAIL, IBAN, SPOSTATO, _casella, _inchiostro, _pixel_neri,  # noqa: E402
                      _sorgente, rect_non_ruotato, scansione, scansione_ruotata, testo_largo)
from test_ora import APP, client  # noqa: E402

ENGINE = ocr.default_engine()


def _rect(truth, parola):
    for riga in truth:
        for t, b in riga:
            if t == parola:
                return fitz.Rect(b[0] * 595, b[1] * 842, (b[0] + b[2]) * 595, (b[1] + b[3]) * 842)
    raise AssertionError(parola)


def _compatto(s):
    return "".join(s.split())


@unittest.skipIf(ENGINE is None, "serve macOS con build/ocr/rizzo-ocr")
class VisionVero(unittest.TestCase):
    def test_legge_la_scansione(self):
        data, _truth = scansione()
        with fitz.open(stream=data, filetype="pdf") as d:
            res = ocr.analyze_doc(d, ENGINE)
        self.assertEqual({}, res.failed)
        testo = res.pages[0].text
        print("\n--- testo letto da Vision ---\n" + testo)
        self.assertIn("TRIBUNALE", testo)
        self.assertIn(IBAN, _compatto(testo))
        self.assertIn(EMAIL, _compatto(testo))

    def test_pdf_anonimizzato_coperto_e_riletto(self):
        data, truth = scansione(strato=SPOSTATO[:400])
        c = client()
        with patch.object(APP, "nlp", lambda chunks: [[] for _ in chunks]), \
                patch.object(APP, "OCR_ENGINE", ENGINE):
            APP._OCR_CACHE.clear()
            r = c.post("/pdf", data={"file": (io.BytesIO(data), "atto.pdf")},
                       content_type="multipart/form-data")
        self.assertEqual(200, r.status_code, r.data[:300])
        print({k: v for k, v in r.headers.items() if k.startswith("X-PII")})
        self.assertEqual("0", r.headers["X-PII-Residual"])
        self.assertEqual("1", r.headers["X-PII-OCR-Pages"])
        self.assertEqual("", r.headers["X-PII-OCR-Unverified"])
        self.assertEqual(0, _pixel_neri(r.data, _rect(truth, IBAN)))
        self.assertEqual(0, _pixel_neri(r.data, _rect(truth, EMAIL)))
        with fitz.open(stream=r.data, filetype="pdf") as d:
            txt = d[0].get_text()
        self.assertNotIn("0542811101000000123456", _compatto(txt))
        self.assertIn("[IBAN_1]", txt)

    def test_pagina_ruotata(self):
        """Scansione con /Rotate 90 e immagine salvata di traverso: a video (e a
        Vision) l'atto si vede dritto, i riquadri devono tornare nelle
        coordinate non ruotate e coprire l'IBAN vero."""
        data, truth = scansione_ruotata()
        with fitz.open(stream=data, filetype="pdf") as d:
            res = ocr.analyze_doc(d, ENGINE)
        self.assertIn(IBAN, _compatto(res.pages[0].text))
        out, rep = pdf_export.redact_pdf(data, {"[IBAN_1]": IBAN},
                                         ocr_pages=res.pages)
        self.assertEqual(1, rep["occurrences"])
        iban = rect_non_ruotato(data, _rect(truth, IBAN))
        self.assertGreater(_pixel_neri(data, iban), 0)
        self.assertEqual(0, _pixel_neri(out, iban))

    def test_testo_type3_come_l_atto(self):
        """Testo VISIBILE in un Type 3 senza mappa Unicode e con i caratteri
        larghi quanto il corpo, come l'atto: la casella sopra un dato non deve
        portarsi via le lettere prima ("IBAN" restava "IBA"). Lo verifica Vision
        rileggendo il PDF in uscita."""
        data = testo_largo()

        def rileggi(out, idx):
            with fitz.open(stream=out, filetype="pdf") as d:
                return ocr.ocr_pages(d, idx, ENGINE)[0]
        with fitz.open(stream=data, filetype="pdf") as d:
            res = ocr.analyze_doc(d, ENGINE)
        self.assertEqual({0: ocr.UNREADABLE}, res.plan)
        self.assertIn(IBAN, _compatto(res.pages[0].text))
        out, rep = pdf_export.redact_pdf(data, {"[IBAN_1]": IBAN, "[EMAIL_1]": EMAIL},
                                         ocr_pages=res.pages, ocr_verify=rileggi)
        self.assertEqual([], rep["residual"])
        self.assertEqual([], rep["ocr_unverified"])
        self.assertEqual([1], rep["flattened"])
        letto = rileggi(out, [0])[0].text
        print("\n--- PDF in uscita riletto da Vision ---\n" + letto)
        self.assertNotIn(IBAN, _compatto(letto))
        self.assertNotIn(EMAIL, _compatto(letto))
        # le parole attorno alle caselle intere; maiuscola/minuscola no: dopo
        # una casella Vision puo' prendere "intestato" per un inizio di frase
        for parola in ("conto", "IBAN", "intestato", "scrivere", "a", "entro"):
            self.assertRegex(letto, re.compile(r"(?<!\w)%s(?!\w)" % re.escape(parola),
                                               re.IGNORECASE))
        # con i riquadri veri di Vision: tra l'ultima lettera prima del dato e
        # la casella resta bianco tutto lo spazio-lettera, e lo stesso dopo
        src = _sorgente()
        parole = {w[4]: fitz.Rect(w[:4]) for w in src[0].get_text("words")}
        src.close()
        for valore in (IBAN, EMAIL):
            w = parole[valore]
            prima, dato, dopo = _inchiostro(data, w)
            c0, c1 = _casella(out, w)
            print("%s: spazio prima %.2f pt -> bianco %.2f pt; dopo %.2f -> %.2f"
                  % (valore[:8], dato[0] - prima, c0 - prima, dopo - dato[1], dopo - c1))
            self.assertGreaterEqual(c0 - prima, dato[0] - prima - 0.3, valore)
            self.assertGreaterEqual(dopo - c1, dopo - dato[1] - 0.3, valore)
            self.assertLessEqual(c0, dato[0] + 0.3, valore)
            self.assertGreaterEqual(c1, dato[1] - 0.3, valore)

if __name__ == "__main__":
    unittest.main(verbosity=2)
