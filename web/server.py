#!/usr/bin/env python3
"""Server locale minimale per l'interfaccia web di gestione articoli.

Nessuna dipendenza esterna (solo standard library): serve la pagina statica
in web/ e fa da proxy autenticato verso l'API di dev.to, perché il browser
da solo non può mandare l'header "api-key" cross-origin (CORS lo blocca,
verificato). La chiave resta lato server, non tocca mai il browser.

Oltre alla consultazione, gestisce anche il pull/push "sicuri" (quando solo
un lato è cambiato dall'ultimo sync): mai un merge automatico in caso di
vero conflitto (stessa regola della skill devto-articles) — lì la risoluzione
resta manuale.

Uso:
    DEV_TO_API_KEY=xxxx python3 web/server.py
    (o, se già nell'ambiente/~/.bashrc, semplicemente: python3 web/server.py)

Poi apri http://localhost:8787
"""
import difflib
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = pathlib.Path(__file__).resolve().parent

# Dove stanno i contenuti: di default `articles/` e `assets/` nella cartella da
# cui si lancia il server (la radice del repo degli articoli), non accanto a
# questo file, così lo strumento può vivere in un repo a parte.
ARTICLES_DIR = pathlib.Path(os.environ.get("ARTICLES_DIR", "articles")).resolve()
# Le immagini seguono gli articoli: di default `assets/` accanto alla cartella
# degli articoli. Con la sola ARTICLES_DIR impostata non si crea così una
# `assets/` nella cartella sbagliata (quella da cui si lancia il server).
ASSETS_DIR = pathlib.Path(os.environ.get("ASSETS_DIR") or ARTICLES_DIR.parent / "assets").resolve()
# Destinazione dell'esportazione degli articoli pubblicati (di solito la cartella
# raw/ di una wiki, in un altro repo). Si fissa qui, all'avvio: non arriva mai da una
# richiesta, altrimenti chi riesce a far parlare il server deciderebbe dove scrive.
# Senza, l'esportazione dalla pagina è spenta.
_export_dest = os.environ.get("EXPORT_DEST")
EXPORT_DEST = pathlib.Path(os.path.expanduser(_export_dest)).resolve() if _export_dest else None
# Radice di questo repo (lo strumento): qui non devono finire articoli.
TOOL_ROOT = WEB_DIR.parent


def inside_tool_repo(path: pathlib.Path) -> bool:
    return path == TOOL_ROOT or TOOL_ROOT in path.parents

IMG_URL_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")

# Intestazione che la pagina manda su ogni POST. Una pagina di un altro sito non
# può aggiungerla senza una richiesta preliminare (OPTIONS) che il server non
# accetta, quindi blocca anche il caso in cui Origin manchi.
WRITE_HEADER = "X-Workbench"

API_KEY = os.environ.get("DEV_TO_API_KEY")
API_BASE = "https://dev.to/api"
PORT = int(os.environ.get("PORT", "8787"))
# Facoltativo: forza il nome mostrato nel titolo. Se manca lo chiede a Dev.to.
USERNAME = os.environ.get("DEVTO_USERNAME")
_username_cache = {}

FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)

# Flusso editoriale delle bozze, campo locale `stato` del front-matter (mai
# inviato a Dev.to). "pubblicato" non è tra questi di proposito: lo deriva
# effective_stato() da `published`, così non esistono due fonti per la stessa
# informazione.
STATI_BOZZA = ("bozza", "revisionata", "pronta")


def effective_stato(published, stato) -> str:
    if published:
        return "pubblicato"
    return stato if stato in STATI_BOZZA else "bozza"


def local_only_fields(loc: dict, published) -> dict:
    """Campi puramente locali da riscrivere nel front-matter, nell'ordine
    del file. Entrambi sono opzionali con default (`stato` assente vale
    "bozza", `reviewed` assente vale false): si scrivono solo se si
    discostano dal default, per non sporcare il diff di ogni file toccato.
    `stato` per un articolo pubblicato non ha più senso e non si scrive."""
    fields = {}
    stato = effective_stato(published, loc.get("stato"))
    if stato in STATI_BOZZA and stato != "bozza":
        fields["stato"] = stato
    if loc.get("reviewed"):
        fields["reviewed"] = True
    return fields


