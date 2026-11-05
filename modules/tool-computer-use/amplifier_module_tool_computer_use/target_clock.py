"""Windows clock values and invocation-local forwarding, with no platform imports.

The controller guard owns classification. A remote dispatcher merely forwards a
condition and its response in this scope; it never acquires a guard or a latch.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
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
    foreground: str | None = None

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
        foreground = raw.get("foreground")
        if foreground is not None and (
            type(foreground) is not str or not foreground or len(foreground) > 256
        ):
            raise ValueError("invalid Windows foreground identity")
        return cls(epoch, now, last, foreground)

    @property
    def idle_ms(self) -> float:
        return float(self.now_ms - self.last_input_ms)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "version": 1,
            "epoch": self.epoch,
            "now_ms": self.now_ms,
            "last_input_ms": self.last_input_ms,
        }
        if self.foreground is not None:
            out["foreground"] = self.foreground
        return out


class TargetClockFailure(RuntimeError):
    """A refused/invalid target result, not a request to retry input."""

    def __init__(self, result: Any) -> None:
        super().__init__("Windows target-clock precondition/receipt failed")
        self.result = result


@dataclass
class TargetClockExchange:
    condition: dict[str, Any]
    guard: Any = None
    focus_target: str | None = None
    focus_check: Callable[[], None] | None = None
    result: Any = None
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if type(self.condition) is not dict:
            raise ValueError("Windows target-clock condition must be an object")
        version = self.condition.get("version")
        override_keys = {
            "operator_presence_override",
            "operator_presence_channel",
            "operator_presence_expires_at",
            "operator_presence_target",
        }
        if type(version) is int and version in (2, 3):
            channel = self.condition.get("operator_presence_channel")
            expiry = self.condition.get("operator_presence_expires_at")
            target = self.condition.get("operator_presence_target")
            if (
                self.condition.get("operator_presence_override") is not True
                or type(channel) is not str
                or not channel.startswith("remote-ssh:windows-wsl2:")
                or not 1 <= len(channel.removeprefix("remote-ssh:windows-wsl2:")) <= 256
                or not channel.isprintable()
                or type(target) is not str
                or not target
                or target == "0"
                or len(target) > 256
                or type(expiry) not in (int, float)
                or not 0 < expiry < 2**53
                or not math.isfinite(expiry)
                or not 0 < expiry - time.time() <= 3600
            ):
                raise ValueError("invalid operator Windows condition")
            observation = TargetObservation.parse({**self.condition, "version": 1})
        else:
            if override_keys & self.condition.keys():
                raise ValueError(
                    "operator presence override requires Windows condition v2"
                )
            observation = TargetObservation.parse(self.condition)
        if version == 3:
            destination = self.condition.get("focus_target")
            if (
                type(destination) is not str
                or not destination
                or destination == "0"
                or len(destination) > 256
                or self.focus_target not in (None, destination)
            ):
                raise ValueError("invalid Windows focus transition")
            self.focus_target = destination
        elif "focus_target" in self.condition or self.focus_target is not None:
            raise ValueError("Windows focus transition requires condition v3")
        if "inject_before_ms" not in self.condition:
            raise ValueError("missing Windows injection precondition")
        injection = self.condition["inject_before_ms"]
        if injection is not None and tick(injection) > observation.now_ms:
            raise ValueError("Windows injection precondition is in the future")
        guard = self.condition.get("guard_ms")
        if type(guard) not in (int, float) or not math.isfinite(guard) or guard < 0:
            raise ValueError("invalid Windows guard precondition")

    def prepare(self) -> dict[str, Any]:
        # Controller policy can expire/be revoked during a composite operation.
        # Forwarding-only target exchanges retain the supplied v2 condition;
        # the bridge independently checks its deadline immediately before input.
        if self.guard is not None:
            self.condition = self.guard.target_condition()
            if self.focus_target is not None:
                self.condition.update(version=3, focus_target=self.focus_target)
        elif self.condition.get("version") == 3 and self.focus_target is None:
            raise TargetClockFailure(None)  # forwarding grant is one-shot too
        return self.condition

    def finish(self, result: Any, *, require_injection: bool) -> None:
        self.result = result
        if self.guard is not None:
            if self.focus_target is not None and self.focus_check is not None:
                self.focus_check()  # caller cancellation/Stop during transport
            self.guard.accept_target_result(
                result,
                require_injection=require_injection,
                transport_latency_ms=(time.monotonic() - self.started_at) * 1000.0,
                **(
                    {"focus_target": self.focus_target}
                    if self.focus_target is not None
                    else {}
                ),
            )
            # One successful focus consumes this invocation's transition authority.
            self.focus_target = None
            self.focus_check = None
            self.condition = self.guard.target_condition()
        elif not isinstance(result, dict) or not result.get("ok"):
            raise TargetClockFailure(result)
        else:
            self.focus_target = None


CURRENT_TARGET_CLOCK: ContextVar[TargetClockExchange | None] = ContextVar(
    "computer_target_clock", default=None
)


@contextmanager
def target_clock_exchange(
    condition: dict[str, Any], guard: Any = None, *, focus_target: str | None = None
):
    exchange = TargetClockExchange(
        condition=condition, guard=guard, focus_target=focus_target
    )
    token = CURRENT_TARGET_CLOCK.set(exchange)
    try:
        yield exchange
    finally:
        CURRENT_TARGET_CLOCK.reset(token)


def guarded_target_call(guard: Any, *, focus_target: str | None = None):
    """Old/non-Windows guard fakes need only the existing before/after API."""
    factory = getattr(guard, "target_clock_event", None)
    if factory is None:
        return nullcontext()
    return factory(focus_target=focus_target) if focus_target is not None else factory()


@contextmanager
def without_target_clock():
    """Emergency releases must not inherit an input precondition."""
    token = CURRENT_TARGET_CLOCK.set(None)
    try:
        yield
    finally:
        CURRENT_TARGET_CLOCK.reset(token)
