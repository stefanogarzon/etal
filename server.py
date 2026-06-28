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
import uuid
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

from llm import (
    BUILTIN_ENABLED, BUILTIN_KEY, DEFAULT_MODEL, LLMError, llm_enabled,
    suggest_topic_llm, summarize_text,
)

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

__version__ = "0.2.1"
GITHUB_REPO = "stefanogarzon/etal"  # owner/repo for self-update checks


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse 'v1.2.3' / '1.2.3' into a comparable tuple. Non-numeric → (0,)."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) or (0,)

APP_DIR = Path(__file__).parent
FRONTEND_DIR = APP_DIR / "frontend"
PACKS_DIR = APP_DIR / "packs"
FIELDS_FILE = APP_DIR / "fields.yaml"

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
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return None


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


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
    summary TEXT,
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


_schema_migrated = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring an existing library DB up to the current schema. Idempotent —
    adds columns introduced after a library was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "summary" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN summary TEXT")
        conn.commit()
        logger.info("Migrated: added articles.summary column")


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    global _schema_migrated
    conn = sqlite3.connect(library_root() / "library.db")
    conn.row_factory = sqlite3.Row
    if not _schema_migrated:
        _ensure_schema(conn)
        _schema_migrated = True
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_library(
    path: Path,
    pack_slugs: list[str] | None = None,
    fields: list[str] | None = None,
) -> None:
    """Create folder structure, DB, and seed topics. ``fields`` records the
    library's declared specialties (AI classification scope); topics may start
    empty and grow organically, or be seeded from optional packs."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "_uncategorized").mkdir(exist_ok=True)
    (path / "_inbox").mkdir(exist_ok=True)

    topics_file = path / "topics.yaml"
    library_topics: dict = {}
    if pack_slugs:
        library_topics, _added, _skipped = install_packs_into_topics(
            {}, pack_slugs, mode="install"
        )
    topics_file.write_text(yaml.safe_dump(
        {"fields": list(fields or []), "topics": library_topics},
        sort_keys=True, allow_unicode=True,
    ), encoding="utf-8")

    for topic in library_topics.keys():
        (path / topic).mkdir(exist_ok=True)

    conn = sqlite3.connect(path / "library.db")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    (path / "library.md").write_text("# Et al.\n\n_Empty._\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

def topics_path(root: Path | None = None) -> Path:
    return (root or library_root()) / "topics.yaml"


def load_library_meta(root: Path | None = None) -> dict:
    """The full topics.yaml document: {fields: [...], topics: {...}}."""
    p = topics_path(root)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load_fields(root: Path | None = None) -> list[str]:
    """User-declared specialty fields for this library (AI classification
    scope). Stored as display names, e.g. ['Cardiology', 'Oncology (medical)']."""
    return list(load_library_meta(root).get("fields", []) or [])


def save_fields(fields: list[str]) -> None:
    data = load_library_meta()
    data["fields"] = list(fields)
    topics_path().write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")


_pack_backfill_done = False


def load_topics(root: Path | None = None) -> dict[str, dict]:
    global _pack_backfill_done
    p = topics_path(root)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    topics = data.get("topics", {})
    if root is None and not _pack_backfill_done:
        _pack_backfill_done = True
        if _backfill_packs(topics):
            save_topics(topics)
    return topics


def save_topics(topics: dict[str, dict]) -> None:
    # Preserve sibling metadata (e.g. fields) when rewriting topics.
    data = load_library_meta()
    data["topics"] = topics
    topics_path().write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")


def list_available_fields() -> list[dict]:
    """The catalog of selectable specialty fields (from fields.yaml)."""
    if not FIELDS_FILE.exists():
        return []
    try:
        data = yaml.safe_load(FIELDS_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("Could not load fields.yaml: %s", e)
        return []
    return data.get("fields", []) or []


def sanitize_topic(name: str) -> str:
    """Make an LLM-proposed topic name safe to use as a folder name."""
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]', "", name)   # filesystem-reserved chars
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name or "_uncategorized"


def register_topic_if_new(name: str) -> None:
    """Promote a brand-new topic (e.g. AI-proposed and confirmed on save) to a
    first-class topic in topics.yaml. System folders ('_'-prefixed) are skipped.
    Keeps the 'Topic = folder' invariant: every committed topic exists in YAML."""
    if not name or name.startswith("_"):
        return
    topics = load_topics()
    if name not in topics:
        topics[name] = {"keywords": []}
        save_topics(topics)
        logger.info("Registered new topic '%s'", name)


def _backfill_packs(topics: dict[str, dict]) -> bool:
    """Assign `pack:` to legacy topics (without provenance) by matching
    their names against the available packs' YAMLs.

    Only assigns when the topic name appears in EXACTLY ONE pack — ambiguous
    matches are left unassigned (the user can decide).
    """
    pack_membership: dict[str, list[str]] = {}
    for pack_info in list_available_packs():
        slug = pack_info["slug"]
        pack = load_pack(slug)
        if not pack:
            continue
        for tname in pack.get("topics", {}):
            pack_membership.setdefault(tname, []).append(slug)

    changed = 0
    for tname, meta in topics.items():
        if meta.get("pack"):
            continue
        candidates = pack_membership.get(tname, [])
        if len(candidates) == 1:
            meta["pack"] = candidates[0]
            changed += 1
    if changed:
        logger.info("Backfilled pack: provenance for %d topic(s)", changed)
    return changed > 0


def list_available_packs() -> list[dict]:
    """List all .yaml files in packs/ dir with their metadata."""
    if not PACKS_DIR.exists():
        return []
    packs = []
    for p in sorted(PACKS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            packs.append({
                "slug": data.get("slug", p.stem),
                "name": data.get("name", p.stem),
                "description": data.get("description", ""),
                "topic_count": len(data.get("topics", {})),
            })
        except Exception as e:
            logger.warning("Could not load pack %s: %s", p, e)
    return packs


def load_pack(slug: str) -> dict | None:
    """Load a single pack by slug."""
    for p in PACKS_DIR.glob("*.yaml"):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if data.get("slug") == slug:
            return data
    return None


def install_packs_into_topics(
    library_topics: dict, pack_slugs: list[str], mode: str = "install"
) -> tuple[dict, list[str], list[str]]:
    """
    Merge selected packs into the library topics dict.

    mode='install': preserve existing topics; new pack topics get suffixed on conflict.
    mode='reset': clear library_topics first; only inter-pack conflicts get suffixed.

    Returns (new_topics_dict, added_names, skipped_names).
    """
    if mode == "reset":
        library_topics = {}
    else:
        library_topics = dict(library_topics)

    pack_data = {}
    for slug in pack_slugs:
        pack = load_pack(slug)
        if pack:
            pack_data[slug] = pack.get("topics", {})

    all_names: dict[str, list[str]] = {}
    for slug, topics in pack_data.items():
        for name in topics:
            all_names.setdefault(name, []).append(slug)
    inter_conflicts = {n for n, slugs in all_names.items() if len(slugs) > 1}

    added: list[str] = []
    skipped: list[str] = []

    for slug, topics in pack_data.items():
        for topic_name, topic_data in topics.items():
            if topic_name in inter_conflicts:
                final_name = f"{topic_name}_{slug}"
            elif topic_name in library_topics:
                final_name = f"{topic_name}_{slug}"
            else:
                final_name = topic_name

            if final_name in library_topics:
                skipped.append(final_name)
                continue

            library_topics[final_name] = {
                "keywords": list(topic_data.get("keywords", [])),
                "pack": slug,
            }
            added.append(final_name)

    return library_topics, added, skipped


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

    (root / "library.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Et al.")


# ----- Models -----

class SetupRequest(BaseModel):
    library_path: str
    fields: list[str] = []
    pack_slugs: list[str] = []


class FieldsRequest(BaseModel):
    fields: list[str] = []


class FolderRequest(BaseModel):
    path: str


class SaveRequest(BaseModel):
    temp_id: str  # filename in tempdir from /api/ingest
    doi: str | None = None
    author: str
    title: str
    journal: str | None = None
    year: int | None = None
    topic: str
    abstract: str | None = None
    summary: str | None = None
    tags: str | None = None


class TopicRequest(BaseModel):
    name: str
    keywords: list[str] = []


class TopicRename(BaseModel):
    old_name: str
    new_name: str
    keywords: list[str] = []


class ArticleEditRequest(BaseModel):
    author: str | None = None
    title: str | None = None
    journal: str | None = None
    year: int | None = None
    doi: str | None = None
    topic: str | None = None
    abstract: str | None = None
    summary: str | None = None
    tags: str | None = None


class RefreshMetadataRequest(BaseModel):
    only_missing: bool = True


class ReclassifyRequest(BaseModel):
    apply: bool = False


class LLMSettings(BaseModel):
    enabled: bool = False
    api_key: str | None = None
    model: str = DEFAULT_MODEL


class SummarizeRequest(BaseModel):
    title: str | None = None
    abstract: str | None = None


# ----- Routes: setup -----

@app.get("/api/config")
def get_config() -> dict:
    cfg = load_config()
    return {"configured": cfg is not None, "config": cfg}


@app.post("/api/setup")
def post_setup(req: SetupRequest) -> dict:
    path = Path(req.library_path).expanduser().resolve()
    # If the folder already holds a library (e.g. picked a synced folder),
    # open it instead of overwriting its topics/fields.
    if _is_etal_library(path):
        return post_open_library(FolderRequest(path=str(path)))
    init_library(path, pack_slugs=req.pack_slugs, fields=req.fields)
    save_config({"library_path": str(path)})
    return {"ok": True, "library_path": str(path)}


@app.get("/api/fields")
def get_fields() -> dict:
    """Catalog of selectable specialty fields + the ones this library uses."""
    selected: list[str] = []
    if load_config():
        selected = load_fields()
    return {"available": list_available_fields(), "selected": selected}


@app.put("/api/library/fields")
def put_library_fields(req: FieldsRequest) -> dict:
    """Update the library's declared specialty fields (AI classification scope)."""
    if not load_config():
        raise HTTPException(400, "Library not configured")
    save_fields(req.fields)
    return {"ok": True, "fields": req.fields}


