# -*- coding: utf-8 -*-
"""Etichette del modello incoerenti col valore: si CAMBIA l'etichetta, non si scarta
l'entita'. "[IBAN_1]" per un numero di pratica e' fuorviante per chi legge il testo
(e per l'LLM a cui lo si manda). Solo stringhe, nessun modello. Valori SINTETICI.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))

import detectors  # noqa: E402


def etichetta(text, valore, label):
    s = text.index(valore)
    ents = [{"label": label, "start": s, "end": s + len(valore), "score": 0.99,
             "validated": False, "source": "modello"}]
    detectors.fix_model_labels(ents, text)
    return ents[0]["label"]


class EtichetteDelModello(unittest.TestCase):
    CAMBIA = [
        ("Contratto n. 12345678901234 del", "12345678901234", "IBAN", "DOCID"),
        ("matricola 123456 assegnata", "123456", "BUILDINGNUM", "DOCID"),
        ("abita al civico 1234567 di", "1234567", "BUILDINGNUM", "DOCID"),
        ("il codice cliente 4455667788 e", "4455667788", "ID_DOC", "DOCID"),
        ("pratica 55667788991 aperta", "55667788991", "PIVA", "DOCID"),
        ("riferimento 1234567890123456 ok", "1234567890123456", "CREDITCARDNUMBER", "DOCID"),
    ]
    RESTA = [
        ("IBAN IT60X0542811101000000123456 qui", "IT60X0542811101000000123456", "IBAN"),
        ("sul conto 12345678 intestato", "12345678", "IBAN"),     # conto -> IBAN (tassonomia)
        ("Partita IVA 12345678901 della", "12345678901", "PIVA"),
        ("P.IVA 12345678903", "12345678903", "PIVA"),
        ("carta 4111 1111 1111 1111 scad", "4111 1111 1111 1111", "CREDITCARDNUMBER"),
        ("Via Roma 24", "24", "BUILDINGNUM"),
        ("civico 1024", "1024", "BUILDINGNUM"),
        ("patente n. U1A234567B", "U1A234567B", "ID_DOC"),
        ("carta d'identita' CA12345AB", "CA12345AB", "ID_DOC"),
    ]

    def test_cambia(self):
        for testo, valore, da, a in self.CAMBIA:
            with self.subTest(valore):
                self.assertEqual(etichetta(testo, valore, da), a)

    def test_resta(self):
        for testo, valore, label in self.RESTA:
            with self.subTest(valore):
                self.assertEqual(etichetta(testo, valore, label), label)

    def test_solo_modello(self):
        t = "Contratto n. 12345678901234 del"
        s = t.index("12345678901234")
        e = {"label": "IBAN", "start": s, "end": s + 14, "validated": False, "source": "regex"}
        detectors.fix_model_labels([e], t)
        self.assertEqual(e["label"], "IBAN")


if __name__ == "__main__":
    unittest.main()
