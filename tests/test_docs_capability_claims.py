"""Guards against exactly the drift `docs/designs/capability-awareness.md`
found in the wild: `README.md` and `docs/SETUP.md` claimed nine actions
raise `BackendError("... is Phase 2")` over the remote wire, for 27 commits
after `94b667e` made every one of them a real dispatch-table entry.

`RemoteAgent._HANDLERS` (`remote_agent.py:760`) is the actual runtime
routing table - it cannot silently go stale, an unwired handler fails at
import time the moment something calls it. Prose describing that table
can and did drift, in the direction that makes an agent *understate* its
own capability to a user. This test ties the prose to the table
mechanically so that class of drift fails loud instead of only being
caught by someone reading a diff.

Setup recovery also drifted after the diagnostic stub shipped. Fake mount
failures below tie the target table and recovery calls to the current tool API.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_core.testing import MockCoordinator
from amplifier_module_tool_computer_use import (
    ComputerUseUnavailableTool,
    DesktopTool,
)
from amplifier_module_tool_computer_use.remote_agent import RemoteAgent
from amplifier_module_tool_computer_use.remote_backend import RemoteTargetUnavailable

# The doc-facing action name (what README.md/SETUP.md call it) mapped to the
# wire-level op name `RemoteAgent._HANDLERS` actually dispatches on. This
# mapping is a naming-convention translation (tool action vs wire op), not
# something derivable from code, so it is maintained here explicitly - it is
# exactly the same set `94b667e`'s commit message names as "the nine ops".
DOC_ACTION_TO_WIRE_OP = {
    "left_mouse_down": "mouse_down",
    "left_mouse_up": "mouse_up",
    "left_click_drag": "drag",
    "scroll": "scroll",
    "hold_key": "hold_key",
    "list_windows": "list_windows",
    "focus_window": "focus_window",
    "get_clipboard": "get_clipboard",
    "set_clipboard": "set_clipboard",
}

DOCS = {
    "README.md": ROOT / "README.md",
    "docs/SETUP.md": ROOT / "docs" / "SETUP.md",
}

BEHAVIOR_FILE = ROOT / "behaviors" / "computer-use.yaml"
BEHAVIOR_INSTALL_FRAGMENT = "#subdirectory=behaviors/computer-use.yaml"

# Matches the family of stale phrasings this repo has actually used:
# "is Phase 2", "Phase 2 — not implemented", "unimplemented", etc.
STALE_CLAIM = re.compile(r"not implemented|unimplemented|phase[\s-]*2", re.IGNORECASE)

# Strip Markdown emphasis before checking the known stale mount-failure claims.
# These contradict the mounted diagnostic stub, not a real desktop capability.
STALE_MOUNT_CLAIM = re.compile(
    r"nothing is mounted|mounts nothing|nothing mounts|no tools mount"
    r"|mount does not catch",
    re.IGNORECASE,
)


def test_setup_does_not_claim_backend_failure_leaves_nothing_mounted():
    text = DOCS["docs/SETUP.md"].read_text(encoding="utf-8").replace("*", "")
    assert not STALE_MOUNT_CLAIM.search(text), (
        "Backend failures mount computer_use_unavailable; setup must not describe "
        "the old silent absence or uncaught RemoteTargetUnavailable path."
    )


@pytest.mark.parametrize(
    "stale",
    [
        "If none is available, nothing is mounted",
        "Mount catches NoBackendAvailable, logs the reason, mounts nothing",
        "An unreachable target raises RemoteTargetUnavailable, which mount does not catch",
    ],
)
def test_stale_mount_detector_matches_the_original_setup_claims(stale):
    assert STALE_MOUNT_CLAIM.search(stale)


@pytest.mark.parametrize(
    ("target", "error", "row_prefix"),
    [
        (None, "local", "| absent |"),
        ("ssh://user@host", "remote", "| `ssh://user@host`"),
        ("user@host", "invalid", "| anything else"),
    ],
)
def test_setup_target_failure_rows_match_the_mounted_tool(
    monkeypatch, target, error, row_prefix
):
    """Exercise the actual mount handler with fake failures, never a desktop/SSH."""
    import amplifier_module_tool_computer_use as cu

    errors = {
        "local": cu.NoBackendAvailable([("fake-local", "no display")]),
        "remote": RemoteTargetUnavailable("fake target unreachable"),
        "invalid": ValueError("invalid target"),
    }

    def fail_selection(cfg):
        raise errors[error]

    monkeypatch.setattr(cu, "select_backend", fail_selection)
    coordinator = MockCoordinator()
    # An owned loop avoids changing the thread's current event loop, which
    # older synchronous tests in this suite still use via get_event_loop().
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        manifest = runner.run(
            cu.mount(coordinator, {"target": target} if target else {})
        )
    stub = coordinator.mount_history[0]["module"]
    assert isinstance(stub, ComputerUseUnavailableTool)
    assert manifest["provides"] == [stub.name]

    text = DOCS["docs/SETUP.md"].read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith(row_prefix))
    assert stub.name in row, f"Setup target row must name the actual tool: {row}"


def test_setup_recovery_calls_match_advertised_tool_actions():
    """Check the recovery recipe's API names, not its exact prose."""
    text = DOCS["docs/SETUP.md"].read_text(encoding="utf-8")
    recipe = text.split("### Recover or change the target without restarting", 1)[1]
    recipe = recipe.split("\n---", 1)[0]
    schemas = {
        "computer_use_unavailable": ComputerUseUnavailableTool("test").input_schema,
        "desktop": DesktopTool(None).input_schema,
    }
    calls = re.findall(
        r'(computer_use_unavailable|desktop)\(action="([a-z_]+)"', recipe
    )
    assert calls, "Setup must give a callable recovery recipe"
    for tool, action in calls:
        assert action in schemas[tool]["properties"]["action"]["enum"]
    assert {action for tool, action in calls if tool == "computer_use_unavailable"} == {
        "discover",
        "persist",
        "activate",
    }
    assert ("desktop", "retarget") in calls
    assert ("desktop", "doctor") in calls


