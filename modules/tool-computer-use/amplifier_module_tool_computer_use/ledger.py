"""The held-input ledger: the most important safety property in the remote
transport (`docs/designs/remote-transport.md` \u00a710.2).

A synthetic keydown/mousedown with no matching up leaves a real human's
desktop broken, and the whole point of a remote agent is that the controller
cannot reach a machine whose link just died to fix it. So release must be
guaranteed *locally*, on the target, by the agent process itself - never by
the controller asking nicely.

This class is the ledger: a thread-safe registry of "what is currently held"
plus the release triggers required to be wired in from day one:

1. Explicit `release_all()` - the controller asking, or the agent's own
   stdin-EOF/signal handlers calling it.
2. A deadman timer - if nothing is heard from the controller for
   `deadman_seconds`, release everything and stop. Chosen in single digits
   (default 5s), not the 60s a first draft might reach for: this is a desktop
   a human is actively using, and 60 seconds of a stuck modifier key on a
   live machine is 60 seconds too many. 5s is comfortably longer than any
   Phase 1 request's real round trip (sub-second even for a screenshot) but
   short enough that a genuine, unnoticed disconnect resolves before anyone
   would think to ask "why is shift stuck".

No I/O of its own - callers supply a `release_fn` per held item. Release is
race-safe if two triggers fire close together (e.g. EOF and the deadman).
Receipt-backed Windows cleanup opts into retaining custody after a failed
release; an attempted release is then not reported as confirmed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Deliberately single-digit - see module docstring. A live desktop a human is
#: using should never have a modifier stuck for anywhere near this long, and
#: Phase 1's whole request set completes in well under a second per action.
DEFAULT_DEADMAN_SECONDS = 5.0


@dataclass
class _Held:
    token: str
    kind: str
    release_fn: Callable[[], None]
    released: bool = False
    retain_on_failure: bool = False


class HeldInputLedger:
    """Tracks every currently-held synthetic input and guarantees release.

    `hold(kind, token, release_fn)` registers one held item and resets the
    deadman timer. `release(token)` releases just that one (e.g. a matching
    `mouse_up` arrived through the ordinary op path). `release_all()` releases
    everything, exactly once each, and is what every trigger ultimately calls.
    """

    def __init__(
        self,
        deadman_seconds: float = DEFAULT_DEADMAN_SECONDS,
        on_release: Callable[[str, str], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._held: dict[str, _Held] = {}
        self._deadman_seconds = deadman_seconds
        self._on_release = on_release
        self._timer: threading.Timer | None = None
        self._stopped = False

    def hold(
        self,
        kind: str,
        token: str,
        release_fn: Callable[[], None],
        *,
        retain_on_failure: bool = True,
    ) -> None:
        """Register `token` (e.g. a modifier name, a button name) as held.

        Failed callbacks always retain custody and suppress success notifications.
        `retain_on_failure` remains accepted for source compatibility; false no
        longer discards an unconfirmed obligation. No original input is retried.
        """
        with self._lock:
            if token in self._held:
                raise RuntimeError(
                    "held-input token already has an outstanding obligation"
                )
            if self._stopped:
                # A hold requested after shutdown must not silently vanish
                # from the ledger - release immediately rather than track a
                # phantom entry no trigger will ever see.
                held = _Held(
                    token=token,
                    kind=kind,
                    release_fn=release_fn,
                    retain_on_failure=True,
                )
                self._held[token] = held
                self._release_locked(held)
                return
            self._held[token] = _Held(
                token=token,
                kind=kind,
                release_fn=release_fn,
                retain_on_failure=retain_on_failure,
            )
            self._reset_deadman_locked()

    def release(self, token: str) -> None:
        """Release one specific held token (idempotent - a second release of
        the same token, or of a token never held, is a no-op, not an error:
        the ledger's whole job is to make release safe to call redundantly
        from multiple independent triggers)."""
        with self._lock:
            held = self._held.get(token)
            if held is None or held.released:
                return
            self._release_locked(held)
            self._reset_deadman_locked()

    def release_all(self, *, reason: str = "release_all") -> list[str]:
        """Release every currently-held token. Returns the tokens actually
        released (for logging/audit - "RELEASED:<token>" per token, matching
        the already-verified bootstrap mechanic in
        docs/designs/remote-transport.md \u00a73.3). Safe to call from a signal
        handler, a `finally` block, EOF detection, or the deadman timer -
        multiple concurrent callers only ever release each token once.
        """
        with self._lock:
            released: list[str] = []
            for held in list(self._held.values()):
                if held.released:
                    continue
                if self._release_locked(held):
                    released.append(held.token)
            if released:
                logger.info(
                    "held-input ledger: released %s (reason=%s)", released, reason
                )
            return released

    def stop(self) -> None:
        """Release everything and cancel the deadman timer permanently -
        called once, at agent shutdown."""
        with self._lock:
            self.release_all(reason="stop")
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    @property
    def held_tokens(self) -> list[str]:
        with self._lock:
            return [t for t, h in self._held.items() if not h.released]

    # -- internals ----------------------------------------------------------

    def _release_locked(self, held: _Held) -> bool:
        # Prevent recursive release while a callback is running, without
        # confusing "attempted" with "confirmed" for receipt-backed releases.
        held.released = True
        try:
            held.release_fn()
        except Exception:
            held.released = False
            logger.exception(
                "held-input ledger: release for %r (%s) unconfirmed; retaining custody",
                held.token,
                held.kind,
            )
            return False
        self._held.pop(held.token, None)
        if self._on_release is not None:
            try:
                self._on_release(held.kind, held.token)
            except Exception:
                logger.exception("held-input ledger: on_release callback raised")
        return True

    def _reset_deadman_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._held or self._stopped:
            return
        self._timer = threading.Timer(self._deadman_seconds, self._on_deadman)
        self._timer.daemon = True
        self._timer.start()

    def _on_deadman(self) -> None:
        logger.warning(
            "held-input ledger: deadman fired after %.1fs with no activity - "
            "releasing everything still held",
            self._deadman_seconds,
        )
        self.release_all(reason="deadman")
