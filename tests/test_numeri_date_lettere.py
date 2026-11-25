# -*- coding: utf-8 -*-
"""Numeri e date scritti IN LETTERE, e le forme a cifre che il modello taglia.

Il modello etichettava "maggio" di "quindici maggio millenovecentoottanta" e non
vedeva per niente "quindicimila/00". Qui si prova la rete regex (`words_it` +
`detectors`) senza modello: solo stringhe. Valori SINTETICI.

Regola che i test difendono: una parola-numero da sola NON e' una PII
("tre rate", "sei mesi"); serve l'ancora (mese, euro, "/00").
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))

import detectors  # noqa: E402
import words_it  # noqa: E402


def trovati(text, label):
    return [text[e["start"]:e["end"]]
            for e in detectors.detect_regex(text) if e["label"] == label]


class ParserNumeri(unittest.TestCase):
    VALORI = {
        "tre": 3, "ventuno": 21, "ventotto": 28, "centotto": 108, "duecento": 200,
        "novecentonovantanove": 999, "mille": 1000, "duemila": 2000,
        "duemilaventitre": 2023, "millenovecentoottanta": 1980,
        "quindicimila": 15000, "duecentocinquantamila": 250000,
        "un milione": 10 ** 6, "due milioni": 2 * 10 ** 6,
    }

    def _valore(self, s):
        ws = words_it._words(s)
        return words_it._value(ws, 0, len(ws) - 1)

    def test_valori(self):
        for testo, atteso in self.VALORI.items():
            with self.subTest(testo):
                self.assertEqual(self._valore(testo), atteso)

    def test_non_numeri_o_malformati(self):
        for testo in ("cento due due", "venti venti", "casa", "mila", "tre venti"):
            with self.subTest(testo):
                self.assertIsNone(self._valore(testo))


class DateInLettere(unittest.TestCase):
    RICONOSCIUTE = [
        ("nato il quindici maggio millenovecentoottanta a Torino",
         "quindici maggio millenovecentoottanta"),
        ("pagato il dodici giugno duemilaventitre.", "dodici giugno duemilaventitre"),
        ("Roma, lì ventotto febbraio duemilaventiquattro.",
         "ventotto febbraio duemilaventiquattro"),
        ("il giorno otto del mese di marzo dell'anno duemilaventi",
         "otto del mese di marzo dell'anno duemilaventi"),
        ("scade il primo gennaio.", "primo gennaio"),
        ("Roma, addì 1° marzo 2024.", "1° marzo 2024"),
        ("nell'anno millenovecentosettantacinque", "millenovecentosettantacinque"),
        ("firmato a marzo 2024 presso lo studio", "marzo 2024"),
    ]
    # non sono date: parola-numero senza ancora, o mese senza giorno ne' anno
    IGNORATE = [
        "Entro tre giorni dalla notifica, sei mesi dopo, venti giorni.",
        "Il mese di giugno e' il piu' caldo.",
        "Tre rate da mille persone.",
        "Mila Rossi ha firmato.",
    ]

    def test_riconosce(self):
        for testo, atteso in self.RICONOSCIUTE:
            with self.subTest(atteso):
                self.assertIn(atteso, trovati(testo, "DATE"))

    def test_ignora(self):
        for testo in self.IGNORATE:
            with self.subTest(testo):
                self.assertEqual(trovati(testo, "DATE"), [])

    def test_anno_a_due_cifre_intero(self):
        # il modello vede "3/24" e lasciava "1/" in chiaro
        self.assertEqual(trovati("Roma, 1/3/24.", "DATE"), ["1/3/24"])
        self.assertEqual(trovati("il 05-06-25 alle", "DATE"), ["05-06-25"])

    def test_anno_a_due_cifre_non_prende_numerazioni(self):
        self.assertEqual(trovati("vedi par. 1.2.24 e la v. 3.4.56", "DATE"), [])


class ImportiInLettere(unittest.TestCase):
    RICONOSCIUTI = [
        ("pattuito in euro quindicimila/00 da pagare", "euro quindicimila/00"),
        ("somma di (quindicimila/00) euro", "quindicimila/00"),
        ("pari a quindicimilaeuro oltre IVA", "quindicimilaeuro"),
        ("Importo: Euro duecentocinquantamila virgola cinquanta oltre IVA",
         "Euro duecentocinquantamila virgola cinquanta"),
        ("versa tremila euro subito", "tremila euro"),
        ("versa 3 mila euro subito", "3 mila euro"),
        ("capitale di 2,5 milioni di euro", "2,5 milioni di euro"),
        ("un compenso di mille euro", "mille euro"),
        ("sei euro e cinquanta centesimi", "sei euro e cinquanta centesimi"),
        ("rate da mille euro, duemilacinquecento e cinquecento euro",
         "mille euro"),
    ]
    IGNORATI = [
        "Tre rate da pagare in sei mesi.",
        "Ci sono venti persone e sei sedie.",
        "Il neuro mille e cinque pazienti.",
        "Sono passati tre giorni, mille grazie.",
    ]

    def test_riconosce(self):
        for testo, atteso in self.RICONOSCIUTI:
            with self.subTest(atteso):
                self.assertIn(atteso, trovati(testo, "AMOUNT"))

    def test_ignora(self):
        for testo in self.IGNORATI:
            with self.subTest(testo):
                self.assertEqual(trovati(testo, "AMOUNT"), [])

    def test_euro_e_centesimi_sono_un_solo_importo(self):
        self.assertEqual(trovati("sei euro e cinquanta centesimi", "AMOUNT"),
                         ["sei euro e cinquanta centesimi"])


class CifreDetteUnaPerUna(unittest.TestCase):
    def test_telefono_con_contesto(self):
        t = "Il numero di telefono e' tre tre tre uno due tre quattro cinque sei sette."
        self.assertEqual(trovati(t, "TELEPHONENUM"),
                         ["tre tre tre uno due tre quattro cinque sei sette"])

    def test_poche_cifre_non_bastano(self):
        self.assertEqual(trovati("tre tre uno due", "TELEPHONENUM"), [])
        self.assertEqual(trovati("tre tre uno due", "DOCID"), [])


class CodiciLunghi(unittest.TestCase):
    def test_codice_dopo_la_sua_parola(self):
        t = "Il numero di pratica 20240001234567 e il codice cliente 4455667788."
        self.assertEqual(trovati(t, "DOCID"), ["20240001234567", "4455667788"])

    def test_n_punto(self):
        self.assertEqual(trovati("Contratto n. 12345678901234 del", "DOCID"),
                         ["12345678901234"])

    def test_numero_lungo_senza_ancora(self):
        self.assertEqual(trovati("ref 123456789012345 ok", "DOCID"), ["123456789012345"])

    def test_negativi(self):
        for t in ("Art. 1234 del codice civile.", "nel 2024 pagina 123456",
                  "cap 00185 Roma", "importo 1500000 oppure"):
            with self.subTest(t):
                self.assertEqual(trovati(t, "DOCID"), [])

    def test_il_telefono_vince_sul_codice(self):
        t = "numero 3331234567"
        self.assertEqual(trovati(t, "TELEPHONENUM"), ["3331234567"])


class FirmatarioDopoLaDicitura(unittest.TestCase):
    def test_nome_dopo_la_dicitura(self):
        for t, atteso in (("F.to Giulio Verdoni", "Giulio Verdoni"),
                          ("firmato da: Dott. Anna Rinaldi Tesi", "Anna Rinaldi Tesi"),
                          ("In fede, Mario ROSSI", "Mario ROSSI"),
                          ("f.to M. Rossi", "M. Rossi"),
                          ("Firma: Giulio Verdoni", "Giulio Verdoni"),
                          ("Firma Giulio Verdoni", "Giulio Verdoni"),
                          ("F.to Verdoni", "Verdoni")):
            with self.subTest(t):
                self.assertEqual(trovati(t, "FULLNAME"), [atteso])

    def test_non_e_un_nome(self):
        for t in ("Firma digitale qualificata", "Firma del legale rappresentante",
                  "Firma leggibile ______", "Firma ______________",
                  "Firma Cliente", "Firma Contraente", "Firma Autorizzata",
                  "Firma Responsabile", "Firma Assicurato", "Firma Legale Rappresentante"):
            with self.subTest(t):
                self.assertEqual(trovati(t, "FULLNAME"), [])


class UnioneStessaEtichetta(unittest.TestCase):
    """Il modello copre un pezzo, la regex la forma intera: vince una sola, ma
    entrambe diventano l'unione (altrimenti resta un pezzo in chiaro)."""

    def _cand(self, label, s, e, src, union=False):
        return {"label": label, "start": s, "end": e, "score": 0.99 if src == "modello" else 0.9,
                "validated": False, "source": src, "union": union}

    def test_unione_sovrapposta(self):
        modello = self._cand("DATE", 4, 9, "modello")
        regex = self._cand("DATE", 2, 9, "regex", union=True)
        detectors.extend_same_label([modello, regex])
        self.assertEqual((modello["start"], modello["end"]), (2, 9))

    def test_altro_tipo_non_si_tocca(self):
        a = self._cand("TIME", 0, 5, "modello")
        b = self._cand("DATE", 3, 9, "regex", union=True)
        detectors.extend_same_label([a, b])
        self.assertEqual((a["start"], a["end"]), (0, 5))

    def test_adiacenti_restano_separati(self):
        a = self._cand("AMOUNT", 0, 5, "modello")
        b = self._cand("AMOUNT", 5, 9, "regex", union=True)
        detectors.extend_same_label([a, b])
        self.assertEqual((a["start"], a["end"]), (0, 5))

    def test_parentesi_spaiata_si_chiude(self):
        t = "somma euro 1 (uno/00) fine"
        a = self._cand("AMOUNT", 6, 17, "modello")
        b = self._cand("AMOUNT", 13, 20, "regex", union=True)
        detectors.extend_same_label([a, b])
        detectors.balance_parens([a], t)
        self.assertEqual(t[a["start"]:a["end"]], "euro 1 (uno/00)")


if __name__ == "__main__":
    unittest.main()
