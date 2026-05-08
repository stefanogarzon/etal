# Et al.

*Your papers and everyone else's.*

Local PDF library manager for clinical research. Drop a PDF, it pulls metadata from CrossRef, suggests a topic, renames the file as `Author-Title-Journal-Year.pdf`, files it under the matching topic folder, and indexes it in `library.md`.

## Quick start

```bash
cd cardio_library
python -m venv .venv && source .venv/bin/activate    # macOS/Linux
# .venv\Scripts\activate                              # Windows
pip install -r requirements.txt
python app.py
```

A native window opens. On first run, click **Choose folder…** to pick where the library lives (e.g. `~/Documents/EtAl`). The app creates:

```
EtAl/
├── library.db           # SQLite — source of truth
├── library.md           # auto-regenerated index
├── topics.yaml          # editable taxonomy
├── _uncategorized/      # overflow
├── IVUS/
├── ACS/
└── ...
```

## How it works

- **Drop a PDF** → first-page text → DOI regex → CrossRef → metadata + topic suggestion → confirm → file is renamed, moved, indexed.
- **No DOI found?** Manually paste a DOI/URL or fill the 4 fields by hand.
- **Duplicate DOI?** Skipped silently with a toast notification.
- **Library tab** → SQLite FTS5 search across title/abstract/author/tags, filter by topic, click to read PDF inline.
- **Topics tab** → add/edit/delete topics. Renaming a topic moves its folder and updates the DB. Deleting a topic with articles in it is blocked.

## Search syntax

Searches use SQLite FTS5 prefix matching. Examples:

- `imaging` — matches "imaging", "imaged"
- `IVUS PCI` — matches articles containing both terms
- `"left main"` — exact phrase

## Packaging as a standalone app

To bundle into a single `.app` (macOS) / `.exe` (Windows):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --add-data "frontend:frontend" --add-data "topics.yaml:." app.py
```

The output lands in `dist/`. On macOS, codesign and notarize for distribution; for personal use, the unsigned `.app` runs fine.

## Config location

The library path is stored in `~/.config/etal/config.json`. To switch libraries, delete that file and restart.

## Limitations & next steps

- DOI extraction depends on the publisher embedding a recognizable DOI on page 1–2. ~95% hit rate in mainstream cardio journals; older scans need manual lookup.
- CrossRef abstracts come as JATS XML — stripped to plain text but formatting is lost.
- No tag autocompletion yet — tags are free-text.
- No bulk import — drop one PDF at a time (intentional, to keep the confirm-before-save flow).
