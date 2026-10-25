"""Test della copia delle immagini in assets/ (web/server.py): immagini del
testo e copertina. Senza rete: urlopen è sostituito da una finta che registra
gli URL richiesti.

    python3 -m unittest discover tests
"""
import importlib.util
import io
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("server_assets_under_test", ROOT / "web" / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

COVER = ("https://media2.dev.to/dynamic/image/width=1000,height=420,fit=cover,gravity=auto,format=auto/"
         "https%3A%2F%2Fdev-to-uploads.s3.amazonaws.com%2Fuploads%2Farticles%2Fabc123.png")
COVER_ORIGINAL = "https://dev-to-uploads.s3.amazonaws.com/uploads/articles/abc123.png"


class CoverSourceUrlTest(unittest.TestCase):
    def test_url_del_ridimensionatore(self):
        self.assertEqual(srv.cover_source_url(COVER), COVER_ORIGINAL)

    def test_senza_format_auto(self):
        url = ("https://media2.dev.to/dynamic/image/width=1000,height=420,fit=cover,gravity=auto/"
               "https%3A%2F%2Fdev-to-uploads.s3.us-east-2.amazonaws.com%2Fuploads%2Farticles%2Fx.webp")
        self.assertEqual(srv.cover_source_url(url),
                         "https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/x.webp")

    def test_altro_url_resta_com_e(self):
        url = "https://example.org/cover.png"
        self.assertEqual(srv.cover_source_url(url), url)

    def test_nessuna_copertina(self):
        self.assertIsNone(srv.cover_source_url(None))
        self.assertIsNone(srv.cover_source_url(""))


class FakeUrlopen:
    def __init__(self):
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)

        class Resp(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                self_inner.close()
        return Resp(b"img")


class DownloadAssetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_assets, self.old_urlopen = srv.ASSETS_DIR, srv.urllib.request.urlopen
        srv.ASSETS_DIR = pathlib.Path(self.tmp.name)
        self.fake = FakeUrlopen()
        srv.urllib.request.urlopen = self.fake

    def tearDown(self):
        srv.ASSETS_DIR, srv.urllib.request.urlopen = self.old_assets, self.old_urlopen
        self.tmp.cleanup()

    def test_testo_e_copertina(self):
        body = "testo ![screenshot](https://dev-to-uploads.s3.amazonaws.com/uploads/articles/shot.png)"
        srv.download_assets(body, 42, COVER)
        self.assertEqual(self.fake.urls, [
            "https://dev-to-uploads.s3.amazonaws.com/uploads/articles/shot.png", COVER_ORIGINAL])
        self.assertEqual(sorted(p.name for p in (srv.ASSETS_DIR / "42").iterdir()), ["abc123.png", "shot.png"])

    def test_solo_copertina(self):
        srv.download_assets("nessuna immagine", 7, COVER)
        self.assertEqual(self.fake.urls, [COVER_ORIGINAL])

    def test_senza_immagini_ne_copertina_non_crea_la_cartella(self):
        srv.download_assets("nessuna immagine", 8, None)
        self.assertEqual(self.fake.urls, [])
        self.assertFalse((srv.ASSETS_DIR / "8").exists())

    def test_copertina_gia_salvata_non_si_riscarica(self):
        (srv.ASSETS_DIR / "9").mkdir()
        (srv.ASSETS_DIR / "9" / "abc123.png").write_bytes(b"gia qui")
        srv.download_assets("", 9, COVER)
        self.assertEqual(self.fake.urls, [])


if __name__ == "__main__":
    unittest.main()
