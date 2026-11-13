"""Bounded synchronous target-side adapter for the private AppKit helper.

Construction/import/status are inert. Call start/update/close off an async loop.
Only an explicit lifecycle-owner update('released') requests that label; neither
timeout, empty input ledger nor helper exit implies real control release.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .macos_indicator_helper import (
    HEARTBEAT_SECONDS,
    MAX_FRAME,
    STATES,
    TERMINAL,
    TERMINAL_DWELL_SECONDS,
    WATCHDOG_SECONDS,
    ProtocolError,
    decode_frame,
    encode_frame,
    validate_reply,
)

_ACTIONS = {
    "cocoa_unavailable": "Install the tool package's Darwin Cocoa dependency in the target interpreter.",
    "console_user_mismatch": "Run as the logged-in target console user.",
    "console_unavailable": "Unlock a logged-in target console session.",
    "gui_unavailable": "Use an available target graphical console session.",
    "unsupported_platform": "The Mac activity panel requires macOS.",
}


class MacOSIndicator:
    """Single-use generation. status() is a monitor-maintained nonblocking copy."""

    def __init__(self):
        self._generation = uuid.uuid4().hex
        self._sequence = 0
        self._proc = None
        self._buffer = b""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor = None
        self._last_reply = 0.0
        self._last_loop = 0
        self._failed = False
        self._failure_reason = None
        self._snapshot = {
            "available": False,
            "ready": False,
            "healthy": False,
            "state": "interrupted",
            "reason": "not_started",
            "generation": self._generation,
            "sequence": 0,
            "terminal": False,
            "own_window_confirmed": False,
            "pixels_verified": False,
            "closed": False,
            "surface_absent": False,
            "process_settled": False,
            "helper_exit": None,
        }

    def status(self) -> dict:
        result = dict(self._snapshot)
        if result["healthy"] and (
            time.monotonic() - self._last_reply >= WATCHDOG_SECONDS
            or (self._proc is not None and self._proc.poll() is not None)
        ):
            # Observation only: do not wait for a lock or invent release.
            # Sticky loss even when a blocked monitor resumes later.
            if self._failure_reason is None:
                self._failure_reason = "connection_lost"
            self._failed = True
            self._stop.set()
        if self._failed:
            # Independent sticky loss wins even if a concurrent exchange just
            # published an unhealthy/closed terminal snapshot after its last
            # failure check. Preserve absence/settlement as separate observations.
            result = {
                **result,
                "healthy": False,
                "ready": False,
                "state": "interrupted",
                "reason": self._failure_reason or "connection_lost",
                "terminal": False,
                "own_window_confirmed": False,
            }
        return result

    def _launch(self):
        # A script path avoids importing the tool's package __init__ in the child.
        helper = Path(__file__).with_name("macos_indicator_helper.py")
        proc = subprocess.Popen(
            [sys.executable, "-I", str(helper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            close_fds=True,
        )
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        return proc

    def start(self, state="viewing") -> dict:
        if state not in ("viewing", "controlling"):
            raise ValueError("invalid indicator start state")
        with self._lock:
            if self._proc is not None or self._failed or self._snapshot["closed"]:
                return self.status()
            if sys.platform != "darwin":
                return self._fail("unsupported_platform")
            try:
                self._proc = self._launch()
                result = self._request("start", state, WATCHDOG_SECONDS - 0.5)
            except OSError:
                result = self._fail("helper_failed")
            if result["healthy"]:
                self._monitor = threading.Thread(
                    target=self._watch, name="mac-indicator-health", daemon=True
                )
                self._monitor.start()
            else:
                self._reap()
            return self.status()

    def update(self, state) -> dict:
        if not isinstance(state, str) or state not in STATES:
            raise ValueError("invalid indicator state")
        with self._lock:
            if not self.status()["healthy"] or self._failed:
                return self._fail("connection_lost")
            current = self._snapshot["state"]
            if (current in TERMINAL and state != current) or (
                current == "stopping" and state not in ("stopping", *TERMINAL)
            ):
                raise ValueError("invalid indicator state transition")
            return self._request("update", state)

    def _request(self, op, state=None, timeout=1.5):
        try:
            return self._exchange(op, state, timeout)
        except TimeoutError:
            return self._fail("reply_timeout")
        except (OSError, ProtocolError):
            return self._fail("connection_lost")

    def _exchange(self, op, state, timeout):
        # Monitor/close callers must observe *prior* health without relying on
        # a model/status poll. Never revive an expired generation with traffic.
        prior = self.status()
        if self._failed or (op != "start" and not prior["healthy"]):
            return self.status()
        lease_deadline = (
            self._last_reply + WATCHDOG_SECONDS if prior["healthy"] else None
        )
        self._sequence += 1
        row = {
            "v": 1,
            "g": self._generation,
            "s": self._sequence,
            "op": op,
            "state": state,
        }
        data = encode_frame(row)
        deadline = time.monotonic() + timeout
        proc = self._proc
        if proc is None:
            raise OSError("helper_failed")
        _, writable, _ = select.select([], [proc.stdin], [], timeout)
        now = time.monotonic()
        if lease_deadline is not None and now >= lease_deadline:
            return self._fail("connection_lost")
        if now >= deadline:
            raise TimeoutError
        if not writable or os.write(proc.stdin.fileno(), data) != len(data):
            raise TimeoutError
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [proc.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if not ready:
                break
            chunk = os.read(proc.stdout.fileno(), MAX_FRAME + 1 - len(self._buffer))
            if not chunk:
                raise OSError("connection_lost")
            self._buffer += chunk
            if len(self._buffer) > MAX_FRAME:
                raise ProtocolError("protocol_error")
            if b"\n" not in self._buffer:
                continue
            frame, self._buffer = self._buffer.split(b"\n", 1)
            reply = decode_frame(frame + b"\n")
            validate_reply(reply)
            expected = (
                "ready"
                if op == "start"
                else "terminal"
                if op in ("close", "abort")
                else "ack"
            )
            if (
                reply["g"] != self._generation
                or reply["s"] != self._sequence
                or reply["event"] not in (expected, "unavailable")
                or (
                    state is not None
                    and reply["state"] != state
                    and reply["reason"] == "ok"
                )
                or (op == "status" and reply["state"] != self._snapshot["state"])
                or self._buffer
            ):
                raise ProtocolError("protocol_error")
            if reply["reason"] != "ok" or reply["event"] == "unavailable":
                return self._fail(reply["reason"])
            closed = expected == "terminal"
            terminal_state = (
                "interrupted"
                if op == "abort" or self._snapshot["state"] not in TERMINAL
                else self._snapshot["state"]
            )
            if (
                reply["loop"] <= self._last_loop
                or reply["window"] == closed
                or (closed and reply["state"] != terminal_state)
            ):
                raise ProtocolError("protocol_error")
            if self._failed:
                return self.status()
            now = time.monotonic()
            # A correlated reply can arrive after the old lease or request
            # deadline, even if select returned readable. Check before renewal,
            # including terminal acknowledgements; do not replay the mutation.
            if lease_deadline is not None and now >= lease_deadline:
                return self._fail("connection_lost")
            if now >= deadline:
                raise TimeoutError
            self._last_reply = now
            self._last_loop = reply["loop"]
            self._snapshot = {
                "available": True,
                "ready": not closed,
                "healthy": not closed,
                "state": reply["state"],
                "reason": "closed" if closed else "ok",
                "generation": self._generation,
                "sequence": self._sequence,
                "terminal": reply["state"] in TERMINAL,
                "own_window_confirmed": reply["window"],
                "pixels_verified": False,
                "closed": closed,
                "surface_absent": closed,
                "process_settled": False,
                "helper_exit": None,
            }
            return self.status()
        raise TimeoutError

    def _fail(self, reason):
        # A failure is sticky. The same generation can never become healthy again.
        if self._failure_reason is None:
            self._failure_reason = reason
        self._failed = True
        self._stop.set()
        self._snapshot = {
            **self._snapshot,
            "ready": False,
            "healthy": False,
            "state": "interrupted",
            "reason": reason,
            "sequence": self._sequence,
            "terminal": False,
            "own_window_confirmed": False,
        }
        if reason in _ACTIONS:
            self._snapshot["action"] = _ACTIONS[reason]
        return self.status()

    def _watch(self):
        try:
            while not self._stop.wait(HEARTBEAT_SECONDS):
                with self._lock:
                    if self._stop.is_set():
                        return
                    if not self._request("status")["healthy"]:
                        return
        finally:
            if self._failed:
                with self._lock:
                    # No retry of an unknown mutation; close pipe => interrupted.
                    self._reap()

    def _reap(self):
        proc = self._proc
        if proc is None:
            return True
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(timeout=TERMINAL_DWELL_SECONDS + 0.5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    return False
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()
        self._snapshot = {
            **self._snapshot,
            "process_settled": True,
            "helper_exit": proc.returncode,
        }
        return True

    def close(self) -> dict:
        return self._finish("close")

    def abort(self) -> dict:
        return self._finish("abort")

    def _finish(self, op):
        self._stop.set()
        with self._lock:
            if self._snapshot["closed"] and self._snapshot["process_settled"]:
                return self.status()
            if self._proc is None:
                self._snapshot = {
                    **self._snapshot,
                    "closed": True,
                    "process_settled": True,
                }
                return self.status()
            if not self._failed and not self._snapshot["closed"]:
                self._request(op, timeout=TERMINAL_DWELL_SECONDS + 1)
            if not self._reap() or (
                self._snapshot["closed"]
                and self._proc.returncode
                != (0 if self._snapshot["state"] == "released" else 2)
            ):
                self._fail("helper_failed")
            return self.status()
