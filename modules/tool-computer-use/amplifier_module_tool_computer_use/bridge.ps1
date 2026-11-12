#requires -Version 5.1
<#
  Amplifier computer-use : WSL2 -> Windows desktop bridge.

  Usage:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File bridge.ps1 -RequestFile <path.json>
  Input :  JSON object { "action": "...", ... }  (Anthropic computer-tool action shape)
  Output:  a single line of JSON on stdout: {"ok":true,...} or {"ok":false,"error":"..."}

  All coordinates are PHYSICAL screen pixels of the virtual desktop.
#>
param([Parameter(Mandatory = $true)][string]$RequestFile)

$ErrorActionPreference = 'Stop'
# PowerShell defaults to the console's OEM codepage; window titles then arrive as
# undecodable bytes on the WSL side. Force UTF-8 on the way out.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class CU {
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, int data, IntPtr extra);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, IntPtr extra);
    [DllImport("user32.dll")] public static extern short VkKeyScan(char ch);
    [DllImport("user32.dll")] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int  GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int  GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    // Coexistence presence detector (docs/designs/coexistence.md \u00a75.5): GetLastInputInfo's
    // dwTime and GetTickCount share the same coarse (~10-16ms) system clock, so both must be
    // read via this same P/Invoke pair - never mixed with a .NET/managed clock - for the
    // reconciliation math (idle = GetTickCount() - dwTime) to be meaningful.
    [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [DllImport("kernel32.dll")] public static extern uint GetTickCount();
    [DllImport("kernel32.dll")] public static extern ulong GetTickCount64();
    [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(int infoClass, IntPtr info, int size, out int returned);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool GetTokenInformation(IntPtr token, int infoClass, IntPtr info, int size, out int returned);

    public class Observation {
        public int version = 1;
        public string epoch;
        public ulong now_ms;
        public ulong last_input_ms;
        public string foreground;
    }
    public class Emission {
        public string epoch;
        public ulong before_ms;
        public ulong after_ms;
        public uint count;
    }
    // All of this state is confined to ONE bridge process/request, never shared
    // between calls. Boot GUID + authentication LUID + session identifies the
    // clock/input stream without estimating boot time from a wall clock.
    static string epoch;
    static string GetEpoch() {
        if (epoch != null) return epoch;
        IntPtr boot = Marshal.AllocHGlobal(32);
        IntPtr stats = Marshal.AllocHGlobal(128);
        IntPtr token = IntPtr.Zero;
        try {
            int returned;
            if (NtQuerySystemInformation(90, boot, 32, out returned) != 0 || returned < 16)
                throw new InvalidOperationException("Windows boot GUID unavailable");
            byte[] guid = new byte[16]; Marshal.Copy(boot, guid, 0, 16);
            if (new Guid(guid) == Guid.Empty)
                throw new InvalidOperationException("Windows boot GUID was empty");
            if (!OpenProcessToken(GetCurrentProcess(), 8, out token) ||
                !GetTokenInformation(token, 10, stats, 128, out returned) || returned < 16)
                throw new InvalidOperationException("Windows logon authentication ID unavailable");
            string identity = new Guid(guid).ToString("D") + ":" +
                System.Diagnostics.Process.GetCurrentProcess().SessionId + ":" +
                Marshal.ReadInt32(stats, 8) + ":" + Marshal.ReadInt32(stats, 12);
            using (var sha = System.Security.Cryptography.SHA256.Create())
                epoch = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(identity))).Replace("-", "").ToLowerInvariant();
            return epoch;
        } finally {
            if (token != IntPtr.Zero) CloseHandle(token);
            Marshal.FreeHGlobal(boot); Marshal.FreeHGlobal(stats);
        }
    }
    public static Observation Observe() {
        string currentEpoch = GetEpoch();
        LASTINPUTINFO lii = new LASTINPUTINFO();
        lii.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
        if (!GetLastInputInfo(ref lii)) throw new InvalidOperationException("GetLastInputInfo failed");
        ulong now = GetTickCount64();
        ulong last = (now & 0xffffffff00000000UL) | lii.dwTime;
        if (last > now) {
            if (last < 0x100000000UL) throw new InvalidOperationException("Future Windows last-input tick");
            last -= 0x100000000UL;
        }
        return new Observation { epoch = currentEpoch, now_ms = now, last_input_ms = last,
            foreground = GetForegroundWindow().ToString() };
    }
    public static bool Guarded;
    public static Observation Latest;
    public static Emission Injection;
    public static bool EmissionAttempted;
    static string expectedEpoch;
    static ulong baselineNow, baselineLast;
    static ulong? injectBefore;
    static double guardMs;
    static bool operatorPresenceOverride;
    static double operatorExpiry, operatorDeadline;
    static string operatorTarget;
    static double UnixSeconds() {
        return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
    }
    public static void ConfigureOperatorPresence(string channel, double expiry, string target) {
        double remaining = expiry - UnixSeconds();
        const string prefix = "remote-ssh:windows-wsl2:";
        if (!Guarded || String.IsNullOrEmpty(channel) || !channel.StartsWith(prefix, StringComparison.Ordinal) ||
            channel.Length <= prefix.Length || channel.Length > prefix.Length + 256 ||
            String.IsNullOrEmpty(target) || target == "0" || target.Length > 256 ||
            Double.IsNaN(expiry) || Double.IsInfinity(expiry) || remaining <= 0 || remaining > 3600)
            throw new InvalidOperationException("Malformed or expired operator presence condition");
        foreach (char ch in channel)
            if (Char.IsControl(ch)) throw new InvalidOperationException("Malformed operator channel");
        operatorExpiry = expiry;
        operatorTarget = target;
        operatorDeadline = (double)GetTickCount64() + remaining * 1000;
        operatorPresenceOverride = true;
    }
    static IntPtr inputMarker;
    public static void ConfigureMarker(long marker) {
        if (marker <= 0 || (IntPtr.Size == 4 && marker > Int32.MaxValue))
            throw new InvalidOperationException("Malformed private input marker");
        inputMarker = new IntPtr(marker);
    }
    static void Stamp(INPUT[] inputs) {
        // Absent marker preserves one-shot CLI callers. This is neither a
        // permission nor evidence that an injected event is physically ours.
        if (inputMarker == IntPtr.Zero) return;
        for (int i = 0; i < inputs.Length; i++) {
            if (inputs[i].type == 1) inputs[i].ki.dwExtraInfo = inputMarker;
            else if (inputs[i].type == 0) inputs[i].mi.dwExtraInfo = inputMarker;
        }
    }
    public static void Configure(string expected, ulong now, ulong last, ulong? injection, double guard) {
        if (String.IsNullOrEmpty(expected) || last > now ||
            now - last >= 0x100000000UL || (injection.HasValue && injection.Value > now) ||
            Double.IsNaN(guard) || Double.IsInfinity(guard) || guard < 0)
            throw new InvalidOperationException("Malformed Windows target-clock condition");
        expectedEpoch = expected; baselineNow = now; baselineLast = last;
        injectBefore = injection; guardMs = guard; Guarded = true;
        operatorPresenceOverride = false;
    }
    public static void Check() {
        if (!Guarded) return;
        Latest = Observe();
        if (Latest.epoch != expectedEpoch || Latest.now_ms < baselineNow ||
            Latest.last_input_ms < baselineLast)
            throw new InvalidOperationException("Windows target-clock epoch/ticks changed");
        if (operatorPresenceOverride &&
            (UnixSeconds() >= operatorExpiry || (double)GetTickCount64() >= operatorDeadline))
            throw new InvalidOperationException("Operator presence condition expired");
        if (operatorPresenceOverride && Latest.foreground != operatorTarget)
            throw new InvalidOperationException("Operator presence target changed");
        // Only this aggregate last-input predicate is overridden. Observation,
        // epoch/regression, checked SendInput count and receipts remain mandatory.
        if (!operatorPresenceOverride && injectBefore.HasValue) {
            if ((double)Latest.last_input_ms - (double)injectBefore.Value > guardMs)
                throw new InvalidOperationException("Windows target-clock input precondition rejected");
        } else if (!operatorPresenceOverride && Latest.last_input_ms != baselineLast) {
            throw new InvalidOperationException("Windows initial input precondition rejected");
        }
    }
    public static void Emit(INPUT[] inputs) {
        EmissionAttempted = false;
        Stamp(inputs);
        Check();
        // No sleep, PowerShell startup or transport between this check and
        // SendInput. This narrows the race; it is NOT an atomic check+syscall.
        ulong before = GetTickCount64();
        if (Guarded) {
            // INPUT.time defaults to the time Windows processes an event, not
            // necessarily this call's start. A delayed SendInput can therefore
            // put our own event outside the unchanged presence guard. Stamp
            // the documented DWORD event time from the SAME target clock as
            // the receipt. Do not credit after/LastInputInfo or widen the band.
            // At DWORD zero Windows retains its documented default-time rule;
            // an ambiguous result still fails closed through the normal guard.
            uint eventTime = unchecked((uint)before);
            for (int i = 0; i < inputs.Length; i++) {
                if (inputs[i].type == 1) inputs[i].ki.time = eventTime;
                else if (inputs[i].type == 0) inputs[i].mi.time = eventTime;
            }
        }
        EmissionAttempted = true;
        uint count = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
        ulong after = GetTickCount64();
        if (count != inputs.Length)
            throw new InvalidOperationException("SendInput count mismatch; input outcome uncertain");
        if (Guarded) {
            Injection = new Emission { epoch = expectedEpoch, before_ms = before, after_ms = after, count = count };
            injectBefore = before;
            baselineNow = Latest.now_ms; baselineLast = Latest.last_input_ms;
        }
    }
    public static void Release(INPUT[] inputs) {
        // Cleanup after a refused/partial hold must not be gated or credited.
        Stamp(inputs);
        uint count = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
        if (count != inputs.Length) throw new InvalidOperationException("SendInput release count mismatch");
    }

    public delegate bool EnumProc(IntPtr h, IntPtr p);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo;
    }
    // Size=40 matches the real x64 INPUT union; ki lands at offset 8 via IntPtr alignment.
    [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
        public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Explicit, Size = 40)] public struct INPUT {
        [FieldOffset(0)] public uint type;
        [FieldOffset(8)] public KEYBDINPUT ki;
        [FieldOffset(8)] public MOUSEINPUT mi;
    }
}
"@ -ReferencedAssemblies System, System.Drawing

