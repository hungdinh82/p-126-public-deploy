"""Official SQLite saver with nonblocking async ports for the edge runtime."""
import asyncio
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


class ThreadedSqliteSaver(SqliteSaver):
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        super().__init__(self.connection)

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config, **kwargs):
        values = await asyncio.to_thread(lambda: list(self.list(config, **kwargs)))
        for value in values:
            yield value

    def close(self):
        self.connection.close()
