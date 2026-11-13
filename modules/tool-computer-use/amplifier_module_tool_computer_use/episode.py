"""Owner-bound activity custody, not a claim of desktop-wide exclusion.

Admission and fencing use a lock never held across native work. A cancelled
awaiter is not a drained worker. Expiry and indicator failure are irreversible
interruptions; neither is a successful hand-back. Renderers only display state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any


class EpisodeError(RuntimeError):
    pass


class Episode:
    """One channel/agent's cooperative arbiter. No permission is conveyed here."""

    def __init__(
        self,
        target: str,
        indicator_factory: Callable,
        release: Callable[[], bool],
        *,
        lease_seconds: float = 10.0,
        clock: Callable = time.monotonic,
    ):
        self.target = target
        self._factory = indicator_factory
        self._release = release
        self._lease = lease_seconds
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._transition = threading.Lock()
        self._render_lock = threading.Lock()
        self._identity = None
        self._indicator = None
        self._state = "idle"
        self._reason = None
        self._deadline = 0.0
        self._workers = 0
        self._cancelled = threading.Event()
        self._confirmed = False
        self._release_failed = False
        self._watch = None
        self._closed = False

    def _match(self, identity):
        if identity != self._identity or not isinstance(identity, dict):
            raise EpisodeError("wrong or stale episode owner/target/generation")

    def begin(self, identity: dict, mode: str = "viewing") -> dict:
        if (
            not isinstance(identity, dict)
            or set(identity) != {"owner", "target", "generation"}
            or identity["target"] != self.target
            or any(
                not isinstance(v, str) or not v or len(v) > 128
                for v in identity.values()
            )
            or mode not in {"viewing", "controlling"}
        ):
            raise EpisodeError("invalid episode identity or mode")
        with self._transition:
            with self._condition:
                if self._identity is not None:
                    self._match(identity)
                    if self._state not in {"viewing", "controlling"}:
                        raise EpisodeError(
                            "episode fenced; explicit new generation required"
                        )
                    return self.status(identity)
                self._identity = dict(identity)
                self._state = "starting"
                self._deadline = self._clock() + self._lease
            try:
                self._indicator = self._factory()
                ready = self._indicator.start(state=mode)
                if not all(
                    ready.get(k) is True for k in ("available", "ready", "healthy")
                ):
                    raise EpisodeError("target activity indicator not ready")
                with self._condition:
                    if self._state != "starting":
                        raise EpisodeError("episode revoked during indicator startup")
                    self._state = mode
                self._watch = threading.Thread(target=self._watchdog, daemon=True)
                self._watch.start()
            except BaseException:
                self.fence(identity, reason="indicator_unavailable")
                raise
            return self.status(identity)

    def heartbeat(self, identity) -> dict:
        with self._condition:
            self._match(identity)
            self._check_locked()
            self._deadline = self._clock() + self._lease
            return self.status(identity)

    def _healthy(self):
        try:
            return self._indicator is not None and all(
                self._indicator.status().get(k) is True
                for k in ("available", "ready", "healthy")
            )
        except Exception:
            return False

    def _check_locked(self):
        if self._state not in {"viewing", "controlling"}:
            raise EpisodeError("episode is fenced")
        if self._clock() >= self._deadline or not self._healthy():
            self._state = "interrupted"
            self._reason = "expired_or_indicator_lost"
            self._cancelled.set()
            self._condition.notify_all()
            raise EpisodeError("episode expired or target indicator lost")

    @contextmanager
    def admit(self, identity, *, controlling: bool = False):
        # Upgrade before admission, never after the input it discloses.
        with self._transition:
            with self._condition:
                self._match(identity)
                self._check_locked()
                upgrade = controlling and self._state == "viewing"
            if upgrade:
                try:
                    result = self._indicator.update("controlling")
                    ready = all(result.get(k) is True for k in ("ready", "healthy"))
                except Exception:
                    ready = False
                if not ready:
                    self.fence(identity, reason="indicator_lost")
                    raise EpisodeError("target indicator update unconfirmed")
            with self._condition:
                self._check_locked()
                if upgrade:
                    self._state = "controlling"
                self._workers += 1
        try:
            yield self._cancelled
        finally:
            with self._condition:
                self._workers -= 1
                self._condition.notify_all()

    def fence(self, identity, *, reason: str = "stopping", interrupted: bool = True):
        """Close admission immediately; never wait for a native worker here."""
        with self._condition:
            if (
                self._identity is None
                and isinstance(identity, dict)
                and (identity.get("target") == self.target)
            ):
                # Cleanup/Stop can overtake the scheduled startup worker.
                self._identity = dict(identity)
                self._state = "interrupted"
            self._match(identity)
            if self._state == "released":
                return
            if self._state in {"viewing", "controlling"}:
                try:
                    self._check_locked()
                except EpisodeError:
                    interrupted = True
            self._state = (
                "interrupted"
                if interrupted or self._state == "interrupted"
                else "stopping"
            )
            self._reason = reason
            self._cancelled.set()
            self._condition.notify_all()
        threading.Thread(target=self._display, args=(self._state,), daemon=True).start()

    def end(self, identity, *, timeout: float = 5.0) -> dict:
        with self._condition:
            self._match(identity)
            if self._state == "released":
                return self.status(identity)
            interrupted = self._state == "interrupted"
            self.fence(identity, interrupted=interrupted)
        with self._transition:
            self._display("interrupted" if interrupted else "stopping")
            deadline = time.monotonic() + timeout
            with self._condition:
                while self._workers:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._reason = "native_workers_pending"
                        return self.status(identity)
                    self._condition.wait(remaining)
            # No new worker can enter. Reconcile only this arbiter's custody.
            try:
                confirmed = self._release() is True
            except Exception:
                confirmed = False
            with self._condition:
                self._confirmed = confirmed and not self._release_failed
                if not confirmed:
                    self._release_failed = True
                    self._state = "interrupted"
                    self._reason = "release_unconfirmed"
                elif (
                    not self._release_failed
                    and self._state != "interrupted"
                    and self._healthy()
                ):
                    self._state = "released"
                    self._reason = None
                else:
                    self._state = "interrupted"
                    self._reason = self._reason or "indicator_lost"
                state = self._state
            self._display(state)
            return self.status(identity)

    def _display(self, state):
        with self._render_lock:
            with self._condition:
                if state != self._state:
                    return  # A delayed stopping update cannot overwrite released.
            self._display_locked(state)

    def _display_locked(self, state):
        if self._indicator is not None:
            try:
                result = self._indicator.update(state)
                if result.get("healthy") is not True and state == "released":
                    with self._condition:
                        self._state = "interrupted"
                        self._reason = "terminal_ack_unconfirmed"
            except Exception:
                # Release may be physically confirmed, but the terminal display
                # acknowledgement is unknown. Do not manufacture a released label.
                with self._condition:
                    self._state = "interrupted"
                    self._reason = "indicator_update_unconfirmed"

    def status(self, identity=None) -> dict[str, Any]:
        with self._condition:
            if identity is not None:
                self._match(identity)
            if self._state in {"viewing", "controlling"}:
                try:
                    self._check_locked()
                except EpisodeError:
                    pass
            return {
                "identity": dict(self._identity) if self._identity else None,
                "state": self._state,
                "reason": self._reason,
                "workers": self._workers,
                "admission_open": self._state in {"viewing", "controlling"},
                "release_confirmed": self._confirmed,
                "indicator_settled": self._closed,
                "scope": "cooperative_channel_agent_not_desktop_wide",
            }

    def close(self, identity) -> dict:
        result = self.end(identity)
        if result["workers"] or not result["release_confirmed"]:
            return result
        with self._render_lock:
            if self._indicator is None:
                self._closed = True  # Start was fenced before allocating a helper.
            if self._indicator is not None and not self._closed:
                closed = self._indicator.close()
                # Normal helper closure is intentionally unhealthy (no live
                # surface), so use explicit absence/exit evidence, not liveness.
                settled = (
                    closed.get("closed") is True
                    and closed.get("surface_absent") is True
                    and closed.get("process_settled") is True
                    and closed.get("helper_exit")
                    == (0 if result["state"] == "released" else 2)
                )
                if not settled:
                    with self._condition:
                        self._state = "interrupted"
                        self._reason = "indicator_close_unconfirmed"
                else:
                    self._closed = True
        return self.status(identity)

    def _watchdog(self):
        while True:
            time.sleep(min(0.25, self._lease / 4))
            with self._condition:
                if self._state not in {"viewing", "controlling"}:
                    interrupted = self._state == "interrupted"
                    break
                try:
                    self._check_locked()
                except EpisodeError:
                    interrupted = True
                    break
        if interrupted:
            # Independent custody path; a blocked native call cannot stop fencing.
            # End may time out, but it must never label pending work released.
            self.end(self._identity)


class OverlayIndicator:
    """Legacy Linux/Windows ambient band; no terminal-label parity is claimed."""

    def __init__(self, handle):
        self.handle = handle
        self.state = "viewing"

    def start(self, state="viewing"):
        self.state = state
        if self.handle is not None:
            self.handle.show()
        return self.status()

    def update(self, state):
        self.state = state
        return self.status()

    def status(self):
        shown = self.handle is not None and getattr(self.handle, "shown", False)
        return {
            "available": shown,
            "ready": shown,
            "healthy": shown,
            "state": self.state,
        }

    def close(self):
        self.handle.hide()
        absent = not getattr(self.handle, "shown", True)
        return {
            "closed": absent,
            "surface_absent": absent,
            "process_settled": absent,
            "helper_exit": 0 if self.state == "released" else 2,
        }
