# -*- coding: utf-8 -*-
"""Tre miglioramenti segnalati su due atti reali (valori qui SINTETICI):

1. Intestazione con le lettere SPAZIATE ("A V V . M A R I O R O S S I"): PyMuPDF
   restituisce una lettera per volta e perde i confini di parola, il modello non
   vede il nome e il nome dell'avvocato restava in chiaro.
2. Cognome in MAIUSCOLO davanti al nome ("COGNOME Nome, nato a ..."): un cognome
   raro il modello lo salta, o lo scambia per una citta' se somiglia a un toponimo.
3. Etichette sulle caselle del PDF: impostazione per lasciare solo il rettangolo.

Nessun modello. Il PDF serve PyMuPDF (senza, quei test si saltano).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

import detectors  # noqa: E402
import pdf_text  # noqa: E402
import server_config  # noqa: E402

try:
    import pymupdf as fitz
    import pdf_export
except ImportError:                      # pragma: no cover
    fitz = None


def _pdf_spaziato():
    """Pagina con intestazione a lettere spaziate (operatore Tc) + testo normale."""
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((50, 50), "x", fontname="helv", fontsize=10)      # registra il font
    stream = (b"BT /helv 14 Tf 3 Tc 1 0 0 1 150 700 Tm (STUDIO LEGALE) Tj ET\n"
              b"BT /helv 14 Tf 3 Tc 1 0 0 1 150 670 Tm (AVV. BRUNELLO FARNESI) Tj ET\n"
              b"BT /helv 14 Tf 3 Tc 1 0 0 1 150 640 Tm (Patrocinante in Cassazione) Tj ET\n"
              b"BT /helv 12 Tf 0 Tc 1 0 0 1 50 600 Tm "
              b"(Elenco: 1 2 3 4 5 e a b c d. Gentile Collega, in data odierna.) Tj ET\n")
    doc.update_stream(pg.get_contents()[0], stream)
    return doc.tobytes()


@unittest.skipIf(fitz is None, "PyMuPDF non installato")
class LettereSpaziate(unittest.TestCase):
    def setUp(self):
        self.doc = fitz.open(stream=_pdf_spaziato(), filetype="pdf")

    def test_get_text_perde_le_parole_e_page_text_le_ricostruisce(self):
        # il difetto di partenza (se PyMuPDF cambiasse, il test lo dice)
        self.assertIn("B R U N E L L O", self.doc[0].get_text())
        t = pdf_text.page_text(self.doc[0])
        self.assertIn("STUDIO LEGALE", t)
        self.assertIn("AVV. BRUNELLO FARNESI", t)
        self.assertIn("Patrocinante in Cassazione", t)

    def test_il_testo_normale_non_si_tocca(self):
        t = pdf_text.page_text(self.doc[0])
        self.assertIn("1 2 3 4 5 e a b c d.", t)          # elenco: niente falsa fusione
        self.assertIn("Gentile Collega, in data odierna.", t)

    def test_pagina_normale_identica_a_get_text(self):
        doc = fitz.open()
        doc.new_page().insert_text((50, 80), "Il sig. Mario Rossi abita a Roma.", fontsize=11)
        self.assertEqual(pdf_text.page_text(doc[0]), doc[0].get_text())

    def test_il_nome_si_redige_nel_pdf(self):
        out, rep = pdf_export.redact_pdf(
            _pdf_spaziato(), {"[FULLNAME_1]": "BRUNELLO FARNESI"})
        self.assertEqual(rep["residual"], [])
        self.assertEqual(rep["occurrences"], 1)
        pagina = fitz.open(stream=out, filetype="pdf")[0]
        txt = pdf_text.page_text(pagina)                 # con le parole ricostruite
        grezzo = pagina.get_text()
        for visto in (txt, grezzo):
            self.assertNotIn("BRUNELLO", visto)
            self.assertNotIn("B R U N", visto)
            self.assertNotIn("F A R N E S I", visto)
        self.assertIn("STUDIO LEGALE", txt)               # il resto dell'intestazione resta

    def test_despace_line_parole(self):
        # tre lettere per parola, scarto 3 pt fra le lettere e 10 fra le parole
        def riga(testo):
            x, items = 0.0, []
            for ch in testo:
                if ch == " ":
                    x += 7.0
                    continue
                items.append((ch, (x, 0.0, x + 8.0, 10.0)))
                items.append((" ", None))                  # lo spazio sintetico
                x += 11.0
            return items[:-1]
        out = pdf_text.despace_line(riga("MARIO ROSSI AVV"))
        self.assertEqual("".join(c for c, _ in out), "MARIO ROSSI AVV")


class CognomeInMaiuscolo(unittest.TestCase):
    @staticmethod
    def _ent(t, v, label, src="modello"):
        s = t.index(v)
        return {"label": label, "start": s, "end": s + len(v), "validated": False,
                "source": src}

    def _vai(self, t, *ents):
        out = detectors.extend_caps_surname(list(ents), t)
        return [(e["label"], t[e["start"]:e["end"]]) for e in out]

    def test_cognome_saltato_dal_modello(self):
        t = "VARGHERA \nTeresa, nata a Roma il 3 marzo 1960"
        self.assertEqual(self._vai(t, self._ent(t, "Teresa", "FULLNAME")),
                         [("FULLNAME", "VARGHERA \nTeresa")])

    def test_cognome_scambiato_per_citta(self):
        t = "BRAMBILLA\nGiovanni, nato a Roma"
        self.assertEqual(
            self._vai(t, self._ent(t, "BRAMBILLA", "CITY"), self._ent(t, "Giovanni", "FULLNAME")),
            [("FULLNAME", "BRAMBILLA\nGiovanni")])

    def test_nome_poi_cognome(self):
        t = "Mario ROSSI, nato a Roma"
        self.assertEqual(self._vai(t, self._ent(t, "Mario", "FULLNAME")),
                         [("FULLNAME", "Mario ROSSI")])

    def test_senza_la_forma_della_parte_non_si_tocca(self):
        for t, v in (("RICORRENTE Franco Neri contro", "Franco Neri"),
                     ("TRIBUNALE\nFranco Neri, nato a Roma", "Franco Neri"),
                     ("CONTRO Franco Neri, nato a Roma", "Franco Neri"),
                     ("IL GIUDICE Franco Neri ha deciso", "Franco Neri")):
            with self.subTest(t):
                self.assertEqual(self._vai(t, self._ent(t, v, "FULLNAME")), [("FULLNAME", v)])

    def test_una_citta_vera_resta_citta(self):
        t = "nato a ROMA\nMario Rossi, residente in Milano"
        self.assertEqual(
            self._vai(t, self._ent(t, "ROMA", "CITY"), self._ent(t, "Mario Rossi", "FULLNAME")),
            [("CITY", "ROMA"), ("FULLNAME", "Mario Rossi")])


class OrgGenerica(unittest.TestCase):
    def test_studio_legale_da_solo_non_e_una_ragione_sociale(self):
        t = "STUDIO LEGALE e poi Studio Legale Gallo"
        ents = [{"label": "ORG", "start": 0, "end": 13, "source": "modello", "validated": False},
                {"label": "ORG", "start": t.index("Studio Legale Gallo"), "end": len(t), "source": "modello",
                 "validated": False}]
        out = detectors.drop_generic_org(ents, t)
        self.assertEqual([t[e["start"]:e["end"]] for e in out], ["Studio Legale Gallo"])


@unittest.skipIf(fitz is None, "PyMuPDF non installato")
class EtichetteSulPdf(unittest.TestCase):
    def _pdf(self):
        doc = fitz.open()
        doc.new_page().insert_text((50, 80), "Il sig. Mario Rossi abita a Roma.", fontsize=11)
        return doc.tobytes()

    MAP = {"[FULLNAME_1]": "Mario Rossi", "[CITY_1]": "Roma"}

    def test_con_etichette(self):
        out, rep = pdf_export.redact_pdf(self._pdf(), self.MAP)
        txt = fitz.open(stream=out, filetype="pdf")[0].get_text()
        self.assertIn("[FULLNAME_1]", txt)
        self.assertIn("[CITY_1]", txt)

    def test_solo_rettangoli(self):
        out, rep = pdf_export.redact_pdf(self._pdf(), self.MAP, labels=False)
        pg = fitz.open(stream=out, filetype="pdf")[0]
        self.assertNotIn("[", pg.get_text())
        self.assertNotIn("Mario", pg.get_text())          # la rimozione resta vera
        self.assertEqual(rep["residual"], [])
        self.assertEqual(rep["occurrences"], 2)
        viola = [d for d in pg.get_drawings() if d.get("fill") and
                 abs(d["fill"][0] - pdf_export.REDACT_FILL[0]) < 0.01]
        self.assertGreaterEqual(len(viola), 2)            # i rettangoli ci sono


class ImpostazioneEtichette(unittest.TestCase):
    def test_salvataggio_e_default(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(server_config, "config_dir", return_value=Path(d)), \
                patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PII_PDF_LABELS", None)
            self.assertTrue(server_config.load_pdf_labels())          # default: acceso
            server_config.save_prefs(excluded_tags=["AGE"])
            self.assertFalse(server_config.save_pdf_labels(False))
            self.assertFalse(server_config.load_pdf_labels())
            data = json.loads((Path(d) / "prefs.json").read_text("utf-8"))
            self.assertEqual(data["excluded_tags"], ["AGE"])           # le altre chiavi restano
            self.assertFalse(data["pdf_labels"])
            os.environ["PII_PDF_LABELS"] = "1"                         # env vince sul file
            self.assertTrue(server_config.load_pdf_labels())
            os.environ.pop("PII_PDF_LABELS")

    def test_api_settings(self):
        from test_ora import APP, client
        if APP is None:
            self.skipTest("app.py non importabile")
        with tempfile.TemporaryDirectory() as d, \
                patch.object(server_config, "config_dir", return_value=Path(d)):
            c = client()
            h = {"Host": "127.0.0.1:5005"}
            vecchio = APP.PDF_LABELS
            try:
                self.assertIn("pdf_labels", c.get("/settings", headers=h).get_json())
                r = c.post("/settings", json={"pdf_labels": False}, headers=h)
                self.assertEqual(r.status_code, 200)
                self.assertFalse(c.get("/settings", headers=h).get_json()["pdf_labels"])
            finally:
                APP.PDF_LABELS = vecchio


if __name__ == "__main__":
    unittest.main()
