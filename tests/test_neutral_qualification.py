"""Synthetic UNIT CONTROLS only: never actual/live attestation or captures.

Temporary PIL images and opaque receipts exercise fail-closed logic. The default
release consumer rejects their explicit test-control evidence class.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import sys
import time
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neutral_qualification as nq
import qualify_neutral_session as runner

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
DIGEST = "a" * 64
REVISION = "b" * 40


def save(path, value):
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


@pytest.fixture
def control(tmp_path):
    # Freshness time is fixed for deterministic UNIT CONTROL tests, not a live date.
    source = {
        f"modules/{r}-computer-use/unit_{r}/__init__.py": DIGEST
        for r in ("tool", "hook")
    }
    packages = {
        r: {
            "distribution": "unit-" + r,
            "module": "unit_" + r,
            "version": "unit-control",
            "revision": None,
            "manifest": {"__init__.py": DIGEST},
        }
        for r in nq.IDENTITY_ROLES
    }
    producer = {"revision": REVISION, "scripts": {"unit-control.py": DIGEST}}
    bindings = [
        {
            "provider": "opaque-unit-control",
            "profile": profile,
            "mode": mode,
            "identities": copy.deepcopy(packages),
            "source": source,
        }
        for profile, mode in (
            ("current", "live"),
            ("current", "offline"),
            ("floor", "offline"),
        )
    ]
    expected = {"producer": producer, "source": source, "bindings": bindings}
    captures = []
    for index in range(2):
        name = f"unit-control-{index}.png"
        Image.new("RGB", (8, 8), (index * 255, 0, 0)).save(tmp_path / name)
        captures.append(nq.artifact(tmp_path, name, "capture"))
    (tmp_path / "unit-control.log").write_text(
        "SYNTHETIC UNIT CONTROL ONLY; no actual execution or wire evidence.\n"
    )
    evidence = [nq.artifact(tmp_path, "unit-control.log", "observation")]
    policy = {
        "owner": "opaque-unit-control",
        "bound": 2,
        "provenance": "synthetic control only",
    }
    bounds = {
        "limits": {"complete": 4, "request_budget": 8, "estimate": 8},
        "calls": {"complete": 4, "request_budget": 4, "estimate": 0},
        "deadline_seconds": 30,
        "elapsed_seconds": 20,
        "configured": {"max_retries": 0, "auto_continue": False},
        "effective_wire": "UNKNOWN",
        "count_policy": policy,
    }
    hashes = [c["sha256"] for c in captures]
    observations = {
        "session": {
            "public_bounds": bounds,
            "supervision": {
                "worker_pid": 111,
                "process_group": 111,
                "deadline_seconds": 30,
                "elapsed_seconds": 21,
                "timed_out": False,
                "group_absent": True,
            },
            "cycles": [
                {
                    "captures": [hashes[0]],
                    "projected": [hashes[0]],
                    "recognized": True,
                    "prior_history_retained": True,
                },
                {
                    "captures": [hashes[1]],
                    "projected": hashes,
                    "recognized": True,
                    "prior_history_retained": True,
                },
            ],
            "owned_display": {
                "pid": 111,
                "process_group": 111,
                "display": ":111",
                "scope": "owned-X11-only",
            },
            "cleanup": {
                "session": "closed",
                "backend": "closed",
                "tk": "closed",
                "xvfb": "absent",
            },
        },
        "provider": {
            "observer": "synthetic external-unit-control",
            "generation": {
                "http_calls": 4,
                "maximum_http_calls": 4,
                "attempt_timeout_seconds": 5,
                "retry_outcomes": ["synthetic-no-retry"],
            },
            "count": {
                "http_calls": 5,
                "maximum_http_calls": 8,
                "attempt_timeout_seconds": 5,
                "retry_outcomes": ["synthetic-bounded-retry"],
            },
            "generation_retries": 0,
            "auto_continue": False,
            "count_policy": policy,
            "evidence": evidence,
        },
        "policy": {
            "read_only_inputs": 0,
            "unapproved_inputs": 0,
            "approved_inputs": 1,
            "final_capture": hashes[0],
        },
        "offline": {"tests": 7, "failed": 0, "skipped": 0, "evidence": evidence},
    }
    rows = []
    for index, binding in enumerate(bindings):
        owners = (
            ("session", "provider", "policy")
            if binding["mode"] == "live"
            else ("offline",)
        )
        refs = []
        for owner in owners:
            name = f"unit-control-{index}-{owner}.json"
            value = nq.receipt(
                binding,
                owner,
                (NOW - timedelta(seconds=25)).isoformat(),
                (NOW - timedelta(seconds=1)).isoformat(),
                "PASS",
                dict.fromkeys(nq.SCENARIOS[owner], "PASS"),
                captures if owner in {"session", "policy"} else [],
                observations=observations[owner],
                evidence_class="test-control",
            )
            save(tmp_path / name, value)
            refs.append(nq.artifact(tmp_path, name, owner))
        rows.append({"binding": binding, "status": "PASS", "artifacts": refs})
    return nq.record(rows, producer, source, now=NOW), expected, tmp_path


def validate_control(control):
    document, expected, root = control
    return nq.validate(
        document, expected, NOW, evidence_root=root, allow_test_controls=True
    )


def change_receipt(control, owner, change):
    document, _, root = control
    ref = next(r for r in document["rows"][0]["artifacts"] if r["kind"] == owner)
    path = root / ref["path"]
    value = nq.load_json(path)
    change(value)
    save(path, value)
    ref["sha256"] = nq.sha256(path)


def test_unit_controls_pass_only_explicit_test_validator(control):
    assert validate_control(control)
    document, expected, root = control
    with pytest.raises(nq.QualificationError, match="genuine"):
        nq.validate(document, expected, NOW, evidence_root=root)


@pytest.mark.parametrize(
    "field", ["status", "producer", "source", "rows", "created_at"]
)
def test_missing_document_fields_fail(control, field):
    del control[0][field]
    with pytest.raises(nq.QualificationError):
        validate_control(control)


@pytest.mark.parametrize("offset", [-31, 1])
def test_stale_and_future_fail(control, offset):
    control[0]["created_at"] = (NOW + timedelta(days=offset)).isoformat()
    with pytest.raises(nq.QualificationError, match="future or stale"):
        validate_control(control)


@pytest.mark.parametrize(
    "mutation",
    [
        "failed",
        "row_failed",
        "duplicate",
        "missing_row",
        "wrong_profile",
        "source",
        "producer",
        "sdk",
        "loaded_source",
    ],
)
def test_document_negative_controls(control, mutation):
    document, expected, _ = control
    if mutation == "failed":
        document["status"] = "FAIL"
    elif mutation == "row_failed":
        document["rows"][0]["status"] = "FAIL"
    elif mutation == "duplicate":
        document["rows"].append(copy.deepcopy(document["rows"][0]))
    elif mutation == "missing_row":
        document["rows"].pop()
    elif mutation == "wrong_profile":
        document["rows"][0]["binding"]["profile"] = "floor"
    elif mutation == "source":
        document["source"] = {"unexpected.py": DIGEST}
    elif mutation == "producer":
        document["producer"] = {"revision": "c" * 40, "scripts": {"other.py": DIGEST}}
    elif mutation == "sdk":
        document["rows"][0]["binding"]["identities"]["sdk"]["version"] = (
            "wrong-profile-sdk"
        )
    else:
        expected["bindings"][0]["identities"]["tool"]["manifest"]["__init__.py"] = (
            "c" * 64
        )
    with pytest.raises(nq.QualificationError):
        validate_control(control)


@pytest.mark.parametrize("owner", ["session", "provider", "policy", "offline"])
def test_missing_independent_owner_fails(control, owner):
    for row in control[0]["rows"]:
        row["artifacts"] = [r for r in row["artifacts"] if r["kind"] != owner]
    with pytest.raises(nq.QualificationError, match="missing independent"):
        validate_control(control)


@pytest.mark.parametrize(
    "case",
    [
        "failed",
        "scenario",
        "bounds",
        "wire_unknown",
        "retry_unknown",
        "history",
        "cleanup",
        "capture",
        "policy_input",
        "policy_binding",
    ],
)
def test_receipt_negative_controls(control, case):
    if case == "failed":
        change_receipt(control, "session", lambda v: v.update(status="FAIL"))
    elif case == "scenario":
        change_receipt(
            control, "session", lambda v: v["scenarios"].pop("marker_recognition")
        )
    elif case == "bounds":
        change_receipt(
            control,
            "session",
            lambda v: v["observations"]["public_bounds"]["calls"].update(complete=100),
        )
    elif case == "wire_unknown":
        change_receipt(
            control,
            "provider",
            lambda v: v["observations"]["count"].update(http_calls=None),
        )
    elif case == "retry_unknown":
        change_receipt(
            control,
            "provider",
            lambda v: v["observations"].update(generation_retries="UNKNOWN"),
        )
    elif case == "history":
        change_receipt(
            control,
            "session",
            lambda v: v["observations"]["cycles"][1].update(projected=[]),
        )
    elif case == "cleanup":
        change_receipt(
            control,
            "session",
            lambda v: v["observations"]["cleanup"].update(xvfb="UNKNOWN"),
        )
    elif case == "capture":
        change_receipt(control, "session", lambda v: v.update(captures=[]))
    elif case == "policy_input":
        change_receipt(
            control, "policy", lambda v: v["observations"].update(unapproved_inputs=1)
        )
    else:
        change_receipt(
            control,
            "provider",
            lambda v: v["observations"].update(
                count_policy={"different": "unit-control"}
            ),
        )
    with pytest.raises(nq.QualificationError):
        validate_control(control)


def test_tampered_and_missing_artifacts_fail(control):
    document, _, root = control
    path = root / document["rows"][0]["artifacts"][0]["path"]
    path.write_text("tampered")
    with pytest.raises(nq.QualificationError, match="hash mismatch"):
        validate_control(control)
    path.unlink()
    with pytest.raises(nq.QualificationError, match="missing"):
        validate_control(control)


def test_capture_tamper_fails(control):
    (control[2] / "unit-control-0.png").write_bytes(b"not PNG")
    with pytest.raises(nq.QualificationError, match="hash mismatch"):
        validate_control(control)


@pytest.mark.parametrize(
    "relative",
    ["/abs.json", "../escape", "a/../b", "a\\b", "file:remote", "./x", "", "."],
)
def test_unsafe_relative_paths_fail(tmp_path, relative):
    with pytest.raises(nq.QualificationError):
        nq.safe_path(tmp_path, relative)


def test_symlink_escape_fails(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").write_text("unit-control")
    (root / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(nq.QualificationError):
        nq.safe_path(root, "link")


@pytest.mark.parametrize("value", ['{"a":1,"a":2}', '{"a":NaN}', "not JSON"])
def test_malformed_json_fails(tmp_path, value):
    path = tmp_path / "control.json"
    path.write_text(value)
    with pytest.raises(nq.QualificationError):
        nq.load_json(path)


def test_record_does_not_hide_failure(control):
    document, expected, _ = control
    document["rows"][0]["status"] = "FAIL"
    assert (
        nq.record(document["rows"], expected["producer"], expected["source"], now=NOW)[
            "status"
        ]
        == "FAIL"
    )
    assert (
        nq.record([], expected["producer"], expected["source"], now=NOW)["status"]
        == "FAIL"
    )


def test_runner_public_quotas_and_purity():
    calls = runner.PublicCalls(
        {"complete": 1, "request_budget": 1, "estimate": 1}, time.monotonic() + 10
    )

    async def complete(request):
        return "unit-control"

    async def budget_result():
        return {"unit-control": True}

    def budget(request):
        return budget_result()

    provider = SimpleNamespace(complete=complete, request_budget=budget)
    request = {"content": "unit-control"}
    calls.wrap(provider)
    assert asyncio.run(provider.complete(request)) == "unit-control"
    assert asyncio.run(provider.request_budget(request)) == {"unit-control": True}
    assert calls.calls == {"complete": 1, "request_budget": 1, "estimate": 0}
    with pytest.raises(nq.QualificationError, match="quota"):
        provider.complete(request)
    calls.restore()
    assert provider.complete is complete and provider.request_budget is budget
    calls.deadline = time.monotonic() - 1
    with pytest.raises(nq.QualificationError, match="deadline"):
        calls.admit("estimate")


def test_runner_rejects_mutated_requests():
    calls = runner.PublicCalls(
        {"complete": 1, "request_budget": 1, "estimate": 1}, time.monotonic() + 10
    )

    def mutate(request):
        request["changed"] = True

    method = calls.wrapper("estimate", mutate)
    with pytest.raises(nq.QualificationError, match="caller request changed"):
        method({})


def test_runner_generic_image_projection(control):
    raw = (control[2] / "unit-control-0.png").read_bytes()
    assert runner.image_hashes({"any": [base64.b64encode(raw).decode()]}) == [
        hashlib_digest(raw)
    ]
    assert "receipt code" in runner.prompt() and "unit-control" not in runner.prompt()


def hashlib_digest(raw):
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def test_runner_cleanup_only_owned_process(monkeypatch):
    actions = []
    process = SimpleNamespace(
        pid=111,
        stdout=None,
        poll=lambda: None,
        terminate=lambda: actions.append("terminate"),
        wait=lambda timeout: actions.append("wait"),
    )

    def absent(pid, signal):
        assert pid == 111 and signal == 0
        raise ProcessLookupError

    monkeypatch.setattr(runner.os, "kill", absent)
    assert runner.stop_owned(process) == "absent"
    assert actions == ["terminate", "wait"]
    assert runner.stop_owned(None) == "absent"


@pytest.mark.parametrize("preclaimed", [False, True])
def test_runner_failed_run_record_no_display_or_network(
    control, monkeypatch, preclaimed
):
    # Deliberate pre-display identity failure. No key is read by a real provider,
    # no Session/display is started, and the receipt cannot pass any live gate.
    _, expected, root = control
    save(root / "expected.json", expected)
    save(root / "policy.json", {"unit-control": "opaque-policy"})
    args = Namespace(
        expected=root / "expected.json",
        provider="opaque-unit-control",
        provider_module="unit_provider",
        keyenv="UNIT_CONTROL_ENV",
        count_policy=root / "policy.json",
        output=root / "failed-run",
        max_complete=4,
        max_budget=4,
        max_estimate=4,
        deadline_seconds=30,
        model="unit-control",
        xvfb="must-not-start",
    )
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("UNIT_CONTROL_ENV", "synthetic-control-only")
    old_home = os.environ.get("HOME")

    def fail(*args):
        raise nq.QualificationError("intentional unit-control identity rejection")

    def no_spawn(*args, **kwargs):
        pytest.fail("identity failure must not start a display or provider")

    monkeypatch.setattr(runner, "verify_identity", fail)
    monkeypatch.setattr(runner.subprocess, "Popen", no_spawn)
    if preclaimed:
        args.output.mkdir(mode=0o700)
        fd = os.open(args.output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            assert asyncio.run(runner.run(args, output_fd=fd)) == "FAIL"
        finally:
            os.close(fd)
    else:
        assert asyncio.run(runner.run(args)) == "FAIL"
    value = nq.load_json(args.output / "session.json")
    assert value["status"] == "FAIL"
    assert value["captures"] == [] and value["observations"]["owned_display"] is None
    assert not any(value["observations"]["public_bounds"]["calls"].values())
    assert os.environ.get("HOME") == old_home


@pytest.mark.parametrize(
    "kind", ["directory", "file", "symlink", "dangling-symlink", "receipt-symlinks"]
)
def test_runner_existing_output_preserved_without_worker(tmp_path, monkeypatch, kind):
    old = tmp_path / "old"
    old.mkdir()
    save(old / "session.json", {"status": "PASS", "observations": {}})
    save(old / "supervisor.json", {"old": "supervisor"})
    (old / "sentinel").write_bytes(b"preserve\x00sentinel")
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve symlink target")
    (old / "linked-receipt").symlink_to(outside)
    if kind == "receipt-symlinks":
        for name in ("session.json", "supervisor.json"):
            target = tmp_path / ("original-" + name)
            (old / name).rename(target)
            (old / name).symlink_to(target)
    output = tmp_path / "output"
    if kind in {"directory", "receipt-symlinks"}:
        output = old
    elif kind == "file":
        output.write_bytes(b"existing output file")
    else:
        output.symlink_to(old if kind == "symlink" else tmp_path / "missing")
    before = {p.name: p.read_bytes() for p in old.iterdir()}
    output_bytes = output.read_bytes() if kind == "file" else None
    entries = sorted(p.name for p in tmp_path.iterdir())

    def no_spawn(*args, **kwargs):
        pytest.fail("existing output must fail before Popen")

    monkeypatch.setattr(runner.subprocess, "Popen", no_spawn)
    args = Namespace(
        output=output,
        deadline_seconds=30,
        expected=tmp_path / "missing-expected.json",
        count_policy=tmp_path / "missing-policy.json",
        provider="opaque",
        provider_module="unit_provider",
        model="unit-model",
        keyenv="UNIT_CONTROL_ENV",
        xvfb="must-not-start",
        max_complete=2,
        max_budget=2,
        max_estimate=2,
    )
    # Inputs are deliberately absent: rejection precedes worker/input reads.
    with pytest.raises(FileExistsError):
        runner.supervise(args)
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before
    assert outside.read_bytes() == b"preserve symlink target"
    assert (old / "linked-receipt").is_symlink()
    if kind == "receipt-symlinks":
        assert all(
            (old / name).is_symlink() for name in ("session.json", "supervisor.json")
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == entries
    if kind == "file":
        assert output.read_bytes() == output_bytes
    elif kind in {"symlink", "dangling-symlink"}:
        assert output.is_symlink()
        assert output.readlink() == (old if kind == "symlink" else tmp_path / "missing")


@pytest.mark.parametrize("kind", ["different-directory", "symlink", "file"])
def test_runner_worker_rejects_unowned_output(tmp_path, monkeypatch, kind):
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    output = tmp_path / "output"
    if kind == "different-directory":
        output.mkdir()
    elif kind == "symlink":
        output.symlink_to(owned)
    else:
        output.write_bytes(b"not a directory")
        owned = output
    fd = os.open(owned, os.O_RDONLY)
    before = sorted(p.name for p in tmp_path.iterdir())

    def no_spawn(*args, **kwargs):
        pytest.fail("unowned worker output must fail before Popen")

    monkeypatch.setattr(runner.subprocess, "Popen", no_spawn)
    try:
        with pytest.raises(nq.QualificationError, match="inherited directory"):
            asyncio.run(runner.run(Namespace(output=output), output_fd=fd))
    finally:
        os.close(fd)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (output / "session.json").exists()


@pytest.mark.parametrize(
    "value,valid",
    [
        ({"action": "screenshot"}, True),
        (
            {
                "version": 1,
                "actions": [{"action": "screenshot"}],
                "capture_after": True,
            },
            True,
        ),
        ({"action": "key", "text": "a"}, False),
        (
            {
                "version": 1,
                "actions": [{"action": "screenshot"}, {"action": "key", "text": "a"}],
            },
            False,
        ),
        ({"version": True, "actions": [{"action": "screenshot"}]}, False),
    ],
)
def test_runner_restricted_capture_controller(value, valid):
    assert runner.capture_only(value) is valid


def test_runner_mount_metadata_is_not_cleanup():
    assert (
        runner.cleanup_handle(
            {
                "name": "hook-computer-use",
                "provides": ["computer-use-history-projection"],
            }
        )
        is None
    )
    assert runner.cleanup_handle(None) is None

    def close():
        return None

    assert runner.cleanup_handle(close) is close


def test_runner_outer_purity_and_generation_only_projection(control):
    calls = runner.PublicCalls(
        {"complete": 2, "request_budget": 2, "estimate": 2}, time.monotonic() + 10
    )
    image = base64.b64encode((control[2] / "unit-control-0.png").read_bytes()).decode()

    async def complete(request):
        return "unit-control"

    provider = SimpleNamespace(complete=complete, request_budget=lambda request: None)
    calls.wrap(provider)
    provider.request_budget({"image": image})
    assert calls.projected["complete"] == [] and calls.projected["request_budget"]
    inner = provider.complete

    async def mutating_projection(request):
        request["mutation"] = True
        return await inner(copy.deepcopy(request))

    # Mirror the real lazy provider:request lifecycle: production priority 50
    # installs projection before the public observer's priority 60 handler runs.
    observer = runner.outer_purity_handler(calls, provider)
    assert provider.complete is inner
    provider.complete = mutating_projection
    asyncio.run(observer("provider:request", {}))
    with pytest.raises(nq.QualificationError, match="caller request changed"):
        asyncio.run(provider.complete({}))
    assert calls.calls["complete"] == 1
    calls.restore()


@pytest.mark.parametrize("timed_out", [False, True])
def test_runner_supervisor_timeout_is_owned_and_fail_closed(
    tmp_path, monkeypatch, timed_out
):
    events = []

    class Worker:
        pid = 222
        returncode = -9 if timed_out else 0
        waits = 0

        def wait(self, timeout):
            assert 0 <= timeout <= 30
            self.waits += 1
            assert "kill-owned-group" in events
            events.append("reap")
            return self.returncode

    def spawn(command, **kwargs):
        assert "--worker" in command and kwargs["start_new_session"] is True
        output = Path(command[command.index("--output") + 1])
        fd = int(command[command.index("--output-fd") + 1])
        assert kwargs["pass_fds"] == (fd,)
        assert output.is_dir() and list(output.iterdir()) == []
        assert output.stat().st_mode & 0o777 == 0o700
        assert os.path.samestat(os.fstat(fd), output.stat())
        if not timed_out:
            # Synthetic worker result only; not a genuine Session/live receipt.
            save(output / "session.json", {"status": "PASS", "observations": {}})
        events.append("spawn")
        return Worker()

    def kill_group(pid, sig):
        assert pid == 222
        if sig == 0:
            raise ProcessLookupError
        assert sig == runner.signal.SIGKILL
        events.append("kill-owned-group")

    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(runner.os, "killpg", kill_group)
    monkeypatch.setattr(
        runner, "wait_owned_exit", lambda process, deadline: not timed_out
    )
    args = Namespace(
        expected="unit-control",
        provider="opaque",
        provider_module="unit_provider",
        model="unit-model",
        keyenv="UNIT_CONTROL_ENV",
        count_policy="unit-control",
        output=tmp_path / "unit-control-worker",
        xvfb="must-not-start",
        deadline_seconds=30,
        max_complete=2,
        max_budget=2,
        max_estimate=2,
    )
    assert runner.supervise(args) == ("FAIL" if timed_out else "PASS")
    observed = nq.load_json(args.output / "supervisor.json")
    assert observed["timed_out"] is timed_out and observed["group_absent"] is True
    if not timed_out:
        result = nq.load_json(args.output / "session.json")
        assert result["status"] == "PASS"
        assert result["observations"]["supervision"] == observed
    assert events == ["spawn", "kill-owned-group", "reap"]


def test_runner_public_session_init_and_event_order():
    from amplifier_core import AmplifierSession, ModuleLoader
    from amplifier_core.models import HookResult

    async def check():
        session = AmplifierSession(
            {
                "session": {
                    "orchestrator": {"module": "loop-streaming"},
                    "context": {"module": "context-simple"},
                },
                "providers": [],
                "tools": [],
                "hooks": [],
            }
        )
        coordinator = session.coordinator
        coordinator.loader = ModuleLoader(coordinator=coordinator)
        order = []

        async def projection(event, data):
            order.append("projection")
            return HookResult(action="continue")

        async def outer(event, data):
            order.append("outer")
            return HookResult(action="continue")

        try:
            await session.initialize()
            coordinator.hooks.register(
                "provider:request",
                projection,
                priority=50,
                name="unit-control-projection",
            )
            coordinator.hooks.register(
                "provider:request", outer, priority=60, name="unit-control-outer"
            )
            await coordinator.hooks.emit("provider:request", {})
            assert order == ["projection", "outer"]
        finally:
            await session.cleanup()

    asyncio.run(check())


def test_runner_waitid_keeps_leader_allocated(monkeypatch):
    def waitid(kind, pid, flags):
        assert kind == runner.os.P_PID and pid == 222
        assert flags & runner.os.WNOWAIT
        return SimpleNamespace(si_pid=pid)

    monkeypatch.setattr(runner.os, "waitid", waitid)
    assert runner.wait_owned_exit(SimpleNamespace(pid=222), time.monotonic() + 1)


def test_runner_consumer_requirements_docs_agree():
    requirements = (ROOT / "tests" / "requirements.txt").read_text()
    docs = (ROOT / "CONTRIBUTING.md").read_text()
    assert "dd916499344671e0209d4951bc025777e3e891fd" in requirements
    assert "db641363013ff906b656f49498a87ab01f5c506f" in requirements
    for phrase in (
        "qualify_neutral_session.py",
        "neutral_qualification.py",
        "--max-complete",
        "--count-policy",
        "30-day",
        "UNKNOWN",
        "offline-only",
        "WNOWAIT",
    ):
        assert phrase in docs
    assert nq.MAX_AGE_DAYS == 30


def test_runner_help_is_pure():
    with pytest.raises(SystemExit) as exc:
        runner.main(["--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc:
        nq.main(["--help"])
    assert exc.value.code == 0


def test_runner_owned_modules_have_no_sdk_or_private_provider_api():
    import ast

    for file in ("neutral_qualification.py", "qualify_neutral_session.py"):
        tree = ast.parse((ROOT / "scripts" / file).read_text())
        imports = [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        imports += [
            name.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for name in node.names
        ]
        assert not any(
            name and (name.startswith(("anthropic", "openai", "httpx")))
            for name in imports
        )
        assert not any(
            isinstance(node, ast.Attribute)
            and node.attr in {"_client", "_build_request", "_token_count"}
            for node in ast.walk(tree)
        )
