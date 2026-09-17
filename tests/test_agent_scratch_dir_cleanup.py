"""Real-process lease tests for the exact generated bootstrap stub.

Every scratch directory is under pytest's ``tmp_path`` and every child gets
``TMPDIR=tmp_path``. The tests signal or remove only PIDs and paths they own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
PACKAGE_DIR = (
    ROOT / "modules" / "tool-computer-use" / "amplifier_module_tool_computer_use"
)

from amplifier_module_tool_computer_use import agent_scratch_lease as lease
from amplifier_module_tool_computer_use import ssh_transport

pytestmark = pytest.mark.skipif(
    lease._secure_flags() is None or lease._flock_module() is None,
    reason="requires POSIX no-follow operations and flock",
)


def _child_env(tmp_path: Path) -> dict[str, str]:
    env = {**os.environ, "TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return env


def _readline(stream, timeout: float = 10) -> bytes:
    result: dict[str, bytes] = {}

    def read() -> None:
        result["line"] = stream.readline()

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "child did not become ready before timeout"
    return result["line"]


def _remove_owned_scratch(tmp_path: Path) -> None:
    for path in tmp_path.glob(f"{lease.AGENT_SCRATCH_DIR_PREFIX}*"):
        shutil.rmtree(path, ignore_errors=True)


def _close_owned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None and process.stdin is not None:
        with contextlib.suppress(OSError, ValueError):
            process.stdin.close()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def _running_bootstrap(
    tmp_path: Path, payload: bytes, *, ready_prefix: bytes | None = None
) -> Iterator[tuple[subprocess.Popen[bytes], bytes | None]]:
    """Yield an owned bootstrap child and clean it on every setup/test failure."""
    process = subprocess.Popen(
        [sys.executable, "-c", ssh_transport._bootstrap_stub(5.0, True)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(tmp_path),
    )
    try:
        assert process.stdin is not None
        process.stdin.write(f"{len(payload)}\n".encode() + payload)
        process.stdin.flush()
        ready_line = None
        if ready_prefix is not None:
            assert process.stdout is not None
            ready_line = _readline(process.stdout)
            assert ready_line.startswith(ready_prefix), ready_line
        yield process, ready_line
    finally:
        _close_owned(process)
        _remove_owned_scratch(tmp_path)


def _fake_payload(remote_agent: bytes | None = None) -> bytes:
    archive = io.BytesIO()
    fake_agent = remote_agent or (
        b"import os,sys\n"
        b"print('READY', os.getpid(), flush=True)\n"
        b"assert sys.stdin.buffer.readline() == b'continue\\n'\n"
        b"from amplifier_cu_agent.backend import BackendError\n"
        b"assert BackendError.__name__ == 'BackendError'\n"
        b"print('LATER', flush=True)\n"
        b"assert sys.stdin.buffer.read() == b''\n"
    )
    with tarfile.open(fileobj=archive, mode="w:gz") as tf:
        for name in ssh_transport.PAYLOAD_MODULES:
            if name == "remote_agent.py":
                data = fake_agent
            elif name in {"agent_scratch_lease.py", "backend.py"}:
                data = (PACKAGE_DIR / name).read_bytes()
            else:
                data = b""
            info = tarfile.TarInfo(f"amplifier_cu_agent/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive.getvalue()


def _sweep_in_other_process(tmp_path: Path) -> int:
    script = (
        "import sys;"
        f"sys.path.insert(0, {str(ROOT / 'modules' / 'tool-computer-use')!r});"
        "from amplifier_module_tool_computer_use.agent_scratch_lease import sweep_stale_agent_dirs;"
        f"print(sweep_stale_agent_dirs(temp_dir={str(tmp_path)!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=_child_env(tmp_path),
        timeout=10,
        check=True,
    )
    return int(result.stdout.strip())


def _old_valid_dir(tmp_path: Path, suffix: str) -> Path:
    directory = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}{suffix}"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    marker = directory / lease.LEASE_FILENAME
    marker.write_bytes(lease.LEASE_MAGIC)
    os.chmod(marker, 0o600)
    os.utime(marker, (time.time() - 48 * 60 * 60,) * 2)
    return directory


def test_bootstrap_lease_precedes_ready_preserves_live_lazy_import_then_eof(
    tmp_path: Path,
) -> None:
    with _running_bootstrap(tmp_path, _fake_payload(), ready_prefix=b"READY ") as (
        process,
        ready,
    ):
        assert ready is not None and int(ready.decode().split()[1]) == process.pid
        scratch_dirs = list(tmp_path.glob(f"{lease.AGENT_SCRATCH_DIR_PREFIX}*"))
        assert len(scratch_dirs) == 1
        marker = scratch_dirs[0] / lease.LEASE_FILENAME
        assert marker.read_bytes() == lease.LEASE_MAGIC
        os.utime(marker, (time.time() - 48 * 60 * 60,) * 2)

        assert _sweep_in_other_process(tmp_path) == 0
        assert scratch_dirs[0].exists()

        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(b"continue\n")
        process.stdin.flush()
        assert _readline(process.stdout) == b"LATER\n"
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert not scratch_dirs[0].exists()


@pytest.mark.skipif(
    sys.platform != "linux", reason="requires a headless Linux backend probe"
)
def test_full_real_payload_bootstraps_headless_and_cleans_its_own_scratch(
    tmp_path: Path,
) -> None:
    payload = ssh_transport._build_payload(PACKAGE_DIR)
    with _running_bootstrap(tmp_path, payload) as (process, _):
        assert process.stdout is not None
        handshake = json.loads(_readline(process.stdout))
        assert handshake["ok"] is True
        assert handshake["result"]["backend"] == "none"
        assert process.wait(timeout=5) == 1
        assert not list(tmp_path.glob(f"{lease.AGENT_SCRATCH_DIR_PREFIX}*"))


def test_bootstrap_setup_failure_cleans_owned_child_and_scratch(tmp_path: Path) -> None:
    wrong_ready = b"import sys\nprint('WRONG', flush=True)\nsys.stdin.buffer.read()\n"
    with (
        pytest.raises(AssertionError),
        _running_bootstrap(
            tmp_path, _fake_payload(wrong_ready), ready_prefix=b"READY "
        ),
    ):
        pass
    assert not list(tmp_path.glob(f"{lease.AGENT_SCRATCH_DIR_PREFIX}*"))


def test_sweep_removes_only_test_owned_dead_lease_not_live_sibling(
    tmp_path: Path,
) -> None:
    with _running_bootstrap(tmp_path, _fake_payload(), ready_prefix=b"READY ") as (
        abandoned,
        ready,
    ):
        assert ready is not None and int(ready.decode().split()[1]) == abandoned.pid
        live_script = (
            "import atexit,os,shutil,sys,tempfile;"
            f"sys.path.insert(0, {str(ROOT / 'modules' / 'tool-computer-use')!r});"
            "from amplifier_module_tool_computer_use.agent_scratch_lease import AGENT_SCRATCH_DIR_PREFIX,acquire_agent_lease;"
            "scratch=tempfile.mkdtemp(prefix=AGENT_SCRATCH_DIR_PREFIX);"
            "os.chmod(scratch,0o700);"
            "atexit.register(shutil.rmtree,scratch,ignore_errors=True);"
            "fd=acquire_agent_lease(scratch);assert fd is not None;"
            "print('READY',os.getpid(),scratch,flush=True);"
            "sys.stdin.buffer.read();os.close(fd)"
        )
        live = subprocess.Popen(
            [sys.executable, "-c", live_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(tmp_path),
        )
        try:
            assert live.stdout is not None
            live_ready = _readline(live.stdout).decode().split()
            assert live_ready[0] == "READY" and int(live_ready[1]) == live.pid
            live_path = Path(live_ready[2])
            paths = list(tmp_path.glob(f"{lease.AGENT_SCRATCH_DIR_PREFIX}*"))
            assert len(paths) == 2
            for directory in paths:
                os.utime(
                    directory / lease.LEASE_FILENAME, (time.time() - 48 * 60 * 60,) * 2
                )

            abandoned.kill()
            assert abandoned.wait(timeout=5) < 0
            assert _sweep_in_other_process(tmp_path) == 1
            assert not any(path.exists() for path in paths if path != live_path)
            assert live_path.exists()
        finally:
            _close_owned(live)
        assert not live_path.exists()


def test_two_real_sweepers_remove_one_abandoned_dir_and_preserve_live_lease(
    tmp_path: Path,
) -> None:
    abandoned = _old_valid_dir(tmp_path, "abandoned")
    live_dir = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}live"
    live_dir.mkdir(mode=0o700)
    os.chmod(live_dir, 0o700)
    live_fd = lease.acquire_agent_lease(live_dir)
    assert live_fd is not None
    os.utime(live_dir / lease.LEASE_FILENAME, (time.time() - 48 * 60 * 60,) * 2)
    sweeper_script = (
        "import sys;"
        f"sys.path.insert(0, {str(ROOT / 'modules' / 'tool-computer-use')!r});"
        "from amplifier_module_tool_computer_use.agent_scratch_lease import sweep_stale_agent_dirs;"
        "print('READY',flush=True);"
        "sys.stdin.buffer.readline();"
        f"print(sweep_stale_agent_dirs(temp_dir={str(tmp_path)!r}),flush=True)"
    )
    sweepers = [
        subprocess.Popen(
            [sys.executable, "-c", sweeper_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(tmp_path),
        )
        for _ in range(2)
    ]
    try:
        for sweeper in sweepers:
            assert (
                sweeper.stdout is not None and _readline(sweeper.stdout) == b"READY\n"
            )
        for sweeper in sweepers:
            assert sweeper.stdin is not None
            sweeper.stdin.write(b"go\n")
            sweeper.stdin.flush()
        counts = []
        for sweeper in sweepers:
            assert sweeper.stdout is not None
            counts.append(int(_readline(sweeper.stdout).strip()))
            assert sweeper.wait(timeout=5) == 0
        assert sum(counts) == 1
        assert not abandoned.exists()
        assert live_dir.exists()
    finally:
        for sweeper in sweepers:
            _close_owned(sweeper)
        os.close(live_fd)
        shutil.rmtree(live_dir, ignore_errors=True)
