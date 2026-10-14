"""Bounded map reads shared by discovery, routing and place resolution."""

import asyncio

from src.vivi.navigation.vietmap import VietMapError


async def read_many(client, operations):
    if hasattr(client, "batch"):
        return await client.batch(operations)
    semaphore = asyncio.Semaphore(3)

    async def single(operation):
        async with semaphore:
            name, args, kwargs = operation
            try:
                return await getattr(client, name)(*args, **kwargs)
            except (VietMapError, TimeoutError) as exc:
                return exc

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(single(operation)) for operation in operations]
    return [task.result() for task in tasks]
