"""Windows clock values and invocation-local forwarding, with no platform imports.

The controller guard owns classification. A remote dispatcher merely forwards a
condition and its response in this scope; it never acquires a guard or a latch.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


def tick(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("target tick must be a nonnegative exact JSON integer")
    return value


@dataclass(frozen=True)
class TargetObservation:
    epoch: str
    now_ms: int
    last_input_ms: int

    @classmethod
    def parse(cls, raw: Any) -> TargetObservation:
        if (
            not isinstance(raw, dict)
            or type(raw.get("version")) is not int
            or raw["version"] != 1
        ):
            raise ValueError("missing Windows target-clock observation v1")
        epoch = raw.get("epoch")
        if not isinstance(epoch, str) or not epoch or len(epoch) > 256:
            raise ValueError("missing Windows boot/session epoch")
        now = tick(raw.get("now_ms"))
        last = tick(raw.get("last_input_ms"))
        if last > now or now - last >= 2**32:
            raise ValueError("invalid/unwrappable Windows last-input tick")
        return cls(epoch, now, last)

    @property
    def idle_ms(self) -> float:
        return float(self.now_ms - self.last_input_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "epoch": self.epoch,
            "now_ms": self.now_ms,
            "last_input_ms": self.last_input_ms,
        }


class TargetClockFailure(RuntimeError):
    """A refused/invalid target result, not a request to retry input."""

    def __init__(self, result: Any) -> None:
        super().__init__("Windows target-clock precondition/receipt failed")
        self.result = result


@dataclass
class TargetClockExchange:
    condition: dict[str, Any]
    guard: Any = None
    result: Any = None
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        observation = TargetObservation.parse(self.condition)
        if "inject_before_ms" not in self.condition:
            raise ValueError("missing Windows injection precondition")
        injection = self.condition["inject_before_ms"]
        if injection is not None and tick(injection) > observation.now_ms:
            raise ValueError("Windows injection precondition is in the future")
        guard = self.condition.get("guard_ms")
        if type(guard) not in (int, float) or not math.isfinite(guard) or guard < 0:
            raise ValueError("invalid Windows guard precondition")

    def finish(self, result: Any, *, require_injection: bool) -> None:
        self.result = result
        if self.guard is not None:
            self.guard.accept_target_result(
                result,
                require_injection=require_injection,
                transport_latency_ms=(time.monotonic() - self.started_at) * 1000.0,
            )
            self.condition = self.guard.presence.target_condition()
        elif not isinstance(result, dict) or not result.get("ok"):
            raise TargetClockFailure(result)


CURRENT_TARGET_CLOCK: ContextVar[TargetClockExchange | None] = ContextVar(
    "computer_target_clock", default=None
)


@contextmanager
def target_clock_exchange(condition: dict[str, Any], guard: Any = None):
    exchange = TargetClockExchange(condition=condition, guard=guard)
    token = CURRENT_TARGET_CLOCK.set(exchange)
    try:
        yield exchange
    finally:
        CURRENT_TARGET_CLOCK.reset(token)


def guarded_target_call(guard: Any):
    """Old/non-Windows guard fakes need only the existing before/after API."""
    factory = getattr(guard, "target_clock_event", None)
    return factory() if factory is not None else nullcontext()


@contextmanager
def without_target_clock():
    """Emergency releases must not inherit an input precondition."""
    token = CURRENT_TARGET_CLOCK.set(None)
    try:
        yield
    finally:
        CURRENT_TARGET_CLOCK.reset(token)
