# -*- coding: utf-8 -*-
"""Numerazione dei segnaposto per sessione.

Con lo stesso session_id due analisi consecutive non si danno lo stesso numero per
valori diversi: [IBAN_2] per il secondo IBAN, [IBAN_1] di nuovo per quello gia'
visto. /pdf/preview riusa la stessa numerazione. Senza session_id l'API resta quella
di prima (ogni analisi da _1). Lo stato del server non contiene i valori.

Tutti i valori sono SINTETICI.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_ora import APP, client  # noqa: E402

IBAN_A = "IT60X0542811101000000123456"
IBAN_B = "IT51X0300203280000400162854"   # checksum valido, sintetico
SID = "sessione_test_123"


@unittest.skipIf(APP is None, "app.py non importabile (flask/fitz assenti)")
class SessioneSegnaposto(unittest.TestCase):
    def setUp(self):
        self.c = client()
        APP.reset_session(SID)
        self.p = patch.object(APP, "nlp", lambda chunks: [[] for _ in chunks])
        self.p.start()

    def tearDown(self):
        self.p.stop()
        APP.reset_session(SID)

    def _an(self, text, **extra):
        r = self.c.post("/analyze", json={"text": text, **extra})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_iban_di_controllo_valido(self):
        self.assertTrue(APP.iban_ok(IBAN_B))

    def test_numerazione_continua(self):
        a = self._an(f"Primo IBAN {IBAN_A}", session_id=SID)
        b = self._an(f"Secondo IBAN {IBAN_B}, poi di nuovo {IBAN_A}", session_id=SID)
        self.assertEqual(a["mapping"], {"[IBAN_1]": IBAN_A})
        self.assertEqual(b["mapping"], {"[IBAN_2]": IBAN_B, "[IBAN_1]": IBAN_A})
        self.assertEqual(b["anonymized_text"], "Secondo IBAN [IBAN_2], poi di nuovo [IBAN_1]")
        self.assertEqual(b["n_unique"], 2)

    def test_senza_sessione_come_prima(self):
        self._an(f"IBAN {IBAN_A}")
        b = self._an(f"IBAN {IBAN_B}")
        self.assertEqual(b["mapping"], {"[IBAN_1]": IBAN_B})

    def test_reset_riparte_da_uno(self):
        self._an(f"IBAN {IBAN_A}", session_id=SID)
        r = self.c.post("/session/reset", json={"session_id": SID})
        self.assertTrue(r.get_json()["existed"])
        b = self._an(f"IBAN {IBAN_B}", session_id=SID)
        self.assertEqual(b["mapping"], {"[IBAN_1]": IBAN_B})

    def test_pdf_stessa_numerazione(self):
        self._an(f"IBAN {IBAN_A}", session_id=SID)
        testo = f"Bonifico su {IBAN_B}"
        self._an(testo, session_id=SID)
        r = self.c.post("/pdf/preview", json={"text": testo, "session_id": SID})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = APP._get_doc(r.get_json()["doc_id"])
        with APP.fitz.open(stream=d["pdf"], filetype="pdf") as doc:
            out = "".join(p.get_text() for p in doc)
        self.assertIn("[IBAN_2]", out)
        self.assertNotIn(IBAN_B, out)

    def test_stato_senza_valori_in_chiaro(self):
        self._an(f"IBAN {IBAN_A}", session_id=SID, include_mapping=False)
        self.assertNotIn(IBAN_A.casefold(), repr(APP._SESSIONS[SID]).casefold())

    def test_session_id_non_valido(self):
        r = self.c.post("/analyze", json={"text": "x", "session_id": "../a b"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/session/reset", json={"session_id": 3})
        self.assertEqual(r.status_code, 400)

    def test_lru_limitata(self):
        for i in range(APP.MAX_SESSIONS + 5):
            APP.analyze(f"IBAN {IBAN_A}", session_id=f"lru_sess_{i:04d}")
        self.assertLessEqual(len(APP._SESSIONS), APP.MAX_SESSIONS)
        for i in range(APP.MAX_SESSIONS + 5):
            APP.reset_session(f"lru_sess_{i:04d}")


if __name__ == "__main__":
    unittest.main()
