import asyncio
from contextlib import asynccontextmanager

import aiosqlite

from shared.observability.logger import debug
from shared.timeouts import Timeouts


class DatabasePool:
    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: asyncio.Queue[aiosqlite.Connection] | None = None
        self._initialized = False

    async def initialize(self):
        if self._initialized:
            return

        self._pool = asyncio.Queue(maxsize=self.pool_size)

        for _ in range(self.pool_size):
            conn = await aiosqlite.connect(
                self.db_path,
                timeout=30.0,
                check_same_thread=False,
            )
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA cache_size=-64000")
            await conn.execute("PRAGMA temp_store=MEMORY")
            await conn.execute("PRAGMA mmap_size=268435456")
            await conn.execute("PRAGMA busy_timeout=5000")
            await self._pool.put(conn)

        self._initialized = True
        debug(f"Database pool initialized with {self.pool_size} connections")

    @asynccontextmanager
    async def acquire(self):
        if not self._initialized:
            raise RuntimeError("DatabasePool not initialized")

        conn = await self._pool.get()
        try:
            yield conn
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise
        finally:
            await self._pool.put(conn)

    async def close(self):
        if not self._initialized:
            return

        while not self._pool.empty():
            try:
                async with asyncio.timeout(Timeouts.DB_CONNECTION):
                    conn = await self._pool.get()
                await conn.close()
            except (TimeoutError, Exception):
                break

        self._initialized = False
        debug("Database pool closed")
