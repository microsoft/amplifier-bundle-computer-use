"""SSH transport: one persistent `ssh -T` subprocess per session, carrying both
the one-time code deployment and the NDJSON protocol session on the same
stdin/stdout pipe pair - `docs/designs/remote-transport.md` \u00a77-8.

No network daemon, no new listening port, no bespoke auth: "if you can SSH to
the box, you can drive its desktop" (\u00a72). This module owns exactly the
SSH subprocess lifecycle and the bootstrap deploy; wire framing lives in
`wire.py`, op dispatch lives in `remote_backend.py`.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shlex
import subprocess
import tarfile
import threading
from pathlib import Path
from typing import Any

from .agent_scratch_lease import AGENT_SCRATCH_DIR_PREFIX
from .wire import Response, validate_handshake

logger = logging.getLogger(__name__)

#: This bundle's own files needed on the target - zero amplifier_core
#: dependency (see pyproject.toml), so this is the complete, self-contained
#: set `remote_agent.py` needs to run standalone. `windows.py` is included
#: unconditionally: it imports nothing platform-specific at module level (only
#: `subprocess`/`shutil`/`tempfile`), so it is safe to ship even to a
#: macOS/Linux target - `registry.select_backend()` just never picks it there.
#: Deliberately EXCLUDES this package's real `__init__.py` (`ComputerTool`,
#: `DesktopTool`, `mount()`) - that file imports `amplifier_core`, which the
#: target has never heard of. `_build_payload` synthesizes an empty
#: `__init__.py` for the deployed package instead (see below).
PAYLOAD_MODULES = (
    "backend.py",
    "geometry.py",
    "imaging.py",
    "monitors.py",
    "registry.py",
    "linux_x11.py",
    "macos.py",
    "windows.py",
    "wire.py",
    "ledger.py",
    "agent_scratch_lease.py",
    "remote_agent.py",
    # The session-start disclosure channel (docs/designs/coexistence.md
    # \u00a77, \u00a710.3) closes the remote-transport gap BACKLOG.md recorded:
    # every one of these three modules was built assuming it runs in the
    # SAME process that owns the injection call site - which, for a remote
    # session, is HERE on the target (`remote_agent.RemoteAgent`), never the
    # controller. `exclusion.py` is `overlay_linux.py`/`overlay_windows.py`'s
    # own dependency (button-rect bookkeeping, pure logic, no display
    # dependency) and is shipped for the same reason those two are.
    "exclusion.py",
    "announce_macos.py",
    "overlay_linux.py",
    "overlay_windows.py",
    # NOT a Python module, and required anyway: `windows.py` resolves
    # `BRIDGE_PS1 = Path(__file__).parent / "bridge.ps1"` and invokes it with
    # `powershell.exe -File`. Omitting it does not fail at import - it fails at
    # the first action, where PowerShell is handed a path that does not exist,
    # prints its startup banner, and exits. The banner then arrives where JSON
    # was expected, which reads like a bridge/protocol bug rather than a missing
    # file. Verified against a live WSL target: handshake succeeded and
    # `screen_info` returned "Copyright (C) Microsoft Corporation..." until this
    # file was shipped.
    "bridge.ps1",
    # Same reasoning as `bridge.ps1` above, for `overlay_windows.py`'s own
    # `OVERLAY_PS1 = Path(__file__).parent / "overlay_windows.ps1"` - the
    # remote Windows persistent overlay would otherwise fail at
    # `announce_raise` time with the identical "missing file" shape.
    "overlay_windows.ps1",
)

_PACKAGE_NAME = "amplifier_cu_agent"

#: Absolute-path candidates for `uv`, in order - the same "don't trust PATH in
#: a non-login shell" lesson `windows.py::_which_powershell` already encodes
#: (\u00a73.1: `uv` was found on PATH here, but a non-login remote shell is not
#: guaranteed to have it). Verified locations for this design's two real
#: targets are tried first; bare `uv` (in case PATH does carry it) is tried
#: too, cheaply, as part of the same one-line remote probe.
UV_CANDIDATES = (
    "uv",
    "$HOME/.local/bin/uv",
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
)

#: Same discipline as the human-verified SSH invocation in the task brief:
#: batch mode (never prompts), no controlmaster/multiplexing surprises,
#: short connect timeout, keepalives so a half-open connection is detected
#: from the controller side too (the agent-side deadman is what actually
#: protects the desktop - \u00a710.2).
_SSH_OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=2",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
]


def _ssh_opts(port: int | None) -> list[str]:
    """`_SSH_OPTS` plus `-p <port>` when a non-standard port is configured
    (bug-hunt defect B: `ssh://host:port` used to parse successfully and
    then silently lose the port - no `-p` existed anywhere in this list).

    `port is None` -> exactly `_SSH_OPTS` unchanged, byte for byte - `ssh`
    itself defaults to port 22 when `-p` is absent, so this is a no-op for
    every target that does not configure a non-standard port; there is no
    behavior change for the common case.
    """
    if port is None:
        return list(_SSH_OPTS)
    return [*_SSH_OPTS, "-p", str(port)]


#: Truncation and redaction for stderr captured from a target during a
#: failed connect/send - long output is dominated by retry noise (the
#: incident that motivated this: a `uv fetch` failure whose real cause was
#: one line in the middle of several), so the useful part is almost always
#: the first couple of lines (what failed) and the last few (the root
#: cause). `_CREDENTIAL_IN_URL_RE` targets embedded basic-auth credentials
#: in URLs (`scheme://user:pass@host`) - a private package-feed index URL
#: is exactly the kind of thing this stderr can legitimately contain, and
#: this is the one place that text is guaranteed to end up in both an
#: exception message and a log line.
_STDERR_HEAD_LINES = 4
_STDERR_TAIL_LINES = 6
_STDERR_MAX_LINES = _STDERR_HEAD_LINES + _STDERR_TAIL_LINES
_CREDENTIAL_IN_URL_RE = re.compile(r"://[^/\s@]+@")


def _redact_credentials(text: str) -> str:
    """Replace `scheme://user:password@host` basic-auth credentials with a
    fixed marker. Hosts and paths are left intact - only the userinfo
    component is secret."""
    return _CREDENTIAL_IN_URL_RE.sub("://***:***@", text)


def _summarize_stderr(raw: str) -> str:
    """Redact credentials, then truncate to the first/last few lines if the
    output is long. Returns text ready to embed under an exception message -
    legible both as a multi-line console dump and as the tail of a single
    (possibly newline-containing) log record."""
    lines = _redact_credentials(raw).strip().splitlines()
    if len(lines) <= _STDERR_MAX_LINES:
        return "\n".join(lines)
    head = lines[:_STDERR_HEAD_LINES]
    tail = lines[-_STDERR_TAIL_LINES:]
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"... ({omitted} lines omitted) ...", *tail])


class AgentStderrError(RuntimeError):
    """Base for exceptions that may carry captured target/agent stderr.

    `agent_stderr` is the raw text captured from the target process's
    stderr stream at the moment of failure - already truncated and
    credential-redacted by `_summarize_stderr` (see
    `SshTransport._drain_stderr_on_failure`). It is `None` when nothing
    could be captured (no fallback, no fabricated content - callers must
    not assume it is always populated).

    `str(exc)` embeds it below the message, so a single `logger.error("%s",
    exc)` call and a printed traceback both show the whole picture - there
    is nothing left for the operator to go read that isn't already here.
    """

    def __init__(self, message: str, *, agent_stderr: str | None = None) -> None:
        self.message = message
        self.agent_stderr = agent_stderr
        super().__init__(message)

    def __str__(self) -> str:
        if self.agent_stderr:
            return f"{self.message}\nagent stderr:\n{self.agent_stderr}"
        return self.message


class SshConnectError(AgentStderrError):
    """The SSH session could not be established or the deploy/handshake failed."""


def _build_payload(package_dir: Path) -> bytes:
    """Deterministic tar.gz of `PAYLOAD_MODULES`, rooted at `_PACKAGE_NAME/`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in PAYLOAD_MODULES:
            path = package_dir / name
            data = path.read_bytes()
            info = tarfile.TarInfo(name=f"{_PACKAGE_NAME}/{name}")
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _resolve_uv_command(
    user_host: str, ssh_path: str = "ssh", *, port: int | None = None
) -> str:
    """Discover an absolute `uv` path on the target via a short, bounded probe.

    Uses Python's own `subprocess.run(..., timeout=...)`, not a shell
    `timeout` wrapper - the task brief's own incident (a bare blocking `ssh`
    hanging 6h55m past both an inner and outer timeout) was specifically a
    shell-level `timeout`+backgrounding interaction; `subprocess.run(timeout=)`
    kills the `ssh` process directly on expiry, which is not subject to that
    failure mode.

    `port` (bug-hunt defect B): must reach THIS probe too, not just the
    main `connect()` command below - a non-standard-port target whose `uv`
    probe still silently went to port 22 would fail confusingly (or worse,
    succeed against an unrelated host on port 22) before ever reaching the
    real connection attempt.
    """
    probe = " || ".join(f"command -v {shlex.quote(c)}" for c in UV_CANDIDATES)
    cmd = [ssh_path, "-n", *_ssh_opts(port), user_host, f"sh -lc {shlex.quote(probe)}"]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SshConnectError(f"timed out probing for uv on {user_host}") from exc
    found = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    if not found:
        raise SshConnectError(
            f"could not find 'uv' on {user_host} (tried: {list(UV_CANDIDATES)}; "
            f"stderr: {proc.stderr.strip()[:300]!r})"
        )
    return found


