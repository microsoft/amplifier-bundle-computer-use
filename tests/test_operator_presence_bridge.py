"""Compile the maintained bridge C# 5 logic with inert OS/clock boundaries.

No PowerShell, target, SSH, native input or device access. PS5.1 dispatch types
are source-contract checked separately, not claimed as runtime qualification.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = (
    ROOT / "modules/tool-computer-use/amplifier_module_tool_computer_use/bridge.ps1"
)


def test_powershell_override_condition_is_explicit_typed_and_old_peers_refuse_v2():
    source = BRIDGE.read_text()
    assert "$condition.version -notin @(1, 2)" in source
    assert "$condition.operator_presence_override -isnot [bool]" in source
    assert "$condition.operator_presence_override -ne $true" in source
    assert "$condition.operator_presence_channel -isnot [string]" in source
    assert "$presentOperatorFields.Count -ne 4" in source
    assert "$expiry -isnot [decimal]" in source  # Windows PS5.1 JSON floats
    assert (
        "[CU]::ConfigureOperatorPresence($condition.operator_presence_channel, $expiry, $condition.operator_presence_target)"
        in source
    )
    assert 'throw "Operator presence override requires Windows condition v2"' in source
    assert source.index("if ($state.locked") < source.index(
        "[CU]::ConfigureOperatorPresence"
    )
    assert source.index("if ($state.no_gui_session") < source.index(
        "[CU]::ConfigureOperatorPresence"
    )
    # The pre-change bridge accepts ONLY v1: v2 cannot silently select unguarded input.
    assert "version=2" in (BRIDGE.parent / "coexistence_guard.py").read_text()


def test_actual_bridge_csharp5_predicates_with_inert_native_boundaries(tmp_path):
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        pytest.skip(
            "offline C# predicate execution needs dotnet SDK; source checks still run"
        )
    env = {
        **os.environ,
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_CLI_HOME": str(tmp_path / "cli"),
        "NUGET_PACKAGES": str(tmp_path / "nuget"),
        "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
        "DOTNET_ADD_GLOBAL_TOOLS_TO_PATH": "false",
        "HOME": str(tmp_path / "home"),
    }
    sdk = subprocess.run(
        [dotnet, "--version"], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()
    major = int(sdk.split(".")[0])
    source = (
        BRIDGE.read_text()
        .split('Add-Type @"\n', 1)[1]
        .split('\n"@ -ReferencedAssemblies', 1)[0]
    )
    # Replace ONLY OS acquisition and physical emission. Keep actual Configure,
    # ConfigureOperatorPresence, Check, Emit, Release, receipt and count logic.
    source, count = re.subn(
        r'\[DllImport\("kernel32.dll"\)\] public static extern ulong GetTickCount64\(\);',
        "public static ulong FakeTick = 10000; public static ulong GetTickCount64() { return FakeTick; }",
        source,
    )
    assert count == 1
    source, count = re.subn(
        r'\[DllImport\("user32.dll"\)\] public static extern uint SendInput\(uint n, INPUT\[\] inputs, int size\);',
        "public static uint? FakeCount; public static int Sends; public static uint SendInput(uint n, INPUT[] inputs, int size) { Sends++; return FakeCount.HasValue ? FakeCount.Value : n; }",
        source,
    )
    assert count == 1
    start = source.index("    public static Observation Observe() {")
    end = source.index("    public static bool Guarded;", start)
    source = (
        source[:start]
        + """    public static Observation FakeObservation;
    public static Observation Observe() { return FakeObservation; }
"""
        + source[end:]
    )
    source, count = re.subn(
        r"static double UnixSeconds\(\) \{.*?\n    \}",
        "public static double FakeWall = 1000; static double UnixSeconds() { return FakeWall; }",
        source,
        flags=re.S,
    )
    assert count == 1
    harness = r"""
