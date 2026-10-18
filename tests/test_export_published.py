"""Test di scripts/export_published.py. Solo standard library:

    python3 -m unittest discover tests
"""
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("export_published", ROOT / "scripts" / "export_published.py")
exp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exp)

BODY = "Primo paragrafo.\n\n```bash\necho «ciao»  \n```\n\nUltima riga senza a capo finale"


def article(id_, published, body=BODY, **extra):
    fields = {"id": id_, "title": f"Titolo {id_}", "published": published, "tags": ["git", "linux"],
              "url": f"https://dev.to/u/a-{id_}", "canonical_url": f"https://dev.to/u/a-{id_}",
              "cover_image": None, "devto_synced_hash": "aa" * 32, **extra}
    return exp.server.render_article(fields, body)


class ExportPublishedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.articles = root / "articles"
        self.dest = root / "brain" / "raw" / "devto"
        self.articles.mkdir()

    def put(self, name, text):
        (self.articles / name).write_text(text, encoding="utf-8")

    def run_export(self, **kw):
        return exp.export_published(self.articles, self.dest, **kw)

    def test_esporta_solo_i_pubblicati(self):
        self.put("1-pubblicato.md", article(1, True))
        self.put("2-bozza.md", article(2, False, BODY + "\nSEGRETO DELLA BOZZA"))
        r = self.run_export()
        self.assertEqual(r["added"], ["1-pubblicato.md"])
        self.assertEqual(r["skipped_drafts"], 1)
        self.assertEqual([p.name for p in self.dest.glob("*.md")], ["1-pubblicato.md"])
        self.assertNotIn("SEGRETO", "".join(p.read_text(encoding="utf-8") for p in self.dest.glob("*")))

    def test_published_deve_essere_true_vero(self):
        # una stringa o un campo mancante non bastano a esporre un articolo
        self.put("3-stringa.md", article(3, "true"))
        self.put("4-assente.md", article(4, False).replace("published: false\n", ""))
        r = self.run_export()
        self.assertEqual(r["added"], [])
        self.assertEqual(r["skipped_drafts"], 2)

    def test_body_identico_e_front_matter_ridotto(self):
        self.put("1-a.md", article(1, True, stato="pronta", reviewed=True))
        self.run_export()
        text = (self.dest / "1-a.md").read_text(encoding="utf-8")
        fm, body = exp.server.split_front_matter(text)
        self.assertEqual(body, BODY)
        self.assertEqual(sorted(fm), ["id", "tags", "title", "url"])

    def test_i_campi_locali_non_cambiano_il_file_esportato(self):
        self.put("1-a.md", article(1, True))
        self.run_export()
        before = (self.dest / "1-a.md").read_bytes()
        # cambiano solo campi che non fanno parte dell'esportazione
        self.put("1-a.md", article(1, True, stato="pronta", reviewed=True).replace("aa" * 32, "bb" * 32))
        r = self.run_export()
        self.assertEqual(r["unchanged"], ["1-a.md"])
        self.assertEqual((self.dest / "1-a.md").read_bytes(), before)

    def test_un_cambio_di_testo_aggiorna(self):
        self.put("1-a.md", article(1, True))
        self.run_export()
        self.put("1-a.md", article(1, True, body=BODY + " modificata"))
        self.assertEqual(self.run_export()["updated"], ["1-a.md"])

    def test_seconda_esecuzione_non_scrive(self):
        self.put("1-a.md", article(1, True))
        self.run_export()
        mtime = (self.dest / "1-a.md").stat().st_mtime_ns
        r = self.run_export()
        self.assertEqual((r["added"], r["updated"], r["unchanged"]), ([], [], ["1-a.md"]))
        self.assertEqual((self.dest / "1-a.md").stat().st_mtime_ns, mtime)

    def test_dry_run_non_scrive_nulla(self):
        self.put("1-a.md", article(1, True))
        r = self.run_export(dry_run=True)
        self.assertEqual(r["added"], ["1-a.md"])
        self.assertFalse(self.dest.exists())

    def test_articolo_tornato_bozza_resta_orfano_finche_non_si_usa_prune(self):
        self.put("1-a.md", article(1, True))
        self.run_export()
        self.put("1-a.md", article(1, False))
        r = self.run_export()
        self.assertEqual(r["orphans"], ["1-a.md"])
        self.assertTrue((self.dest / "1-a.md").exists())
        r = self.run_export(prune=True)
        self.assertEqual(r["removed"], ["1-a.md"])
        self.assertFalse((self.dest / "1-a.md").exists())

    def test_prune_non_tocca_i_file_che_non_sono_articoli(self):
        self.put("1-a.md", article(1, True))
        self.dest.mkdir(parents=True)
        (self.dest / "NOTE.md").write_text("# appunti senza front-matter\n", encoding="utf-8")
        self.run_export(prune=True)
        self.assertTrue((self.dest / "NOTE.md").exists())

    def test_rifiuta_destinazioni_che_si_sovrappongono_agli_articoli(self):
        self.put("1-a.md", article(1, True))
        for dest in (self.articles, self.articles / "export", self.articles.parent):
            with self.assertRaises(ValueError):
                exp.export_published(self.articles, dest)
        self.assertEqual([p.name for p in self.articles.glob("*.md")], ["1-a.md"])

    def test_non_modifica_mai_la_cartella_degli_articoli(self):
        self.put("1-a.md", article(1, True))
        self.put("2-b.md", article(2, False))
        before = {p.name: p.read_bytes() for p in self.articles.glob("*")}
        self.run_export(prune=True)
        self.assertEqual({p.name: p.read_bytes() for p in self.articles.glob("*")}, before)

    def test_cartella_articoli_mancante(self):
        with self.assertRaises(ValueError):
            exp.export_published(self.articles / "non-esiste", self.dest)


if __name__ == "__main__":
    unittest.main()