_SIZE_LINE_RE = re.compile(rb"^\d+$")


def _bootstrap_stub(deadman_seconds: float, read_only: bool) -> str:
    """The pure-Python stub run as the SSH remote command's payload consumer.

    Reads a `SIZE\\n` line, then exactly that many raw bytes
    (`sys.stdin.buffer.read(n)` - the \u00a73.3-proven safe primitive; `head -c
    N` is explicitly unsafe here, it over-reads and swallows the NDJSON
    requests that follow), extracts the tar.gz into a scratch dir, and runs
    the agent IN-PROCESS via `runpy` (an `os.exec*` would discard whatever of
    stdin Python has already buffered past the tar bytes).

    Scratch-dir lifecycle (the leak this stub used to have - `w` was created
    and never removed by any exit path):

    `atexit.register(shutil.rmtree, w, ...)` is registered the INSTANT `w` is
    created, before extraction even happens, and fires whenever THIS process
    reaches a normal Python-level shutdown - which covers every exit path
    that runs any Python code on the way out:

    * stdin EOF / the agent's own `bye` op (the ordinary case - `run()`'s
      loop ends, `main()` returns, `SystemExit(main())` propagates to the
      top of this `-c` script).
    * SIGTERM, SIGHUP, or SIGINT delivered after `remote_agent.RemoteAgent
      .install_signal_handlers()` has run - each of those handlers calls
      `sys.exit(0)`, which is a normal (if abrupt) Python exit, not the raw
      OS default action, so it still reaches this same shutdown path.

    It does NOT cover a SIGKILL (to this process or an ancestor `uv run`
    it can't intercept), an OOM-kill, or the host disappearing outright -
    none of those ever run another line of Python, so nothing registered
    with `atexit` can fire. `amplifier_cu_agent.remote_agent
    .sweep_stale_agent_dirs()` (called from `main()`, so it runs once per
    NEW connection, on the fresh agent process) exists to bound the damage
    from exactly that gap: it age-gates rather than identity-gates so a
    live sibling session's directory (a second, independent connection to
    the same target - see `_build_ssh_transport` in `registry.py`) is never
    at risk, but anything old enough to be almost certainly orphaned gets
    swept up the next time anyone connects. See that function's docstring
    for the full reasoning.
    """
    read_only_arg = "true" if read_only else "false"
    # Restrict even older Python 3.11 patch releases without tar filters to
    # our fixed, regular-file manifest. Never fall back to arbitrary extraction.
    allowed_names = tuple(f"{_PACKAGE_NAME}/{name}" for name in PAYLOAD_MODULES)
    return (
        "import sys,os,tarfile,io,runpy,hashlib,tempfile,atexit,shutil;"
        "buf=sys.stdin.buffer;"
        "n=int(buf.readline().strip());"
        "data=buf.read(n);"
        "sys.exit(97) if len(data)!=n else None;"
        "d=hashlib.sha256(data).hexdigest();"
        f"w=tempfile.mkdtemp(prefix={AGENT_SCRATCH_DIR_PREFIX!r});"
        "atexit.register(shutil.rmtree,w,ignore_errors=True);"
        "t=tarfile.open(fileobj=io.BytesIO(data),mode='r:gz');"
        f"allowed={allowed_names!r};"
        "members=t.getmembers();"
        "sys.exit('unsafe agent payload: expected only manifest regular files') "
        "if sorted(m.name for m in members)!=sorted(allowed) "
        "or any(not m.isreg() for m in members) "
        "else None;"
        "t.extractall(w,**({'filter':'data'} if hasattr(tarfile,'data_filter') else {}));"
        "t.close();"
        "sys.path.insert(0,w);"
        "from amplifier_cu_agent.agent_scratch_lease import acquire_agent_lease;"
        "lease_fd=acquire_agent_lease(w);"
        "os.environ['AMPLIFIER_CU_AGENT_SHA256']=d;"
        f"sys.argv=['remote_agent','--deadman-seconds={deadman_seconds}',"
        f"'--read-only={read_only_arg}'];"
        "runpy.run_module('amplifier_cu_agent.remote_agent',run_name='__main__')"
    )


