"""Хранилище тредов ChatKit в SQLite.

В пакете openai-chatkit есть только абстрактный Store. Здесь — минимальная реализация:
треды и сообщения лежат JSON-ом, каждый посетитель видит только свои треды
(user_id из cookie, его выставляет main.py). Вложения в виджете выключены.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

from chatkit.store import NotFoundError, Store
from chatkit.types import Attachment, Page, ThreadItem, ThreadMetadata
from pydantic import TypeAdapter

_ITEM = TypeAdapter(ThreadItem)

Context = dict[str, Any]


class SQLiteStore(Store[Context]):
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS threads_user ON threads(user_id, created_at);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    seq INTEGER NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS items_thread ON items(thread_id, seq);
                """
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    @staticmethod
    def _user(context: Context) -> str:
        return str(context.get("user_id") or "anonymous")

    def _owned(self, c: sqlite3.Connection, thread_id: str, context: Context) -> bool:
        row = c.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        return row is not None and row[0] == self._user(context)

    # --- треды -------------------------------------------------------------

    async def load_thread(self, thread_id: str, context: Context) -> ThreadMetadata:
        with self._conn() as c:
            row = c.execute("SELECT data, user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None or row[1] != self._user(context):
            raise NotFoundError(f"Thread {thread_id} not found")
        return ThreadMetadata.model_validate_json(row[0])

    async def save_thread(self, thread: ThreadMetadata, context: Context) -> None:
        meta = ThreadMetadata.model_validate(thread.model_dump())  # без items, если пришел Thread
        async with self._lock:
            with self._conn() as c:
                row = c.execute("SELECT user_id FROM threads WHERE id=?", (thread.id,)).fetchone()
                if row is not None and row[0] != self._user(context):
                    raise NotFoundError(f"Thread {thread.id} not found")
                c.execute(
                    "INSERT INTO threads(id, user_id, created_at, data) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (meta.id, self._user(context), meta.created_at.isoformat(), meta.model_dump_json()),
                )

    async def load_threads(self, limit: int, after: str | None, order: str, context: Context) -> Page[ThreadMetadata]:
        desc = order == "desc"
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id, created_at, data FROM threads WHERE user_id=? ORDER BY created_at {'DESC' if desc else 'ASC'}, id",
                (self._user(context),),
            ).fetchall()
        return _page([(r[0], ThreadMetadata.model_validate_json(r[2])) for r in rows], after, limit)

    async def delete_thread(self, thread_id: str, context: Context) -> None:
        async with self._lock:
            with self._conn() as c:
                if not self._owned(c, thread_id, context):
                    return
                c.execute("DELETE FROM items WHERE thread_id=?", (thread_id,))
                c.execute("DELETE FROM threads WHERE id=?", (thread_id,))

    # --- сообщения ---------------------------------------------------------

    async def load_thread_items(
        self, thread_id: str, after: str | None, limit: int, order: str, context: Context
    ) -> Page[ThreadItem]:
        desc = order == "desc"
        with self._conn() as c:
            if not self._owned(c, thread_id, context):
                raise NotFoundError(f"Thread {thread_id} not found")
            rows = c.execute(
                f"SELECT id, data FROM items WHERE thread_id=? ORDER BY seq {'DESC' if desc else 'ASC'}",
                (thread_id,),
            ).fetchall()
        return _page([(r[0], _ITEM.validate_json(r[1])) for r in rows], after, limit)

    async def add_thread_item(self, thread_id: str, item: ThreadItem, context: Context) -> None:
        await self.save_item(thread_id, item, context)

    async def save_item(self, thread_id: str, item: ThreadItem, context: Context) -> None:
        async with self._lock:
            with self._conn() as c:
                if not self._owned(c, thread_id, context):
                    raise NotFoundError(f"Thread {thread_id} not found")
                existing = c.execute("SELECT seq FROM items WHERE id=?", (item.id,)).fetchone()
                seq = existing[0] if existing else (
                    c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM items WHERE thread_id=?", (thread_id,)).fetchone()[0]
                )
                c.execute(
                    "INSERT INTO items(id, thread_id, created_at, seq, data) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (item.id, thread_id, item.created_at.isoformat(), seq, _ITEM.dump_json(item).decode()),
                )

    async def load_item(self, thread_id: str, item_id: str, context: Context) -> ThreadItem:
        with self._conn() as c:
            if not self._owned(c, thread_id, context):
                raise NotFoundError(f"Thread {thread_id} not found")
            row = c.execute("SELECT data FROM items WHERE id=? AND thread_id=?", (item_id, thread_id)).fetchone()
        if row is None:
            raise NotFoundError(f"Item {item_id} not found")
        return _ITEM.validate_json(row[0])

    async def delete_thread_item(self, thread_id: str, item_id: str, context: Context) -> None:
        async with self._lock:
            with self._conn() as c:
                if self._owned(c, thread_id, context):
                    c.execute("DELETE FROM items WHERE id=? AND thread_id=?", (item_id, thread_id))

    # --- вложения: в виджете выключены -------------------------------------

    async def save_attachment(self, attachment: Attachment, context: Context) -> None:
        raise NotImplementedError("Attachments are disabled")

    async def load_attachment(self, attachment_id: str, context: Context) -> Attachment:
        raise NotFoundError("Attachments are disabled")

    async def delete_attachment(self, attachment_id: str, context: Context) -> None:
        return None


def _page(rows: list[tuple[str, Any]], after: str | None, limit: int) -> Page:
    """Курсор — id последней отданной записи, как в ChatKit."""
    start = 0
    if after:
        ids = [r[0] for r in rows]
        start = ids.index(after) + 1 if after in ids else 0
    chunk = rows[start:start + limit]
    has_more = start + limit < len(rows)
    return Page(data=[r[1] for r in chunk], has_more=has_more, after=chunk[-1][0] if chunk and has_more else None)
