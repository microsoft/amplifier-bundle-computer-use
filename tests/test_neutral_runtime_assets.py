"""Runtime asset identity controls; no PowerShell or desktop actions execute."""

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import neutral_qualification as nq


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    module = tmp_path / "modules/tool-computer-use"
    package = module / "unit_tool"
    package.mkdir(parents=True)
    (module / "pyproject.toml").write_text('[project]\nname = "unit-tool"\n')
    (package / "__init__.py").write_text("# identity fixture\n")
    (package / "bridge.ps1").write_text("# original bridge\n")
    (package / "overlay_windows.ps1").write_text("# original overlay\n")
    (package / "view.dat").write_bytes(b"runtime data")
    monkeypatch.setattr(
        nq.importlib,
        "import_module",
        lambda name: SimpleNamespace(__file__=str(package / "__init__.py")),
    )
    monkeypatch.setattr(
        nq.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="1", read_text=lambda name: None),
    )
    # Exercise the installed-root check with this temporary installed tree.
    monkeypatch.setattr(nq.sysconfig, "get_path", lambda key: str(tmp_path))
    return tmp_path, module, package


def identity():
    return nq.package_identity("unit-tool", "unit_tool")


@pytest.mark.parametrize("name", ["bridge.ps1", "overlay_windows.ps1", "view.dat"])
def test_runtime_asset_only_change_changes_source_and_installed_identity(runtime, name):
    root, _, package = runtime
    source, installed = nq.production_manifest(root), identity()
    assert installed["manifest"][name] == nq.sha256(package / name)
    (package / name).write_bytes(b"changed runtime bytes")
    assert nq.production_manifest(root) != source
    assert identity() != installed


def test_package_configuration_is_bound_and_generated_bytecode_is_excluded(runtime):
    root, module, package = runtime
    before, installed = nq.production_manifest(root), identity()
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-313.pyc").write_bytes(b"cache")
    (package / "legacy.pyc").write_bytes(b"cache")
    assert nq.production_manifest(root) == before
    assert identity() == installed
    (module / "pyproject.toml").write_text('[project]\nname = "changed-tool"\n')
    assert nq.production_manifest(root) != before


def test_consumer_rejects_installed_bridge_that_differs_from_bound_source(runtime):
    root, _, package = runtime
    source, installed = nq.production_manifest(root), identity()
    hook = copy.deepcopy(installed)
    hook["module"] = "unit_hook"
    source.update(
        {
            f"modules/hook-computer-use/unit_hook/{p}": h
            for p, h in hook["manifest"].items()
        }
    )
    binding = {
        "provider": "unit",
        "profile": "current",
        "mode": "offline",
        "source": source,
        "identities": {role: copy.deepcopy(installed) for role in nq.IDENTITY_ROLES},
    }
    binding["identities"]["hook"] = hook
    nq.binding_check(binding)
    (package / "bridge.ps1").write_text("# changed installed bridge\n")
    binding["identities"]["tool"] = identity()
    with pytest.raises(nq.QualificationError, match="production code not bound"):
        nq.binding_check(binding)


def test_checkout_binds_both_windows_scripts_and_packaging_configuration():
    manifest = nq.production_manifest(ROOT)
    prefix = "modules/tool-computer-use/"
    for name in ("bridge.ps1", "overlay_windows.ps1"):
        path = prefix + "amplifier_module_tool_computer_use/" + name
        assert manifest[path] == nq.sha256(ROOT / path)
    assert prefix + "pyproject.toml" in manifest
