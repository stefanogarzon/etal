# Cardio Library — project context

Local PDF library manager for clinical research articles. Drop a PDF → CrossRef metadata → topic suggestion → file renamed and indexed.

## Stack

- **Python 3.11+**
- **PyWebView** — native window wrapping the UI (no browser)
- **FastAPI + uvicorn** — HTTP server running in a background thread
- **SQLite + FTS5** — single-file DB inside the user's library folder, source of truth
- **pypdf** — first-page text extraction
- **CrossRef API** — DOI → metadata (no auth, no key)
- **Vanilla JS + HTML** — single-page frontend, no build step

## File map

```
app.py              # entry: starts uvicorn thread + opens PyWebView window
server.py           # FastAPI routes + all business logic (DB, ingestion, topics)
frontend/index.html # single-page UI (3 tabs: Inbox, Library, Topics)
topics.yaml         # default taxonomy seed (copied to library on first run)
requirements.txt
README.md
```

## Architecture decisions (locked in)

1. **SQLite is source of truth.** `library.md` is regenerated on every mutation — never edit it by hand.
2. **Confirmation-before-save on ingestion.** Dropping a PDF stages it in a tempdir; user reviews metadata in a card and clicks "Save" to commit. Discarding removes the temp file.
3. **No LLM.** DOI extraction + CrossRef + keyword-based topic matching. User confirms every classification.
4. **Topic = folder.** Renaming a topic moves the folder and updates DB rows. Deleting a topic with articles is blocked.
5. **Duplicates by DOI.** Skipped silently (with toast notification) on save. Enforced both at app level and via UNIQUE constraint.
6. **Library path is configurable.** Stored in `~/.config/cardio-library/config.json`. First run shows a folder picker.

## Filename convention

`{Author}-{TitleSlug}-{Journal}-{Year}.pdf`

- Author: first author surname, alpha-only
- TitleSlug: first 6 significant words (stopwords removed), CamelCase
- Journal: `short-container-title` from CrossRef, alphanumeric only
- Year: 4 digits, or `ND` if missing
- Collisions: `-2`, `-3` suffix

## API surface

| Method | Path                       | Purpose                                  |
|--------|----------------------------|------------------------------------------|
| GET    | `/api/config`              | Has the user picked a library folder?    |
| POST   | `/api/setup`               | Initialize library at given path         |
| POST   | `/api/ingest`              | Stage PDF, return DOI/metadata/suggestion|
| POST   | `/api/lookup_doi`          | Manual DOI/URL → CrossRef                |
| POST   | `/api/save`                | Confirm & commit staged PDF              |
| GET    | `/api/articles?q=&topic=`  | List/search (FTS5 prefix)                |
| GET    | `/api/article/{id}`        | Single article metadata                  |
| DELETE | `/api/article/{id}`        | Remove from DB + delete PDF              |
| GET    | `/pdf/{id}`                | Serve PDF inline (used by `<embed>`)     |
| GET    | `/api/topics`              | Topics + article counts                  |
| POST   | `/api/topics`              | New topic                                |
| PUT    | `/api/topics`              | Rename + update keywords                 |
| DELETE | `/api/topics/{name}`       | Delete (only if empty)                   |

## Conventions

- Python: type hints throughout, `from __future__ import annotations`, no async unless needed.
- Errors: raise `HTTPException` with clear `detail` strings — frontend toasts them as-is.
- Frontend: no framework, no build. Plain `fetch()` against the same-origin API. Keep `index.html` self-contained.
- Tests: there's no test suite yet. When adding logic, smoke-test via `TestClient` (see how server.py was validated initially).

## Starter pack
The bundled `topics.yaml` is a cardio interventional pack (18 topics). 
When the project ships beyond personal use, this becomes one of several 
area-specific packs the user picks at first run.

## Things explicitly NOT in v0.1

- Bulk PDF import
- Per-article notes/highlights
- Export to BibTeX / CSV
- Cloud sync
- Multi-user / sharing
- LLM-powered classification or summarization

## When iterating

- Keep the **SQLite-as-truth + regenerate-md** invariant. Any feature that touches articles must call `regenerate_library_md()` after committing.
- Keep the **confirmation-before-save** flow. Don't add silent ingestion — bad data is harder to clean than missing data.
- Keep the **single-file frontend**. If it ever needs a build step, reconsider the whole frontend stack first.
- Before changing the filename convention, consider that existing PDFs on disk won't match — write a migration script.
