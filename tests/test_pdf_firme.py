# -*- coding: utf-8 -*-
"""Le FIRME che non sono testo vengono tolte dal PDF.

Il nome sotto "F.to" lo trova il modello; la firma vera (immagine, tratto a mano
libera, timbro, campo /Sig, tracciato vettoriale) non sta nel layer testuale e
`redact_pdf` per valore non la toccava. Si rimuove per FORMA, ma solo vicino a una
dicitura di firma: un logo lontano resta.

Serve PyMuPDF; senza, il test si salta. Nessun modello. Valori SINTETICI.

La firma DIGITALE vera (PAdES) ha il nome del firmatario in /V /Name, il certificato
(col codice fiscale) in /Contents, e spesso /DSS e /Perms nel catalogo: cancellare il
widget non basta, e se il campo e' un "figlio" il padre resta in /Fields col suo /V.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))

try:
    import pymupdf as fitz
    import pdf_export
except ImportError:                      # pragma: no cover
    fitz = None


def _pdf(con_firme=True, logo=True, tabella=False):
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((50, 60), "Contratto tra Mario Rossi e Luigi Bianchi.", fontsize=11)
    pg.insert_text((50, 500), "Il Venditore", fontsize=11)
    pg.insert_text((330, 500), "Il Compratore", fontsize=11)
    if tabella:   # un bordo di tabella accanto a un nome: non deve sparire
        pg.draw_rect(fitz.Rect(40, 40, 400, 70), color=(0, 0, 0), width=0.8)
    if con_firme:
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 160, 50), False)
        pix.set_rect(pix.irect, (0, 0, 120))
        pg.insert_image(fitz.Rect(50, 505, 200, 550), pixmap=pix)        # raster
        sh = pg.new_shape()
        p0 = fitz.Point(330, 530)
        for _ in range(6):                                                # curve
            sh.draw_bezier(p0, p0 + (10, -25), p0 + (20, 25), p0 + (30, 0))
            p0 = p0 + (30, 0)
        sh.finish(color=(0, 0, 0.5), fill=None, width=1.2)
        sh.commit()
        pg.add_ink_annot([[(60, 700), (90, 680), (120, 705), (150, 690)]]).update()
        pg.add_stamp_annot(fitz.Rect(300, 660, 400, 700), stamp=fitz.STAMP_Approved).update()
        w = fitz.Widget()
        w.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
        w.field_name = "Firma1"
        w.rect = fitz.Rect(300, 720, 480, 770)
        pg.add_widget(w)
    if logo:
        pix2 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 60), False)
        pix2.set_rect(pix2.irect, (200, 0, 0))
        pg.insert_image(fitz.Rect(480, 20, 540, 80), pixmap=pix2)
    return doc.tobytes()


NOME_FIRMATARIO = "Ernesto Valli"
CF_CERT = "TINIT-VLLRNS70A01H501X"


def _tutto_il_testo_del_file(pdf_bytes):
    """Ogni oggetto e ogni stream decodificato del PDF come bytes: dove cercare un
    nome che NON deve piu' esistere in nessuna forma."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parti = [pdf_bytes]
    for x in range(1, doc.xref_length()):
        try:
            parti.append(doc.xref_object(x).encode("latin-1", "ignore"))
            if doc.xref_is_stream(x):
                parti.append(doc.xref_stream(x) or b"")
        except Exception:
            pass
    return b"\n".join(parti)


