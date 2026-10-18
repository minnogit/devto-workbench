# devto-workbench

Strumenti per tenere i propri articoli Dev.to come file Markdown in un repository Git e lavorarci senza perdere le modifiche fatte direttamente dal portale.

Dev.to non offre una storia delle modifiche, e chi scrive può modificare un articolo sia dal portale sia dal repo. Qui ogni file ricorda l'hash dell'ultima versione sincronizzata, così prima di sovrascrivere in un senso o nell'altro si vede se l'altro lato è cambiato nel frattempo.

Il repo contiene tre cose:

- `web/`: un'interfaccia web locale (solo standard library Python) con vista elenco, card e kanban per stato editoriale, stato di sync di ogni articolo, confronto riga per riga e pull/push per i casi non ambigui;
- `.claude/skills/devto-articles/`: una skill per Claude Code che gestisce import, pull, push, pubblicazione e conflitti, appoggiandosi a un server MCP per Dev.to;
- `scripts/import_articles.py`: l'importazione bulk iniziale di tutti gli articoli;
- `scripts/export_published.py`: l'esportazione dei soli articoli pubblicati in un'altra cartella, per alimentare una wiki o una base di conoscenza.

Gli articoli non stanno qui. Servono un repo a parte, meglio privato perché le bozze lo sono, con una cartella `articles/`.

## Requisiti