def test_behavior_install_commands_use_the_behavior_registered_name():
    """The install URI and follow-up commands must name the same bundle.

    The documented URI targets the behavior manifest rather than the root bundle,
    so CLI lookup must use that manifest's declared name.
    """
    behavior_text = BEHAVIOR_FILE.read_text(encoding="utf-8")
    match = re.search(r"^\s+name:\s+([^\s#]+)", behavior_text, re.MULTILINE)
    assert match, "behavior manifest has no bundle name"
    behavior_name = match.group(1)

    for doc_name, path in DOCS.items():
        text = path.read_text(encoding="utf-8")
        assert BEHAVIOR_INSTALL_FRAGMENT in text, (
            f"{doc_name} must install the behavior"
        )
        assert f"--bundle {behavior_name}" in text, f"{doc_name} runs the wrong bundle"
        assert f"bundle show {behavior_name}" in text, (
            f"{doc_name} shows the wrong bundle"
        )
        assert '--bundle computer-use"' not in text, f"{doc_name} uses the root name"


def test_docs_do_not_claim_a_dispatched_op_is_unimplemented_remotely():
    """Fails if a doc line names one of the nine actions alongside stale
    "unimplemented"/"Phase 2" phrasing while `_HANDLERS` actually dispatches
    it - i.e. fails exactly when the docs and the dispatch table disagree.

    Genuinely-missing ops are not flagged: the check only fires for an
    action whose wire op IS present in `_HANDLERS`, so a real future gap
    (op removed from the table, or a new op that really isn't wired up
    yet) does not trip this test - only a doc claiming otherwise-working
    code doesn't work does.
    """
    handlers = RemoteAgent._HANDLERS

    violations: list[str] = []
    for doc_name, path in DOCS.items():
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not STALE_CLAIM.search(line):
                continue
            for doc_action, wire_op in DOC_ACTION_TO_WIRE_OP.items():
                if doc_action in line and wire_op in handlers:
                    violations.append(
                        f"{doc_name}: {line.strip()!r} claims {doc_action!r} is "
                        f"unimplemented, but wire op {wire_op!r} is dispatched "
                        f"by RemoteAgent._HANDLERS (remote_agent.py:760) - the "
                        f"docs are stale, not the code."
                    )

    assert not violations, "\n".join(violations)


def test_self_test_stale_claim_regex_actually_matches_known_stale_wording():
    """The instrument that caught the drift must be proven to fire on the
    exact wording it is supposed to catch - a regex that silently fails to
    match is indistinguishable from "no drift found". This reproduces the
    real stale sentence that shipped in this repo for 27 commits and
    asserts the detector above would have caught it.
    """
    stale_readme_line = '> `desktop.set_clipboard` all raise `BackendError("... over the wire is Phase 2")`.'
    assert STALE_CLAIM.search(stale_readme_line), (
        "detector regex does not match the real historical stale wording - "
        "it would have silently passed on the actual defect"
    )
    assert "set_clipboard" in stale_readme_line
    assert "set_clipboard" in DOC_ACTION_TO_WIRE_OP
    assert DOC_ACTION_TO_WIRE_OP["set_clipboard"] in RemoteAgent._HANDLERS