# ----- Routes: library location / sync -----

def _is_etal_library(path: Path) -> bool:
    return (path / "library.db").exists()


def _library_article_count(path: Path) -> int:
    try:
        conn = sqlite3.connect(path / "library.db")
        try:
            n = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        finally:
            conn.close()
        return int(n)
    except Exception:
        return 0


def detect_cloud_roots() -> list[dict]:
    """Best-effort detection of cloud-sync folders, so the user can keep the
    library in a place that syncs across machines. Returns existing candidates
    as {service, path, suggested} where suggested appends an 'EtAl' subfolder."""
    home = Path.home()
    candidates: list[tuple[str, Path]] = []

    # Google Drive — desktop app mounts a virtual drive (often G:) with
    # "My Drive"/"Meu Drive", or a classic ~/Google Drive folder.
    if sys.platform == "win32":
        for letter in "GHIJKLMNOPQDEF":
            for leaf in ("My Drive", "Meu Drive"):
                candidates.append(("Google Drive", Path(f"{letter}:/") / leaf))
        candidates.append(("Google Drive", home / "Google Drive"))
    elif sys.platform == "darwin":
        cs = home / "Library" / "CloudStorage"
        if cs.exists():
            for d in cs.glob("GoogleDrive-*"):
                candidates.append(("Google Drive", d / "My Drive"))
        candidates.append(("Google Drive", home / "Google Drive"))

    # Dropbox / OneDrive / iCloud
    candidates.append(("Dropbox", home / "Dropbox"))
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        candidates.append(("OneDrive", Path(onedrive)))
    candidates.append(("OneDrive", home / "OneDrive"))
    if sys.platform == "darwin":
        candidates.append(("iCloud Drive",
                           home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"))
    else:
        candidates.append(("iCloud Drive", home / "iCloudDrive"))

    seen: set[str] = set()
    out: list[dict] = []
    for service, p in candidates:
        try:
            if p.exists() and p.is_dir() and str(p) not in seen:
                seen.add(str(p))
                out.append({
                    "service": service,
                    "path": str(p),
                    "suggested": str(p / "EtAl"),
                })
        except OSError:
            continue
    return out


@app.get("/api/cloud_locations")
def get_cloud_locations() -> dict:
    """Detected cloud-sync folders to suggest as a library home."""
    return {"locations": detect_cloud_roots()}


@app.post("/api/inspect_folder")
def post_inspect_folder(req: FolderRequest) -> dict:
    """Tell the UI whether a chosen folder already holds an Et al. library
    (so it can offer 'Open' instead of 'Create')."""
    path = Path(req.path).expanduser().resolve()
    exists = _is_etal_library(path)
    info: dict[str, Any] = {
        "path": str(path),
        "is_library": exists,
        "exists": path.exists(),
    }
    if exists:
        info["article_count"] = _library_article_count(path)
        try:
            info["fields"] = load_fields(path)
        except Exception:
            info["fields"] = []
    return info


@app.post("/api/open_library")
def post_open_library(req: FolderRequest) -> dict:
    """Point the app at an existing library (e.g. a synced Google Drive folder
    on another computer). Does NOT re-initialize — preserves topics/fields."""
    global _schema_migrated
    path = Path(req.path).expanduser().resolve()
    if not _is_etal_library(path):
        raise HTTPException(400, "No Et al. library found in that folder "
                                 "(missing library.db).")
    save_config({"library_path": str(path)})
    _schema_migrated = False  # re-run migration against the opened library
    with db() as conn:  # triggers _ensure_schema on the newly-opened DB
        conn.execute("SELECT 1")
    return {"ok": True, "library_path": str(path),
            "article_count": _library_article_count(path)}


@app.get("/api/packs")
def get_packs() -> dict:
    return {"packs": list_available_packs()}


class PackInstallRequest(BaseModel):
    pack_slugs: list[str]
    mode: str = "install"


@app.post("/api/packs/install")
def post_packs_install(req: PackInstallRequest) -> dict:
    if req.mode not in ("install", "reset"):
        raise HTTPException(400, "mode must be 'install' or 'reset'")
    current = load_topics()
    new_topics, added, skipped = install_packs_into_topics(
        current, req.pack_slugs, mode=req.mode
    )
    save_topics(new_topics)
    for t in added:
        (library_root() / t).mkdir(exist_ok=True)
    regenerate_library_md()
    return {
        "ok": True,
        "mode": req.mode,
        "added": added,
        "skipped": skipped,
        "total_topics": len(new_topics),
    }


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

    cfg = load_config()
    topics_map = load_topics()

    # Baseline: keyword heuristic (always available, offline).
    kw_topic = suggest_topic(
        meta.get("title", ""),
        f"{meta.get('abstract', '')} {text[:2000]}",
    )
    suggested_topic = kw_topic

    # Optional AI layer — advisory. Prefers the model's pick when available;
    # may propose a brand-new topic. Falls back silently to the keyword pick.
    ai_block: dict[str, Any] = {"enabled": llm_enabled(cfg)}
    ai = suggest_topic_llm(
        cfg, meta.get("title", ""), meta.get("abstract", ""),
        text[:3000], topics_map, fields=load_fields(),
    )
    if ai and ai.get("topic"):
        proposed = ai["topic"]
        if ai.get("is_new"):
            proposed = sanitize_topic(proposed)
        if proposed and proposed != "_uncategorized":
            suggested_topic = proposed
        ai_block.update(
            topic=proposed,
            is_new=bool(ai.get("is_new")) and proposed != "_uncategorized",
            reason=ai.get("reason", ""),
            confidence=ai.get("confidence"),
        )
    elif ai and ai.get("error"):
        # AI call failed — fell back to the keyword pick. Tell the UI why.
        ai_block["error"] = ai["error"]

    return {
        "temp_id": temp_id,
        "original_filename": file.filename,
        "source": source,
        "doi_found_in_pdf": doi,
        "metadata": meta,
        "suggested_topic": suggested_topic,
        "keyword_topic": kw_topic,
        "ai": ai_block,
        "duplicate": duplicate,
        "topics": list(topics_map.keys()) + ["_uncategorized"],
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


def _http_from_llm_error(e: LLMError) -> HTTPException:
    """Map a failed Groq call to a user-facing HTTP error (toasted as-is)."""
    if e.reason == "rate_limited":
        return HTTPException(
            429, "Groq rate limit reached — wait a moment and try again, "
                 "or add your own key in Tools."
        )
    if e.reason == "auth":
        return HTTPException(400, "Groq rejected the API key — check it in Tools.")
    return HTTPException(502, "Groq request failed — try again in a moment.")


@app.post("/api/summarize/{temp_id}")
def post_summarize_staged(temp_id: str, req: SummarizeRequest) -> dict:
    """AI summary of a staged (not-yet-saved) PDF, for the ingestion card."""
    if "/" in temp_id or ".." in temp_id:
        raise HTTPException(400, "Invalid temp_id")
    path = STAGING / temp_id
    if not path.exists():
        raise HTTPException(404, "Staged file not found")
    source = (req.abstract or "").strip() or extract_first_pages_text(path, n=3)
    try:
        summary = summarize_text(load_config(), req.title or "", source)
    except LLMError as e:
        raise _http_from_llm_error(e) from e
    if summary is None:
        raise HTTPException(
            400, "AI summary unavailable — enable Groq in Tools and check your key"
        )
    return {"summary": summary}


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

    cfg = load_config()
    suggested = suggest_topic(cr.get("title", ""), cr.get("abstract", ""))
    ai_block: dict[str, Any] = {"enabled": llm_enabled(cfg)}
    ai = suggest_topic_llm(
        cfg, cr.get("title", ""), cr.get("abstract", ""), "", load_topics(),
        fields=load_fields(),
    )
    if ai and ai.get("topic"):
        proposed = ai["topic"]
        if ai.get("is_new"):
            proposed = sanitize_topic(proposed)
        if proposed and proposed != "_uncategorized":
            suggested = proposed
        ai_block.update(
            topic=proposed,
            is_new=bool(ai.get("is_new")) and proposed != "_uncategorized",
            reason=ai.get("reason", ""),
            confidence=ai.get("confidence"),
        )
    elif ai and ai.get("error"):
        ai_block["error"] = ai["error"]
    return {"ok": True, "metadata": cr, "duplicate": duplicate,
            "suggested_topic": suggested, "ai": ai_block}


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
    topic = sanitize_topic(req.topic)
    # Promote AI-proposed (or any unknown) topic to a first-class topic.
    register_topic_if_new(topic)
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
               (doi, author, title, journal, year, topic, filename,
                abstract, summary, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.doi, req.author, req.title, req.journal, req.year, topic,
             final_path.name, req.abstract, req.summary, req.tags),
        )
        new_id = cur.lastrowid

    regenerate_library_md()
    return {"ok": True, "id": new_id, "filename": final_path.name, "topic": topic}


# ----- Routes: query -----

@app.get("/api/articles")
def get_articles(
    q: str | None = None,
    topic: str | None = None,
    packs: str | None = None,
) -> list[dict]:
    sql = "SELECT a.* FROM articles a"
    params: list[Any] = []

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
    if packs:
        wanted = {s.strip() for s in packs.split(",") if s.strip()}
        all_topics = load_topics()
        in_pack_topics = [
            name for name, meta in all_topics.items()
            if (meta.get("pack") or "") in wanted
        ]
        if not in_pack_topics:
            return []
        placeholders = ",".join("?" * len(in_pack_topics))
        sql += " AND " if "WHERE" in sql else " WHERE "
        sql += f"a.topic IN ({placeholders})"
        params.extend(in_pack_topics)

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
    return {"topics": topics, "counts": counts, "fields": load_fields()}


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

    merged["topic"] = sanitize_topic(merged["topic"])
    topic_changed = merged["topic"] != current["topic"]
    if topic_changed:
        register_topic_if_new(merged["topic"])
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
                 topic = ?, filename = ?, abstract = ?, summary = ?, tags = ?
               WHERE id = ?""",
            (merged["author"], merged["title"], merged["journal"], merged["year"],
             merged["doi"], merged["topic"], final_filename,
             merged["abstract"], merged.get("summary"), merged["tags"], article_id),
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


# ----- Routes: bulk import -----
#
# Flow: pick a folder -> background job extracts DOI/metadata/topic for every
# PDF (AI throttled, falling back to keyword on rate-limit) -> the UI shows a
# review table -> the user confirms once and the batch is committed.

_bulk_jobs: dict[str, dict] = {}
_bulk_lock = threading.Lock()

BULK_MAX_FILES = 3000
_AI_THROTTLE_S = 0.15  # gentle pacing between AI calls during a bulk run


class BulkStartRequest(BaseModel):
    path: str
    recursive: bool = True
    use_ai: bool = True


class BulkImportItem(BaseModel):
    index: int
    topic: str


class BulkImportRequest(BaseModel):
    job_id: str
    items: list[BulkImportItem]


def _bulk_process_file(pdf: Path, cfg: dict | None, topics_map: dict,
                       fields: list[str], use_ai: bool, batch_dois: dict) -> dict:
    """Extract metadata + suggest a topic for one PDF (no DB writes)."""
    text = extract_first_pages_text(pdf)
    text_doi = find_doi(text)
    meta_doi = find_doi_in_metadata(pdf)
    fn_doi = find_doi_in_filename(pdf.name)

    doi: str | None = None
    cr: dict | None = None
    for cand in (text_doi, meta_doi, fn_doi):
        if cand:
            cr = crossref_lookup(cand)
            if cr:
                doi = cand
                break
    if not doi:
        doi = text_doi or meta_doi or fn_doi

    meta = cr or {"doi": doi, "author": "", "title": "", "journal": "",
                  "year": None, "abstract": ""}
    source = "crossref" if cr else "manual"
    if not meta.get("title"):
        meta["title"] = pdf.stem
    if not meta.get("author"):
        meta["author"] = "Unknown"
    if not meta.get("abstract"):
        meta["abstract"] = extract_abstract_heuristic(text)

    # Duplicate check: existing DB + earlier in this same batch
    duplicate = False
    dup_reason = ""
    item_doi = meta.get("doi")
    if item_doi:
        if item_doi in batch_dois:
            duplicate, dup_reason = True, "duplicate within this batch"
        else:
            with db() as conn:
                if conn.execute("SELECT 1 FROM articles WHERE doi = ?",
                                (item_doi,)).fetchone():
                    duplicate, dup_reason = True, "already in library"

    kw_topic = suggest_topic(meta.get("title", ""),
                             f"{meta.get('abstract', '')} {text[:2000]}")
    suggested = kw_topic
    ai_block: dict[str, Any] = {"used": False}
    if use_ai:
        ai = suggest_topic_llm(cfg, meta.get("title", ""), meta.get("abstract", ""),
                               text[:3000], topics_map, fields=fields)
        if ai and ai.get("topic"):
            proposed = sanitize_topic(ai["topic"]) if ai.get("is_new") else ai["topic"]
            if proposed and proposed != "_uncategorized":
                suggested = proposed
            ai_block = {"used": True, "topic": proposed,
                        "is_new": bool(ai.get("is_new")) and proposed != "_uncategorized",
                        "reason": ai.get("reason", "")}
        elif ai and ai.get("error"):
            ai_block = {"used": False, "error": ai["error"]}

    return {
        "source_path": str(pdf),
        "filename": pdf.name,
        "doi": meta.get("doi"),
        "author": meta.get("author") or "Unknown",
        "title": meta.get("title") or pdf.stem,
        "journal": meta.get("journal") or "",
        "year": meta.get("year"),
        "abstract": meta.get("abstract") or "",
        "source": source,
        "suggested_topic": suggested,
        "keyword_topic": kw_topic,
        "ai": ai_block,
        "duplicate": duplicate,
        "dup_reason": dup_reason,
        "include": not duplicate,  # default: skip duplicates
    }


def _bulk_worker(job_id: str, files: list[Path], use_ai: bool) -> None:
    cfg = load_config()
    topics_map = load_topics()
    fields = load_fields()
    ai_on = use_ai and llm_enabled(cfg)
    batch_dois: dict[str, int] = {}

    for i, pdf in enumerate(files):
        with _bulk_lock:
            if _bulk_jobs[job_id].get("cancelled"):
                break
            ai_now = ai_on and not _bulk_jobs[job_id].get("ai_cooldown")
        try:
            item = _bulk_process_file(pdf, cfg, topics_map, fields, ai_now, batch_dois)
        except LLMError as e:
            # Rate-limited mid-batch: stop calling AI for the rest, keep going.
            if e.reason == "rate_limited":
                with _bulk_lock:
                    _bulk_jobs[job_id]["ai_cooldown"] = True
                item = _bulk_process_file(pdf, cfg, topics_map, fields, False, batch_dois)
                item["ai"] = {"used": False, "error": "rate_limited"}
            else:
                item = _bulk_process_file(pdf, cfg, topics_map, fields, False, batch_dois)
        except Exception as e:  # never let one bad PDF kill the batch
            logger.warning("Bulk: failed on %s: %s", pdf, e)
            item = {"source_path": str(pdf), "filename": pdf.name, "error": str(e),
                    "title": pdf.stem, "author": "Unknown", "journal": "", "year": None,
                    "doi": None, "abstract": "", "source": "error",
                    "suggested_topic": "_uncategorized", "ai": {"used": False},
                    "duplicate": False, "dup_reason": "", "include": False}
        if item.get("doi"):
            batch_dois.setdefault(item["doi"], i)
        with _bulk_lock:
            item["index"] = i
            _bulk_jobs[job_id]["items"].append(item)
            _bulk_jobs[job_id]["processed"] = i + 1
        if ai_now:
            time.sleep(_AI_THROTTLE_S)

    with _bulk_lock:
        _bulk_jobs[job_id]["done"] = True


@app.post("/api/bulk/start")
def post_bulk_start(req: BulkStartRequest) -> dict:
    folder = Path(req.path).expanduser().resolve()
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(400, "Folder not found")
    globber = folder.rglob if req.recursive else folder.glob
    files = sorted(p for p in globber("*.pdf") if p.is_file())
    files = [p for p in files if p.suffix.lower() == ".pdf"][:BULK_MAX_FILES]
    if not files:
        raise HTTPException(400, "No PDF files found in that folder")

    job_id = uuid.uuid4().hex[:12]
    with _bulk_lock:
        _bulk_jobs[job_id] = {
            "id": job_id, "total": len(files), "processed": 0,
            "done": False, "cancelled": False, "ai_cooldown": False, "items": [],
        }
    threading.Thread(target=_bulk_worker, args=(job_id, files, req.use_ai),
                     daemon=True).start()
    return {"job_id": job_id, "total": len(files)}


@app.get("/api/bulk/status/{job_id}")
def get_bulk_status(job_id: str) -> dict:
    with _bulk_lock:
        job = _bulk_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {
            "id": job["id"], "total": job["total"], "processed": job["processed"],
            "done": job["done"], "ai_cooldown": job["ai_cooldown"],
            "items": list(job["items"]),
        }


@app.post("/api/bulk/cancel/{job_id}")
def post_bulk_cancel(job_id: str) -> dict:
    with _bulk_lock:
        if job_id in _bulk_jobs:
            _bulk_jobs[job_id]["cancelled"] = True
    return {"ok": True}


@app.post("/api/bulk/import")
def post_bulk_import(req: BulkImportRequest) -> dict:
    with _bulk_lock:
        job = _bulk_jobs.get(req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        items_by_index = {it["index"]: it for it in job["items"]}

    root = library_root()
    imported = 0
    skipped_dup = 0
    failed = 0
    for sel in req.items:
        it = items_by_index.get(sel.index)
        if not it:
            failed += 1
            continue
        doi = it.get("doi")
        if doi:
            with db() as conn:
                if conn.execute("SELECT 1 FROM articles WHERE doi = ?",
                                (doi,)).fetchone():
                    skipped_dup += 1
                    continue
        src = Path(it["source_path"])
        if not src.exists():
            failed += 1
            continue
        topic = sanitize_topic(sel.topic or it.get("suggested_topic") or "_uncategorized")
        register_topic_if_new(topic)
        topic_dir = root / topic
        topic_dir.mkdir(exist_ok=True)
        filename = make_filename({
            "author": it.get("author"), "title": it.get("title"),
            "journal": it.get("journal"), "year": it.get("year"),
        })
        final_path = unique_path(topic_dir, filename)
        try:
            shutil.copy2(str(src), str(final_path))
        except Exception as e:
            logger.warning("Bulk import copy failed for %s: %s", src, e)
            failed += 1
            continue
        with db() as conn:
            conn.execute(
                """INSERT INTO articles
                   (doi, author, title, journal, year, topic, filename,
                    abstract, summary, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doi, it.get("author"), it.get("title"), it.get("journal"),
                 it.get("year"), topic, final_path.name, it.get("abstract"),
                 None, None),
            )
        imported += 1

    if imported:
        regenerate_library_md()
    with _bulk_lock:
        _bulk_jobs.pop(req.job_id, None)  # job consumed
    return {"ok": True, "imported": imported, "skipped_dup": skipped_dup,
            "failed": failed}


# ----- Routes: AI (Groq) -----

@app.get("/api/llm/settings")
def get_llm_settings() -> dict:
    """Report AI status to the UI. The API key itself is never returned.
    AI is on by default via a shared built-in key; a user key overrides it."""
    llm = (load_config() or {}).get("llm") or {}
    user_key = bool(llm.get("api_key"))
    enabled = bool(llm.get("enabled", BUILTIN_ENABLED))
    return {
        "enabled": enabled and (user_key or bool(BUILTIN_KEY)),
        "model": llm.get("model") or DEFAULT_MODEL,
        "has_key": user_key,
        "using_builtin": enabled and not user_key and bool(BUILTIN_KEY),
        "builtin_available": bool(BUILTIN_KEY),
    }


@app.put("/api/llm/settings")
def put_llm_settings(req: LLMSettings) -> dict:
    cfg = load_config()
    if cfg is None:
        raise HTTPException(400, "Library not configured")
    llm = cfg.get("llm") or {}
    llm["enabled"] = req.enabled
    llm["model"] = req.model or DEFAULT_MODEL
    if req.api_key:  # only overwrite when a fresh key is supplied
        llm["api_key"] = req.api_key.strip()
    cfg["llm"] = llm
    save_config(cfg)
    return {"ok": True, "enabled": llm["enabled"], "model": llm["model"],
            "has_key": bool(llm.get("api_key")),
            "using_builtin": llm["enabled"] and not llm.get("api_key") and bool(BUILTIN_KEY)}


@app.post("/api/article/{article_id}/summarize")
def post_summarize_article(article_id: int) -> dict:
    """Generate (and store) an AI summary for a saved article."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    a = dict(row)
    source = (a.get("abstract") or "").strip()
    if not source:
        pdf = library_root() / a["topic"] / a["filename"]
        if pdf.exists():
            source = extract_first_pages_text(pdf, n=3)
    try:
        summary = summarize_text(load_config(), a.get("title", ""), source)
    except LLMError as e:
        raise _http_from_llm_error(e) from e
    if summary is None:
        raise HTTPException(
            400, "AI summary unavailable — enable Groq in Tools and check your key"
        )
    with db() as conn:
        conn.execute(
            "UPDATE articles SET summary = ? WHERE id = ?", (summary, article_id)
        )
    return {"ok": True, "summary": summary}


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


def _bundled_exe_path() -> Path | None:
    """Return the running .exe path when frozen on Windows (onefile), else None."""
    if not _is_frozen() or sys.platform != "win32":
        return None
    return Path(sys.executable)


@app.get("/api/app/version")
def get_app_version() -> dict:
    return {"version": __version__}


def _github_owner_repo() -> tuple[str, str] | None:
    """Resolve owner/repo for update checks.

    Source installs: prefer the local `origin` remote so forks work.
    Bundled .app builds (no .git): use the GITHUB_REPO constant baked in
    at build time.
    """
    if (APP_DIR / ".git").exists():
        code, remote_url, _ = _git("remote", "get-url", "origin")
        if code == 0 and remote_url:
            m = re.search(
                r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote_url,
            )
            if m:
                return m.group(1), m.group(2)
    if GITHUB_REPO and "/" in GITHUB_REPO:
        owner, repo = GITHUB_REPO.split("/", 1)
        return owner, repo
    return None


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
    available = bool(latest_tag) and _version_tuple(latest_tag) > _version_tuple(__version__)
    notes = (data.get("body") or "").strip()
    return {
        "ok": True,
        "current": __version__,
        "latest": latest_tag or None,
        "updates_available": available,
        "release_url": data.get("html_url"),
        "release_name": data.get("name"),
        "notes": notes[:1500],
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
    # Prefer an explicit .app.zip; fall back to any .zip.
    zip_asset = next(
        (a for a in assets if a.get("name", "").lower().endswith(".app.zip")), None
    ) or next(
        (a for a in assets if a.get("name", "").lower().endswith(".zip")), None
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
    cur_q = shlex.quote(str(current_app))
    new_q = shlex.quote(str(new_app))
    bak_q = shlex.quote(str(backup))
    swap_script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        f"APP_PID={os.getpid()}\n"
        "# Wait until the app process actually exits\n"
        "while kill -0 \"$APP_PID\" 2>/dev/null; do sleep 0.3; done\n"
        "sleep 0.5\n"  # filesystem settling buffer
        # Strip the quarantine flag so Gatekeeper doesn't block the freshly
        # downloaded (unsigned) build on relaunch.
        f"xattr -dr com.apple.quarantine {new_q} 2>/dev/null || true\n"
        f"mv {cur_q} {bak_q} || true\n"
        # If the swap fails, restore the previous app so we never end up with none.
        f"mv {new_q} {cur_q} || {{ mv {bak_q} {cur_q} 2>/dev/null; exit 1; }}\n"
        f"open {cur_q}\n",
        encoding="utf-8",
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


def _update_packaged_windows() -> dict:
    """Download the latest Windows .exe from the GitHub release and swap it in
    via a detached batch script that waits for this process to exit, replaces
    the exe, relaunches, and self-deletes."""
    current_exe = _bundled_exe_path()
    if not current_exe:
        raise HTTPException(500, "Could not locate the running .exe")

    release = _fetch_latest_release()
    assets = release.get("assets") or []
    exe_asset = next(
        (a for a in assets if a.get("name", "").lower().endswith(".exe")), None
    )
    if not exe_asset:
        raise HTTPException(400, "Latest release has no Windows .exe asset")
    download_url = exe_asset.get("browser_download_url")
    if not download_url:
        raise HTTPException(400, "Asset has no download URL")

    staging = Path(tempfile.gettempdir()) / "etal_update"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    new_exe = staging / "EtAl-new.exe"

    logger.info("Downloading update from %s", download_url)
    try:
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            r = client.get(download_url)
    except Exception as e:
        raise HTTPException(400, f"Download failed: {e}") from e
    if r.status_code != 200:
        raise HTTPException(400, f"Download returned {r.status_code}")
    new_exe.write_bytes(r.content)

    bat = staging / "swap.bat"
    pid = os.getpid()
    cur, old = str(current_exe), str(current_exe) + ".old"
    bat.write_text(
        "@echo off\r\n"
        ":waitloop\r\n"
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        "  goto waitloop\r\n"
        ")\r\n"
        f'move /Y "{cur}" "{old}" >nul 2>&1\r\n'
        f'move /Y "{new_exe}" "{cur}" >nul\r\n'
        f'if errorlevel 1 ( move /Y "{old}" "{cur}" >nul 2>&1 & goto end )\r\n'
        f'start "" "{cur}"\r\n'
        f'del /Q "{old}" >nul 2>&1\r\n'
        ":end\r\n"
        'del "%~f0"\r\n',
        encoding="utf-8",
    )

    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so the swapper outlives us.
    subprocess.Popen(["cmd", "/c", str(bat)],
                     creationflags=0x00000008 | 0x00000200, close_fds=True)

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
    Packaged builds: download the latest release asset and swap (mac .app / win .exe)."""
    if _is_frozen():
        if sys.platform == "darwin":
            return _update_packaged()
        if sys.platform == "win32":
            return _update_packaged_windows()
        raise HTTPException(400, "Self-update isn't supported on this platform yet")
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
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
app.mount("/icons", StaticFiles(directory=APP_DIR / "icons"), name="icons")
