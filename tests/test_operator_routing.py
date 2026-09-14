"""The desktop operator must not request the generic image-analysis role."""

from pathlib import Path

import yaml


def test_operator_uses_general_not_vision():
    agent = Path(__file__).resolve().parents[1] / "agents/computer-operator.md"
    frontmatter = yaml.safe_load(agent.read_text().split("---", 2)[1])
    assert frontmatter["model_role"] == "general"
