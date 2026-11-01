"""SQLite storage with FTS5 full-text search. No MCP code here - testable on its own."""
from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, body, tags, content='notes', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body, tags) VALUES ('delete', old.id, old.title, old.body, old.tags);
    INSERT INTO notes_fts(rowid, title, body, tags) VALUES (new.id, new.title, new.body, new.tags);
END;
"""

MAX_TITLE = 200
MAX_BODY = 20_000
_WORD = re.compile(r"\w+", re.UNICODE)
_TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class NoteNotFound(LookupError):
    pass


class InvalidNote(ValueError):
    pass


@dataclass(frozen=True)
class Note:
    id: int
    title: str
    body: str
    tags: list[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SearchHit:
    id: int
    title: str
    snippet: str
    tags: list[str]
    score: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_tags(tags: list[str] | None) -> list[str]:
    out = sorted({t.strip().lower() for t in (tags or []) if t.strip()})
    bad = [t for t in out if not _TAG.match(t)]
    if bad:
        raise InvalidNote(f"invalid tags {bad}: use lowercase letters, digits, '-' or '_' (max 40 chars)")
    return out


def to_fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query.

    User input is never passed to MATCH directly: characters like quotes, '*', '-' or
    words like NOT and NEAR are FTS5 syntax, and a stray one raises an error or changes
    the meaning of the search. Each word is quoted as a literal and the words are ANDed.
    """
    words = _WORD.findall(text)
    return " ".join(f'"{w}"' for w in words)


def _locked(method: Callable) -> Callable:
    def wrapper(self: NoteStore, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    wrapper.__name__, wrapper.__doc__ = method.__name__, method.__doc__
    return wrapper


class NoteStore:
    """Thread-safe: MCP servers run synchronous tools in a worker-thread pool, so the
    connection is opened with ``check_same_thread=False`` and every operation holds a lock."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        target = str(Path(path).expanduser()) if path != ":memory:" else path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(target, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    @_locked
    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _row_to_note(row: sqlite3.Row) -> Note:
        tags = row["tags"].split(",") if row["tags"] else []
        return Note(row["id"], row["title"], row["body"], tags, row["created_at"], row["updated_at"])

    @staticmethod
    def _validate(title: str, body: str) -> tuple[str, str]:
        title, body = title.strip(), body.strip()
        if not title:
            raise InvalidNote("title must not be empty")
        if len(title) > MAX_TITLE:
            raise InvalidNote(f"title is longer than {MAX_TITLE} characters")
        if len(body) > MAX_BODY:
            raise InvalidNote(f"body is longer than {MAX_BODY} characters")
        return title, body

    @_locked
    def add(self, title: str, body: str, tags: list[str] | None = None) -> Note:
        title, body = self._validate(title, body)
        now = _now()
        with self.db:
            cur = self.db.execute(
                "INSERT INTO notes (title, body, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (title, body, ",".join(normalise_tags(tags)), now, now),
            )
        return self.get(cur.lastrowid)

    @_locked
    def get(self, note_id: int) -> Note:
        row = self.db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise NoteNotFound(f"note {note_id} does not exist")
        return self._row_to_note(row)

    @_locked
    def update(
        self, note_id: int, title: str | None = None, body: str | None = None, tags: list[str] | None = None
    ) -> Note:
        current = self.get(note_id)
        new_title, new_body = self._validate(
            title if title is not None else current.title, body if body is not None else current.body
        )
        new_tags = normalise_tags(tags) if tags is not None else current.tags
        with self.db:
            self.db.execute(
                "UPDATE notes SET title = ?, body = ?, tags = ?, updated_at = ? WHERE id = ?",
                (new_title, new_body, ",".join(new_tags), _now(), note_id),
            )
        return self.get(note_id)

    @_locked
    def delete(self, note_id: int) -> None:
        with self.db:
            if self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount == 0:
                raise NoteNotFound(f"note {note_id} does not exist")

    @_locked
    def search(self, query: str, tag: str | None = None, limit: int = 10) -> list[SearchHit]:
        limit = max(1, min(limit, 50))
        fts = to_fts_query(query)
        if not fts:
            return []
        sql = """
            SELECT n.id, n.title, n.tags,
                   snippet(notes_fts, 1, '[', ']', ' ... ', 12) AS snippet,
                   bm25(notes_fts, 10.0, 1.0, 5.0) AS rank
            FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid
            WHERE notes_fts MATCH ?
        """
        params: list = [fts]
        if tag:
            sql += " AND (',' || n.tags || ',') LIKE ?"
            params.append(f"%,{tag.strip().lower()},%")
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self.db.execute(sql, params).fetchall()
        # FTS5 bm25() is lower-is-better; negate so callers see higher-is-better scores
        return [
            SearchHit(r["id"], r["title"], r["snippet"], r["tags"].split(",") if r["tags"] else [], -r["rank"])
            for r in rows
        ]

    @_locked
    def recent(self, limit: int = 10) -> list[Note]:
        rows = self.db.execute("SELECT * FROM notes ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_note(r) for r in rows]

    @_locked
    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for (tags,) in self.db.execute("SELECT tags FROM notes WHERE tags != ''"):
            for t in tags.split(","):
                counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
