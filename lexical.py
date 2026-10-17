"""Lexical retrieval over knowledge-base entries.

The vector index is good at paraphrases but weak at exact terms and half
queries ("安装" for an entry titled "安装指南"). This module adds a second,
full-corpus lexical channel:

* tokenisation: jieba words plus all CJK bigrams, ASCII words >= 2 chars
* scoring: sum of matched token lengths (same idea as Alife memory Z)
* a candidate is admitted when the title matches OR the content reaches the
  configured content threshold (never AND)
* storage: SQLite FTS5 over pre-tokenised columns, with an in-memory fallback

Everything runs synchronously inside ``asyncio.to_thread`` so the event loop is
never blocked.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

try:
    import jieba  # type: ignore
    _JIEBA = True
except Exception:  # pragma: no cover - optional dependency
    jieba = None  # type: ignore
    _JIEBA = False

SCHEMA_VERSION = "1"
TITLE_BOOST = 2
_ASCII_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")
_MIX_RE = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+")

# A small built-in set on top of the plugin's stopword files. These are the
# question words that otherwise make every entry look relevant.
_BUILTIN_STOP = {
    "怎么", "什么", "为什么", "哪个", "哪些", "这个", "那个", "这些", "那些",
    "可以", "是否", "有没有", "一下", "一个", "我们", "你们", "他们", "它们",
    "以及", "但是", "因为", "所以", "如果", "然后", "还有", "就是", "不是",
    "现在", "今天", "昨天", "明天", "时候", "东西", "事情",
}


def load_stopwords(paths: Iterable[Optional[str]]) -> Set[str]:
    words: Set[str] = set(_BUILTIN_STOP)
    for path in paths:
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    word = line.strip()
                    if word:
                        words.add(word.lower())
        except Exception:
            continue
    return words


def normalise(text: str) -> str:
    return (text or "").lower()


def tokenize(text: str, stopwords: Optional[Set[str]] = None) -> Set[str]:
    """Return the lexical token set for indexing and querying."""
    stop = stopwords or _BUILTIN_STOP
    out: Set[str] = set()
    for chunk in _MIX_RE.findall(normalise(text)):
        if _ASCII_RE.fullmatch(chunk):
            if len(chunk) >= 2 and chunk not in stop:
                out.add(chunk)
            continue
        if _JIEBA:
            try:
                for word in jieba.lcut(chunk):
                    word = word.strip()
                    if len(word) >= 2 and word not in stop:
                        out.add(word)
            except Exception:
                pass
        for i in range(len(chunk) - 1):
            bigram = chunk[i:i + 2]
            if bigram not in stop:
                out.add(bigram)
    return out


def score_token_sets(query_tokens: Set[str], text_tokens: Set[str]) -> int:
    return sum(len(token) for token in query_tokens if token in text_tokens)


class LexicalIndex:
    """Per-knowledge-base entry index (title + content)."""

    def __init__(self, db_path: Path, raw_docs_dir: Path,
                 stopword_paths: Sequence[Optional[str]] = (),
                 logger=None, force_memory: bool = False):
        self.db_path = Path(db_path)
        self.raw_docs_dir = Path(raw_docs_dir)
        self.stopwords = load_stopwords(stopword_paths)
        self._logger = logger
        self._lock = asyncio.Lock()
        self._ready = False
        self._use_fts = True
        self._force_memory = bool(force_memory)
        self._mem: Dict[str, dict] = {}
        self._mem_index: Dict[str, Set[str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def ensure_ready(self) -> bool:
        if self._ready:
            return True
        async with self._lock:
            if self._ready:
                return True
            try:
                await asyncio.to_thread(self._ensure_sync)
                self._ready = True
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"lexical index unavailable: {exc}")
                self._ready = False
        return self._ready

    async def rebuild(self) -> bool:
        async with self._lock:
            try:
                await asyncio.to_thread(self._rebuild_sync)
                self._ready = True
                return True
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"lexical rebuild failed: {exc}")
                return False

    async def upsert(self, doc_id: str, name: str, content: str, updated_at: float) -> None:
        if not self._ready:
            return
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, doc_id, name, content, updated_at)

    async def delete(self, doc_id: str) -> None:
        if not self._ready:
            return
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, doc_id)

    async def search(self, query: str, *, allowed_ids: Optional[Set[str]] = None,
                     min_content_score: int = 2, limit: int = 20,
                     ignore_time: bool = True) -> List[dict]:
        if not self._ready:
            await self.ensure_ready()
        if not self._ready:
            return []
        tokens = tokenize(query, self.stopwords)
        if not tokens:
            return []
        async with self._lock:
            return await asyncio.to_thread(
                self._search_sync, tokens, allowed_ids,
                max(1, int(min_content_score)), max(1, int(limit)), bool(ignore_time),
            )

    # ------------------------------------------------------------------
    # SQLite / memory internals
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            if self._force_memory:
                self._use_fts = False
            try:
                if self._force_memory:
                    raise sqlite3.OperationalError("memory mode")
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS entry_fts USING fts5("
                    "doc_id UNINDEXED, name UNINDEXED, updated_at UNINDEXED,"
                    "name_tokens, content_tokens, tokenize='unicode61')"
                )
                self._use_fts = True
            except sqlite3.OperationalError:
                self._use_fts = False
            conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            count_row = conn.execute("SELECT value FROM meta WHERE key='count'").fetchone()
            schema_ok = bool(row and row[0] == SCHEMA_VERSION)
            count_ok = False
            if count_row and count_row[0].isdigit():
                count_ok = int(count_row[0]) == self._active_count()
            if not schema_ok or not count_ok:
                self._rebuild_with(conn)
            conn.commit()
        finally:
            conn.close()

    def _rebuild_sync(self) -> None:
        conn = self._connect()
        try:
            self._rebuild_with(conn)
            conn.commit()
        finally:
            conn.close()

    def _rebuild_with(self, conn: sqlite3.Connection) -> None:
        self._mem = {}
        self._mem_index = {}
        try:
            if self._force_memory:
                raise sqlite3.OperationalError("memory mode")
            conn.execute("DELETE FROM entry_fts")
        except sqlite3.OperationalError:
            try:
                if self._force_memory:
                    raise sqlite3.OperationalError("memory mode")
                conn.execute("DROP TABLE IF EXISTS entry_fts")
                conn.execute(
                    "CREATE VIRTUAL TABLE entry_fts USING fts5("
                    "doc_id UNINDEXED, name UNINDEXED, updated_at UNINDEXED,"
                    "name_tokens, content_tokens, tokenize='unicode61')"
                )
                self._use_fts = True
            except sqlite3.OperationalError:
                self._use_fts = False
        count = 0
        for path in sorted(self.raw_docs_dir.glob("*.txt")):
            doc_id = path.stem
            meta = self._read_meta(doc_id)
            if meta.get("deleted"):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                continue
            name = meta.get("original_name") or doc_id
            updated = float(meta.get("updated_at") or meta.get("created_at") or 0.0)
            self._insert_sync(conn, doc_id, name, content, updated)
            count += 1
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema',?)", (SCHEMA_VERSION,))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('count',?)", (str(count),))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('built_at',?)", (str(time.time()),))

    def _insert_sync(self, conn: sqlite3.Connection, doc_id: str, name: str,
                     content: str, updated_at: float) -> None:
        name_tokens = tokenize(name, self.stopwords)
        content_tokens = tokenize(content, self.stopwords)
        if self._use_fts:
            conn.execute("DELETE FROM entry_fts WHERE doc_id=?", (doc_id,))
            conn.execute(
                "INSERT INTO entry_fts(doc_id,name,updated_at,name_tokens,content_tokens)"
                " VALUES(?,?,?,?,?)",
                (doc_id, name, str(updated_at),
                 " ".join(sorted(name_tokens)), " ".join(sorted(content_tokens))),
            )
        else:
            self._mem[doc_id] = {
                "name": name,
                "updated_at": updated_at,
                "name_tokens": name_tokens,
                "content_tokens": content_tokens,
            }
            for token in name_tokens | content_tokens:
                self._mem_index.setdefault(token, set()).add(doc_id)

    def _upsert_sync(self, doc_id: str, name: str, content: str, updated_at: float) -> None:
        conn = self._connect()
        try:
            if self._use_fts:
                self._insert_sync(conn, doc_id, name, content, updated_at)
                conn.commit()
            else:
                if doc_id in self._mem:
                    self._delete_mem(doc_id)
                self._insert_sync(conn, doc_id, name, content, updated_at)
        finally:
            conn.close()

    def _delete_sync(self, doc_id: str) -> None:
        conn = self._connect()
        try:
            if self._use_fts:
                conn.execute("DELETE FROM entry_fts WHERE doc_id=?", (doc_id,))
                conn.commit()
            else:
                self._delete_mem(doc_id)
        finally:
            conn.close()

    def _delete_mem(self, doc_id: str) -> None:
        row = self._mem.pop(doc_id, None)
        if not row:
            return
        for token in row["name_tokens"] | row["content_tokens"]:
            bucket = self._mem_index.get(token)
            if bucket:
                bucket.discard(doc_id)
                if not bucket:
                    self._mem_index.pop(token, None)

    def _search_sync(self, query_tokens: Set[str], allowed_ids: Optional[Set[str]],
                     min_content_score: int, limit: int, ignore_time: bool) -> List[dict]:
        tokens = sorted(query_tokens, key=len, reverse=True)[:128]
        rows = []
        if self._use_fts:
            expr = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT doc_id,name,updated_at,name_tokens,content_tokens FROM entry_fts"
                    " WHERE entry_fts MATCH ? ORDER BY bm25(entry_fts) LIMIT ?",
                    (expr, max(limit * 20, 100)),
                )
                for doc_id, name, updated_at, name_tokens, content_tokens in cur.fetchall():
                    if allowed_ids is not None and doc_id not in allowed_ids:
                        continue
                    rows.append(self._score_row(
                        doc_id, name, updated_at,
                        set((name_tokens or "").split()),
                        set((content_tokens or "").split()),
                        query_tokens, min_content_score,
                    ))
            finally:
                conn.close()
        else:
            candidates: Set[str] = set()
            for token in tokens:
                candidates |= self._mem_index.get(token, set())
            for doc_id in candidates:
                if allowed_ids is not None and doc_id not in allowed_ids:
                    continue
                row = self._mem.get(doc_id)
                if not row:
                    continue
                rows.append(self._score_row(
                    doc_id, row["name"], row["updated_at"],
                    row["name_tokens"], row["content_tokens"],
                    query_tokens, min_content_score,
                ))
        rows = [row for row in rows if row is not None]
        rows.sort(
            key=lambda row: (
                row["score"],
                (row["updated_at"] if not ignore_time else 0.0),
                row["doc_id"],
            ),
            reverse=True,
        )
        return rows[:limit]

    def _score_row(self, doc_id: str, name: str, updated_at, name_tokens: Set[str],
                   content_tokens: Set[str], query_tokens: Set[str],
                   min_content_score: int) -> Optional[dict]:
        title_score = score_token_sets(query_tokens, name_tokens)
        content_score = score_token_sets(query_tokens, content_tokens)
        if title_score <= 0 and content_score < min_content_score:
            return None
        try:
            updated = float(updated_at or 0)
        except (TypeError, ValueError):
            updated = 0.0
        return {
            "doc_id": doc_id,
            "name": name or doc_id,
            "title_score": title_score,
            "content_score": content_score,
            "score": title_score * TITLE_BOOST + content_score,
            "updated_at": updated,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _read_meta(self, doc_id: str) -> dict:
        path = self.raw_docs_dir / f"{doc_id}.meta.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _active_count(self) -> int:
        count = 0
        for path in self.raw_docs_dir.glob("*.txt"):
            if not self._read_meta(path.stem).get("deleted"):
                count += 1
        return count

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        fn = getattr(self._logger, level, None)
        if callable(fn):
            fn(message)
