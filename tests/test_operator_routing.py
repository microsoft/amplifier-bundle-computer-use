"""Regression checks for conservative desktop-operator routing."""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _agent_parts():
    agent = REPO / "agents/computer-operator.md"
    _, frontmatter, body = agent.read_text().split("---", 2)
    return yaml.safe_load(frontmatter), body


def _normalized(text):
    return " ".join(text.lower().split())


def _assert_keyword_only_routing_is_rejected(text):
    assert re.search(
        r"merely because.*\bopen\b.*\bcheck\b.*\bnavigate\b",
        _normalized(text),
    )


def test_operator_uses_general_not_vision():
    frontmatter, _ = _agent_parts()
    assert frontmatter["model_role"] == "general"


def test_operator_declares_the_behavior_mounted_tool_and_hook_without_config():
    frontmatter, _ = _agent_parts()

    expected = {
        "tools": (
            "tool-computer-use",
            "git+https://github.com/microsoft/amplifier-bundle-computer-use.git@main#subdirectory=modules/tool-computer-use",
        ),
        "hooks": (
            "hook-computer-use",
            "git+https://github.com/microsoft/amplifier-bundle-computer-use.git@main#subdirectory=modules/hook-computer-use",
        ),
    }
    for section, (module, source) in expected.items():
        assert frontmatter[section] == [{"module": module, "source": source}]


def test_operator_catalog_has_a_compact_strict_routing_gate():
    frontmatter, _ = _agent_parts()
    description = frontmatter["meta"]["description"]
    normalized = _normalized(description)

    assert len(description) <= 600
    assert "use only when the user explicitly requests live desktop gui" in normalized
    assert "or screen interaction" in normalized
    assert "do not use" in normalized
    assert (
        "required state or action is gui-only and no suitable structured path"
        in normalized
    )
    assert "specific tool can complete the required work" in normalized
    assert "<example>" not in normalized
    assert "<commentary>" not in normalized
    assert "proactively" not in normalized
    assert "use it whenever" not in normalized


def test_operator_body_uses_explicit_routing_and_retargeting():
    _, body = _agent_parts()
    normalized = _normalized(body)

    assert "## routing gate" in normalized
    assert "actual rendered desktop state or interaction is required" in normalized
    assert (
        "explicit request to view or operate the actual desktop is sufficient"
        in normalized
    )
    assert (
        "only for a gui-only required state or action when no suitable browser, mobile, cli, api, or code/file workflow can complete it"
        in normalized
    )
    assert "structured method" in normalized
    _assert_keyword_only_routing_is_rejected(body)
    assert 'desktop(action="retarget")' in body
    assert "cannot retarget mid-session" not in normalized


def _assert_strict_escalation_gate(text):
    normalized = _normalized(text)

    assert (
        "only when the user explicitly requests actual rendered desktop gui"
        in normalized
    )
    assert (
        "required state or action is gui-only and no suitable structured path"
        in normalized
    )
    assert (
        "prefer a browser, mobile, cli, api, or code/file workflow when it can complete the task"
        in normalized
    )
    assert "speculatively" in normalized
    _assert_keyword_only_routing_is_rejected(text)
    assert "use it whenever" not in normalized
    assert "proactively" not in normalized


def test_awareness_uses_strict_escalation_not_keyword_routing():
    awareness = (REPO / "context/computer-use-awareness.md").read_text()
    _assert_strict_escalation_gate(awareness)


def test_bundle_documents_strict_escalation_not_keyword_routing():
    bundle = (REPO / "bundle.md").read_text()
    _assert_strict_escalation_gate(bundle)
