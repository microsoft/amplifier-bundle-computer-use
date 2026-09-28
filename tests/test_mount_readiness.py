"""Real remote/registry/Core paths with an in-memory wire, never a desktop.

A successful SSH handshake is not proof that display discovery will work.
Operational display failures must leave an actionable unavailable capability;
programming failures still fail Core validation. Every failed builder releases
its own transport reference without disconnecting another consumer.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import uuid

import amplifier_module_tool_computer_use as cu
import pytest
from amplifier_core.testing import MockCoordinator
from amplifier_core.validation.base import ValidationResult
from amplifier_core.validation.tool import ToolValidator
from amplifier_module_tool_computer_use import registry, shared_transport
from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.ssh_transport import SshConnectError
from amplifier_module_tool_computer_use.wire import Response


class Wire:
    def __init__(self, mode="display-error", *, close_fails=False):
        self.mode = mode
        self.close_fails = close_fails
        self.calls = []
        self.closed = 0

    def connect(self, **kwargs):
        self.calls.append("connect")
        if self.mode == "connect-error":
            raise SshConnectError("synthetic handshake failure")
        return {
            "protocol": 1,
            "agent_sha256": "fixture",
            "python": "3.12.0",
            "backend": "macos",
            "platform": "darwin",
            "probe": {"available": True, "reason": ""},
            "permissions": {},
            "capabilities": [],
            "monitors": [],
        }

    def send(self, line, **kwargs):
        req = json.loads(line)
        self.calls.append(req["op"])
        # No captures, input, clipboard, presence reads or disclosure are allowed.
        assert req["op"] in ("list_monitors", "screen_geometry")
        if self.mode == "disconnect":
            raise SshConnectError("synthetic connection lost after handshake")
        if self.mode == "programming-error":
            raise AttributeError("synthetic implementation defect")
        if self.mode == "display-error":
            return Response(
                req["id"],
                False,
                error_type="BackendError",
                error_message="synthetic display unavailable",
            ).encode()
        if req["op"] == "list_monitors":
            # Use the already-supported virtual-desktop fallback.
            return Response(
                req["id"],
                False,
                error_type="BackendError",
                error_message="synthetic monitor enumeration unavailable",
            ).encode()
        return Response(
            req["id"], True, result={"width": 1920, "height": 1080, "x": 0, "y": 0}
        ).encode()

    def close(self):
        self.closed += 1
        if self.close_fails:
            raise OSError("synthetic cleanup failure")


@pytest.fixture
def harness(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real external or desktop action prohibited")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(cu, "_build_announcement", forbidden)
    monkeypatch.setattr(cu, "_build_coexistence_guard", lambda backend, cfg: None)
    key = ("fixture", uuid.uuid4().hex, None)
    wires, handles = [], []

    def build_transport(*args, **kwargs):
        handle = shared_transport.acquire_shared_transport(key, lambda: wires.pop(0))
        handles.append(handle)
        return handle

    monkeypatch.setattr(registry, "_build_ssh_transport", build_transport)
    yield wires, handles, build_transport
    for handle in handles:
        try:
            handle.close()
        except OSError:
            pass


def config():
    return {
        "target": "ssh://fixture.invalid",
        "read_only": False,
        "gate_writes": True,
        "clipboard_read_policy": "redact",
    }


def tools(coordinator):
    return {
        item["name"]: item["module"]
        for item in coordinator.mount_history
        if item["mount_point"] == "tools"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disconnect", "display-error"])
async def test_display_readiness_failure_mounts_only_unavailable_and_releases(
    harness, mode
):
    wires, handles, _ = harness
    wire = Wire(mode)
    wires.append(wire)
    coordinator = MockCoordinator()
    manifest = await cu.mount(coordinator, config())
    assert manifest["provides"] == ["computer_use_unavailable"]
    stub = tools(coordinator)["computer_use_unavailable"]
    assert set(tools(coordinator)) == {"computer_use_unavailable"}
    assert "prepare its display" in stub.description
    assert "retry activation" in stub.description
    assert stub._cfg == config()
    assert handles[0]._released
    assert handles[0]._entry.refcount == 0
    assert wire.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,passes",
    [("disconnect", True), ("display-error", True), ("programming-error", False)],
)
async def test_real_core_validator_classifies_operational_and_programming_failure(
    harness, mode, passes
):
    wires, handles, _ = harness
    wire = Wire(mode)
    wires.append(wire)
    result = ValidationResult(module_type="tool", module_path="fixture")
    await ToolValidator()._check_protocol_compliance(result, cu.mount, config=config())
    assert result.passed is passes, result.summary()
    assert handles[0]._released
    assert handles[0]._entry.refcount == 0
    assert wire.closed == 1


@pytest.mark.asyncio
async def test_failed_activation_retains_stub_then_explicit_retry_gets_new_connection(
    harness,
):
    wires, handles, _ = harness
    wire = Wire()
    wires.append(wire)
    coordinator = MockCoordinator()
    await cu.mount(coordinator, config())
    stub = tools(coordinator)["computer_use_unavailable"]
    failed = Wire()
    wires.append(failed)
    result = await stub._activate("ssh://fixture.invalid")
    assert not result.success
    assert result.error["type"] == "BackendNotReady"
    assert set(tools(coordinator)) == {"computer_use_unavailable"}
    assert stub._cfg == config()
    assert all(h._released for h in handles)

    healthy = Wire("healthy")
    wires.append(healthy)
    result = await stub._activate("ssh://fixture.invalid")
    assert result.success
    mounted = tools(coordinator)
    computer = mounted["computer"]
    assert computer._read_only is False
    assert computer._gate_writes is True
    assert computer._clipboard_read_policy == "redact"
    assert computer._announced is False  # first real use still requires disclosure
    assert computer._backend.is_remote
    assert healthy.calls.count("connect") == 1
    assert wire.closed == failed.closed == 1
    assert handles[-1]._entry is not handles[0]._entry
    assert not handles[-1]._released


def test_failed_build_releases_only_its_reference(harness):
    wires, handles, acquire = harness
    wire = Wire()
    wires.append(wire)
    sibling = acquire()
    sibling.connect()
    with pytest.raises(cu._BackendNotReady):
        cu._select_and_build(config())
    assert handles[-1]._released
    assert not sibling._released
    assert sibling._entry.refcount == 1
    assert wire.closed == 0
    sibling.close()
    assert wire.closed == 1


@pytest.mark.parametrize(
    "mode,exception",
    [("display-error", cu._BackendNotReady), ("programming-error", AttributeError)],
)
def test_cleanup_failure_preserves_original_build_error(harness, mode, exception):
    wires, handles, _ = harness
    wire = Wire(mode, close_fails=True)
    wires.append(wire)
    with pytest.raises(exception) as caught:
        cu._select_and_build(config())
    assert "synthetic cleanup failure" not in str(caught.value)
    if mode == "display-error":
        assert isinstance(caught.value.__cause__, BackendError)
        assert "synthetic display unavailable" in str(caught.value.__cause__)
    assert handles[0]._released
    assert wire.closed == 1


@pytest.mark.asyncio
async def test_failed_connect_also_releases_ownership_and_preserves_diagnostic(harness):
    wires, handles, _ = harness
    wire = Wire("connect-error", close_fails=True)
    wires.append(wire)
    coordinator = MockCoordinator()
    manifest = await cu.mount(coordinator, config())
    assert manifest["provides"] == ["computer_use_unavailable"]
    assert (
        "synthetic handshake failure"
        in tools(coordinator)["computer_use_unavailable"].description
    )
    assert handles[0]._released
    assert handles[0]._entry.refcount == 0
    assert wire.calls == ["connect"]
    assert wire.closed == 1


def test_guard_programming_error_preserved_after_successful_display(
    harness, monkeypatch
):
    wires, handles, _ = harness
    wire = Wire("healthy")
    wires.append(wire)

    def broken_guard(*args):
        raise AttributeError("synthetic guard defect")

    monkeypatch.setattr(cu, "_build_coexistence_guard", broken_guard)
    with pytest.raises(AttributeError, match="synthetic guard defect"):
        cu._select_and_build(config())
    assert handles[0]._released
    assert wire.closed == 1


@pytest.mark.asyncio
async def test_failed_retarget_preserves_current_binding_and_releases_candidate(
    harness,
):
    wires, handles, _ = harness
    healthy = Wire("healthy")
    wires.append(healthy)
    current = cu._select_and_build(config())
    old_binding = current._binding
    # Different key/target to exercise candidate initialization, not same-target no-op.
    failed = Wire()
    wires.append(failed)
    failed_key = ("fixture", uuid.uuid4().hex, None)
    from unittest.mock import patch

    def acquire(*args, **kwargs):
        handle = shared_transport.acquire_shared_transport(
            failed_key, lambda: wires.pop(0)
        )
        handles.append(handle)
        return handle

    with patch.object(registry, "_build_ssh_transport", acquire):
        result = await asyncio.to_thread(current.retarget, "ssh://different.invalid")
    assert not result.success
    assert current._binding is old_binding
    assert current._cfg == config()
    assert handles[-1]._released
    assert failed.closed == 1
    assert healthy.closed == 0


@pytest.mark.asyncio
async def test_local_display_readiness_has_same_failure_contract(harness, monkeypatch):
    class LocalBackend:
        name = "fixture-local"
        is_remote = False
        closed = 0

        def type_text(self, text):
            raise AssertionError("Input prohibited")

        def list_monitors(self):
            raise BackendError("synthetic no active desktop")

        def screen_geometry(self):
            raise BackendError("synthetic no active desktop")

        def close(self):
            self.closed += 1

    backend = LocalBackend()
    monkeypatch.setattr(cu, "select_backend", lambda cfg: backend)
    coordinator = MockCoordinator()
    manifest = await cu.mount(coordinator, {})
    assert manifest["provides"] == ["computer_use_unavailable"]
    assert (
        "synthetic no active desktop"
        in tools(coordinator)["computer_use_unavailable"].description
    )
    assert backend.closed == 1