def parse_front_matter(text: str) -> dict:
    """Front-matter scritto da scripts/import_articles.py: una riga per
    campo, `key: <valore JSON>` (non YAML vero, ma un JSON per riga)."""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}
    data = {}
    for line in m.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        try:
            data[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            data[key] = raw_value
    return data


def split_front_matter(text: str):
    """Ritorna (front_matter_dict, body). Il body è esattamente il testo
    che scripts/import_articles.py (e le funzioni di questo file) scrivono
    su disco: c'è una riga vuota "strutturale" tra il "---" di chiusura e
    il body, di cui la regex ne consuma solo una delle due newline — va
    tolta anche l'altra per riottenere il body byte-per-byte."""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    fm = parse_front_matter(text)
    rest = text[m.end():]
    body = rest[1:] if rest.startswith("\n") else rest
    return fm, body


def load_local_articles() -> dict:
    """Ritorna {id: {title, tags, published, devto_synced_hash, stato, reviewed, body, path, file}}."""
    local = {}
    if not ARTICLES_DIR.exists():
        return local
    for path in sorted(ARTICLES_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fm, body = split_front_matter(text)
        article_id = fm.get("id")
        if article_id is None:
            continue
        local[article_id] = {
            "title": fm.get("title"),
            "tags": fm.get("tags", []),
            "published": fm.get("published"),
            "devto_synced_hash": fm.get("devto_synced_hash"),
            # Campo puramente locale, mai inviato a Dev.to: posizione della
            # bozza nel flusso bozza → revisionata → pronta (vedi CLAUDE.md).
            # Grezzo: il valore effettivo lo calcola effective_stato().
            "stato": fm.get("stato"),
            # Campo puramente locale, mai inviato a Dev.to: promemoria "questo
            # articolo è stato rivisto" (bug tecnici, stile). Resta valido
            # anche dopo la pubblicazione — la revisione riguarda il
            # contenuto, non lo stato di pubblicazione.
            "reviewed": bool(fm.get("reviewed", False)),
            "url": fm.get("url"),
            "canonical_url": fm.get("canonical_url"),
            "cover_image": fm.get("cover_image"),
            "body": body,
            "path": path,
            "file": path.name,
        }
    return local


def fetch_remote_articles() -> list:
    if not API_KEY:
        raise RuntimeError(
            "DEV_TO_API_KEY non impostata nell'ambiente del server "
            "(export DEV_TO_API_KEY=... prima di avviarlo)."
        )
    articles = []
    for path in ("/articles/me", "/articles/me/unpublished"):
        req = urllib.request.Request(
            f"{API_BASE}{path}",
            headers={
                "api-key": API_KEY,
                # senza uno User-Agent "da browser" alcuni endpoint di
                # dev.to rispondono 403 (bloccano lo user-agent di default
                # di urllib.request).
                "User-Agent": "Mozilla/5.0 devto-articles-web/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            articles.extend(json.loads(resp.read().decode("utf-8")))
    return articles


def get_username() -> str | None:
    """Username Dev.to di chi possiede la chiave API, solo per il titolo
    della pagina: DEVTO_USERNAME se impostata, altrimenti GET /users/me.
    Mai fatale: in caso di errore ritorna None e la pagina mostra il titolo
    senza nome. Si memorizza solo un successo, così un errore di rete al
    primo tentativo non resta fino al riavvio."""
    if USERNAME:
        return USERNAME
    if "name" in _username_cache:
        return _username_cache["name"]
    if not API_KEY:
        return None
    req = urllib.request.Request(
        f"{API_BASE}/users/me",
        headers={"api-key": API_KEY, "User-Agent": "Mozilla/5.0 devto-articles-web/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            name = json.loads(resp.read().decode("utf-8")).get("username")
    except Exception:
        return None
    if name:
        _username_cache["name"] = name
    return name


def slugify(title: str) -> str:
    """Stessa logica di scripts/import_articles.py, per generare nomi file
    coerenti con quelli già importati da lì."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:80] or "untitled"


def download_assets(body_markdown: str, article_id) -> None:
    urls = IMG_URL_RE.findall(body_markdown)
    if not urls:
        return
    out_dir = ASSETS_DIR / str(article_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        name = url.split("/")[-1].split("?")[0] or "image"
        dest = out_dir / name
        if dest.exists():
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 devto-articles-web/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
        except Exception as e:
            print(f"  WARNING: impossibile scaricare {url}: {e}", file=sys.stderr)


def render_article(front_matter: dict, body: str) -> str:
    """Testo di un file articolo: front-matter (una riga `chiave: <JSON>` per
    campo), riga vuota, body esatto. Separata dalla scrittura perché
    scripts/export_published.py confronta il testo prima di toccare il disco."""
    lines = ["---"]
    for key, value in front_matter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def write_local_article(path: pathlib.Path, front_matter: dict, body: str) -> None:
    path.write_text(render_article(front_matter, body), encoding="utf-8")


def find_remote_article(article_id: int) -> dict | None:
    for a in fetch_remote_articles():
        if a["id"] == article_id:
            return a
    return None


def import_article_locally(article_id: int) -> dict:
    """Bootstrap di UN articolo che esiste solo su Dev.to (stato 'solo_remoto')
    dentro articles/, con lo stesso schema di front-matter di
    scripts/import_articles.py. Rifiuta se esiste già un file locale per
    quell'id: questa funzione serve solo a creare, mai a sovrascrivere —
    per quello resta la skill devto-articles, che gestisce i conflitti.
    """
    if inside_tool_repo(ARTICLES_DIR):
        raise ValueError(
            f"La cartella degli articoli ({ARTICLES_DIR}) è dentro il repo dello strumento, "
            "che non è il posto dei contenuti. Lancia il server dalla radice del repo degli "
            "articoli oppure imposta ARTICLES_DIR."
        )
    local = load_local_articles()
    if article_id in local:
        raise ValueError(
            f"Esiste già un file locale per l'articolo {article_id} "
            f"({local[article_id]['file']}); usa la skill devto-articles per "
            "aggiornarlo, non questo pulsante (pensato solo per articoli nuovi)."
        )

    article = find_remote_article(article_id)
    if article is None:
        raise ValueError(f"Articolo {article_id} non trovato su Dev.to (o non è tuo).")

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(article["title"])
    filename = f"{article_id}-{slug}.md"
    path = ARTICLES_DIR / filename

    body = article.get("body_markdown") or ""
    body_hash = hashlib.sha256(body.encode()).hexdigest()

    front_matter = {
        "id": article_id,
        "title": article["title"],
        "published": article["published"],
        "tags": article.get("tag_list", []),
        "url": article["url"],
        "canonical_url": article.get("canonical_url"),
        "cover_image": article.get("cover_image"),
        "devto_synced_hash": body_hash,
    }
    write_local_article(path, front_matter, body)
    download_assets(body, article_id)

    return {"file": filename}


def pull_article(article_id: int) -> dict:
    """Sovrascrive il file locale con lo stato attuale di Dev.to — solo se
    il locale non è stato toccato dall'ultimo sync (altrimenti sarebbe una
    perdita silenziosa di modifiche locali: rifiuta e basta, come la skill
    devto-articles farebbe con un conflitto)."""
    local = load_local_articles()
    loc = local.get(article_id)
    if loc is None:
        raise ValueError(
            f"Nessun file locale per l'articolo {article_id}: usa 'Scarica in "
            "locale' per crearlo la prima volta."
        )

    article = find_remote_article(article_id)
    if article is None:
        raise ValueError(f"Articolo {article_id} non trovato su Dev.to (o non è tuo).")

    body = article.get("body_markdown") or ""
    remote_hash = hashlib.sha256(body.encode()).hexdigest()
    local_hash = hashlib.sha256(loc["body"].encode()).hexdigest()
    synced_hash = loc.get("devto_synced_hash")

    if remote_hash == synced_hash:
        raise ValueError("Il remoto non è cambiato dall'ultimo sync: niente da scaricare.")
    if local_hash != synced_hash:
        raise ValueError(
            "Conflitto: sia il locale che il remoto sono cambiati dall'ultimo sync. "
            "Non risolvibile automaticamente da qui — usa la skill devto-articles per "
            "confrontare le due versioni e decidere."
        )

    front_matter = {
        "id": article_id,
        "title": article["title"],
        "published": article["published"],
        "tags": article.get("tag_list", []),
        "url": article["url"],
        "canonical_url": article.get("canonical_url"),
        "cover_image": article.get("cover_image"),
        "devto_synced_hash": remote_hash,
        **local_only_fields(loc, article["published"]),
    }
    write_local_article(loc["path"], front_matter, body)
    download_assets(body, article_id)

    return {"file": loc["file"]}


def push_article(article_id: int) -> dict:
    """Manda su Dev.to il contenuto locale — solo se il remoto non è
    cambiato dall'ultimo sync (altrimenti sovrascriverebbe alla cieca una
    modifica fatta dal portale: rifiuta, stessa regola della skill
    devto-articles)."""
    local = load_local_articles()
    loc = local.get(article_id)
    if loc is None:
        raise ValueError(f"Nessun file locale per l'articolo {article_id}.")

    article = find_remote_article(article_id)
    if article is None:
        raise ValueError(
            f"Articolo {article_id} non trovato su Dev.to: questo pulsante aggiorna "
            "un articolo esistente, non ne crea uno nuovo."
        )

    remote_body = article.get("body_markdown") or ""
    remote_hash = hashlib.sha256(remote_body.encode()).hexdigest()
    local_hash = hashlib.sha256(loc["body"].encode()).hexdigest()
    synced_hash = loc.get("devto_synced_hash")

    if local_hash == synced_hash:
        raise ValueError("Il locale non è cambiato dall'ultimo sync: niente da mandare online.")
    if remote_hash != synced_hash:
        raise ValueError(
            "Conflitto: il remoto è cambiato dall'ultimo sync (probabilmente modificato "
            "dal portale). Non risolvibile automaticamente da qui — usa la skill "
            "devto-articles per confrontare le due versioni e decidere."
        )

    tags = loc.get("tags") or []
    payload = {
        "article": {
            "title": loc.get("title"),
            "body_markdown": loc["body"],
            # Dev.to applica i tag solo se inviati come lista JSON; una stringa
            # comma-separated sotto la stessa chiave viene accettata (200 OK) ma
            # ignorata silenziosamente. Stesso bug trovato e corretto in devto-mcp.
            "tags": tags,
            "published": bool(loc.get("published")),
        }
    }
    req = urllib.request.Request(
        f"{API_BASE}/articles/{article_id}",
        data=json.dumps(payload).encode("utf-8"),
        method="PUT",
        headers={
            "api-key": API_KEY,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 devto-articles-web/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        updated = json.loads(resp.read().decode("utf-8"))

    new_body = updated.get("body_markdown", loc["body"])
    new_hash = hashlib.sha256(new_body.encode()).hexdigest()
    front_matter = {
        "id": article_id,
        "title": updated.get("title", loc.get("title")),
        "published": updated.get("published", loc.get("published")),
        # A differenza degli endpoint di elenco (usati da import/pull), la
        # risposta della PUT su singolo articolo restituisce "tag_list" come
        # stringa comma-separated, non lista — verificato contro l'API reale.
        # Il campo "tags" invece è già la lista corretta.
        "tags": updated.get("tags", tags),
        "url": updated.get("url", loc.get("url")),
        "canonical_url": updated.get("canonical_url", loc.get("canonical_url")),
        "cover_image": updated.get("cover_image", loc.get("cover_image")),
        "devto_synced_hash": new_hash,
        # Se questo push ha reso l'articolo pubblicato, local_only_fields()
        # non scrive più `stato` (vale "pubblicato", derivato da `published`).
        **local_only_fields(loc, updated.get("published", loc.get("published"))),
    }
    write_local_article(loc["path"], front_matter, new_body)

    return {"file": loc["file"], "url": updated.get("url")}


def classify_status(local_hash: str | None, remote_hash: str | None, synced_hash: str | None) -> str:
    """Confronto a tre vie condiviso da build_report() e diff_article()."""
    remote_changed = remote_hash != synced_hash
    local_changed = local_hash != synced_hash
    if not remote_changed and not local_changed:
        return "allineato"
    if remote_changed and not local_changed:
        return "remoto_aggiornato"  # sicuro da scaricare (pull)
    if local_changed and not remote_changed:
        return "locale_da_pushare"  # sicuro da mandare online (push)
    if local_hash == remote_hash:
        return "allineato"  # entrambi cambiati, ma nello stesso modo
    return "conflitto"  # entrambi cambiati, in modo diverso


# Campi del front-matter che rispecchiano lo stato dell'articolo su Dev.to. Possono
# differire dal locale senza che il testo sia cambiato: il caso tipico è la
# pubblicazione dal portale, che cambia `published` e sostituisce lo slug
# provvisorio negli indirizzi.
METADATA_FIELDS = ("title", "published", "tags", "url", "canonical_url", "cover_image")
PUBLICATION_FIELDS = {"published", "url", "canonical_url"}


def metadata_diff(loc: dict, article: dict) -> dict:
    """{campo: {"locale": ..., "remoto": ...}} per i metadati che differiscono."""
    remote = {
        "title": article.get("title"),
        "published": bool(article.get("published")),
        "tags": article.get("tag_list") or [],
        "url": article.get("url"),
        "canonical_url": article.get("canonical_url"),
        "cover_image": article.get("cover_image"),
    }
    local = {
        "title": loc.get("title"),
        "published": bool(loc.get("published")),
        "tags": loc.get("tags") or [],
        "url": loc.get("url"),
        "canonical_url": loc.get("canonical_url"),
        "cover_image": loc.get("cover_image"),
    }
    return {k: {"locale": local[k], "remoto": remote[k]} for k in METADATA_FIELDS if local[k] != remote[k]}


def metadata_action(diff: dict) -> str | None:
    """Cosa si può fare con una differenza di metadati. Non c'è un hash dei
    metadati all'ultimo sync, quindi di solito non si sa quale lato è cambiato
    e la differenza è «ambigua»: nessuna scrittura automatica, come per un
    conflitto. L'unico caso non ambiguo è la pubblicazione dal portale: locale
    bozza, remoto pubblicato, e cambiano solo published e gli indirizzi."""
    if not diff:
        return None
    pub = diff.get("published")
    if set(diff) <= PUBLICATION_FIELDS and pub and pub["locale"] is False and pub["remoto"] is True:
        return "pubblicato_dal_portale"
    return "ambiguo"


def sync_metadata(article_id: int) -> dict:
    """Allinea il front-matter locale a Dev.to quando l'articolo è stato
    pubblicato dal portale e il testo non è cambiato. Rifiuta tutto il resto:
    testo diverso (si usano pull, push o la skill) e differenze di metadati
    ambigue (titolo, tag, copertina, o un articolo tornato bozza)."""
    loc = load_local_articles().get(article_id)
    if loc is None:
        raise ValueError(f"Nessun file locale per l'articolo {article_id}.")
    article = find_remote_article(article_id)
    if article is None:
        raise ValueError(f"Articolo {article_id} non trovato su Dev.to (o non è tuo).")

    body = article.get("body_markdown") or ""
    remote_hash = hashlib.sha256(body.encode()).hexdigest()
    local_hash = hashlib.sha256(loc["body"].encode()).hexdigest()
    synced_hash = loc.get("devto_synced_hash")
    if not (local_hash == remote_hash == synced_hash):
        raise ValueError(
            "Il testo non è allineato (locale, remoto e ultimo sync non coincidono): "
            "questo pulsante aggiorna solo i metadati. Usa pull, push o la skill "
            "devto-articles per il testo."
        )

    diff = metadata_diff(loc, article)
    if not diff:
        raise ValueError("I metadati sono già allineati: niente da aggiornare.")
    if metadata_action(diff) != "pubblicato_dal_portale":
        raise ValueError(
            "Le differenze (" + ", ".join(sorted(diff)) + ") non sono solo una pubblicazione "
            "dal portale: da qui non si può sapere quale lato è cambiato. Decidi con la "
            "skill devto-articles."
        )

    front_matter = {
        "id": article_id,
        "title": loc.get("title"),
        "published": True,
        "tags": loc.get("tags", []),
        "url": article["url"],
        "canonical_url": article.get("canonical_url"),
        "cover_image": loc.get("cover_image"),
        "devto_synced_hash": remote_hash,
        # stato non si scrive più: per un articolo pubblicato vale «pubblicato»
        **local_only_fields(loc, True),
    }
    write_local_article(loc["path"], front_matter, loc["body"])
    return {"file": loc["file"], "campi": sorted(diff)}


def build_side_by_side(local_lines: list[str], remote_lines: list[str]) -> list[dict]:
    """Righe allineate per la vista fianco a fianco: ogni riga è
    {type, left, right}, con left/right a None dove un lato non ha
    corrispondenza (inserimento/cancellazione puri)."""
    rows = []
    sm = difflib.SequenceMatcher(None, local_lines, remote_lines)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for a, b in zip(local_lines[i1:i2], remote_lines[j1:j2]):
                rows.append({"type": "equal", "left": a, "right": b})
        elif tag == "replace":
            left_seg = local_lines[i1:i2]
            right_seg = remote_lines[j1:j2]
            for k in range(max(len(left_seg), len(right_seg))):
                rows.append({
                    "type": "replace",
                    "left": left_seg[k] if k < len(left_seg) else None,
                    "right": right_seg[k] if k < len(right_seg) else None,
                })
        elif tag == "delete":
            for a in local_lines[i1:i2]:
                rows.append({"type": "delete", "left": a, "right": None})
        elif tag == "insert":
            for b in remote_lines[j1:j2]:
                rows.append({"type": "insert", "left": None, "right": b})
    return rows


def diff_article(article_id: int) -> dict:
    """Diff riga per riga tra la versione locale e quella remota attuale,
    per gli stati in cui esistono davvero due versioni da confrontare
    (remoto_aggiornato, locale_da_pushare, conflitto). Sola lettura: non
    scrive né decide nulla, coerente con la regola che un conflitto si
    risolve sempre a mano (vedi skill devto-articles)."""
    local = load_local_articles()
    loc = local.get(article_id)
    if loc is None:
        raise ValueError(f"Nessun file locale per l'articolo {article_id}.")

    article = find_remote_article(article_id)
    if article is None:
        raise ValueError(f"Articolo {article_id} non trovato su Dev.to (o non è tuo).")

    remote_body = article.get("body_markdown") or ""
    local_body = loc["body"]
    remote_hash = hashlib.sha256(remote_body.encode()).hexdigest()
    local_hash = hashlib.sha256(local_body.encode()).hexdigest()
    synced_hash = loc.get("devto_synced_hash")
    status = classify_status(local_hash, remote_hash, synced_hash)

    if status == "allineato":
        diff = metadata_diff(loc, article)
        if diff:
            return {"status": "metadati_diversi", "diff": "", "rows": [],
                    "metadati": diff, "azione": metadata_action(diff)}
        raise ValueError("Locale e remoto sono identici: niente da confrontare.")

    local_lines = local_body.splitlines()
    remote_lines = remote_body.splitlines()

    diff_lines = list(difflib.unified_diff(
        local_body.splitlines(keepends=True),
        remote_body.splitlines(keepends=True),
        fromfile="locale",
        tofile="dev.to",
    ))

    return {
        "status": status,
        "diff": "".join(diff_lines),
        "rows": build_side_by_side(local_lines, remote_lines),
    }


def local_front_matter(article_id: int, loc: dict, **overrides) -> dict:
    """Front-matter di un file locale così com'è, con i campi locali
    sostituiti da `overrides` (stato, reviewed). Serve alle scritture
    puramente locali, che non devono toccare né il body né i campi di sync."""
    local_loc = {**loc, **overrides}
    return {
        "id": article_id,
        "title": loc.get("title"),
        "published": loc.get("published"),
        "tags": loc.get("tags", []),
        "url": loc.get("url"),
        "canonical_url": loc.get("canonical_url"),
        "cover_image": loc.get("cover_image"),
        "devto_synced_hash": loc.get("devto_synced_hash"),
        **local_only_fields(local_loc, loc.get("published")),
    }


def set_stato(article_id: int, stato) -> dict:
    """Sposta una bozza nel flusso bozza → revisionata → pronta. Pura
    scrittura sul file locale: nessuna chiamata a Dev.to, nessun controllo
    di hash, non tocca il body né i tag. "pubblicato" non si imposta da
    qui: lo deriva `published`, e pubblicare è un'azione a parte."""
    if stato not in STATI_BOZZA:
        raise ValueError(
            f"Stato non valido: {stato!r}. Da qui si impostano solo "
            f"{', '.join(STATI_BOZZA)}; «pubblicato» deriva da `published`."
        )
    local = load_local_articles()
    loc = local.get(article_id)
    if loc is None:
        raise ValueError(f"Nessun file locale per l'articolo {article_id}.")
    if loc.get("published"):
        raise ValueError(
            "L'articolo è già pubblicato: non ha più uno stato di bozza da spostare."
        )

    write_local_article(
        loc["path"], local_front_matter(article_id, loc, stato=stato), loc["body"]
    )
    return {"stato": stato}


def toggle_reviewed(article_id: int) -> dict:
    """Inverte il promemoria locale reviewed. Stessa natura di set_stato:
    pura scrittura sul file locale, nessuna chiamata a Dev.to, nessun
    controllo di hash — serve solo a ricordarsi che un articolo è già stato
    rivisto (bug tecnici, stile), indipendente dal fatto che sia pubblicato
    o meno."""
    local = load_local_articles()
    loc = local.get(article_id)
    if loc is None:
        raise ValueError(f"Nessun file locale per l'articolo {article_id}.")

    new_value = not loc.get("reviewed", False)
    write_local_article(
        loc["path"], local_front_matter(article_id, loc, reviewed=new_value), loc["body"]
    )

    return {"reviewed": new_value}


# Campi del front-matter che passano all'esportazione: quelli che descrivono
# l'articolo e non cambiano finché il testo resta lo stesso.
EXPORT_FIELDS = ("id", "title", "url", "tags")


def overlaps(a: pathlib.Path, b: pathlib.Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def export_published(articles_dir: pathlib.Path, dest: pathlib.Path, prune: bool = False,
                     dry_run: bool = False) -> dict:
    """Copia in `dest` i soli articoli con `published: true`, con il body identico
    byte per byte e il front-matter ridotto ai campi stabili (EXPORT_FIELDS). Ritorna
    {added, updated, unchanged, orphans, removed: liste di nomi file, skipped_drafts:
    conteggio}. Non legge Dev.to e non scrive mai nella cartella degli articoli."""
    articles_dir = articles_dir.resolve()
    dest = dest.resolve()
    if not articles_dir.is_dir():
        raise ValueError(f"Cartella degli articoli non trovata: {articles_dir}")
    if overlaps(articles_dir, dest):
        raise ValueError(
            f"La destinazione ({dest}) e la cartella degli articoli ({articles_dir}) "
            "non possono coincidere né contenersi: la destinazione va in un altro repo."
        )

    result = {"added": [], "updated": [], "unchanged": [], "orphans": [], "removed": [],
              "skipped_drafts": 0}
    wanted = {}
    for path in sorted(articles_dir.glob("*.md")):
        fm, body = split_front_matter(path.read_text(encoding="utf-8"))
        if fm.get("id") is None:
            continue
        # Solo `true` vero: una stringa o un campo mancante non bastano a esporre un articolo.
        if fm.get("published") is not True:
            result["skipped_drafts"] += 1
            continue
        exported = {k: fm[k] for k in EXPORT_FIELDS if k in fm}
        wanted[path.name] = render_article(exported, body)

    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for name, text in wanted.items():
        target = dest / name
        if not target.exists():
            kind = "added"
        elif target.read_text(encoding="utf-8") == text:
            kind = "unchanged"
        else:
            kind = "updated"
        result[kind].append(name)
        if kind != "unchanged" and not dry_run:
            target.write_text(text, encoding="utf-8")

    # File della destinazione che sembrano articoli esportati (hanno un `id`) ma non sono
    # più tra i pubblicati: bozze tornate tali, articoli cancellati, nomi cambiati.
    if dest.is_dir():
        for path in sorted(dest.glob("*.md")):
            if path.name in wanted:
                continue
            fm, _ = split_front_matter(path.read_text(encoding="utf-8"))
            if fm.get("id") is None:
                continue
            result["orphans"].append(path.name)
            if prune:
                result["removed"].append(path.name)
                if not dry_run:
                    path.unlink()
    return result


def _git_root(path: pathlib.Path) -> pathlib.Path | None:
    start = path if path.is_dir() else path.parent
    try:
        out = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return pathlib.Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def export_preview() -> dict:
    """Cosa farebbe l'esportazione, senza scrivere. Segnala anche gli articoli
    pubblicati su Dev.to che nel file risultano ancora bozze (per esempio dopo una
    pubblicazione dal portale): lo script si fida del `published` del file, quindi
    non li esporterebbe, ed è la trappola dello «0 aggiunti»."""
    if EXPORT_DEST is None:
        return {"enabled": False}
    result = export_published(ARTICLES_DIR, EXPORT_DEST, dry_run=True)
    pendenti = None  # None = non verificabile (per esempio senza rete)
    try:
        local = load_local_articles()
        pendenti = [
            {"id": r["id"], "title": r["title"]}
            for r in build_report()
            if r.get("local_file") and r.get("published") is True and local[r["id"]].get("published") is not True
        ]
    except Exception:
        pass
    return {"enabled": True, "dest": str(EXPORT_DEST), "risultato": result, "pendenti": pendenti}


def export_apply(prune: bool = False) -> dict:
    if EXPORT_DEST is None:
        raise ValueError("L'esportazione non è configurata: imposta EXPORT_DEST all'avvio del server.")
    result = export_published(ARTICLES_DIR, EXPORT_DEST, prune=prune)
    result["comando_commit"] = None
    if result["added"] or result["updated"] or result["removed"]:
        root = _git_root(EXPORT_DEST)
        if root is not None:
            rel = EXPORT_DEST.relative_to(root) if root in EXPORT_DEST.parents else pathlib.Path(".")
            result["comando_commit"] = f'git -C "{root}" add "{rel}" && git -C "{root}" commit -m "Aggiorna {rel}"'
    return result


def build_report() -> list:
    local = load_local_articles()
    remote = fetch_remote_articles()
    remote_ids = set()
    rows = []

    for article in remote:
        article_id = article["id"]
        metadati = None
        remote_ids.add(article_id)
        body = article.get("body_markdown") or ""
        remote_hash = hashlib.sha256(body.encode()).hexdigest()
        loc = local.get(article_id)

        if loc is None:
            status = "solo_remoto"
        else:
            synced_hash = loc.get("devto_synced_hash")
            local_hash = hashlib.sha256(loc["body"].encode()).hexdigest()
            status = classify_status(local_hash, remote_hash, synced_hash)
            if status == "allineato":
                mdiff = metadata_diff(loc, article)
                if mdiff:
                    status = "metadati_diversi"
                    metadati = {"differenze": mdiff, "azione": metadata_action(mdiff)}

        rows.append({
            "id": article_id,
            "title": article["title"],
            "tags": article.get("tag_list", []),
            "published": article["published"],
            "url": article["url"],
            "edited_at": article.get("edited_at"),
            "published_at": article.get("published_at"),
            "status": status,
            "metadati": metadati,
            "local_file": loc["file"] if loc else None,
            "stato": effective_stato(article["published"], loc.get("stato") if loc else None),
            "reviewed": loc.get("reviewed", False) if loc else None,
        })

    for article_id, loc in local.items():
        if article_id not in remote_ids:
            rows.append({
                "id": article_id,
                "title": loc.get("title"),
                "tags": loc.get("tags", []),
                "published": loc.get("published"),
                "url": loc.get("url"),
                "edited_at": None,
                "published_at": None,
                "status": "solo_locale",
                "metadati": None,
                "local_file": loc.get("file"),
                "stato": effective_stato(loc.get("published"), loc.get("stato")),
                "reviewed": loc.get("reviewed", False),
            })

    rows.sort(key=lambda r: (r.get("edited_at") or r.get("published_at") or ""), reverse=True)
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Senza questo il browser può servire una risposta cache dal click
        # precedente di "Aggiorna" invece di rifare la richiesta (osservato:
        # serviva un reload completo della pagina per vedere dati nuovi).
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- difesa contro richieste che non vengono dalla pagina ---
    # Il server è su 127.0.0.1 ma senza autenticazione: un sito qualsiasi aperto nel
    # browser può mandare una POST «semplice» a localhost, e il server la esegue anche
    # se il browser non fa leggere la risposta (provato). Tre controlli:
    # Host consentito (contro il DNS rebinding), Origin della pagina stessa sui POST,
    # intestazione propria sui POST.
    def _allowed_hosts(self) -> set:
        port = self.server.server_address[1]
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
        if port == 80:
            hosts |= {"localhost", "127.0.0.1", "[::1]"}
        return hosts

    def _check_host(self) -> bool:
        if self.headers.get("Host", "").lower() in self._allowed_hosts():
            return True
        self._send_json({"error": "Host non consentito: apri la pagina da http://localhost:<porta>."}, status=403)
        return False

    def _check_write_request(self) -> bool:
        origin = self.headers.get("Origin")  # assente da curl e script, sempre presente da un browser
        if origin is not None and origin.lower() not in {f"http://{h}" for h in self._allowed_hosts()}:
            self._send_json({"error": "Origin non consentita: la richiesta non viene dalla pagina."}, status=403)
            return False
        if self.headers.get(WRITE_HEADER) != "1":
            self._send_json({"error": f"Manca l'intestazione {WRITE_HEADER}: 1 (la manda la pagina)."}, status=403)
            return False
        return True

    def do_GET(self):
        if not self._check_host():
            return
        if self.path == "/api/me":
            self._send_json({"username": get_username()})
            return

        if self.path == "/api/export":
            try:
                self._send_json(export_preview())
            except ValueError as e:
                self._send_json({"error": str(e)}, status=409)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if self.path == "/api/articles":
            try:
                self._send_json(build_report())
            except urllib.error.HTTPError as e:
                self._send_json(
                    {"error": f"dev.to ha risposto {e.code}: {e.read().decode('utf-8', 'ignore')}"},
                    status=502,
                )
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        m = re.match(r"^/api/diff/(\d+)$", self.path)
        if m:
            try:
                self._send_json(diff_article(int(m.group(1))))
            except ValueError as e:
                self._send_json({"error": str(e)}, status=409)
            except urllib.error.HTTPError as e:
                self._send_json(
                    {"error": f"dev.to ha risposto {e.code}: {e.read().decode('utf-8', 'ignore')}"},
                    status=502,
                )
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        # Static files da web/
        rel = self.path.lstrip("/") or "index.html"
        file_path = (WEB_DIR / rel).resolve()
        if WEB_DIR not in file_path.parents and file_path != WEB_DIR:
            self.send_error(403)
            return
        if not file_path.is_file():
            self.send_error(404)
            return
        content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else \
            "application/javascript; charset=utf-8" if file_path.suffix == ".js" else \
            "text/css; charset=utf-8" if file_path.suffix == ".css" else \
            "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Senza questo il browser può servire dalla cache una versione vecchia
        # di index.html/script dopo una modifica e un riavvio del server
        # (osservato: il pulsante "Segna come pronta" non compariva finché non
        # si forzava un refresh). Le risposte /api/* lo fanno già.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("Body della richiesta non è JSON valido.")
        return data if isinstance(data, dict) else {}

    def do_POST(self):
        if not self._check_host() or not self._check_write_request():
            return
        if self.path == "/api/export":
            # La destinazione è EXPORT_DEST, fissata all'avvio: dal corpo si legge solo `prune`.
            try:
                prune = self._read_json_body().get("prune", False)
                if not isinstance(prune, bool):
                    raise ValueError("prune deve essere true o false.")
                self._send_json({"ok": True, **export_apply(prune)})
            except ValueError as e:
                self._send_json({"error": str(e)}, status=409)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        m = re.match(r"^/api/(import|pull|push|metadata|stato|toggle-reviewed)/(\d+)$", self.path)
        if not m:
            self.send_error(404)
            return
        action, article_id = m.group(1), int(m.group(2))
        handler = {
            "import": import_article_locally,
            "pull": pull_article,
            "push": push_article,
            "metadata": sync_metadata,
            "stato": lambda i: set_stato(i, self._read_json_body().get("stato")),
            "toggle-reviewed": toggle_reviewed,
        }[action]
        try:
            result = handler(article_id)
            self._send_json({"ok": True, **result})
        except ValueError as e:
            self._send_json({"error": str(e)}, status=409)
        except urllib.error.HTTPError as e:
            self._send_json(
                {"error": f"dev.to ha risposto {e.code}: {e.read().decode('utf-8', 'ignore')}"},
                status=502,
            )
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)


def main():
    if not API_KEY:
        print(
            "ATTENZIONE: DEV_TO_API_KEY non è impostata nell'ambiente. "
            "Il server parte comunque, ma /api/articles fallirà finché non la esporti.",
            file=sys.stderr,
        )
    print(f"Articoli letti da {ARTICLES_DIR}", file=sys.stderr)
    if not ARTICLES_DIR.is_dir():
        if pathlib.Path.cwd().resolve() == TOOL_ROOT:
            hint = (
                "Stai lanciando il server dalla cartella dello strumento, che non contiene "
                "articoli. Lancialo dalla radice del repo degli articoli, oppure da qui con "
                "ARTICLES_DIR=/percorso/del/repo-articoli/articles"
            )
        else:
            hint = (
                "Lancia il server dalla radice del repo degli articoli oppure imposta "
                "ARTICLES_DIR."
            )
        print(
            "ATTENZIONE: la cartella degli articoli non esiste, quindi ogni articolo "
            "risulterà «solo su Dev.to». " + hint,
            file=sys.stderr,
        )
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Interfaccia articoli dev.to su http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