- Python 3.10 o successivo (nel codice c'è la sintassi `X | None`). L'ho provato con Python 3.14 su Linux, non su Windows.
- Una API key Dev.to ([Settings → Extensions](https://dev.to/settings/extensions)), esportata come `DEV_TO_API_KEY`. Non va mai committata.
- Per lo script di import: `pip install httpx`. Il server web non ha dipendenze.
- Per la skill: un server MCP per Dev.to configurato nel repo dei contenuti (vedi `.mcp.json.example`), in una versione che supporti la lettura delle proprie bozze e il campo `ai_disclosure_level`. La skill indica quali tool usa.

## Come sono organizzati i contenuti

```
il-tuo-repo/
  articles/<id>-<slug>.md     un file per articolo
  assets/<id>/                copia offline delle immagini (facoltativa)
```

Il nome del file è comodo ma non conta: l'identificatore è l'`id` nel front-matter. Il front-matter ha una riga per campo, nella forma `chiave: <valore JSON>` (non è YAML vero, anche se ci assomiglia):

```
---
id: 1234567
title: "Titolo"
published: false
tags: ["git", "linux"]
url: "https://dev.to/..."
canonical_url: "https://dev.to/..."
cover_image: null
devto_synced_hash: "sha256 del body come era su Dev.to all'ultimo sync"
stato: "revisionata"
reviewed: true
---

Il testo dell'articolo...
```

Il testo dopo il front-matter è il `body_markdown` esatto di Dev.to, byte per byte. Non vanno normalizzati spazi o markdown: cambierebbe l'hash e produrrebbe falsi conflitti. Il documento [`CLAUDE.md`](./CLAUDE.md) spiega schema e meccanismo a hash in dettaglio.

`stato` e `reviewed` sono campi solo locali, mai inviati a Dev.to. `stato` vale `bozza` (se manca), `revisionata` o `pronta`. «Pubblicato» non è un valore: è lo stato di un articolo con `published: true`.

## Interfaccia web

Dalla radice del repo dei contenuti:

```bash
python3 /percorso/di/devto-workbench/web/server.py
```

poi apri http://localhost:8787. Il server ascolta solo su `127.0.0.1`.

| Variabile | Significato |
|---|---|
| `DEV_TO_API_KEY` | Obbligatoria per parlare con Dev.to. Senza, il server parte ma `/api/articles` risponde con un errore. |
| `ARTICLES_DIR` | Dove cercare gli articoli. Di default `articles/` nella cartella da cui lanci il server. |
| `ASSETS_DIR` | Dove salvare le immagini. Di default `assets/` accanto a `ARTICLES_DIR`. |
| `PORT` | Porta del server, di default 8787. |
| `DEVTO_USERNAME` | Nome mostrato nel titolo. Se manca lo chiede a Dev.to. |

Se lo lanci dalla cartella dello strumento (per esempio mentre lo sviluppi) non trova articoli, e lo scrive all'avvio. Imposta `ARTICLES_DIR`:

```bash
ARTICLES_DIR=/percorso/del/repo-articoli/articles python3 web/server.py
```

Il pulsante «Scarica in locale» rifiuta di creare file se la cartella degli articoli è dentro il repo dello strumento.

Il server non ricarica il codice da solo: dopo una modifica a `web/server.py` o `web/index.html` va riavviato.

### Stati di sync

Per ogni articolo confronta tre cose: l'hash del body locale, quello del body remoto e `devto_synced_hash`.

| Stato | Significato | Azione dalla pagina |
|---|---|---|
| allineato | Nessuno ha cambiato nulla | nessuna |
| remoto più recente | È cambiato solo il portale | scarica l'aggiornamento (pull) |
| locale più recente | È cambiato solo il file | manda online (push) |
| conflitto | Sono cambiati entrambi, in modo diverso | nessuna: il merge non è automatico, si risolve con la skill |
| metadati diversi | Il testo è allineato ma cambiano `published`, titolo, tag, copertina o indirizzi | se l'articolo è stato pubblicato dal portale: «Segna come pubblicato». Per il resto nessuna, si decide con la skill |
| solo su Dev.to | Esiste sul portale ma non c'è il file | scarica in locale |
| solo locale | C'è il file ma non l'articolo | nessuna |

### Metadati diversi

Pubblicare un articolo dal portale non cambia il testo, quindi il controllo a hash lo vedrebbe «allineato», mentre cambiano `published` e gli indirizzi, che perdono lo slug provvisorio. Per questo c'è lo stato «metadati diversi»: scatta quando il testo è allineato ma qualcuno tra `title`, `published`, `tags`, `url`, `canonical_url` e `cover_image` differisce dal file.

Non c'è un hash dei metadati all'ultimo sync, quindi di solito non si sa quale lato è cambiato, e la pagina non scrive niente, come per un conflitto. L'unico caso non ambiguo è la pubblicazione dal portale: il file è una bozza, il remoto è pubblicato e cambiano solo `published`, `url` e `canonical_url`. Lì compare «Segna come pubblicato», che aggiorna questi tre campi e toglie `stato`, lasciando il testo identico byte per byte. Se insieme cambiano anche il titolo, i tag o la copertina, oppure un articolo è tornato bozza, il pulsante non c'è e il confronto mostra i campi.

Dopo una pubblicazione dal portale va fatto prima questo passo e poi l'esportazione, perché lo script di esportazione legge il `published` del file.

### Kanban

La vista Kanban ha una colonna per stato editoriale: Bozza, Revisionata, Pronta, Pubblicato. Trascinare una bozza tra le prime tre colonne imposta `stato` nel front-matter del file, senza chiamate a Dev.to. La colonna Pubblicato non accetta drop: pubblicare è un'azione a parte, non un trascinamento. Lo stato di sync resta un badge sulla card.

## Esportare gli articoli pubblicati

Se vuoi usare gli articoli come sorgenti di una wiki o di una base di conoscenza, senza esporre le bozze, `scripts/export_published.py` copia in una cartella a parte solo quelli con `published: true`:

```bash
python3 /percorso/di/devto-workbench/scripts/export_published.py --dest /percorso/altro-repo/raw/devto
```

Legge `articles/` dalla cartella da cui lo lanci (o da `ARTICLES_DIR`, o da `--articles-dir`) e non scrive mai lì. Per ogni articolo pubblicato crea un file con lo stesso nome, il body identico byte per byte e un front-matter ridotto a `id`, `title`, `url` e `tags`. Restano fuori `devto_synced_hash`, `stato`, `reviewed` e `cover_image`: cambiano senza che il testo cambi, e uno strumento che confronta i file con un checksum li vedrebbe come modificati.

- `--dry-run` mostra cosa farebbe senza scrivere.
- Una seconda esecuzione senza modifiche non tocca nessun file.
- Se un articolo esportato non è più pubblicato (è tornato bozza, o è stato cancellato) il file resta nella destinazione e viene segnalato. `--prune` lo rimuove, ma solo tra i file che hanno un `id` nel front-matter.
- Rifiuta una destinazione che coincide con la cartella degli articoli o la contiene.

Si fida del `published` nel front-matter locale: conviene lanciarlo dopo un sync, perché un articolo tornato bozza sul portale risulterebbe ancora esportato. La destinazione va in un repo diverso da quello degli articoli, e lì si committa il risultato.

## Skill per Claude Code

`.claude/skills/devto-articles/` va nella cartella `.claude/skills/` del repo dei contenuti (copiata o collegata con un link simbolico: non ho verificato che Claude Code segua i link simbolici per le skill). Descrive le operazioni in cui un confronto a hash evita di sovrascrivere una modifica altrui: creazione di un articolo nuovo, pull, push, conflitti, pubblicazione. Non contiene norme di scrittura: quelle sono del repo dei contenuti.

## Sicurezza

- La chiave API vive solo nell'ambiente. Il server la aggiunge lui alle richieste verso Dev.to, il browser non la vede mai.
- I punti di scrittura del server (`/api/pull`, `/api/push`, `/api/stato` e gli altri) non hanno autenticazione: chiunque possa connettersi a `127.0.0.1` sulla tua macchina può chiamarli. Non esporre la porta.
- Tieni privato il repo dei contenuti: le bozze possono contenere appunti non destinati alla pubblicazione.

## Licenza

MIT, vedi [`LICENSE`](./LICENSE).
