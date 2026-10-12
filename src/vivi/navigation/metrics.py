"""Request-local dependency durations; overlapping stages are not additive."""

import time
from contextvars import ContextVar

from src.vivi.turn_control import current_control

navigation_timings = ContextVar("navigation_timings", default=None)
navigation_routes = ContextVar("navigation_routes", default=None)


def record(stage, started):
    timings = navigation_timings.get()
    if timings is not None:
        timings[stage] = round(timings.get(stage, 0) + (time.perf_counter() - started) * 1000, 2)
        control = current_control.get()
        if control:
            control.timings[stage] = timings[stage]


class MeasuredEnergy:
    def __init__(self, planner):
        self.planner = planner

    def __getattr__(self, name):
        value = getattr(self.planner, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            started = time.perf_counter()
            try:
                return value(*args, **kwargs)
            finally:
                record("navigation_energy", started)

        return call
