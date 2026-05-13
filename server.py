"""
Et al. backend — FastAPI server.

Responsibilities:
- First-run configuration (library folder path)
- PDF ingestion: extract DOI → CrossRef lookup → topic suggestion → save
- SQLite + FTS5 storage
- Search & filter
- Topic CRUD (with folder rename on the filesystem)
- Auto-regeneration of library.md after every mutation
- Serving PDFs to the embedded webview
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

import httpx
import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

__version__ = "0.1.2"

APP_DIR = Path(__file__).parent
FRONTEND_DIR = APP_DIR / "frontend"
DEFAULT_TOPICS_FILE = APP_DIR / "topics.yaml"

CONFIG_DIR = Path.home() / ".config" / "etal"
CONFIG_FILE = CONFIG_DIR / "config.json"

DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+", re.IGNORECASE)
CROSSREF_URL = "https://api.crossref.org/works/{doi}"

logger = logging.getLogger("etal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Set by app.py once the webview window exists
_window_ref: Any = None


def set_window_ref(win: Any) -> None:
    global _window_ref
    _window_ref = win


def load_config() -> dict | None:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return None


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def library_root() -> Path:
    cfg = load_config()
    if not cfg:
        raise HTTPException(status_code=400, detail="Library not configured")
    return Path(cfg["library_path"])


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT UNIQUE,
    author TEXT NOT NULL,
    title TEXT NOT NULL,
    journal TEXT,
    year INTEGER,
    topic TEXT NOT NULL,
    filename TEXT NOT NULL,
    abstract TEXT,
    tags TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, abstract, author, tags,
    content='articles', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, abstract, author, tags)
    VALUES (new.id, new.title, new.abstract, new.author, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, abstract, author, tags)
    VALUES ('delete', old.id, old.title, old.abstract, old.author, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, abstract, author, tags)
    VALUES ('delete', old.id, old.title, old.abstract, old.author, old.tags);
    INSERT INTO articles_fts(rowid, title, abstract, author, tags)
    VALUES (new.id, new.title, new.abstract, new.author, new.tags);
END;
"""


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(library_root() / "library.db")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_library(path: Path) -> None:
    """Create folder structure, DB, and seed topics.yaml on first run."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "_uncategorized").mkdir(exist_ok=True)
    (path / "_inbox").mkdir(exist_ok=True)  # quarantine for failed ingestion

    # Seed topics.yaml from default template if missing
    topics_file = path / "topics.yaml"
    if not topics_file.exists():
        shutil.copy(DEFAULT_TOPICS_FILE, topics_file)

    # Create folders for each topic
    for topic in load_topics(path).keys():
        (path / topic).mkdir(exist_ok=True)

    # Init DB
    conn = sqlite3.connect(path / "library.db")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Seed library.md
    (path / "library.md").write_text("# Et al.\n\n_Empty._\n")


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

def topics_path(root: Path | None = None) -> Path:
    return (root or library_root()) / "topics.yaml"


def load_topics(root: Path | None = None) -> dict[str, dict]:
    p = topics_path(root)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("topics", {})


def save_topics(topics: dict[str, dict]) -> None:
    topics_path().write_text(yaml.safe_dump({"topics": topics}, sort_keys=True))


_STEM_SUFFIXES = ("ations", "ating", "ated", "ation", "ies", "ied", "ying",
                  "ing", "ed", "es", "s")


def _stem(word: str) -> str:
    """Crude English suffix-stripper. Returns the word unchanged if no suffix
    matches or the stem would be too short."""
    w = word.lower()
    for suffix in _STEM_SUFFIXES:
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def suggest_topic(title: str, body: str) -> str:
    """Score topics by keyword frequency. Title hits dominate (20x); longer
    keywords count more so specific terms beat generic single words. Topic
    name appearing in the title is a strong dedicated bonus. Single-word
    keywords match their stem so 'bifurcation' catches 'bifurcated'."""
    title_lc = (title or "").lower()
    body_lc = (body or "").lower()
    scores: dict[str, float] = {}
    for topic, meta in load_topics().items():
        score = 0.0
        for kw in meta.get("keywords", []):
            kw_lc = kw.lower().strip()
            if not kw_lc:
                continue
            # Single-word keywords match the stem; multi-word stay literal
            matcher = _stem(kw_lc) if (" " not in kw_lc and "-" not in kw_lc) else kw_lc
            kw_weight = max(len(kw_lc), 1)
            score += title_lc.count(matcher) * kw_weight * 20.0
            score += body_lc.count(matcher) * kw_weight
        # Topic name in title (stem-aware prefix match for longer names)
        topic_lc = topic.lower()
        if len(topic_lc) >= 6:
            pattern = r"\b" + re.escape(_stem(topic_lc))
        else:
            pattern = r"\b" + re.escape(topic_lc) + r"\b"
        if re.search(pattern, title_lc):
            score += 50.0
        if score > 0:
            scores[topic] = score
    if not scores:
        return "_uncategorized"
    best = max(scores, key=lambda k: scores[k])
    logger.info("Topic scores: %s -> %s",
                {k: round(v, 1) for k, v in sorted(scores.items(), key=lambda x: -x[1])[:5]},
                best)
    return best


# ---------------------------------------------------------------------------
# PDF & metadata
# ---------------------------------------------------------------------------

def extract_first_pages_text(pdf_path: Path, n: int = 2) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        pages = reader.pages[:n]
        return "\n".join(p.extract_text() or "" for p in pages)
    except Exception as e:
        logger.warning("PDF extraction failed: %s", e)
        return ""


def extract_abstract_heuristic(text: str) -> str:
    """Find the abstract section in raw PDF text. Used when CrossRef has none."""
    if not text:
        return ""

    start = -1

    # 1) "Abstract" / "Summary" as a section heading on its own line
    m = re.search(r"(?:^|\n)\s*(?:abstract|summary)\s*[:.]?\s*\n", text, re.IGNORECASE)
    if m:
        start = m.end()

    # 2) "Abstract" / "Summary" followed by capitalized body text
    if start < 0:
        m = re.search(r"\b(?:abstract|summary)\s+(?=[A-Z])", text)
        if m:
            start = m.end()

    # 3) Structured abstract — starts with Background/Objective/Aims/etc.
    if start < 0:
        m = re.search(
            r"\b(background|objectives?|aims?|context|purpose|rationale)\s*[—–\-:]\s*",
            text, re.IGNORECASE,
        )
        if m:
            start = m.start()  # include the section label

    # 4) Fallback: any standalone "abstract" or "summary"
    if start < 0:
        m = re.search(r"\b(?:abstract|summary)\b", text, re.IGNORECASE)
        if m:
            start = m.end()

    if start < 0:
        logger.info("Abstract heuristic: no anchor in %d chars", len(text))
        return ""

    rest = text[start:start + 4500]
    end_patterns = [
        r"\n\s*\d+\.?\s*introduction\b",
        r"\n\s*introduction\s*\n",
        r"\bkey\s*words?\s*:",
        r"\bkeywords?\s*:",
        r"\n\s*\d+\s*\|\s*introduction",
        r"\n\s*©\s*\d{4}",  # copyright line often follows abstract
    ]
    end_pos = len(rest)
    for pat in end_patterns:
        em = re.search(pat, rest, re.IGNORECASE)
        if em and em.start() > 120:
            end_pos = min(end_pos, em.start())

    abstract = rest[:end_pos].strip()
    abstract = re.sub(r"\s+", " ", abstract).strip()
    if len(abstract) > 2500:
        abstract = abstract[:2500].rstrip() + "..."
    logger.info("Abstract heuristic: text=%d, extracted=%d", len(text), len(abstract))
    return abstract


def find_doi(text: str) -> str | None:
    if not text:
        return None
    m = DOI_REGEX.search(text)
    if not m:
        # Whitespace-split DOI: anchor on a "doi:" or "doi.org/" prefix
        anchor = re.search(
            r"(?:doi[:\s]*|doi\.org[/]|dx\.doi\.org[/])",
            text, re.IGNORECASE,
        )
        if anchor:
            tail = text[anchor.end():anchor.end() + 120]
            compact = re.sub(r"\s+", "", tail)
            m = DOI_REGEX.search(compact)
    if not m:
        return None
    doi = m.group(0).rstrip(".,;)")
    for ext in (".pdf", ".PDF", ".Pdf"):
        if doi.endswith(ext):
            doi = doi[: -len(ext)]
            break
    return doi


def find_doi_in_filename(filename: str | None) -> str | None:
    """Some publishers encode the DOI in the filename, with @ replacing /."""
    if not filename:
        return None
    stem = filename.rsplit(".", 1)[0]
    return find_doi(stem.replace("@", "/"))


def find_doi_in_metadata(pdf_path: Path) -> str | None:
    """Look for a DOI in the PDF's /Info and XMP metadata."""
    try:
        reader = PdfReader(str(pdf_path))
        if reader.metadata:
            for key in ("/Subject", "/Keywords", "/Title", "/doi", "/DOI"):
                val = reader.metadata.get(key)
                if val:
                    found = find_doi(str(val))
                    if found:
                        return found
        xmp = reader.xmp_metadata
        if xmp:
            for attr in ("dc_identifier", "prism_doi", "prism_url"):
                val = getattr(xmp, attr, None)
                if not val:
                    continue
                values = val if isinstance(val, (list, tuple)) else [val]
                for v in values:
                    found = find_doi(str(v))
                    if found:
                        return found
    except Exception as e:
        logger.warning("PDF metadata DOI extraction failed: %s", e)
    return None


