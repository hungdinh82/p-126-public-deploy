"""Bounded admission and resource scheduling for resident inference engines.

One LLM request and one speech request may execute together. STT and TTS
share the speech lane; a long LLM prefill cannot consume that lane. Queues
are bounded per model, with ageing to prevent TTS starvation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class SchedulingError(Exception):
    def __init__(self, capability, code, message, status):
        super().__init__(message)
        self.capability, self.code, self.status = capability, code, status


@dataclass(eq=False)
class _Job:
    model: str
    created: float
    future: asyncio.Future
    call_id: str
    lease: Lease | None = None


class Lease:
    def __init__(self, scheduler, job):
        self.scheduler, self.job = scheduler, job
        self.started = time.monotonic()
        self.queue_ms = round((self.started - job.created) * 1000)
        self.released = False
        self.running = True

    def pause(self):
        self.running = False
        self.scheduler._dispatch()

    @asynccontextmanager
    async def step(self, timeout=None):
        """Keep exclusive model ownership while yielding GPU between TTS chunks."""
        job = _Job(self.job.model, time.monotonic(), asyncio.get_running_loop().create_future(),
                   self.job.call_id, self)
        self.scheduler._pending.append(job)
        self.scheduler._dispatch()
        await self.scheduler._wait(job, timeout)
        try:
            yield
        finally:
            self.pause()

    def release(self):
        if self.released:
            return
        self.released = True
        self.scheduler._active.pop(self.job.model, None)
        self.scheduler._last_queue_ms[self.job.model] = self.queue_ms
        logger.info("inference_complete capability=%s call_id=%s queue_ms=%d execution_ms=%d",
                    self.job.model, self.job.call_id, self.queue_ms,
                    round((time.monotonic() - self.started) * 1000))
        self.scheduler._dispatch()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.release()


class InferenceScheduler:
    def __init__(self, *, queue_limits=None, queue_timeouts=None, policy="speech_reserved"):
        if policy not in {"independent", "speech_reserved", "serialized"}:
            raise ValueError("Scheduling policy must be independent, speech_reserved or serialized")
        self.policy = policy
        self.queue_limits = queue_limits or {"llm": 4, "stt": 8, "tts": 8}
        self.queue_timeouts = queue_timeouts or {"llm": 90., "stt": 15., "tts": 30.}
        self._pending: list[_Job] = []
        self._active: dict[str, Lease] = {}
        self._last_queue_ms = {name: None for name in self.queue_limits}
        self._rejected = {name: 0 for name in self.queue_limits}

    def _resource(self, model):
        if self.policy == "independent":
            return model
        return "gpu" if self.policy == "serialized" else "llm" if model == "llm" else "speech"

    def snapshot(self, model):
        depth = sum(job.model == model and job.lease is None for job in self._pending)
        return {"active_requests": int(model in self._active), "queue_depth": depth,
                "queue_capacity": self.queue_limits[model],
                "accepting_requests": depth < self.queue_limits[model],
                "queue_timeout_seconds": self.queue_timeouts[model],
                "last_queue_ms": self._last_queue_ms[model], "rejected_requests": self._rejected[model],
                "resource_group": self._resource(model)}

    def _dispatch(self):
        occupied = {self._resource(model) for model, lease in self._active.items() if lease.running}
        now = time.monotonic()
        # A TTS request waiting 5 s outranks newly arriving STT work. Equal
        # priority requests retain FIFO order; running native work is not preempted.
        ordered = sorted(self._pending, key=lambda job:
                         (0 if job.lease is not None else 1,
                          (0 if job.model == "stt" else 1) - (now-job.created)/5,
                          job.created))
        for job in ordered:
            if job.future.cancelled():
                self._pending.remove(job)
                continue
            resource = self._resource(job.model)
            if job.model in self._active and job.lease is None:
                continue
            if resource in occupied:
                continue
            self._pending.remove(job)
            lease = job.lease or Lease(self, job)
            lease.running = True
            self._active[job.model] = lease
            occupied.add(resource)
            job.future.set_result(lease)

    async def acquire(self, model, *, timeout=None, call_id=""):
        if model not in self.queue_limits:
            raise ValueError(f"Unknown capability: {model}")
        depth = sum(job.model == model and job.lease is None for job in self._pending)
        if depth >= self.queue_limits[model]:
            self._rejected[model] += 1
            raise SchedulingError(model, "QUEUE_FULL", "Inference queue is full", 429)
        job = _Job(model, time.monotonic(), asyncio.get_running_loop().create_future(), call_id)
        self._pending.append(job)
        self._dispatch()
        return await self._wait(job, timeout)

    async def _wait(self, job, timeout):
        wait = self.queue_timeouts[job.model]
        if timeout is not None:
            wait = min(wait, timeout)
        try:
            return await asyncio.wait_for(asyncio.shield(job.future), wait)
        except BaseException as exc:
            if job in self._pending:
                self._pending.remove(job)
                job.future.cancel()
            elif job.future.done() and not job.future.cancelled():
                job.future.result().release()
            self._dispatch()
            if isinstance(exc, TimeoutError):
                self._rejected[job.model] += 1
                raise SchedulingError(job.model, "QUEUE_TIMEOUT", "Inference queue wait expired", 504) from exc
            raise
