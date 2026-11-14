# -*- coding: utf-8 -*-
"""Telefoni: gruppi, parentesi, prefissi esteri (src/app/detectors.py, TELEPHONENUM).

Ogni caso e' un numero come lo si trova stampato. Tutti i numeri sono SINTETICI.
Il negativo conta quanto il positivo: date, rinvii a pagine, CAP, importi e orari
non devono diventare telefoni.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))

import detectors  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_ora import APP  # noqa: E402


def tel(text):
    return [text[e["start"]:e["end"]] for e in detectors.detect_regex(text)
            if e["label"] == "TELEPHONENUM"]


POSITIVI = [
    # gia' coperti prima: non devono regredire
    ("Cell. 333 123 4567 per contatti.", "333 123 4567"),
    ("Cell. 3331234567.", "3331234567"),
    ("Cell. +39-333-123-4567 per contatti.", "+39-333-123-4567"),
    ("Tel. 010-2471234 per contatti.", "010-2471234"),
    ("Tel. 010.2471234 per contatti.", "010.2471234"),
    # gruppi
    ("Cell. 333 12 34 567, ore ufficio.", "333 12 34 567"),
    ("Cell. 347 1234 567.", "347 1234 567"),
    ("Tel. 02 1234 5678 (centralino).", "02 1234 5678"),
    ("Tel. 011 123 45 67.", "011 123 45 67"),
    ("Tel. 06 12 34 56 78.", "06 12 34 56 78"),
    # parentesi
    ("Tel. (011) 1234567.", "(011) 1234567"),
    ("Tel. (011) 123 45 67.", "(011) 123 45 67"),
    ("Cell. (+39) 333 1234567.", "(+39) 333 1234567"),
    ("Tel. (39) 011 1234567.", "(39) 011 1234567"),
    # prefissi
    ("Cell. 0039 333 1234567.", "0039 333 1234567"),
    ("Tel. +39 011 1234567.", "+39 011 1234567"),
    ("Tel. +41 79 123 45 67 (Svizzera).", "+41 79 123 45 67"),
    ("Tel. +44 20 7946 0958.", "+44 20 7946 0958"),
    ("Tel. 0044 20 7946 0958.", "0044 20 7946 0958"),
    ("Phone +1 (555) 123-4567.", "+1 (555) 123-4567"),
    ("Tel. +33 1 23 45 67 89.", "+33 1 23 45 67 89"),
    ("Tel. +49 30 12345678.", "+49 30 12345678"),
]

NEGATIVI = [
    "Provvedimento emesso il 12.03.2024 dal collegio.",
    "Udienze del 01.02.2024 e 03.04.2025, rinviate.",
    "Termine al 03-04-2025, poi al 30-09-25.",
    "Cfr. pagg. 12-15 e 20-24 della memoria.",
    "Cfr. pagg. 312-315.",
    "Sede in 00185 Roma, via Nazionale.",
    "Udienza alle ore 09.30 in aula 3.",
    "Importo di euro 1.234,56 da versare.",
    "Art. 2043 c.c. e art. 2059 c.c.",
    "Numero di ruolo 3 del 2024.",
    "+39 a ogni costo",
    "Il 3 33 volte.",
]


class Telefoni(unittest.TestCase):
    def test_positivi(self):
        for testo, atteso in POSITIVI:
            with self.subTest(testo):
                self.assertEqual([atteso], tel(testo))

    def test_negativi(self):
        for testo in NEGATIVI:
            with self.subTest(testo):
                self.assertEqual([], tel(testo))

    def test_niente_a_capo_tra_i_gruppi(self):
        # una colonna di numeri in tabella non e' un telefono
        self.assertEqual([], tel("Totale\n333\n123\n4567"))

    def test_lunghezze_italiane(self):
        self.assertEqual([], tel("Cell. 333 1234."))          # troppo corto
        self.assertEqual([], tel("Cell. 333 1234 5678 90."))  # troppo lungo

    @unittest.skipIf(APP is None, "app.py non importabile")
    def test_dentro_iban_e_piva_vince_il_checksum(self):
        # la forma a gruppi vede un "telefono" dentro l'IBAN stampato e nella P.IVA:
        # in fusione vince il candidato validato e il valore resta uno solo
        for testo, label in (("IBAN IT60 X054 2811 1010 0000 0123 456", "IBAN"),
                             ("P.IVA 00743110157", "PIVA")):
            with self.subTest(testo):
                kept = APP._merge(detectors.detect_regex(testo), testo)
                self.assertEqual([label], [e["label"] for e in kept])


if __name__ == "__main__":
    unittest.main()