# ---- constants ---------------------------------------------------------------
$MOUSE = @{ ldown = 0x02; lup = 0x04; rdown = 0x08; rup = 0x10; mdown = 0x20; mup = 0x40; wheel = 0x0800; hwheel = 0x1000 }
$KEYUP = 0x0002
$UNICODE = 0x0004

# xdotool / X11 key names -> Windows virtual key codes (Claude emits these names)
$VK = @{
  'return' = 0x0D; 'enter' = 0x0D; 'kp_enter' = 0x0D; 'tab' = 0x09; 'escape' = 0x1B; 'esc' = 0x1B
  'backspace' = 0x08; 'delete' = 0x2E; 'space' = 0x20; 'insert' = 0x2D
  'home' = 0x24; 'end' = 0x23; 'page_up' = 0x21; 'prior' = 0x21; 'page_down' = 0x22; 'next' = 0x22
  'left' = 0x25; 'up' = 0x26; 'right' = 0x27; 'down' = 0x28
  'ctrl' = 0x11; 'control' = 0x11; 'alt' = 0x12; 'shift' = 0x10
  'super' = 0x5B; 'win' = 0x5B; 'cmd' = 0x5B; 'meta' = 0x5B
  'capslock' = 0x14; 'numlock' = 0x90; 'printscreen' = 0x2C; 'print' = 0x2C; 'menu' = 0x5D; 'pause' = 0x13
  'f1' = 0x70; 'f2' = 0x71; 'f3' = 0x72; 'f4' = 0x73; 'f5' = 0x74; 'f6' = 0x75
  'f7' = 0x76; 'f8' = 0x77; 'f9' = 0x78; 'f10' = 0x79; 'f11' = 0x7A; 'f12' = 0x7B
}

