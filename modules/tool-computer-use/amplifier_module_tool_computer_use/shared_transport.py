"""Per-target singleton `SshTransport` sharing: at most ONE remote agent process
per (ssh_path, host) target, shared by every consumer in this process.

Root cause this fixes (isolated and reproduced 2026-08-01, not re-investigated
here): two concurrent SSH-deployed agent processes against the same macOS
target corrupt each other's Screen Recording TCC grant. `CGDisplayCreateImage`
then returns `None` for BOTH agents - including the one that was capturing
successfully a moment earlier - and macOS raises no exception, just hands back
nothing (see `macos.py::capture`). This bites through Amplifier specifically
because more than one consumer in a single process can end up calling
`registry.select_backend()` for the SAME remote target: the parent session's
own `computer`/`desktop` tools (if configured) and a delegated
`computer-operator` child-session's `mount()` each used to build their OWN
`SshTransport` - two agent processes, one target, one poisoned grant.

Design
------
A module-level registry maps a target key -> one `_SharedEntry`, which owns
exactly one real `SshTransport` and a refcount. `acquire_shared_transport()`
returns a `SharedTransportHandle` that duck-types `SshTransport`'s
`connect()`/`send()`/`close()` (and exposes `.handshake`), so `RemoteBackend`
needs NO changes - it already only calls those three methods on whatever
object `registry._build_ssh_transport()` hands it.

Sharing contract
----------------
- The FIRST `connect()` for a target actually deploys the payload and runs the
  handshake; every later `connect()` for the SAME target (from a different
  handle) reuses the cached handshake and just re-validates the CALLING
  consumer's own `required_permissions` against it - no second deploy, no
  second agent process.
- `close()` decrements the entry's refcount; the underlying `SshTransport` is
  only actually torn down (SSH subprocess killed, `release_all`/`bye` sent)
  when the LAST handle releases it. An earlier `close()` never kills a
  transport another handle is still using.
- If the shared transport is ever detected as dead (a `send()`/`connect()`
  raises `SshConnectError` - lost connection, crashed agent, etc.), the entry
  is marked broken and evicted from the registry immediately, under the same
  lock that guards refcounting - no window where a broken entry can still be
  handed out to a new `acquire_shared_transport()` call. Any handle that is
  STILL holding a reference to that now-broken entry fails loud and
  immediately on its next `connect()`/`send()` call, naming the retirement
  explicitly, rather than transparently reconnecting mid-operation - a silent
  reconnect would spin up a brand-new agent process (and therefore a brand-new,
  empty `HeldInputLedger`) out from under whichever OTHER handle still
  believes inputs are held on the old one. The next genuinely new
  `acquire_shared_transport()` call for that target (e.g. the next session
  that mounts this tool) builds a fresh entry and goes through a real
  connect+deploy+handshake again.

Thread safety
-------------
`ComputerTool.execute()` dispatches through `asyncio.to_thread`, so concurrent
callers sharing one handle are real, not hypothetical. `SshTransport.send()`
already serializes request/response pairs with its own internal lock (see
`ssh_transport.py`); this module only needs to protect its OWN state
(refcount, `broken`, the registry dict, and "has anyone connected yet"), which
`_registry_lock` and each entry's `_connect_lock` do.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .ssh_transport import SshConnectError, SshTransport
from .wire import validate_handshake

logger = logging.getLogger(__name__)

#: (ssh_path, host) -> _SharedEntry. Guarded exclusively by `_registry_lock` -
#: every read and write of this dict, and every mutation of `refcount`/
#: `broken` on any entry, happens while holding this lock.
#: Bug-hunt defect B: the key gained a third element (`port`) so two
#: targets differing only by port are never folded into the same shared
#: transport - see `registry._build_ssh_transport`'s own comment.
_registry: dict[tuple[str, str, int | None], _SharedEntry] = {}
_registry_lock = threading.Lock()


class _SharedEntry:
    """One target's shared `SshTransport`, its refcount, and its connect state.

    `refcount`/`broken`/registry-membership are only ever mutated while
    holding the module-level `_registry_lock` (see `acquire_shared_transport`,
    `_release`, `_mark_broken`) - never independently - so refcount-reaches-
    zero-and-evict and a concurrent acquire-and-increment can never race each
    other into an inconsistent state. `_connect_lock` is a separate, per-entry
    lock that only serializes the (possibly slow: SSH deploy + handshake)
    `connect()` call itself, so it is never held across the module lock.
    """

    def __init__(
        self, key: tuple[str, str, int | None], transport: SshTransport
    ) -> None:
        self.key = key
        self.transport = transport
        self.refcount = 0
        self.handshake: dict[str, Any] | None = None
        self.broken = False
        self._closed = False
        self._connect_lock = threading.Lock()

    @property
    def host(self) -> str:
        return self.key[1]

    def _retired_error(self) -> SshConnectError:
        return SshConnectError(
            f"shared SSH transport for {self.host!r} already failed earlier "
            "in this process and was retired (see the original error in the "
            "log above) - this handle cannot be reused. Use desktop(action="
            "'retarget', target='ssh://...') with the same target to rebuild "
            "the connection. This does not retry the failed operation or "
            "clear a human halt/pause; inspect current state before new input."
        )

    def connect(
        self, *, required_permissions: tuple[str, ...], connect_timeout: float
    ) -> dict[str, Any]:
        if self.broken:
            raise self._retired_error()
        with self._connect_lock:
            if self.broken:  # re-check: another thread may have marked it
                raise self._retired_error()  # broken while we waited for the lock
            if self.handshake is not None:
                # Someone else already deployed and connected for this
                # target - reuse it, but still validate THIS caller's own
                # required_permissions against the cached handshake rather
                # than silently skipping a caller-specific check.
                validate_handshake(
                    self.handshake,
                    expected_sha256=str(self.handshake.get("agent_sha256", "")),
                    required_permissions=required_permissions,
                )
                return self.handshake
            try:
                handshake = self.transport.connect(
                    required_permissions=required_permissions,
                    connect_timeout=connect_timeout,
                )
            except SshConnectError:
                _mark_broken(self)
                raise
            self.handshake = handshake
            return handshake

    def send(self, line: bytes, *, timeout: float) -> bytes:
        if self.broken:
            raise self._retired_error()
        try:
            return self.transport.send(line, timeout=timeout)
        except SshConnectError:
            _mark_broken(self)
            raise


class SharedTransportHandle:
    """Per-`acquire_shared_transport()`-call handle onto a shared `_SharedEntry`.

    Duck-types `SshTransport.connect()`/`send()`/`close()` (plus the
    `.handshake` property `RemoteBackend` reads after connecting) so
    `RemoteBackend` requires zero changes - it already only calls these three
    methods on whatever `registry._build_ssh_transport()` returns.

    `close()` is idempotent and safe to call more than once (e.g. Amplifier's
    module cleanup callable running alongside an explicit `close()` call) -
    only the first call actually decrements the shared refcount.
    """

    def __init__(self, entry: _SharedEntry) -> None:
        self._entry = entry
        self._released = False

    @property
    def user_host(self) -> str:
        return self._entry.host

    @property
    def handshake(self) -> dict[str, Any] | None:
        return self._entry.handshake

    @property
    def retired(self) -> bool:
        """Cached lifecycle fact; reading it never reconnects or sends input."""
        return self._entry.broken

    def connect(
        self,
        *,
        required_permissions: tuple[str, ...] = (),
        connect_timeout: float = 30.0,
    ) -> dict[str, Any]:
        return self._entry.connect(
            required_permissions=required_permissions,
            connect_timeout=connect_timeout,
        )

    def send(self, line: bytes, *, timeout: float = 30.0) -> bytes:
        return self._entry.send(line, timeout=timeout)

    def notify(self, line: bytes) -> None:
        if self._released or self._entry.broken:
            raise self._entry._retired_error()
        self._entry.transport.notify(line)

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        _release(self._entry)


def acquire_shared_transport(
    key: tuple[str, str, int | None], factory: Callable[[], SshTransport]
) -> SharedTransportHandle:
    """Return a handle onto the shared `SshTransport` for `key`, creating one
    via `factory()` if none exists yet (or the previous one was retired).

    `factory` is only ever invoked while holding `_registry_lock` and only
    when actually needed (no entry, or the existing entry is broken) - it
    must be cheap (plain construction, no I/O; `SshTransport.__init__` does
    none). The real, possibly-slow work (SSH connect + deploy + handshake)
    happens later, in `SharedTransportHandle.connect()`, outside this lock.
    """
    with _registry_lock:
        entry = _registry.get(key)
        if entry is None or entry.broken:
            entry = _SharedEntry(key, factory())
            _registry[key] = entry
        entry.refcount += 1
    return SharedTransportHandle(entry)


def _release(entry: _SharedEntry) -> None:
    with _registry_lock:
        entry.refcount -= 1
        should_close = entry.refcount <= 0 and not entry._closed
        if should_close:
            entry._closed = True
            if _registry.get(entry.key) is entry:
                del _registry[entry.key]
    if should_close:
        # Outside the lock: SshTransport.close() sends release_all/bye and
        # waits on the subprocess - real I/O that must never block every
        # other target's acquire()/release() in this process.
        entry.transport.close()


def _mark_broken(entry: _SharedEntry) -> None:
    """Retire a broken entry AND tear down its underlying transport.

    A broken entry is evicted here and, per this module's "Sharing contract"
    (see the module docstring), is never handed out or reconnected again -
    the next `acquire_shared_transport()` for this target builds an entirely
    fresh entry. That guarantee means the OLD `SshTransport` is provably
    unreachable through this module from this point on, exactly the
    condition `_release()` tears one down under when refcount reaches zero -
    so the same teardown belongs here too.

    Without this, a `connect()`/`send()` failure (e.g. a handshake timeout)
    left the transport's `ssh` subprocess - and whatever bootstrap or agent
    process it was still driving on the remote target - to Python's
    reference-counting GC, which drops the object but does not terminate a
    still-running child process. On a target where a *second* connect is
    then retried (a fresh entry, a fresh `ssh`/agent process against the
    SAME host), an abandoned-but-still-alive straggler from the first
    attempt is precisely the "two concurrent agent processes against the
    same macOS target" hazard this module's own docstring describes -
    reached through the failed-connect door instead of a missing explicit
    `close()`.

    Best-effort: a failure while closing an already-broken transport must
    never mask the original error that got the caller here.
    """
    with _registry_lock:
        entry.broken = True
        entry._closed = (
            True  # this transport is retired; a later _release() must not re-close it
        )
        if _registry.get(entry.key) is entry:
            del _registry[entry.key]
    try:
        entry.transport.close()
    except Exception:  # noqa: BLE001 - best-effort teardown of an already-broken transport
        logger.exception(
            "shared-transport: close() on broken entry for %r failed", entry.host
        )


def _registry_size() -> int:
    """Test-only introspection: how many live (non-evicted) targets are
    currently registered. Not part of the public API."""
    with _registry_lock:
        return len(_registry)