def _pdf_firma_digitale(layout):
    """PDF con un campo /Sig firmato (finto ma strutturalmente uguale a un PAdES):
    layout "unico" = campo e widget insieme; "padre" = padre con /V + widget figlio."""
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_text((50, 100), "Verbale firmato", fontsize=11)
    pg.insert_text((50, 700), "Firma digitale", fontsize=9)
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_SIGNATURE
    w.field_name = "SigCampo"
    w.rect = fitz.Rect(50, 710, 250, 760)
    pg.add_widget(w)
    wx = pg.first_widget.xref
    v = doc.get_new_xref()
    doc.update_object(v, (
        "<</Type/Sig/Filter/Adobe.PPKLite/SubFilter/adbe.pkcs7.detached"
        f"/Name({NOME_FIRMATARIO})/Reason(Approvazione)/Location(Milano)"
        "/ContactInfo(ernesto.valli@example.it)/ByteRange[0 10 20 30]"
        f"/Contents<{(CF_CERT + ' ' + NOME_FIRMATARIO).encode().hex()}>>>"))
    cat = doc.pdf_catalog()
    if layout == "unico":
        doc.xref_set_key(wx, "V", f"{v} 0 R")
    else:
        padre = doc.get_new_xref()
        doc.update_object(padre, f"<</FT/Sig/T(Padre)/V {v} 0 R/Kids[{wx} 0 R]>>")
        doc.xref_set_key(wx, "Parent", f"{padre} 0 R")
        doc.xref_set_key(wx, "FT", "null")
        af = doc.xref_get_key(cat, "AcroForm")[1]
        doc.xref_set_key(cat, "AcroForm", f"<</Fields[{padre} 0 R]/SigFlags 3>>"
                         if "Fields" not in af else af.replace(f"{wx} 0 R", f"{padre} 0 R"))
    dss = doc.get_new_xref()
    doc.update_object(dss, f"<</Certs[<{CF_CERT.encode().hex()}>]>>")
    doc.xref_set_key(cat, "DSS", f"{dss} 0 R")
    doc.xref_set_key(cat, "Perms", f"<</DocMDP {v} 0 R>>")
    return doc.tobytes()


MAP = {"[FULLNAME_1]": "Mario Rossi", "[FULLNAME_2]": "Luigi Bianchi"}