class SshTransport:
    """Owns one `ssh -T` subprocess for the life of a remote-backend session.

    `connect()` deploys the agent payload and performs the handshake;
    `send()` writes one request line and blocks for the matching response
    line (Phase 1 does not pipeline); `close()` runs the documented shutdown
    order (\u00a710.1): `release_all` -> `bye` -> close stdin -> wait -> SIGTERM
    -> wait -> SIGKILL.
    """

    def __init__(
        self,
        user_host: str,
        *,
        package_dir: Path,
        ssh_path: str = "ssh",
        deadman_seconds: float = 5.0,
        read_only: bool = True,
        with_pillow: bool = True,
        port: int | None = None,
    ) -> None:
        self.user_host = user_host
        self._package_dir = package_dir
        self._ssh_path = ssh_path
        self._deadman_seconds = deadman_seconds
        self._read_only = read_only
        self._with_pillow = with_pillow
        # Bug-hunt defect B: a non-standard SSH port. `None` (the default -
        # ordinary port 22) reproduces every ssh invocation this class made
        # before this field existed, byte for byte - see `_ssh_opts`.
        self.port = port
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._sent_sha256: str | None = None
        self.handshake: dict[str, Any] | None = None

    def connect(
        self,
        *,
        required_permissions: tuple[str, ...] = (),
        connect_timeout: float = 30.0,
    ) -> dict[str, Any]:
        payload = _build_payload(self._package_dir)
        self._sent_sha256 = hashlib.sha256(payload).hexdigest()

        uv_cmd = _resolve_uv_command(self.user_host, self._ssh_path, port=self.port)
        stub = _bootstrap_stub(self._deadman_seconds, self._read_only)
        if self._with_pillow:
            # Mirrors this bundle's own pyproject.toml dependency markers
            # exactly (pyobjc-framework-Quartz only on darwin, python-xlib
            # only on linux) - `uv run --with` accepts a full PEP 508
            # requirement string, environment marker included, so uv installs
            # only what the TARGET's actual platform needs. Neither backend
            # module is importable without its native extension (Quartz/Xlib)
            # - see the guarded-import fix in linux_x11.py/macos.py - so
            # skipping the wrong one here is not a functional loss, only a
            # smaller ephemeral env.
            extras = [
                "pillow",
                'pyobjc-framework-Quartz; sys_platform=="darwin"',
                'python-xlib; sys_platform=="linux"',
            ]
            with_args = " ".join(f"--with {shlex.quote(e)}" for e in extras)
            remote_cmd = (
                f"{shlex.quote(uv_cmd)} run {with_args} python3 -c {shlex.quote(stub)}"
            )
        else:
            remote_cmd = f"python3 -c {shlex.quote(stub)}"

        cmd = [self._ssh_path, "-T", *_ssh_opts(self.port), self.user_host, remote_cmd]
        logger.info("ssh-transport: connecting to %s", self.user_host)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._proc = proc
        assert proc.stdin is not None and proc.stdout is not None

        try:
            size_line = f"{len(payload)}\n".encode()
            proc.stdin.write(size_line)
            proc.stdin.write(payload)
            proc.stdin.flush()
        except BrokenPipeError as exc:
            stderr = self._drain_stderr_on_failure()
            raise SshConnectError(
                f"broken pipe writing payload to {self.user_host} - deploy failed",
                agent_stderr=stderr,
            ) from exc

        line = self._read_line_with_timeout(proc.stdout, connect_timeout)
        if line is None:
            stderr = self._drain_stderr_on_failure()
            raise SshConnectError(
                f"no handshake from {self.user_host} within {connect_timeout}s "
                "(agent may have crashed during bootstrap)",
                agent_stderr=stderr,
            )
        resp = Response.decode(line)
        if not resp.ok or not isinstance(resp.result, dict):
            raise SshConnectError(
                f"malformed handshake from {self.user_host}: {line!r}"
            )

        validate_handshake(
            resp.result,
            expected_sha256=self._sent_sha256,
            required_permissions=required_permissions,
        )
        self.handshake = resp.result
        logger.info(
            "ssh-transport: handshake ok backend=%s platform=%s",
            resp.result.get("backend"),
            resp.result.get("platform"),
        )
        return resp.result

    def send(self, line: bytes, *, timeout: float = 30.0) -> bytes:
        """Write one request line, block for exactly the matching response
        line. Phase 1 is not pipelined - one in flight at a time - so
        correlation by `id` is a cheap sanity check here, not real multiplexing."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise SshConnectError("not connected")
        with self._lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except BrokenPipeError as exc:
                raise SshConnectError(
                    f"connection to {self.user_host} lost while sending"
                ) from exc
            resp_line = self._read_line_with_timeout(proc.stdout, timeout)
            if resp_line is None:
                stderr = self._drain_stderr_on_failure()
                raise SshConnectError(
                    f"no response from {self.user_host} within {timeout}s "
                    "(connection likely lost)",
                    agent_stderr=stderr,
                )
            # Surface the agent's own stderr whenever it reports an error.
            # Previously stderr was drained ONLY on connect failure, so a
            # request-level error arrived as a bare message with the agent's
            # side of the story discarded - which is exactly the case where it
            # matters most. A platform API that fails by returning nothing
            # (macOS `CGDisplayCreateImage` under a denied TCC grant is the
            # example that motivated this) leaves no other trace at all.
            if b'"error' in resp_line or b'"ok": false' in resp_line:
                self._drain_stderr_on_failure()
            return resp_line

    def _read_line_with_timeout(self, stream: Any, timeout: float) -> bytes | None:
        result: dict[str, bytes | None] = {"line": None}

        def _read() -> None:
            result["line"] = stream.readline() or None

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        return result["line"]

    def _drain_stderr_on_failure(self) -> str | None:
        """Read at most 4096 already-available bytes; never wait for stderr.

        Popen uses unbuffered pipes. An empty, still-open pipe blocks even
        after a valid error response arrived on stdout, outside send's timeout.
        Switch the fd to nonblocking for this read and restore its mode. If
        the platform cannot do that, omit diagnostics rather than risk a hang.
        Both the console and exception receive the credential-redacted summary.
        This is accumulated output, not necessarily from the failed operation.
        """
        if self._proc is None or self._proc.stderr is None:
            return None
        try:
            fd = self._proc.stderr.fileno()
            was_blocking = os.get_blocking(fd)
            try:
                os.set_blocking(fd, False)
                data = os.read(fd, 4096)
            finally:
                os.set_blocking(fd, was_blocking)
        except BlockingIOError:
            return None
        except Exception as exc:  # noqa: BLE001 - best-effort diagnostics only
            logger.warning(
                "ssh-transport: nonblocking stderr drain unavailable: %s", exc
            )
            return None
        if not data:
            return None
        summary = _summarize_stderr(data.decode(errors="replace"))
        logger.error(
            "ssh-transport: agent stderr (accumulated since last drain): %s", summary
        )
        return summary

    def close(self) -> None:
        """Shutdown order per \u00a710.1: release_all -> bye -> close stdin ->
        wait 2s -> SIGTERM -> wait 2s -> SIGKILL."""
        proc = self._proc
        if proc is None:
            return
        try:
            from .wire import Request

            try:
                self.send(Request(id=999_000_001, op="release_all").encode(), timeout=5)
            except Exception as exc:  # noqa: BLE001 - best-effort on the way out
                logger.debug("ssh-transport: release_all on close failed: %s", exc)
            try:
                self.send(Request(id=999_000_002, op="bye").encode(), timeout=5)
            except Exception as exc:  # noqa: BLE001 - best-effort on the way out
                logger.debug("ssh-transport: bye on close failed: %s", exc)
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._proc = None
