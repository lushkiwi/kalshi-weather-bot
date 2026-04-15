from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Recorder:
    """Thin async wrapper around an aiosqlite connection with the project schema applied."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(SCHEMA_PATH.read_text())
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "Recorder":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Recorder.connect() not called")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.conn.execute(sql, params)

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        await self.conn.executemany(sql, rows)

    async def commit(self) -> None:
        await self.conn.commit()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        async with self.conn.execute(sql, params) as cur:
            return [tuple(r) for r in await cur.fetchall()]