def crossref_lookup(doi: str) -> dict | None:
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(
                CROSSREF_URL.format(doi=doi),
                headers={"User-Agent": "EtAl/0.1 (local app)"},
            )
            if r.status_code != 200:
                return None
            msg = r.json().get("message", {})
            return parse_crossref(msg, doi)
    except Exception as e:
        logger.warning("CrossRef lookup failed for %s: %s", doi, e)
        return None


def parse_crossref(msg: dict, doi: str) -> dict:
    title = (msg.get("title") or [""])[0]
    journal = (msg.get("container-title") or [""])[0]
    journal_short = (msg.get("short-container-title") or [journal])[0]
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = parts[0][0]
            break
    authors = msg.get("author") or []
    first_author = authors[0].get("family", "Unknown") if authors else "Unknown"
    abstract = msg.get("abstract", "")
    # CrossRef abstracts contain JATS XML — strip tags crudely
    abstract = re.sub(r"<[^>]+>", "", abstract).strip()
    return {
        "doi": doi,
        "author": first_author,
        "title": title,
        "journal": journal_short or journal,
        "year": year,
        "abstract": abstract,
    }


def slugify(s: str, max_words: int = 6) -> str:
    """CamelCase-ish slug: take first N significant words, drop punctuation."""
    words = re.findall(r"[A-Za-z0-9]+", s)
    stop = {"a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "with", "by"}
    significant = [w for w in words if w.lower() not in stop][:max_words]
    return "".join(w.capitalize() for w in significant) or "Untitled"


def make_filename(meta: dict) -> str:
    author = re.sub(r"[^A-Za-z]", "", meta.get("author") or "Unknown") or "Unknown"
    title = slugify(meta.get("title") or "Untitled")
    journal = re.sub(r"[^A-Za-z0-9]", "", meta.get("journal") or "Journal") or "Journal"
    year = meta.get("year") or "ND"
    return f"{author}-{title}-{journal}-{year}.pdf"


def unique_path(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem, ext = target.stem, target.suffix
    i = 2
    while True:
        candidate = folder / f"{stem}-{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# library.md generator
# ---------------------------------------------------------------------------

def regenerate_library_md() -> None:
    root = library_root()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM articles ORDER BY topic, year DESC, author"
        ).fetchall()

    lines = ["# Et al.", "", "_Your papers and everyone else's._", ""]
    lines.append(f"_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_  ")
    lines.append(f"_Total: {len(rows)} articles_")
    lines.append("")

    by_topic: dict[str, list] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append(r)

    for topic in sorted(by_topic.keys()):
        items = by_topic[topic]
        lines.append(f"## {topic} ({len(items)})")
        lines.append("")
        for r in items:
            rel = f"{r['topic']}/{r['filename']}"
            entry = (
                f"- [{r['author']} — {r['title']} — *{r['journal']}* — {r['year']}]"
                f"({rel})"
            )
            if r["doi"]:
                entry += f"  ·  [doi](https://doi.org/{r['doi']})"
            lines.append(entry)
        lines.append("")

    (root / "library.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Et al.")


# ----- Models -----

class SetupRequest(BaseModel):
    library_path: str


class SaveRequest(BaseModel):
    temp_id: str  # filename in tempdir from /api/ingest
    doi: str | None = None
    author: str
    title: str
    journal: str | None = None
    year: int | None = None
    topic: str
    abstract: str | None = None
    tags: str | None = None


class TopicRequest(BaseModel):
    name: str
    keywords: list[str] = []


class TopicRename(BaseModel):
    old_name: str
    new_name: str
    keywords: list[str] = []


class TopicSyncRequest(BaseModel):
    mode: Literal["update", "reset"]


class ArticleEditRequest(BaseModel):
    author: str | None = None
    title: str | None = None
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    topic: str | None = None
    abstract: str | None = None
    tags: str | None = None


class RefreshMetadataRequest(BaseModel):
    only_missing: bool = True


class ReclassifyRequest(BaseModel):
    apply: bool = False


# ----- Routes: setup -----

@app.get("/api/config")
def get_config() -> dict:
    cfg = load_config()
    return {"configured": cfg is not None, "config": cfg}


@app.post("/api/setup")
def post_setup(req: SetupRequest) -> dict:
    path = Path(req.library_path).expanduser().resolve()
    init_library(path)
    save_config({"library_path": str(path)})
    return {"ok": True, "library_path": str(path)}


# ----- Routes: ingestion -----

# Tempdir for staged uploads awaiting confirmation
STAGING = Path(tempfile.gettempdir()) / "etal_staging"
STAGING.mkdir(exist_ok=True)


@app.post("/api/ingest")
async def post_ingest(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDFs are accepted")

    # Stage the file
    temp_id = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}.pdf"
    temp_path = STAGING / temp_id
    content = await file.read()
    temp_path.write_bytes(content)

    # Extract text and gather DOI candidates (text + PDF metadata + filename)
    text = extract_first_pages_text(temp_path)
    text_doi = find_doi(text)
    meta_doi = find_doi_in_metadata(temp_path)
    fn_doi = find_doi_in_filename(file.filename)

    # Try each candidate against CrossRef — first that resolves wins
    doi: str | None = None
    cr: dict | None = None
    seen: set[str] = set()
    for candidate in (text_doi, meta_doi, fn_doi):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result = crossref_lookup(candidate)
        if result:
            doi = candidate
            cr = result
            break
    # Surface a DOI even if it didn't resolve, so the user sees what we found
    if not doi:
        doi = text_doi or meta_doi or fn_doi
    logger.info("DOI: text=%s, meta=%s, filename=%s, resolved=%s",
                text_doi, meta_doi, fn_doi, doi if cr else None)

    meta: dict[str, Any] = {
        "doi": None, "author": "", "title": "", "journal": "",
        "year": None, "abstract": "",
    }
    source = "manual"

    if cr:
        meta = cr
        source = "crossref"

    # If no abstract from CrossRef, try a heuristic on the PDF text
    if not meta.get("abstract"):
        meta["abstract"] = extract_abstract_heuristic(text)

    # Check duplicate
    duplicate = False
    if meta.get("doi"):
        with db() as conn:
            row = conn.execute(
                "SELECT id FROM articles WHERE doi = ?", (meta["doi"],)
            ).fetchone()
            duplicate = row is not None

    suggested_topic = suggest_topic(
        meta.get("title", ""),
        f"{meta.get('abstract', '')} {text[:2000]}",
    )

    return {
        "temp_id": temp_id,
        "original_filename": file.filename,
        "source": source,
        "doi_found_in_pdf": doi,
        "metadata": meta,
        "suggested_topic": suggested_topic,
        "duplicate": duplicate,
        "topics": list(load_topics().keys()) + ["_uncategorized"],
    }


@app.post("/api/extract_abstract/{temp_id}")
def post_extract_abstract(temp_id: str) -> dict:
    """Re-run abstract extraction on a staged PDF (manual button in the UI)."""
    if "/" in temp_id or ".." in temp_id:
        raise HTTPException(400, "Invalid temp_id")
    path = STAGING / temp_id
    if not path.exists():
        raise HTTPException(404, "Staged file not found")
    text = extract_first_pages_text(path, n=3)
    abstract = extract_abstract_heuristic(text)
    return {"abstract": abstract, "text_chars": len(text)}


@app.post("/api/discard/{temp_id}")
def post_discard(temp_id: str) -> dict:
    """Delete a staged temp file (user discarded the card)."""
    if "/" in temp_id or ".." in temp_id:
        raise HTTPException(400, "Invalid temp_id")
    path = STAGING / temp_id
    path.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/lookup_doi")
def post_lookup_doi(payload: dict) -> dict:
    """Manual DOI/URL lookup (when extraction failed)."""
    raw = (payload.get("query") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty query")

    # Try to extract DOI from URL or raw string
    doi = find_doi(raw) or raw if raw.startswith("10.") else find_doi(raw)
    if not doi:
        return {"ok": False, "reason": "No DOI pattern found"}

    cr = crossref_lookup(doi)
    if not cr:
        return {"ok": False, "reason": "DOI not found in CrossRef"}

    duplicate = False
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM articles WHERE doi = ?", (cr["doi"],)
        ).fetchone()
        duplicate = row is not None

    suggested = suggest_topic(cr.get("title", ""), cr.get("abstract", ""))
    return {"ok": True, "metadata": cr, "duplicate": duplicate,
            "suggested_topic": suggested}


@app.post("/api/save")
def post_save(req: SaveRequest) -> dict:
    temp_path = STAGING / req.temp_id
    if not temp_path.exists():
        raise HTTPException(status_code=404, detail="Staged file expired")

    # Skip-on-duplicate (also enforced by UNIQUE constraint)
    if req.doi:
        with db() as conn:
            row = conn.execute(
                "SELECT id, topic, filename FROM articles WHERE doi = ?", (req.doi,)
            ).fetchone()
            if row:
                logger.info("Skip duplicate DOI %s", req.doi)
                temp_path.unlink(missing_ok=True)
                return {"ok": False, "skipped": True,
                        "reason": "Already in library", "existing": dict(row)}

    root = library_root()
    topic = req.topic
    topic_dir = root / topic
    topic_dir.mkdir(exist_ok=True)

    meta = {
        "author": req.author or "Unknown",
        "title": req.title or "Untitled",
        "journal": req.journal or "Journal",
        "year": req.year,
    }
    filename = make_filename(meta)
    final_path = unique_path(topic_dir, filename)
    shutil.move(str(temp_path), str(final_path))

    with db() as conn:
        cur = conn.execute(
            """INSERT INTO articles
               (doi, author, title, journal, year, topic, filename, abstract, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.doi, req.author, req.title, req.journal, req.year, topic,
             final_path.name, req.abstract, req.tags),
        )
        new_id = cur.lastrowid

    regenerate_library_md()
    return {"ok": True, "id": new_id, "filename": final_path.name, "topic": topic}


# ----- Routes: query -----

@app.get("/api/articles")
def get_articles(q: str | None = None, topic: str | None = None) -> list[dict]:
    sql = "SELECT a.* FROM articles a"
    params: list[Any] = []
    where: list[str] = []

    if q:
        # FTS5 query — escape quotes
        fts_q = q.replace('"', '""')
        sql = (
            "SELECT a.* FROM articles a "
            "JOIN articles_fts f ON f.rowid = a.id "
            "WHERE articles_fts MATCH ?"
        )
        params.append(f'"{fts_q}"*')
    if topic:
        sql += " AND " if "WHERE" in sql else " WHERE "
        sql += "a.topic = ?"
        params.append(topic)

    sql += " ORDER BY a.added_at DESC LIMIT 500"

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/article/{article_id}")
def get_article(article_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    return dict(row)


@app.get("/staged/{temp_id}")
def get_staged_pdf(temp_id: str) -> FileResponse:
    if "/" in temp_id or ".." in temp_id:
        raise HTTPException(400, "Invalid temp_id")
    path = STAGING / temp_id
    if not path.exists():
        raise HTTPException(404, "Staged file not found")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@app.get("/pdf/{article_id}")
def get_pdf(article_id: int) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT topic, filename FROM articles WHERE id = ?",
                           (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    path = library_root() / row["topic"] / row["filename"]
    if not path.exists():
        raise HTTPException(404, "PDF missing on disk")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


# ----- Routes: topics CRUD -----

@app.get("/api/topics")
def get_topics() -> dict:
    topics = load_topics()
    counts: dict[str, int] = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT topic, COUNT(*) c FROM articles GROUP BY topic"
        ).fetchall()
        for r in rows:
            counts[r["topic"]] = r["c"]
    return {"topics": topics, "counts": counts}


@app.post("/api/topics")
def post_topic(req: TopicRequest) -> dict:
    topics = load_topics()
    if req.name in topics:
        raise HTTPException(400, "Topic already exists")
    topics[req.name] = {"keywords": req.keywords}
    save_topics(topics)
    (library_root() / req.name).mkdir(exist_ok=True)
    return {"ok": True}


@app.put("/api/topics")
def put_topic(req: TopicRename) -> dict:
    topics = load_topics()
    if req.old_name not in topics:
        raise HTTPException(404, "Topic not found")

    if req.new_name != req.old_name:
        if req.new_name in topics:
            raise HTTPException(400, "Target name already exists")
        # Rename folder
        old_dir = library_root() / req.old_name
        new_dir = library_root() / req.new_name
        if old_dir.exists():
            old_dir.rename(new_dir)
        # Update DB
        with db() as conn:
            conn.execute("UPDATE articles SET topic = ? WHERE topic = ?",
                         (req.new_name, req.old_name))
        # Update topics.yaml
        topics[req.new_name] = {"keywords": req.keywords}
        del topics[req.old_name]
    else:
        topics[req.old_name]["keywords"] = req.keywords

    save_topics(topics)
    regenerate_library_md()
    return {"ok": True}


@app.delete("/api/topics/{name}")
def delete_topic(name: str) -> dict:
    topics = load_topics()
    folder = library_root() / name
    is_known = name in topics

    with db() as conn:
        cnt = conn.execute("SELECT COUNT(*) c FROM articles WHERE topic = ?",
                           (name,)).fetchone()["c"]

    # Orphan = not in topics.yaml but has a folder or DB rows
    if not is_known and cnt == 0 and not folder.exists():
        raise HTTPException(404, "Topic not found")

    if cnt > 0:
        raise HTTPException(400, f"Topic has {cnt} articles — move them first")

    if is_known:
        del topics[name]
        save_topics(topics)
    if folder.exists() and not any(folder.iterdir()):
        folder.rmdir()
    return {"ok": True}


@app.post("/api/topics/sync")
def post_topics_sync(req: TopicSyncRequest) -> dict:
    """Sync the library's topics.yaml with the project's bundled pack."""
    pack_data = yaml.safe_load(DEFAULT_TOPICS_FILE.read_text()) or {}
    pack_topics: dict[str, dict] = pack_data.get("topics", {})
    current = load_topics()

    added: list[str] = []
    kept: list[str] = []
    removed: list[str] = []

    if req.mode == "update":
        merged = dict(current)
        for name, meta in pack_topics.items():
            if name in current:
                kept.append(name)
            else:
                merged[name] = {"keywords": meta.get("keywords", [])}
                added.append(name)
        save_topics(merged)
    else:  # reset
        for name in pack_topics:
            (kept if name in current else added).append(name)
        for name in current:
            if name not in pack_topics:
                removed.append(name)
        shutil.copy(DEFAULT_TOPICS_FILE, topics_path())

    root = library_root()
    for name in added:
        (root / name).mkdir(exist_ok=True)

    return {
        "ok": True,
        "added": sorted(added),
        "kept": sorted(kept),
        "removed": sorted(removed),
    }


# ----- Routes: delete article -----

@app.delete("/api/article/{article_id}")
def delete_article(article_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT topic, filename FROM articles WHERE id = ?",
                           (article_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    pdf = library_root() / row["topic"] / row["filename"]
    pdf.unlink(missing_ok=True)
    regenerate_library_md()
    return {"ok": True}


# ----- Routes: edit & bulk maintenance -----

@app.patch("/api/article/{article_id}")
def patch_article(article_id: int, req: ArticleEditRequest) -> dict:
    """Update fields of an existing article. Moves the file if topic or
    naming-relevant fields change."""
    updates = req.model_dump(exclude_none=True)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        current = dict(row)

    merged = {**current, **updates}
    root = library_root()
    old_path = root / current["topic"] / current["filename"]

    # Recompute filename if author/title/journal/year changed
    rename_fields = ("author", "title", "journal", "year")
    needs_rename = any(
        k in updates and updates[k] != current[k] for k in rename_fields
    )
    new_filename = make_filename(merged) if needs_rename else current["filename"]

    topic_changed = merged["topic"] != current["topic"]
    new_topic_dir = root / merged["topic"]
    new_topic_dir.mkdir(exist_ok=True)

    if topic_changed or needs_rename:
        new_path = unique_path(new_topic_dir, new_filename)
        if old_path.exists():
            shutil.move(str(old_path), str(new_path))
        final_filename = new_path.name
    else:
        final_filename = current["filename"]

    with db() as conn:
        conn.execute(
            """UPDATE articles SET
                 author = ?, title = ?, journal = ?, year = ?, doi = ?,
                 topic = ?, filename = ?, abstract = ?, tags = ?
               WHERE id = ?""",
            (merged["author"], merged["title"], merged["journal"], merged["year"],
             merged["doi"], merged["topic"], final_filename,
             merged["abstract"], merged["tags"], article_id),
        )

    regenerate_library_md()
    return {"ok": True, "id": article_id, "filename": final_filename,
            "topic": merged["topic"]}


@app.post("/api/articles/refresh_metadata")
def post_refresh_metadata(req: RefreshMetadataRequest) -> dict:
    """Re-query CrossRef for every article with a DOI."""
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM articles").fetchall()]

    updated = 0
    skipped_no_doi = 0
    failed = 0
    for row in rows:
        if not row.get("doi"):
            skipped_no_doi += 1
            continue
        cr = crossref_lookup(row["doi"])
        if not cr:
            failed += 1
            continue
        new_values: dict[str, Any] = {}
        for field in ("author", "title", "journal", "year", "abstract"):
            cr_val = cr.get(field)
            cur_val = row.get(field)
            if cr_val and (not req.only_missing or not cur_val):
                if cr_val != cur_val:
                    new_values[field] = cr_val
        if new_values:
            set_clause = ", ".join(f"{k} = ?" for k in new_values)
            params = list(new_values.values()) + [row["id"]]
            with db() as conn:
                conn.execute(
                    f"UPDATE articles SET {set_clause} WHERE id = ?", params,
                )
            updated += 1

    if updated > 0:
        regenerate_library_md()
    return {
        "ok": True, "total": len(rows), "updated": updated,
        "skipped_no_doi": skipped_no_doi, "failed": failed,
    }


@app.post("/api/articles/reclassify")
def post_reclassify(req: ReclassifyRequest) -> dict:
    """Re-run topic suggestion against every article. dry-run unless apply=true."""
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM articles").fetchall()]

    changes: list[dict] = []
    for row in rows:
        suggested = suggest_topic(row.get("title") or "", row.get("abstract") or "")
        if suggested != "_uncategorized" and suggested != row["topic"]:
            changes.append({
                "id": row["id"],
                "title": row.get("title") or "",
                "from_topic": row["topic"],
                "to_topic": suggested,
                "filename": row["filename"],
            })

    if not req.apply:
        return {"ok": True, "preview": True, "changes": changes}

    root = library_root()
    applied = 0
    for change in changes:
        old_path = root / change["from_topic"] / change["filename"]
        new_dir = root / change["to_topic"]
        new_dir.mkdir(exist_ok=True)
        new_path = unique_path(new_dir, change["filename"])
        if old_path.exists():
            shutil.move(str(old_path), str(new_path))
        with db() as conn:
            conn.execute(
                "UPDATE articles SET topic = ?, filename = ? WHERE id = ?",
                (change["to_topic"], new_path.name, change["id"]),
            )
        applied += 1

    if applied > 0:
        regenerate_library_md()
    return {"ok": True, "applied": applied, "changes": changes}


# ----- Routes: app self-update -----

def _git(*args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args], cwd=str(APP_DIR),
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _is_frozen() -> bool:
    """True when running inside a PyInstaller-bundled .app."""
    return bool(getattr(sys, "frozen", False))


def _bundled_app_path() -> Path | None:
    """Return the .app bundle path when running frozen, else None."""
    if not _is_frozen():
        return None
    exe = Path(sys.executable)
    # PyInstaller --windowed layout: <Name>.app/Contents/MacOS/<binary>
    if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
        return exe.parent.parent.parent
    return None


@app.get("/api/app/version")
def get_app_version() -> dict:
    return {"version": __version__}


def _github_owner_repo() -> tuple[str, str] | None:
    """Parse the owner/repo from the origin remote URL."""
    if not (APP_DIR / ".git").exists():
        return None
    code, remote_url, _ = _git("remote", "get-url", "origin")
    if code != 0 or not remote_url:
        return None
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote_url)
    if not m:
        return None
    return m.group(1), m.group(2)


@app.post("/api/app/check_updates")
def post_check_updates() -> dict:
    """Compare local __version__ with the latest GitHub Release tag."""
    owner_repo = _github_owner_repo()
    if not owner_repo:
        return {"ok": False, "reason": "No GitHub remote configured.",
                "current": __version__}
    owner, repo = owner_repo
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(
                f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": f"EtAl/{__version__}"},
            )
        if r.status_code == 404:
            return {"ok": True, "current": __version__, "latest": None,
                    "updates_available": False,
                    "reason": "No releases published yet"}
        if r.status_code != 200:
            return {"ok": False, "current": __version__,
                    "reason": f"GitHub API returned {r.status_code}"}
        data = r.json()
    except Exception as e:
        return {"ok": False, "current": __version__,
                "reason": f"Could not reach GitHub: {e}"}

    latest_tag = (data.get("tag_name") or "").lstrip("v")
    return {
        "ok": True,
        "current": __version__,
        "latest": latest_tag or None,
        "updates_available": bool(latest_tag) and latest_tag != __version__,
        "release_url": data.get("html_url"),
    }


def _fetch_latest_release() -> dict:
    """Hit the GitHub API for the latest release. Raises HTTPException."""
    owner_repo = _github_owner_repo()
    if not owner_repo:
        raise HTTPException(400, "No GitHub remote configured")
    owner, repo = owner_repo
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(
                f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": f"EtAl/{__version__}"},
            )
    except Exception as e:
        raise HTTPException(400, f"Could not reach GitHub: {e}") from e
    if r.status_code != 200:
        raise HTTPException(400, f"GitHub API returned {r.status_code}")
    return r.json()


def _update_packaged() -> dict:
    """Download latest .app.zip from GitHub release, extract, swap in place."""
    current_app = _bundled_app_path()
    if not current_app:
        raise HTTPException(500, "Could not locate the running .app bundle")

    release = _fetch_latest_release()
    assets = release.get("assets") or []
    zip_asset = next(
        (a for a in assets if a.get("name", "").lower().endswith(".app.zip")
         or a.get("name", "").lower().endswith(".zip")),
        None,
    )
    if not zip_asset:
        raise HTTPException(400, "Latest release has no .app.zip asset")
    download_url = zip_asset.get("browser_download_url")
    if not download_url:
        raise HTTPException(400, "Asset has no download URL")

    staging = Path(tempfile.gettempdir()) / "etal_update"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    zip_path = staging / "update.zip"

    logger.info("Downloading update from %s", download_url)
    try:
        with httpx.Client(timeout=180.0, follow_redirects=True) as client:
            r = client.get(download_url)
    except Exception as e:
        raise HTTPException(400, f"Download failed: {e}") from e
    if r.status_code != 200:
        raise HTTPException(400, f"Download returned {r.status_code}")
    zip_path.write_bytes(r.content)

    extract_dir = staging / "extracted"
    extract_dir.mkdir()
    # ditto preserves macOS metadata and signature info
    result = subprocess.run(
        ["ditto", "-x", "-k", str(zip_path), str(extract_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise HTTPException(400, f"Extract failed: {result.stderr or result.stdout}")

    apps = list(extract_dir.glob("*.app"))
    if not apps:
        raise HTTPException(400, "Extracted archive contains no .app bundle")
    new_app = apps[0]

    # Write a shell script that waits for us to exit, then swaps and relaunches.
    swap_script = staging / "swap.sh"
    timestamp = int(time.time())
    backup = Path.home() / ".Trash" / f"{current_app.name}.{timestamp}.bak"
    swap_script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        f"APP_PID={os.getpid()}\n"
        "# Wait until the app process actually exits\n"
        "while kill -0 \"$APP_PID\" 2>/dev/null; do sleep 0.3; done\n"
        "sleep 0.5\n"  # filesystem settling buffer
        f"mv {shlex.quote(str(current_app))} {shlex.quote(str(backup))} || true\n"
        f"mv {shlex.quote(str(new_app))} {shlex.quote(str(current_app))}\n"
        f"open {shlex.quote(str(current_app))}\n"
    )
    swap_script.chmod(0o755)

    subprocess.Popen(
        ["/bin/bash", str(swap_script)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )

    # Give the HTTP response a moment to flush, then exit so the swap script
    # can take over.
    def quit_soon() -> None:
        time.sleep(1.2)
        os._exit(0)

    threading.Thread(target=quit_soon, daemon=True).start()

    return {
        "ok": True,
        "from_version": __version__,
        "to_version": (release.get("tag_name") or "").lstrip("v"),
        "restart_required": True,
        "message": "Update downloaded. The app will quit and relaunch in a moment.",
    }


@app.post("/api/app/update")
def post_app_update() -> dict:
    """Apply the latest update. Source installs: git pull --ff-only.
    Packaged builds: download the latest release's .app.zip and swap."""
    if _is_frozen():
        return _update_packaged()
    if not (APP_DIR / ".git").exists():
        raise HTTPException(400, "No update mechanism available "
                                 "(not a git checkout and not a packaged build)")
    code, out, err = _git("pull", "--ff-only")
    if code != 0:
        raise HTTPException(400, err or out or "git pull failed")
    return {"ok": True, "output": out, "restart_required": True}


# ----- Static frontend -----

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text())


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
app.mount("/icons", StaticFiles(directory=APP_DIR / "icons"), name="icons")