# ---- helpers -----------------------------------------------------------------
function Get-VirtualScreen { [System.Windows.Forms.SystemInformation]::VirtualScreen }

function Get-IdleMs {
  # docs/designs/coexistence.md \u00a75.5: milliseconds since the last input event
  # (real OR synthetic) reached this machine - GetTickCount() - dwTime, both read
  # via the SAME P/Invoke pair so they share one clock (the \u007e10-16ms GetTickCount
  # tick this bundle's measured GUARD_MS["windows-wsl2"]=20.0 already accounts for).
  $observation = [CU]::Observe()
  return [double]($observation.now_ms - $observation.last_input_ms)
}

function New-KeyInput([int]$vkCode, [int]$flags = 0, [int]$scan = 0) {
  $inputEvent = New-Object CU+INPUT; $inputEvent.type = 1
  $key = New-Object CU+KEYBDINPUT
  $key.wVk = [uint16]$vkCode; $key.wScan = [uint16]$scan; $key.dwFlags = [uint32]$flags
  $inputEvent.ki = $key
  return $inputEvent
}

function Send-MouseInput([uint32]$flags, [int]$data = 0) {
  $inputEvent = New-Object CU+INPUT
  $mouse = New-Object CU+MOUSEINPUT; $mouse.dwFlags = $flags
  # Preserve negative wheel deltas as DWORD bit patterns.
  $mouse.mouseData = [BitConverter]::ToUInt32([BitConverter]::GetBytes($data), 0)
  $inputEvent.mi = $mouse
  [CU]::Emit([CU+INPUT[]]@($inputEvent))
}

function Resolve-Vk([string]$name) {
  $n = $name.ToLower()
  if ($VK.ContainsKey($n)) { return @{ vk = $VK[$n]; shift = $false } }
  if ($n.Length -eq 1) {
    $s = [CU]::VkKeyScan($name[0])
    if ($s -eq -1) { throw "unmappable key '$name'" }
    return @{ vk = ($s -band 0xFF); shift = (($s -shr 8) -band 1) -eq 1 }
  }
  throw "unknown key name '$name'"
}

