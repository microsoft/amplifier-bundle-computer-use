"""Run the actual bootstrap in a local subprocess, never against a desktop."""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules/tool-computer-use"))

from amplifier_module_tool_computer_use.ssh_transport import (
    PAYLOAD_MODULES,
    _bootstrap_stub,
)


def _run_stub(tmp_path, extra=None, *, legacy=False, omit=None):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tf:
        for name in PAYLOAD_MODULES:
            if name == omit:
                continue
            if name == "remote_agent.py":
                code = b"print('agent started')\n"
            elif name == "agent_scratch_lease.py":
                code = (
                    ROOT
                    / "modules"
                    / "tool-computer-use"
                    / "amplifier_module_tool_computer_use"
                    / name
                ).read_bytes()
            else:
                code = b""
            info = tarfile.TarInfo(f"amplifier_cu_agent/{name}")
            info.size = len(code)
            tf.addfile(info, io.BytesIO(code))
        if extra is not None:
            tf.addfile(extra, io.BytesIO(b"") if extra.isreg() else None)
    payload = archive.getvalue()
    stub = _bootstrap_stub(5.0, True)
    if legacy:
        # Emulate a pre-filter 3.11 tarfile API; still extract using the real
        # archive implementation. The stub must validate names/types first.
        stub = (
            "import tarfile\n"
            "_extractall = tarfile.TarFile.extractall\n"
            "def legacy_extractall(self, path):\n"
            "    return _extractall(self, path, filter='fully_trusted')\n"
            "tarfile.TarFile.extractall = legacy_extractall\n"
            "del tarfile.data_filter\n"
        ) + stub
    return subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", stub],
        input=f"{len(payload)}\n".encode() + payload,
        capture_output=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_bootstrap_extracts_payload_without_warning(tmp_path, legacy):
    result = _run_stub(tmp_path, legacy=legacy)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"agent started\n"
    assert result.stderr == b""
    assert not list(tmp_path.glob("amplifier-cu-v2-agent-*"))


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("defect", ["missing", "duplicate"])
def test_bootstrap_requires_each_manifest_member_exactly_once(tmp_path, legacy, defect):
    duplicate = tarfile.TarInfo("amplifier_cu_agent/backend.py")
    result = _run_stub(
        tmp_path,
        duplicate if defect == "duplicate" else None,
        omit="backend.py" if defect == "missing" else None,
        legacy=legacy,
    )
    assert result.returncode != 0
    assert b"unsafe agent payload" in result.stderr
    assert result.stdout == b""


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escaped", tarfile.REGTYPE),
        ("absolute-path", tarfile.REGTYPE),
        ("amplifier_cu_agent/unexpected.py", tarfile.REGTYPE),
        ("amplifier_cu_agent/backend.py", tarfile.SYMTYPE),
        ("amplifier_cu_agent/backend.py", tarfile.LNKTYPE),
        ("amplifier_cu_agent/backend.py", tarfile.FIFOTYPE),
    ],
)
def test_bootstrap_refuses_non_manifest_or_non_regular_files(
    tmp_path, name, kind, legacy
):
    if name == "absolute-path":
        name = str(tmp_path / "escaped")
    extra = tarfile.TarInfo(name)
    extra.type = kind
    extra.linkname = "../escaped"
    result = _run_stub(tmp_path, extra, legacy=legacy)
    assert result.returncode != 0
    assert b"unsafe agent payload" in result.stderr
    assert b"agent started" not in result.stdout
    assert not (tmp_path / "escaped").exists()
    assert not list(tmp_path.glob("amplifier-cu-v2-agent-*"))