@unittest.skipIf(fitz is None, "PyMuPDF non installato")
class FirmeNelPdf(unittest.TestCase):

    def setUp(self):
        self.out, self.rep = pdf_export.redact_pdf(_pdf(), MAP)
        self.page = fitz.open(stream=self.out, filetype="pdf")[0]

    def test_conta_e_segnala(self):
        # immagine + curve + ink + timbro + campo firma
        self.assertEqual(self.rep["signatures"], 5)
        self.assertEqual(self.rep["residual"], [])

    def test_via_annotazioni_e_campi_firma(self):
        self.assertEqual([a.type[1] for a in self.page.annots()], [])
        self.assertEqual([w.field_type_string for w in self.page.widgets()], [])

    def test_via_il_tracciato_vettoriale(self):
        curve = sum(1 for d in self.page.get_drawings() for it in d["items"] if it[0] == "c")
        self.assertEqual(curve, 0)

    def test_via_l_immagine_vicino_alla_dicitura_ma_non_il_logo(self):
        boxes = [tuple(round(x) for x in im["bbox"]) for im in self.page.get_image_info()]
        self.assertEqual(boxes, [(480, 20, 540, 80)])           # solo il logo

    def test_casella_etichettata(self):
        self.assertEqual(self.page.get_text().count("[FIRMA]"), 5)

    def test_i_nomi_sono_redatti_lo_stesso(self):
        txt = self.page.get_text()
        self.assertNotIn("Mario Rossi", txt)
        self.assertIn("[FULLNAME_1]", txt)

    def test_nessun_campo_firma_resta_nell_acroform(self):
        doc = fitz.open(stream=self.out, filetype="pdf")
        af = doc.xref_get_key(doc.pdf_catalog(), "AcroForm")[1]
        self.assertNotIn("Firma1", af)
        self.assertNotIn(b"Firma1", self.out)

    def test_firma_digitale_non_lascia_il_nome_in_nessuna_forma(self):
        for layout in ("unico", "padre"):
            with self.subTest(layout):
                pdf = _pdf_firma_digitale(layout)
                # il fixture ha davvero i dati (altrimenti il test non prova niente)
                self.assertIn(NOME_FIRMATARIO.encode(), _tutto_il_testo_del_file(pdf))
                out, rep = pdf_export.redact_pdf(pdf, {"[FULLNAME_1]": "Verbale"})
                self.assertGreaterEqual(rep["signatures"], 1)
                self.assertGreaterEqual(rep["signature_data"], 1)
                tutto = _tutto_il_testo_del_file(out)
                for segreto in (NOME_FIRMATARIO, "ernesto.valli", "Milano", CF_CERT,
                                CF_CERT.encode().hex(), NOME_FIRMATARIO.encode().hex()):
                    self.assertNotIn(segreto.encode(), tutto, segreto)
                doc = fitz.open(stream=out, filetype="pdf")
                cat = doc.pdf_catalog()
                self.assertEqual(doc.xref_get_key(cat, "DSS")[0], "null")
                self.assertEqual(doc.xref_get_key(cat, "Perms")[0], "null")
                self.assertEqual([w.field_type_string for w in doc[0].widgets()], [])

    def test_logo_accanto_a_prosa_con_il_sottoscritto_resta(self):
        # "Il sottoscritto" in una frase lunga NON e' una dicitura di firma
        doc = fitz.open()
        pg = doc.new_page()
        pg.insert_text((50, 150), "Il sottoscritto Mario Rossi dichiara di aver letto e accettato "
                       "le condizioni.", fontsize=9)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40), False)
        pix.set_rect(pix.irect, (0, 90, 0))
        pg.insert_image(fitz.Rect(50, 100, 110, 140), pixmap=pix)         # logo d'intestazione
        out, rep = pdf_export.redact_pdf(doc.tobytes(), MAP)
        self.assertEqual(rep["signatures"], 0)
        self.assertEqual(len(fitz.open(stream=out, filetype="pdf")[0].get_image_info()), 1)

    def test_pulsante_radio_accanto_a_firma_non_e_una_firma(self):
        doc = fitz.open()
        pg = doc.new_page()
        pg.insert_text((50, 100), "Mario Rossi", fontsize=11)
        pg.insert_text((50, 600), "Firma:", fontsize=11)
        sh = pg.new_shape()
        sh.draw_circle(fitz.Point(120, 598), 6)                            # 4 curve chiuse
        sh.finish(color=(0, 0, 0), width=1)
        sh.commit()
        out, rep = pdf_export.redact_pdf(doc.tobytes(), MAP)
        self.assertEqual(rep["signatures"], 0)
        curve = sum(1 for d in fitz.open(stream=out, filetype="pdf")[0].get_drawings()
                    for it in d["items"] if it[0] == "c")
        self.assertGreaterEqual(curve, 4)

    def test_immagine_sopra_un_nome_redatto_senza_dicitura(self):
        doc = fitz.open()
        pg = doc.new_page()
        pg.insert_text((50, 300), "Mario Rossi", fontsize=14)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 30), False)
        pix.set_rect(pix.irect, (0, 0, 120))
        pg.insert_image(fitz.Rect(48, 285, 140, 310), pixmap=pix)           # firma sul nome
        out, rep = pdf_export.redact_pdf(doc.tobytes(), MAP)
        self.assertEqual(rep["signatures"], 1)
        self.assertEqual(len(fitz.open(stream=out, filetype="pdf")[0].get_image_info()), 0)

    def test_senza_firme_nessuna_modifica_inattesa(self):
        _, rep = pdf_export.redact_pdf(_pdf(con_firme=False), MAP)
        self.assertEqual(rep["signatures"], 0)

    def test_un_logo_lontano_da_ogni_dicitura_resta(self):
        # stessa pagina ma senza diciture di firma: l'immagine in basso NON e' una firma
        doc = fitz.open()
        pg = doc.new_page()
        pg.insert_text((50, 60), "Contratto tra Mario Rossi e Luigi Bianchi.", fontsize=11)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 40), False)
        pix.set_rect(pix.irect, (0, 90, 0))
        pg.insert_image(fitz.Rect(50, 300, 150, 340), pixmap=pix)
        out, rep = pdf_export.redact_pdf(doc.tobytes(), MAP)
        self.assertEqual(rep["signatures"], 0)
        self.assertEqual(len(fitz.open(stream=out, filetype="pdf")[0].get_image_info()), 1)

    def test_bordo_di_tabella_vicino_a_un_nome_non_sparisce(self):
        out, _ = pdf_export.redact_pdf(_pdf(con_firme=True, tabella=True), MAP)
        pg = fitz.open(stream=out, filetype="pdf")[0]
        # il riquadro (tabella) e' un 're': deve esserci ancora oltre alle caselle viola
        grigi = [d for d in pg.get_drawings()
                 if d.get("color") == (0.0, 0.0, 0.0) and any(it[0] == "re" for it in d["items"])]
        self.assertTrue(grigi)


if __name__ == "__main__":
    unittest.main()
