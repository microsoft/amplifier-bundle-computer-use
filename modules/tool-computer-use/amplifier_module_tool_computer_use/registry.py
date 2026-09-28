"""Backend selection: probe candidates in order, use the first one that works.

D1 fix lives here. `mount()` must never register a tool that cannot possibly work on
this machine - this module is where that decision gets made, once, before any tool is
mounted. Every candidate backend gets a cheap `probe()` (see `backend.Backend.probe`);
the first one that reports itself available is returned. If none can serve this
machine, `select_backend` raises `NoBackendAvailable` with every attempt's reason, and
`mount()` is expected to catch it, log it clearly, and mount nothing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .backend import Backend
from .linux_x11 import LinuxX11Backend
from .macos import MacOSBackend
from .windows import WindowsBackend

logger = logging.getLogger(__name__)


#: The shape fact about how a target machine is selected - the one thing a
#: model resolving "is remote possible?" from schema silence cannot see on its
#: own. Shared VERBATIM by two audiences so the fact never drifts between them:
#: `_REMEDIATION` below (the failure path - an operator reads this only when
#: mount() could not find a working backend) and `DesktopTool.description`
#: (`__init__.py` - the success path, read by the model on every mounted
#: session). Deliberately a shape claim, not an existence claim: asserting
#: "remote is possible" contradicts a schema that shows no host parameter, and
#: a model reconciling prose against schema trusts the schema. Explaining WHY
#: the schema is silent on the ORDINARY action calls (click, type,
#: screenshot, ...) - the target binds at mount from config.target, and is
#: re-bindable live via two dedicated actions rather than a per-call host
#: argument - is the fact that actually closes the gap.
#:
#: Updated for M4 live re-target (65f97b7, `ComputerTool.retarget`,
#: __init__.py:1533) and the unavailable-stub bootstrap (1b497ff,
#: `ComputerUseUnavailableTool._activate`, __init__.py:4988): binding is no
#: longer restart-only. Keep this fact in sync with those two call sites if
#: either changes what it rebinds - this constant drifting stale here is
#: exactly the failure mode `tests/test_target_model_claims.py` guards
#: against.
_TARGET_MODEL = (
    "set config.target='ssh://user@host' to point this capability at a "
    "different, reachable machine. The target binds at mount time from "
    "config.target - there is deliberately no per-call host parameter on "
    "the ordinary actions (click, type, screenshot, ...), and its absence "
    "is not evidence this capability is local-only. It IS re-bindable "
    'live, mid-session, with no restart: desktop(action="retarget", '
    "target=...) switches an already-mounted session to a new machine, "
    'and computer_use_unavailable(action="activate", target=...) binds '
    "and mounts real tools for a session where nothing mounted yet. "
    "Driving a different machine means one of those two action calls - a "
    "session restart is not required."
)

#: Appended to `NoBackendAvailable`'s message - the "what to do next" a bare
#: exception type name and a per-backend reason string do not supply on their
#: own. `mount()` (`tool-computer-use/__init__.py`) logs this whole message via
#: `logger.warning` and echoes it into the mounted-module manifest's
#: `description` field, so this text - not a traceback - is what an operator
#: actually sees when computer-use silently isn't there. Deliberately generic
#: (not per-backend) because `attempts` already carries the specific reason for
#: each candidate that was actually tried; this is the part that reason text
#: cannot supply on its own - the remediation options that apply regardless of
#: which specific backend failed and why.
_REMEDIATION = (
    "What to do: fix the backend that applies to this machine (see its reason "
    "above - e.g. a missing dependency, no DISPLAY/XAUTHORITY, powershell.exe "
    "unreachable, or a missing macOS Accessibility/Screen Recording grant), or "
    f"{_TARGET_MODEL} See docs/SETUP.md \u00a74 for per-platform backend "
    "requirements."
)


class NoBackendAvailable(RuntimeError):
    """No configured backend could serve this machine.

    This is the actual operator-facing message a human sees when computer-use
    refuses to mount - not a bare exception type name. Three parts, in order:
    WHAT happened (no backend available), WHY (every candidate's own probe
    reason - `attempts` - so a real X11 connection failure is never confused
    with a missing dependency), and WHAT TO DO about it (`_REMEDIATION`).
    Fail-loud-to-the-system (refusing to mount a tool that cannot work) and
    fail-loud-to-the-human-who-has-to-act-on-it (telling them what to actually
    do) are different properties; this exists so both are built, not just the
    first.
    """

    def __init__(self, attempts: list[tuple[str, str]]) -> None:
        self.attempts = attempts
        if attempts:
            detail = "; ".join(f"{name}: {reason}" for name, reason in attempts)
            message = f"no computer-use backend available ({detail}). {_REMEDIATION}"
        else:
            message = (
                "no computer-use backends configured (registry.BACKEND_FACTORIES "
                "is empty). What to do: this is a packaging/config bug, not a "
                "normal environment problem - file an issue against "
                "amplifier-bundle-computer-use."
            )
        super().__init__(message)


#: `ssh://user@host` (or `ssh://host` - user optional, taken from the local
#: SSH config/default in that case), plus an OPTIONAL `:port` suffix
#: (bug-hunt defect B: previously accepted here and then silently dropped -
#: no `-p` ever reached `ssh`, see `ssh_transport.py`'s `_SSH_OPTS`).
#:
#: A bare (unbracketed) IPv6 literal is deliberately NOT matched by `host`
#: (`[^/:\[\]]+` excludes `:`) - `ssh://user@::1` is genuinely ambiguous
#: between "host `::1`, no port" and "host `:`, port `:1`"-shaped nonsense,
#: so `_parse_target` raises rather than guessing. The bracketed form
#: (`ssh://[::1]:2222`, mirroring RFC 3986's own authority syntax for a
#: literal IPv6 address next to a port) is unambiguous and IS supported.
_SSH_TARGET_RE = re.compile(
    r"^ssh://"
    r"(?:(?P<user>[^@/]+)@)?"
    r"(?:\[(?P<v6host>[^\]]+)\]|(?P<host>[^:/\[\]]+))"
    r"(?::(?P<port>\d+))?"
    r"$"
)


def _parse_target(target: str) -> tuple[str, int | None]:
    """Return the (`[user@]host`, `port`) pair `ssh` itself expects - `host`
    is exactly the string `ssh`'s classic `[user@]hostname` destination
    argument wants (no port embedded, bracket-free even for IPv6 - `ssh`
    accepts a bare IPv6 literal there since there is no port suffix to
    disambiguate it from); `port` is `None` for a standard/unconfigured
    port, in which case callers must not pass `-p` at all (byte-identical
    to this function's pre-port-support behavior).

    Raises `ValueError` for anything that isn't a well-formed `ssh://`
    target - a malformed config value should fail loud with a clear parse
    error, not silently be handed to `ssh` as a garbage argument. This
    includes a bare (unbracketed) IPv6 host - see `_SSH_TARGET_RE`'s own
    comment for why that is rejected rather than guessed at.
    """
    match = _SSH_TARGET_RE.match(target.strip())
    if not match:
        raise ValueError(
            f"config.target={target!r} is not a valid ssh:// target "
            "(expected 'ssh://user@host', 'ssh://host[:port]', or "
            "'ssh://[ipv6-literal][:port]' - a bare, unbracketed IPv6 "
            "literal is not accepted: it is ambiguous with a "
            "'host:port' suffix, use 'ssh://[::1]:2222' instead)"
        )
    user = match.group("user")
    host = match.group("host") or match.group("v6host")
    port_str = match.group("port")
    port = int(port_str) if port_str else None
    user_host = f"{user}@{host}" if user else host
    return user_host, port


#: Probe order. Windows-over-WSL2 first preserves today's default behavior; Linux X11
#: is only tried if the Windows bridge is not reachable (e.g. a bare Linux box with no
#: `powershell.exe`, such as this bundle's original test box). macOS is only tried if
#: neither of those is reachable (e.g. a bare macOS box) - its `probe()` returns
#: unavailable immediately on any non-Darwin platform, so trying it earlier would cost
#: nothing functionally, but this order keeps the two previously-verified platforms'
#: behavior completely undisturbed by this addition.
BACKEND_FACTORIES: tuple[type[Backend], ...] = (
    WindowsBackend,
    LinuxX11Backend,
    MacOSBackend,
)


def _build_ssh_transport(
    host: str, package_dir: Any, config: dict[str, Any], *, port: int | None = None
) -> Any:
    """Return a per-target SHARED transport handle for a remote target.

    Singleton fix: this used to construct a brand-new `SshTransport` (hence a
    brand-new SSH subprocess, hence a brand-new remote agent process on the
    target) on EVERY call - so a parent session's own `computer`/`desktop`
    tools and a delegated `computer-operator` child session's `mount()` each
    built their OWN transport for the SAME target. Two concurrent agent
    processes against the same macOS target corrupt each other's Screen
    Recording TCC grant: `CGDisplayCreateImage` then returns `None` for BOTH
    agents - including the one that was capturing successfully a moment
    earlier - with no exception raised on either side (see `macos.py`).

    `shared_transport.acquire_shared_transport` makes every consumer in this
    process that resolves to the SAME `(ssh_path, host)` key share ONE
    underlying `SshTransport`/agent process, refcounted so the last consumer
    to release it is the one that actually tears it down.

    Kept separate from `select_backend` so tests can monkeypatch this one
    function to inject a fake transport without touching real SSH at all -
    that seam is unchanged; only what it returns (a shared handle instead of
    a bare `SshTransport`) is different, and `RemoteBackend` needs no changes
    since the handle duck-types the same `connect()`/`send()`/`close()`
    surface.
    """
    from .shared_transport import acquire_shared_transport
    from .ssh_transport import SshTransport

    ssh_path = str(config.get("ssh_path", "ssh"))

    def _factory() -> SshTransport:
        return SshTransport(
            host,
            package_dir=package_dir,
            ssh_path=ssh_path,
            deadman_seconds=float(config.get("deadman_seconds", 5.0)),
            read_only=bool(config.get("read_only", True)),
            with_pillow=bool(config.get("with_pillow", True)),
            port=port,
        )

    # `port` is part of the sharing key (bug-hunt defect B): two configured
    # targets that differ ONLY by port (e.g. two agents on the same host,
    # one on 22 and one on 2222) are genuinely different destinations and
    # must never be folded into the same shared transport/agent process.
    return acquire_shared_transport((ssh_path, host, port), _factory)


def select_backend(
    config: dict[str, Any], factories: tuple[type[Backend], ...] = BACKEND_FACTORIES
) -> Backend:
    """Probe each candidate backend in order; return the first that is available.

    C3: `config["target"]` absent -> exactly today's behavior, unchanged
    (probe local backends in order, `NoBackendAvailable` degrades to a silent
    skip in `mount()`). `config["target"]` present (`ssh://user@host`) ->
    `RemoteBackend` is the ONLY candidate - no probe-fallthrough to a local
    backend. An unreachable explicit target raises `RemoteTargetUnavailable`,
    which `mount()` does NOT catch (unlike `NoBackendAvailable`): the agent
    must never silently fall back to driving the controller's own local
    desktop when a specific remote machine was asked for and could not be
    reached (\u00a79 / acceptance item 7).
    """
    target = config.get("target")
    if target:
        # Import here, not at module top: remote_backend.py pulls in
        # ssh_transport.py (subprocess/tarfile) which local-only callers
        # (including remote_agent.py itself, which never uses `target`)
        # have no reason to load.
        from pathlib import Path

        from .remote_backend import RemoteBackend

        host, port = _parse_target(str(target))
        package_dir = Path(__file__).parent
        backend = RemoteBackend(
            {
                "_host": host,
                "_transport": _build_ssh_transport(
                    host, package_dir, config, port=port
                ),
            }
        )
        # M1 (docs/designs/capability-awareness.md \u00a74): this used to be a bare
        # statement - `connect()`'s return value (the whole handshake: probe,
        # capabilities, permissions, monitors, ops) was computed on the target
        # and then discarded three lines later. `RemoteBackend.connect()` now
        # binds it to `backend.handshake` itself (a connect-time SNAPSHOT, not
        # a live cache - see that method), so `desktop(action="doctor")`
        # (\u00a75) can report every honest fact already in that dict without
        # re-probing anything remote. Nothing here needs the return value
        # directly; the assignment inside `connect()` is what keeps it
        # reachable.
        try:
            backend.connect(
                required_permissions=tuple(config.get("required_permissions") or ()),
                connect_timeout=float(config.get("connect_timeout", 30.0)),
            )
        except Exception:
            # Selection owns this reference until it returns a backend. A failed
            # handshake must not leak it or replace the original connection error.
            try:
                backend.close()
            except Exception:  # noqa: BLE001 - best-effort failed-selection cleanup
                logger.debug(
                    "computer-use: failed-connect cleanup failed", exc_info=True
                )
            raise
        logger.info(
            "computer-use: selected remote backend %r (target=%r)", backend.name, target
        )
        return backend

    attempts: list[tuple[str, str]] = []
    for factory in factories:
        backend = factory(config)  # construction is cheap; probing is the real check
        try:
            result = backend.probe()
        except Exception as exc:
            logger.exception("computer-use: %s.probe() raised", factory.__name__)
            attempts.append(
                (
                    getattr(backend, "name", factory.__name__),
                    f"probe raised {type(exc).__name__}: {exc}",
                )
            )
            continue
        if result.available:
            logger.info("computer-use: selected backend %r", backend.name)
            return backend
        attempts.append((backend.name, result.reason or "unavailable"))
        logger.info(
            "computer-use: backend %r unavailable (%s)", backend.name, result.reason
        )
    raise NoBackendAvailable(attempts)
