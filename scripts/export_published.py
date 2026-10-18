#!/usr/bin/env python3
"""Esporta gli articoli pubblicati in una cartella a parte.

Pensato per alimentare una base di conoscenza (una wiki, un "second brain")
con i soli articoli già usciti, senza le bozze né la storia git delle bozze:
la cartella di destinazione sta in un altro repo e ci si committa il risultato.

Per ogni articolo con `published: true` scrive `<dest>/<nome file originale>`
con il body identico byte per byte e un front-matter ridotto ai campi stabili
(`id`, `title`, `url`, `tags`). Restano fuori `devto_synced_hash`, `stato`,
`reviewed` e `cover_image`: cambiano senza che il testo cambi, e chi controlla i
file con un checksum (come wiki-mcp-api, sull'intero file) vedrebbe sorgenti
"modificate" che non lo sono.

Non legge mai Dev.to e non scrive mai nella cartella degli articoli. Si fida di
`published` nel front-matter locale: conviene lanciarlo dopo un sync, perché un
articolo tornato bozza sul portale risulterebbe ancora esportato.

Uso:
    python3 scripts/export_published.py --dest /percorso/second-brain/raw/devto
    python3 scripts/export_published.py --dest ... --dry-run
    python3 scripts/export_published.py --dest ... --prune
"""
import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "web"))
import server  # noqa: E402  (solo per riusare parser e formato dei file articolo)

# Campi del front-matter che passano all'esportazione: quelli che descrivono
# l'articolo e non cambiano finché il testo resta lo stesso.
EXPORT_FIELDS = ("id", "title", "url", "tags")


def overlaps(a: pathlib.Path, b: pathlib.Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def export_published(articles_dir: pathlib.Path, dest: pathlib.Path, prune: bool = False,
                     dry_run: bool = False) -> dict:
    """Ritorna {added, updated, unchanged, orphans, removed, skipped_drafts}:
    liste di nomi file (skipped_drafts è un conteggio)."""
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
        fm, body = server.split_front_matter(path.read_text(encoding="utf-8"))
        if fm.get("id") is None:
            continue
        # Solo `true` vero: una stringa o un campo mancante non bastano a esporre un articolo.
        if fm.get("published") is not True:
            result["skipped_drafts"] += 1
            continue
        exported = {k: fm[k] for k in EXPORT_FIELDS if k in fm}
        wanted[path.name] = server.render_article(exported, body)

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
            fm, _ = server.split_front_matter(path.read_text(encoding="utf-8"))
            if fm.get("id") is None:
                continue
            result["orphans"].append(path.name)
            if prune:
                result["removed"].append(path.name)
                if not dry_run:
                    path.unlink()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--articles-dir", default=os.environ.get("ARTICLES_DIR", "articles"),
                        help="cartella degli articoli (default: ARTICLES_DIR o ./articles)")
    parser.add_argument("--dest", required=True, help="cartella di destinazione, in un altro repo")
    parser.add_argument("--prune", action="store_true",
                        help="cancella dalla destinazione gli articoli esportati che non sono più pubblicati")
    parser.add_argument("--dry-run", action="store_true", help="mostra cosa farebbe senza scrivere")
    args = parser.parse_args()

    try:
        r = export_published(pathlib.Path(args.articles_dir), pathlib.Path(args.dest),
                             prune=args.prune, dry_run=args.dry_run)
    except ValueError as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 1

    prefix = "[prova, nessun file toccato] " if args.dry_run else ""
    print(f"{prefix}aggiunti {len(r['added'])}, aggiornati {len(r['updated'])}, "
          f"invariati {len(r['unchanged'])}; bozze escluse {r['skipped_drafts']}")
    for name in r["added"]:
        print(f"  + {name}")
    for name in r["updated"]:
        print(f"  ~ {name}")
    if r["orphans"]:
        verb = "rimossi" if args.prune else "non più pubblicati (usa --prune per rimuoverli)"
        print(f"{len(r['orphans'])} file {verb}:")
        for name in r["orphans"]:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
