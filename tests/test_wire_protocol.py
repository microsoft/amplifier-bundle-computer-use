"""Unit tests for the NDJSON wire protocol (`wire.py`) - framing, op
classification (the retry policy), and handshake validation. Pure logic, no
sockets, no subprocess, runs anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use.wire import (
    PROTOCOL_VERSION,
    AgentBackendUnavailableError,
    ProtocolMismatchError,
    Request,
    Response,
    UnknownOpError,
    classify_op,
    is_retryable,
    validate_handshake,
)


def test_request_encode_is_one_ndjson_line():
    req = Request(id=17, op="click", args={"x": 1024, "y": 768})
    encoded = req.encode()
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert b'"id": 17' in encoded or b'"id":17' in encoded


def test_response_round_trips_success():
    resp = Response(id=5, ok=True, result={"width": 100})
    decoded = Response.decode(resp.encode())
    assert decoded.ok is True
    assert decoded.id == 5
    assert decoded.result == {"width": 100}


def test_response_round_trips_error():
    resp = Response(id=6, ok=False, error_type="BackendError", error_message="boom")
    decoded = Response.decode(resp.encode())
    assert decoded.ok is False
    assert decoded.error_type == "BackendError"
    assert decoded.error_message == "boom"


def test_response_decode_rejects_malformed_line():
    with pytest.raises(ValueError):
        Response.decode(b'{"nope": true}\n')


# -- op classification (the retry policy) ------------------------------------


@pytest.mark.parametrize(
    "op",
    ["probe", "screen_geometry", "list_monitors", "capture_scaled", "cursor_position"],
)
def test_read_ops_are_retryable(op: str):
    assert classify_op(op) == "read"
    assert is_retryable(op) is True


@pytest.mark.parametrize(
    "op",
    ["move", "click", "key", "type_text", "hold", "focus_window", "set_clipboard"],
)
def test_write_ops_are_never_retryable(op: str):
    """The single most load-bearing assertion in this module: a lost response
    to a WRITE must never be replayed - see docs/designs/remote-transport.md
    \u00a76.3."""
    assert classify_op(op) == "write"
    assert is_retryable(op) is False


@pytest.mark.parametrize("op", ["hello", "release_all", "bye", "announce_raise"])
def test_control_ops_are_retryable(op: str):
    assert classify_op(op) == "control"
    assert is_retryable(op) is True


def test_unknown_op_raises_rather_than_guessing_retry_safety():
    with pytest.raises(UnknownOpError):
        classify_op("delete_everything")


# -- coexistence announcement ops (docs/designs/coexistence.md \u00a710.3) --------


def test_announcement_status_is_a_retryable_read():
    """A query, no side effect - same idempotency argument as `presence_idle`."""
    assert classify_op("announcement_status") == "read"
    assert is_retryable("announcement_status") is True


# -- handshake validation -----------------------------------------------------


def _good_handshake(**overrides):
    base = {
        "protocol": PROTOCOL_VERSION,
        "agent_sha256": "abc123",
        "python": "3.14.5",
        "platform": "darwin",
        "backend": "macos",
        "probe": {"available": True, "reason": ""},
        "capabilities": ["capture_scaled"],
        "permissions": {"accessibility": True, "screen_recording": True},
        "monitors": [],
    }
    base.update(overrides)
    return base


def test_validate_handshake_accepts_a_good_handshake():
    validate_handshake(
        _good_handshake(),
        expected_sha256="abc123",
        required_permissions=("accessibility", "screen_recording"),
    )  # must not raise


def test_validate_handshake_rejects_wrong_protocol_version():
    with pytest.raises(ProtocolMismatchError, match="protocol"):
        validate_handshake(_good_handshake(protocol=999), expected_sha256="abc123")


def test_validate_handshake_rejects_sha_mismatch():
    """This is the deliberately-corrupted-payload case from the Phase 1
    acceptance gate (item 5) - a mismatch must fail loud, not proceed."""
    with pytest.raises(ProtocolMismatchError, match="sha256"):
        validate_handshake(
            _good_handshake(agent_sha256="deadbeef"), expected_sha256="abc123"
        )


def test_validate_handshake_rejects_unavailable_probe():
    with pytest.raises(AgentBackendUnavailableError, match="unavailable"):
        validate_handshake(
            _good_handshake(probe={"available": False, "reason": "no display"}),
            expected_sha256="abc123",
        )


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"protocol": 999}, "protocol"),
        ({"agent_sha256": "wrong"}, "sha256"),
        ({"permissions": {"accessibility": False}}, "accessibility"),
    ],
)
def test_unavailable_probe_cannot_mask_hard_requirements(overrides, reason):
    handshake = _good_handshake(
        probe={"available": False, "reason": "no active display"}, **overrides
    )
    with pytest.raises(ProtocolMismatchError, match=reason) as caught:
        validate_handshake(
            handshake,
            expected_sha256="abc123",
            required_permissions=("accessibility",),
        )
    assert not isinstance(caught.value, AgentBackendUnavailableError)


@pytest.mark.parametrize(
    "probe", [None, {}, [], {"available": 0}, {"available": "false"}]
)
def test_malformed_probe_is_not_operational_unavailability(probe):
    with pytest.raises(ProtocolMismatchError, match="boolean") as caught:
        validate_handshake(_good_handshake(probe=probe), expected_sha256="abc123")
    assert not isinstance(caught.value, AgentBackendUnavailableError)


def test_validate_handshake_rejects_missing_required_permission():
    with pytest.raises(ProtocolMismatchError, match="accessibility"):
        validate_handshake(
            _good_handshake(
                permissions={"accessibility": False, "screen_recording": True}
            ),
            expected_sha256="abc123",
            required_permissions=("accessibility",),
        )


def test_validate_handshake_does_not_require_unrequested_permissions():
    # No required_permissions given -> a False accessibility flag is not fatal.
    validate_handshake(
        _good_handshake(permissions={"accessibility": False}),
        expected_sha256="abc123",
    )
