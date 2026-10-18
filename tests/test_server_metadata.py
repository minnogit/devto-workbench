"""Test dei metadati diversi (pubblicazione dal portale) in web/server.py.
Solo standard library, senza rete:

    python3 -m unittest discover tests
"""
import hashlib
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("server_metadata_under_test", ROOT / "web" / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

BODY = "Primo paragrafo.\n\n```bash\necho «ciao»  \n```\n\nUltima riga senza a capo finale"
BODY_HASH = hashlib.sha256(BODY.encode()).hexdigest()
TEMP_URL = "https://dev.to/u/titolo-4abc-temp-slug-123"
FINAL_URL = "https://dev.to/u/titolo-4abc"


def local_fm(**over):
    fm = {"id": 7, "title": "Titolo", "published": False, "tags": ["git", "linux"],
          "url": TEMP_URL, "canonical_url": TEMP_URL, "cover_image": None,
          "devto_synced_hash": BODY_HASH, "stato": "pronta", "reviewed": True}
    fm.update(over)
    return {k: v for k, v in fm.items() if v is not None or k in ("cover_image",)}


def remote(**over):
    r = {"id": 7, "title": "Titolo", "published": True, "tag_list": ["git", "linux"],
         "url": FINAL_URL, "canonical_url": FINAL_URL, "cover_image": None,
         "body_markdown": BODY, "published_at": "2026-10-18T18:18:37.727Z"}
    r.update(over)
    return r


class MetadatiDiversiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self._saved = (srv.ARTICLES_DIR, srv.fetch_remote_articles, srv.find_remote_article)
        self.addCleanup(self._restore)
        srv.ARTICLES_DIR = self.dir
        self.remote = remote()
        srv.fetch_remote_articles = lambda: [self.remote]
        srv.find_remote_article = lambda i: self.remote if i == 7 else None

    def _restore(self):
        srv.ARTICLES_DIR, srv.fetch_remote_articles, srv.find_remote_article = self._saved

    def put(self, fm=None, body=BODY):
        path = self.dir / "7-titolo.md"
        path.write_text(srv.render_article(fm or local_fm(), body), encoding="utf-8")
        return path

    def row(self):
        return srv.build_report()[0]

    # ---- riconoscimento
    def test_pubblicazione_dal_portale_e_uno_stato_a_se(self):
        self.put()
        r = self.row()
        self.assertEqual(r["status"], "metadati_diversi")
        self.assertEqual(r["metadati"]["azione"], "pubblicato_dal_portale")
        self.assertEqual(sorted(r["metadati"]["differenze"]), ["canonical_url", "published", "url"])

    def test_tutto_uguale_resta_allineato(self):
        self.put(local_fm(published=True, url=FINAL_URL, canonical_url=FINAL_URL))
        r = self.row()
        self.assertEqual(r["status"], "allineato")
        self.assertIsNone(r["metadati"])

    def test_se_il_testo_e_cambiato_vale_lo_stato_del_testo(self):
        self.put(body=BODY + "\nmodificato in locale")
        self.assertEqual(self.row()["status"], "locale_da_pushare")
        self.put()
        self.remote["body_markdown"] = BODY + "\nmodificato sul portale"
        self.assertEqual(self.row()["status"], "remoto_aggiornato")

    # ---- casi ambigui: nessuna azione automatica
    def test_titolo_cambiato_insieme_alla_pubblicazione_e_ambiguo(self):
        self.put()
        self.remote["title"] = "Un altro titolo"
        self.assertEqual(self.row()["metadati"]["azione"], "ambiguo")

    def test_tag_diversi_sono_ambigui(self):
        self.put(local_fm(published=True, url=FINAL_URL, canonical_url=FINAL_URL))
        self.remote["tag_list"] = ["git"]
        r = self.row()
        self.assertEqual((r["status"], r["metadati"]["azione"]), ("metadati_diversi", "ambiguo"))

    def test_articolo_tornato_bozza_e_ambiguo(self):
        self.put(local_fm(published=True, url=FINAL_URL, canonical_url=FINAL_URL))
        self.remote["published"] = False
        self.assertEqual(self.row()["metadati"]["azione"], "ambiguo")

    # ---- aggiornamento del front-matter
    def test_sync_metadata_aggiorna_solo_il_front_matter(self):
        path = self.put()
        before_body = srv.load_local_articles()[7]["body"]
        res = srv.sync_metadata(7)
        self.assertEqual(res["campi"], ["canonical_url", "published", "url"])
        loc = srv.load_local_articles()[7]
        self.assertIs(loc["published"], True)
        self.assertEqual((loc["url"], loc["canonical_url"]), (FINAL_URL, FINAL_URL))
        self.assertEqual(loc["body"], before_body)  # testo identico byte per byte
        self.assertEqual(loc["devto_synced_hash"], BODY_HASH)
        self.assertTrue(loc["reviewed"])  # resta valido dopo la pubblicazione
        self.assertNotIn("stato:", path.read_text(encoding="utf-8"))  # per un pubblicato vale «pubblicato»
        self.assertEqual(self.row()["status"], "allineato")

    def test_sync_metadata_non_cambia_titolo_tag_e_copertina(self):
        self.put(local_fm(cover_image="https://x/img.png"))
        self.remote["cover_image"] = "https://x/img.png"
        srv.sync_metadata(7)
        loc = srv.load_local_articles()[7]
        self.assertEqual((loc["title"], loc["tags"], loc["cover_image"]),
                         ("Titolo", ["git", "linux"], "https://x/img.png"))

    def test_seconda_chiamata_non_ha_niente_da_fare(self):
        self.put()
        srv.sync_metadata(7)
        with self.assertRaises(ValueError):
            srv.sync_metadata(7)

    def test_rifiuta_se_il_testo_non_e_allineato(self):
        self.put(body=BODY + "\nmodificato in locale")
        before = (self.dir / "7-titolo.md").read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            srv.sync_metadata(7)
        self.assertEqual((self.dir / "7-titolo.md").read_text(encoding="utf-8"), before)

    def test_rifiuta_le_differenze_ambigue_senza_scrivere(self):
        self.put()
        self.remote["title"] = "Un altro titolo"
        before = (self.dir / "7-titolo.md").read_text(encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            srv.sync_metadata(7)
        self.assertIn("title", str(cm.exception))
        self.assertEqual((self.dir / "7-titolo.md").read_text(encoding="utf-8"), before)

    def test_articolo_senza_file_locale(self):
        with self.assertRaises(ValueError):
            srv.sync_metadata(7)

    # ---- confronto
    def test_diff_mostra_i_metadati_invece_di_un_errore(self):
        self.put()
        d = srv.diff_article(7)
        self.assertEqual(d["status"], "metadati_diversi")
        self.assertEqual(d["diff"], "")
        self.assertEqual(d["metadati"]["published"], {"locale": False, "remoto": True})

    def test_diff_con_tutto_uguale_resta_un_errore(self):
        self.put(local_fm(published=True, url=FINAL_URL, canonical_url=FINAL_URL))
        with self.assertRaises(ValueError):
            srv.diff_article(7)


if __name__ == "__main__":
    unittest.main()