public static class OfflineChecks {
    static int checks;
    const string Channel = "remote-ssh:windows-wsl2:operator@synthetic";
    static void Assert(bool ok) { if (!ok) throw new Exception("offline check failed"); checks++; }
    static void Refuses(Action action) {
        bool refused = false;
        try { action(); } catch (InvalidOperationException) { refused = true; }
        Assert(refused);
    }
    static void Fresh(ulong? injection) {
        CU.FakeWall = 1000; CU.FakeTick = 10100; CU.FakeCount = null;
        CU.Configure("boot/session", 10000, 1000, injection, 20);
        CU.FakeObservation = new CU.Observation { epoch="boot/session", now_ms=10100, last_input_ms=10090, foreground="window-a" };
    }
    public static void Main() {
        Fresh(null); Refuses(CU.Check);  // default initial aggregate change
        Fresh(9990); Refuses(CU.Check); // default >20ms post-inject change
        Fresh(null); CU.ConfigureOperatorPresence(Channel, 1100, "window-a"); CU.Check();
        Assert(CU.Latest.last_input_ms == 10090); // observation not hidden
        CU.Emit(new CU.INPUT[2]); Assert(CU.Injection.count == 2);
        Assert(CU.Injection.before_ms == 10100); // own tick, never foreign 10090
        Fresh(9990); CU.ConfigureOperatorPresence(Channel, 1100, "window-a"); CU.Check();
        CU.FakeObservation.epoch = "other"; Refuses(CU.Check);
        Fresh(9990); CU.ConfigureOperatorPresence(Channel, 1100, "window-a");
        CU.FakeObservation.now_ms = 9999; Refuses(CU.Check);
        Fresh(9990); CU.ConfigureOperatorPresence(Channel, 1100, "window-a");
        CU.FakeObservation.last_input_ms = 999; Refuses(CU.Check);
        Fresh(null); CU.ConfigureOperatorPresence(Channel, 1100, "window-a");
        CU.FakeWall = 1100; int before = CU.Sends;
        Refuses(delegate { CU.Emit(new CU.INPUT[2]); }); Assert(CU.Sends == before);
        CU.Release(new CU.INPUT[2]); Assert(CU.Sends == before + 1); // expiry cannot prevent release
        Fresh(null); CU.ConfigureOperatorPresence(Channel, 1100, "window-a");
        CU.FakeWall = 900; CU.FakeTick = 111100; Refuses(CU.Check); // rollback cannot extend
        Fresh(null); Refuses(delegate { CU.ConfigureOperatorPresence(Channel, 1000, "window-a"); });
        Refuses(delegate { CU.ConfigureOperatorPresence(Channel, 4601, "window-a"); });
        Refuses(delegate { CU.ConfigureOperatorPresence(Channel, Double.NaN, "window-a"); });
        Refuses(delegate { CU.ConfigureOperatorPresence("local", 1100, "window-a"); });
        Refuses(delegate { CU.ConfigureOperatorPresence(Channel, 1100, "0"); });
        Fresh(null); CU.ConfigureOperatorPresence(Channel, 1100, "window-a");
        CU.FakeObservation.foreground = "window-b"; Refuses(CU.Check);
        Fresh(null); CU.ConfigureOperatorPresence(Channel, 1100, "window-a"); CU.FakeCount = 0;
        Refuses(delegate { CU.Emit(new CU.INPUT[2]); }); // count checked despite policy
        Fresh(null); CU.ConfigureOperatorPresence(Channel, 1100, "window-a");
        CU.Configure("boot/session", 10000, 1000, null, 20); Refuses(CU.Check); // no policy leakage
        Refuses(delegate { CU.Configure("", 10000, 1000, null, 20); });
        Refuses(delegate { CU.Configure("boot/session", 10000, 10001, null, 20); });
        Console.WriteLine("CSharp5 inert bridge checks=" + checks);
    }
}
"""
    (tmp_path / "Bridge.cs").write_text(source + "\n" + harness)
    (tmp_path / "Bridge.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
        f"<TargetFramework>net{major}.0</TargetFramework><LangVersion>5</LangVersion>"
        "<EnableNETAnalyzers>false</EnableNETAnalyzers><ImplicitUsings>disable</ImplicitUsings>"
        "<Nullable>disable</Nullable></PropertyGroup></Project>"
    )
    # No packages or package feeds: the installed SDK supplies all references.
    (tmp_path / "NuGet.Config").write_text(
        "<configuration><packageSources><clear /></packageSources></configuration>"
    )
    result = subprocess.run(
        [
            dotnet,
            "run",
            "--project",
            str(tmp_path / "Bridge.csproj"),
            "--configuration",
            "Release",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CSharp5 inert bridge checks=22" in result.stdout
