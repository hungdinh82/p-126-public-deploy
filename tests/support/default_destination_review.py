"""Shared component fixtures extracted from historical scenario modules."""

from tests.support.long_term_memory import services as services


class UnavailableModel:
    calls = 0

    async def agenerate_json(self, **kwargs):
        self.calls += 1
        raise AssertionError("An unambiguous destination answer must not await a model")