function Send-KeyCombo([string]$combo, [double]$holdSeconds = 0) {
  # "ctrl+s", "alt+Tab", "shift+Home", "Return"
  $parts = $combo -split '\+' | Where-Object { $_ -ne '' }
  $mods = @(); $mainKey = $null
  foreach ($p in $parts) {
    $lp = $p.ToLower()
    if ($lp -in @('ctrl', 'control', 'alt', 'shift', 'super', 'win', 'cmd', 'meta')) { $mods += $VK[$lp] } else { $mainKey = $p }
  }
  $needShift = $false
  $mainVk = $null
  if ($null -ne $mainKey) { $r = Resolve-Vk $mainKey; $mainVk = $r.vk; $needShift = $r.shift }
  if ($needShift -and ($VK['shift'] -notin $mods)) { $mods += $VK['shift'] }

  $downs = @(); $ups = @()
  foreach ($m in $mods) { $downs += (New-KeyInput $m) }
  if ($null -ne $mainVk) { $downs += (New-KeyInput $mainVk); $ups += (New-KeyInput $mainVk $KEYUP) }
  [array]::Reverse($mods)
  foreach ($m in $mods) { $ups += (New-KeyInput $m $KEYUP) }
  if ($holdSeconds -le 0) {
    try { [CU]::Emit([CU+INPUT[]]($downs + $ups)) }
    catch {
      if ([CU]::EmissionAttempted) { [CU]::Release([CU+INPUT[]]$ups) }
      throw
    }
  } else {
    $released = $false; $pressed = $false
    try {
      [CU]::Emit([CU+INPUT[]]$downs); $pressed = $true
      Start-Sleep -Milliseconds ([int]($holdSeconds * 1000))
      [CU]::Emit([CU+INPUT[]]$ups); $released = $true
    } finally {
      if (-not $released -and ($pressed -or [CU]::EmissionAttempted)) { [CU]::Release([CU+INPUT[]]$ups) }
    }
  }
}

function Send-UnicodeText([string]$text) {
  # KEYEVENTF_UNICODE sends the literal character - immune to SendKeys escaping and layout issues.
  foreach ($ch in $text.ToCharArray()) {
    if ($ch -eq "`n" -or $ch -eq "`r") { Send-KeyCombo 'Return'; continue }
    if ($ch -eq "`t") { Send-KeyCombo 'Tab'; continue }
    $down = New-Object CU+INPUT; $down.type = 1
    $kd = New-Object CU+KEYBDINPUT; $kd.wVk = 0; $kd.wScan = [uint16][char]$ch; $kd.dwFlags = $UNICODE
    $down.ki = $kd
    $up = New-Object CU+INPUT; $up.type = 1
    $ku = New-Object CU+KEYBDINPUT; $ku.wVk = 0; $ku.wScan = [uint16][char]$ch; $ku.dwFlags = $UNICODE -bor $KEYUP
    $up.ki = $ku
    try { [CU]::Emit([CU+INPUT[]]@($down, $up)) }
    catch {
      if ([CU]::EmissionAttempted) { [CU]::Release([CU+INPUT[]]@($up)) }
      throw
    }
    Start-Sleep -Milliseconds 8
  }
}

function Move-To([int]$x, [int]$y) {
  [CU]::Check()
  if (-not [CU]::SetCursorPos($x, $y)) { throw "SetCursorPos failed" }
  Start-Sleep -Milliseconds 20
}

function Invoke-Click([string]$button, [int]$times) {
  $d = $MOUSE["$($button)down"]; $u = $MOUSE["$($button)up"]
  for ($i = 0; $i -lt $times; $i++) {
    $released = $false; $pressed = $false
    try {
      Send-MouseInput $d; $pressed = $true
      Start-Sleep -Milliseconds 25
      Send-MouseInput $u; $released = $true
    } finally {
      if (-not $released -and ($pressed -or [CU]::EmissionAttempted)) {
        $savedGuarded = [CU]::Guarded; [CU]::Guarded = $false
        try { Send-MouseInput $u } finally { [CU]::Guarded = $savedGuarded }
      }
    }
    if ($i -lt $times - 1) { Start-Sleep -Milliseconds 90 }
  }
}

function Save-Screenshot([int]$x, [int]$y, [int]$w, [int]$h) {
  $dir = Join-Path $env:TEMP 'amplifier-computer-use'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Get-ChildItem $dir -Filter 'shot-*.png' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddMinutes(-30) } | Remove-Item -Force -ErrorAction SilentlyContinue
  $path = Join-Path $dir ("shot-{0}.png" -f ([guid]::NewGuid().ToString('N')))
  $bmp = New-Object System.Drawing.Bitmap $w, $h
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  try {
    $g.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size($w, $h)))
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  } finally { $g.Dispose(); $bmp.Dispose() }
  return $path
}

