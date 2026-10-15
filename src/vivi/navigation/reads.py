"""Bounded map reads shared by discovery, routing and place resolution."""

import asyncio
import time

from src.vivi.navigation.vietmap import VietMapError
from src.vivi.turn_control import current_control


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


def coordinates(value):
    return value["lat"], value["lng"]


def read_budget(maximum):
    control = current_control.get()
    # Reserve time for the domain commit, response, checkpoint and turn receipt.
    return max(0.01, min(maximum, control.deadline - time.monotonic() - 5)) if control else maximum
