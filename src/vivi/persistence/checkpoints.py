"""Official SQLite saver with nonblocking async ports for the edge runtime."""

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from src.vivi.turn_control import record_io


class ThreadedSqliteSaver(SqliteSaver):
    def __init__(self, path):
        self.path = Path(path)
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        existing = str(path) != ":memory:" and Path(path).exists() and Path(path).stat().st_size > 0
        self.connection = sqlite3.connect(path, check_same_thread=False)
        if existing:
            backup = Path(str(path) + ".before-navigation-workspace.sqlite3")
            if not backup.exists():
                temporary = Path(str(backup) + ".tmp")
                target = sqlite3.connect(temporary)
                try:
                    self.connection.backup(target)
                finally:
                    target.close()
                temporary.replace(backup)
        super().__init__(self.connection)
        # A cancelled asyncio caller must not close a connection still in use by
        # its worker. One executor serializes reads, writes and the final close.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vivi-checkpoint")
        self._lifecycle = threading.Lock()
        self._closing = False
        self._close_future = None

    async def _run(self, function, *args):
        with self._lifecycle:
            if self._closing:
                raise RuntimeError("Checkpoint store is closing")
            future = self._executor.submit(function, *args)
        started = time.perf_counter()
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        finally:
            record_io("checkpoint", started)

    async def aget_tuple(self, config):
        return await self._run(self.get_tuple, config)

    async def adelete_thread(self, thread_id):
        await self._run(self.delete_thread, thread_id)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._run(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await self._run(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config, **kwargs):
        values = await self._run(lambda: list(self.list(config, **kwargs)))
        for value in values:
            yield value

    def close(self):
        with self._lifecycle:
            if not self._closing:
                self._closing = True
                self._close_future = self._executor.submit(self.connection.close)
                self._executor.shutdown(wait=False)

    async def aclose(self):
        self.close()
        await asyncio.shield(asyncio.wrap_future(self._close_future))