function Get-Coord($req, [string]$name) {
  $c = $req.$name
  if ($null -eq $c) { throw "action '$($req.action)' requires '$name'" }
  return @([int]$c[0], [int]$c[1])
}

# ---- session/lock detection (silent-failure fix) ------------------------------
# A locked workstation and a genuinely-missing GUI session both used to leave
# Get-VirtualScreen/CopyFromScreen/SendInput throwing an unrelated .NET exception -
# or, worse for capture, "succeeding" against a lock screen that looks like a real
# desktop from byte count alone (see MacOSBackend._macos_session_state's docstring
# for the real incident this mirrors: a locked screen was misdiagnosed as a missing
# permission grant, and that WRONG diagnosis stood as fact for days). Checked once,
# up front, for every action that captures pixels or injects input - read-only
# geometry/cursor/window-enumeration actions are left unaffected, matching the same
# scoping MacOSBackend uses.
function Get-SessionState {
  # LogonUI.exe is the lock-screen host process Windows runs while the session is
  # locked - its mere presence is the standard, documented signal (the Win32
  # equivalent of macOS's CGSSessionScreenIsLocked: a fact you check for, not a
  # value you read). NOT independently verified against a live Windows target for
  # this change (see the accompanying report - no Windows host was reachable) but
  # unlike the mac backend, this reuses Get-VirtualScreen, the SAME call every
  # other action here already depends on, as the "no GUI session" probe - so a
  # legitimate desktop that can already run screenshot/mouse_move today can never
  # be newly blocked by this check.
  $locked = @(Get-Process -Name logonui -ErrorAction SilentlyContinue).Count -gt 0
  try {
    $vs = Get-VirtualScreen
    if ($null -eq $vs -or $vs.Width -le 0 -or $vs.Height -le 0) {
      return @{ locked = $locked; no_gui_session = $true; vs = $vs
        detail = "SystemInformation.VirtualScreen returned an empty/zero-size rectangle" }
    }
    return @{ locked = $locked; no_gui_session = $false; vs = $vs; detail = $null }
  } catch {
    return @{ locked = $locked; no_gui_session = $true; vs = $null
      detail = "Get-VirtualScreen failed: $($_.Exception.Message)" }
  }
}

#: Actions that either capture pixels or inject input - the two silent-failure
#: paths this check exists to close. Read-only actions (cursor_position,
#: screen_info, list_windows, presence_idle, get_clipboard) are deliberately
#: excluded: they either still work correctly against a locked session (cursor
#: position, geometry) or their own result already makes the state obvious
#: (list_windows returns nothing) - narrowing scope here matches MacOSBackend's
#: same-scoped distinction rather than blocking strictly more than proven necessary.
$CAPTURE_OR_WRITE_ACTIONS = @(
  'screenshot', 'zoom', 'mouse_move', 'left_click', 'right_click', 'middle_click',
  'double_click', 'triple_click', 'left_mouse_down', 'left_mouse_up',
  'left_click_drag', 'scroll', 'key', 'hold_key', 'type', 'focus_window', 'set_clipboard'
)

# ---- dispatch ----------------------------------------------------------------
$out = @{ ok = $true }
try {
  # Per-Monitor-v2 DPI awareness (Windows 10 1703+, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
  # = -4) MUST be set before any Screen/CopyFromScreen/cursor call below. The legacy,
  # process-wide "system DPI aware" declaration this replaces (see git history) only
  # covers the primary monitor's scale factor: on a real multi-monitor desktop where
  # monitor's scaling (measured live on the four-monitor 4K box this bug was filed
  # against - every physical 3840x2160 panel at 150% scaling reported as a
  # DPI-VIRTUALIZED 2560x1440 by [System.Windows.Forms.Screen]::AllScreens), Windows
  # DPI-virtualizes every monitor's bounds AND every CopyFromScreen/SetCursorPos/
  # GetWindowRect coordinate to the primary's DPI. `Save-Screenshot` below then
  # captures a squished/cropped fraction of the real monitor into a bitmap sized for
  # the WRONG (virtualized, not physical) dimensions - the "monitor renders into a
  # fraction of the frame" defect, and every subsequent click/move computed from
  # those same virtualized bounds lands off-target on that monitor. Fails loud
  # (never silently falls back to the DPI-broken legacy call) if per-monitor-v2
  # awareness cannot be set: a build predating the Creators Update cannot guarantee
  # correct multi-monitor capture or input, and this bundle refuses to serve a
  # capture it cannot guarantee is correct rather than return a misleading one.
  if (-not [CU]::SetProcessDpiAwarenessContext([IntPtr]-4)) {
    throw ("DPI_AWARENESS_FAILED: SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2) " +
      "was refused by this Windows build. Multi-monitor capture and input cannot be " +
      "guaranteed correct without per-monitor DPI awareness (requires the Windows 10 " +
      "Creators Update, 1703, or later) - refusing to capture/drive rather than risk " +
      "a silently mis-scaled or mis-scoped monitor.")
  }
  $req = Get-Content -Raw -LiteralPath $RequestFile | ConvertFrom-Json
  if ($null -ne $req._input_marker) {
    if (($req._input_marker -isnot [int] -and $req._input_marker -isnot [long]) -or
        $req._input_marker -le 0) { throw "Malformed private input marker" }
    [CU]::ConfigureMarker($req._input_marker)
  }
  $action = "$($req.action)".ToLower()
  $state = Get-SessionState

  if ($action -eq 'session_state') {
    $out.locked = $state.locked; $out.no_gui_session = $state.no_gui_session
    if ($state.detail) { $out.detail = $state.detail }
    $out | ConvertTo-Json -Depth 6 -Compress
    return
  }
  if ($state.no_gui_session -and ($action -in $CAPTURE_OR_WRITE_ACTIONS)) {
    throw ("NO_GUI_SESSION: no interactive GUI session is available on this " +
      "Windows host to capture or drive ($($state.detail)). This is NOT a lock - " +
      "the session itself has no desktop attached (e.g. a disconnected RDP/console " +
      "session, or a Server Core / session-0 host). Reconnect to the console (RDP, " +
      "or physically) before retrying.")
  }
  if ($state.locked -and ($action -in $CAPTURE_OR_WRITE_ACTIONS)) {
    throw ("SESSION_LOCKED: the Windows session is LOCKED (LogonUI.exe is running - " +
      "the lock-screen host process). A locked screen and a missing capture/input " +
      "permission look identical from a screenshot's byte size alone; this check " +
      "confirms it is the LOCK, not a permission problem. Unlock the session (sign " +
      "back in at the keyboard, or via RDP) before retrying - screenshots of the " +
      "lock screen are refused, never returned as if they were the real desktop, " +
      "and no input is sent to a locked session.")
  }
  $vs = $state.vs
  if ($null -ne $req.target_clock_condition) {
    $condition = $req.target_clock_condition
    # Reject JSON strings/bools/null in integer fields before PowerShell's
    # permissive numeric coercion can manufacture safety evidence.
    foreach ($field in @('version', 'now_ms', 'last_input_ms')) {
      $value = $condition.$field
      if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 0) {
        throw "Malformed Windows target-clock integer"
      }
    }
    if ($condition.version -notin @(1, 2) -or $condition.epoch -isnot [string]) {
      throw "Malformed Windows target-clock version/epoch"
    }
    $injection = $condition.inject_before_ms
    if ($null -ne $injection -and (
        ($injection -isnot [int] -and $injection -isnot [long]) -or $injection -lt 0)) {
      throw "Malformed Windows target-clock injection"
    }
    # Windows PowerShell 5.1 ConvertFrom-Json represents JSON 20.0 as Decimal.
    # Accept numeric Decimal, never strings/bools; Configure validates finiteness.
    if (($condition.guard_ms -isnot [double] -and $condition.guard_ms -isnot [decimal] -and $condition.guard_ms -isnot [int] -and
         $condition.guard_ms -isnot [long]) -or $condition.guard_ms -lt 0) {
      throw "Malformed Windows target-clock guard"
    }
    [CU]::Configure($condition.epoch, $condition.now_ms, $condition.last_input_ms,
      $injection, $condition.guard_ms)
    $operatorFields = @('operator_presence_override', 'operator_presence_channel', 'operator_presence_expires_at', 'operator_presence_target')
    $presentOperatorFields = @($condition.PSObject.Properties.Name | Where-Object { $_ -in $operatorFields })
    if ($condition.version -eq 2) {
      if ($presentOperatorFields.Count -ne 4 -or
          $condition.operator_presence_override -isnot [bool] -or
          $condition.operator_presence_override -ne $true -or
          $condition.operator_presence_channel -isnot [string] -or
          $condition.operator_presence_target -isnot [string]) {
        throw "Malformed operator presence condition"
      }
      $expiry = $condition.operator_presence_expires_at
      if ($expiry -isnot [double] -and $expiry -isnot [decimal] -and
          $expiry -isnot [int] -and $expiry -isnot [long]) {
        throw "Malformed operator presence expiry"
      }
      [CU]::ConfigureOperatorPresence($condition.operator_presence_channel, $expiry, $condition.operator_presence_target)
    } elseif ($presentOperatorFields.Count -ne 0) {
      throw "Operator presence override requires Windows condition v2"
    }
  }

  switch ($action) {
    'screenshot' {
      $out.path = Save-Screenshot $vs.X $vs.Y $vs.Width $vs.Height
      $out.width = $vs.Width; $out.height = $vs.Height
    }
    'zoom' {
      $c = $req.coordinate
      if ($null -ne $req.region) { $c = $req.region }
      if (($null -eq $c -or $c.Count -lt 4) -and $null -ne $req.start_coordinate -and $null -ne $req.coordinate) {
        $c = @($req.start_coordinate[0], $req.start_coordinate[1], $req.coordinate[0], $req.coordinate[1])
      }
      if ($null -eq $c -or $c.Count -lt 4) { throw "zoom requires coordinate [x1,y1,x2,y2]" }
      $x1 = [int]$c[0]; $y1 = [int]$c[1]; $x2 = [int]$c[2]; $y2 = [int]$c[3]
      $out.path = Save-Screenshot $x1 $y1 ([Math]::Max(1, $x2 - $x1)) ([Math]::Max(1, $y2 - $y1))
      $out.width = $x2 - $x1; $out.height = $y2 - $y1
    }
    'cursor_position' {
      $p = New-Object CU+POINT; [void][CU]::GetCursorPos([ref]$p)
      $out.coordinate = @($p.X, $p.Y)
    }
    'mouse_move' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; $out.coordinate = $c }
    'left_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'l' 1; $out.coordinate = $c }
    'right_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'r' 1; $out.coordinate = $c }
    'middle_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'm' 1; $out.coordinate = $c }
    'double_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'l' 2; $out.coordinate = $c }
    'triple_click' { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1]; Invoke-Click 'l' 3; $out.coordinate = $c }
    'left_mouse_down' {
      if ($req.coordinate) { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1] }
      Send-MouseInput $MOUSE.ldown
    }
    'left_mouse_up' {
      if ($req.coordinate) { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1] }
      Send-MouseInput $MOUSE.lup
    }
    'left_click_drag' {
      $s = if ($req.start_coordinate) { Get-Coord $req 'start_coordinate' } else { $p = New-Object CU+POINT; [void][CU]::GetCursorPos([ref]$p); @($p.X, $p.Y) }
      $e = Get-Coord $req 'coordinate'
      Move-To $s[0] $s[1]
      $released = $false; $pressed = $false
      try {
        Send-MouseInput $MOUSE.ldown; $pressed = $true; Start-Sleep -Milliseconds 60
        $steps = 24
        for ($i = 1; $i -le $steps; $i++) {
          Move-To ([int]($s[0] + ($e[0] - $s[0]) * $i / $steps)) ([int]($s[1] + ($e[1] - $s[1]) * $i / $steps))
        }
        Start-Sleep -Milliseconds 60
        Send-MouseInput $MOUSE.lup; $released = $true
      } finally {
        if (-not $released -and ($pressed -or [CU]::EmissionAttempted)) {
          $savedGuarded = [CU]::Guarded; [CU]::Guarded = $false
          try { Send-MouseInput $MOUSE.lup } finally { [CU]::Guarded = $savedGuarded }
        }
      }
      $out.start = $s; $out.end = $e
    }
    'scroll' {
      if ($req.coordinate) { $c = Get-Coord $req 'coordinate'; Move-To $c[0] $c[1] }
      $dir = "$(if ($req.scroll_direction) { $req.scroll_direction } else { $req.direction })".ToLower()
      $amtRaw = if ($null -ne $req.scroll_amount) { $req.scroll_amount } elseif ($null -ne $req.amount) { $req.amount } else { 3 }
      $clicks = [Math]::Max(1, [int]$amtRaw)
      for ($i = 0; $i -lt $clicks; $i++) {
        switch ($dir) {
          'up' { Send-MouseInput $MOUSE.wheel 120 }
          'down' { Send-MouseInput $MOUSE.wheel -120 }
          'right' { Send-MouseInput $MOUSE.hwheel 120 }
          'left' { Send-MouseInput $MOUSE.hwheel -120 }
          default { throw "scroll direction must be up/down/left/right (got '$dir')" }
        }
        Start-Sleep -Milliseconds 40
      }
      $out.direction = $dir; $out.clicks = $clicks
    }
    'key' {
      $t = if ($req.text) { $req.text } else { $req.key }
      if (-not $t) { throw "key requires 'text'" }
      foreach ($combo in ("$t" -split '\s+' | Where-Object { $_ })) { Send-KeyCombo $combo; Start-Sleep -Milliseconds 30 }
      $out.keys = "$t"
    }
    'hold_key' {
      $t = if ($req.text) { $req.text } else { $req.key }
      $dur = if ($null -ne $req.duration) { [double]$req.duration } else { 1.0 }
      if ($dur -gt 10) { throw "hold_key duration capped at 10s" }
      Send-KeyCombo "$t" $dur
      $out.keys = "$t"; $out.duration = $dur
    }
    'type' {
      $t = "$($req.text)"
      if ([string]::IsNullOrEmpty($t)) { throw "type requires 'text'" }
      Send-UnicodeText $t
      $out.typed = $t.Length
    }
    'wait' {
      $dur = if ($null -ne $req.duration) { [double]$req.duration } else { 1.0 }
      if ($dur -gt 30) { throw "wait duration capped at 30s" }
      Start-Sleep -Milliseconds ([int]($dur * 1000))
      $out.duration = $dur
    }
    'screen_info' {
      $out.width = $vs.Width; $out.height = $vs.Height; $out.x = $vs.X; $out.y = $vs.Y
      $out.screens = @([System.Windows.Forms.Screen]::AllScreens | ForEach-Object {
          @{ name = $_.DeviceName; primary = $_.Primary; bounds = @($_.Bounds.X, $_.Bounds.Y, $_.Bounds.Width, $_.Bounds.Height) }
        })
    }
    'presence_idle' {
      # docs/designs/coexistence.md \u00a75.5 - a dedicated, lightweight action reusing
      # this SAME bridge script/dispatcher (no new mechanism, no new transport) rather
      # than a standalone tool. Deliberately does nothing else (no CopyFromScreen, no
      # SendInput) - the cheapest possible action this bridge can perform.
      $out.observation = [CU]::Observe()
      $out.idle_ms = [double]($out.observation.now_ms - $out.observation.last_input_ms)
    }
    'get_clipboard' {
      # Get-Clipboard/Set-Clipboard handle the STA requirement internally; the
      # System.Windows.Forms.Clipboard API does not and throws under -File.
      $t = Get-Clipboard -Raw -ErrorAction SilentlyContinue
      $out.text = "$t"
    }
    'set_clipboard' {
      $val = "$($req.text)"
      [CU]::Check()
      Set-Clipboard -Value $val
      $out.set = $val.Length
    }
    'list_windows' {
      $list = New-Object System.Collections.Generic.List[object]
      $cb = [CU+EnumProc] {
        param($h, $p)
        if ([CU]::IsWindowVisible($h)) {
          $len = [CU]::GetWindowTextLength($h)
          if ($len -gt 0) {
            $sb = New-Object System.Text.StringBuilder ($len + 1)
            [void][CU]::GetWindowText($h, $sb, $sb.Capacity)
            $r = New-Object CU+RECT; [void][CU]::GetWindowRect($h, [ref]$r)
            $procId = 0; [void][CU]::GetWindowThreadProcessId($h, [ref]$procId)
            if (($r.R - $r.L) -gt 0 -and ($r.B - $r.T) -gt 0) {
              $list.Add(@{
                  handle = $h.ToString(); title = $sb.ToString(); pid = [int]$pid
                  minimized = [bool][CU]::IsIconic($h); rect = @($r.L, $r.T, $r.R, $r.B)
                }) | Out-Null
            }
          }
        }
        return $true
      }
      [void][CU]::EnumWindows($cb, [IntPtr]::Zero)
      $fg = [CU]::GetForegroundWindow()
      $out.windows = $list.ToArray(); $out.foreground = $fg.ToString()
    }
    'focus_window' {
      $h = [IntPtr][int64]"$($req.handle)"
      [CU]::Check()
      if ([CU]::IsIconic($h)) { [void][CU]::ShowWindow($h, 9) }   # SW_RESTORE
      # ALT nudge defeats Windows' foreground-lock heuristic
      $released = $false; $pressed = $false
      $altUp = [CU+INPUT[]]@((New-KeyInput 0x12 $KEYUP))
      try {
        [CU]::Emit([CU+INPUT[]]@((New-KeyInput 0x12))); $pressed = $true
        [CU]::Check()
        [void][CU]::SetForegroundWindow($h)
        [CU]::Emit($altUp); $released = $true
      } finally {
        if (-not $released -and ($pressed -or [CU]::EmissionAttempted)) { [CU]::Release($altUp) }
      }
      Start-Sleep -Milliseconds 250
      $out.focused = ([CU]::GetForegroundWindow()).ToString()
      $out.requested = $h.ToString()
    }
    default { throw "unknown action '$($req.action)'" }
  }
}
catch {
  $out = @{ ok = $false; error = "$($_.Exception.Message)" }
}

if ($null -ne $req.target_clock_condition) {
  try {
    # Retain the rejected preflight, not an observation of our subsequent
    # emergency release (which could otherwise be mislabelled as human input).
    $observation = if ($out.ok) { [CU]::Observe() } else { [CU]::Latest }
    $out.target_clock = @{ observation = $observation; injection = [CU]::Injection }
  } catch {
    $out.ok = $false
    $out.error = "Windows target-clock response unavailable: $($_.Exception.Message)"
    $out.target_clock = @{ observation = $null; injection = [CU]::Injection }
  }
}
$out | ConvertTo-Json -Depth 6 -Compress
