#requires -Version 5.1
# Explicit, disposable passive observer. Private configuration arrives on stdin;
# never supply the marker in argv, logs, public action arguments or reports.
# Adapted from the retained cu0543-passive-probe.ps1; original remains unchanged.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$source = @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class InputProbe {
    const uint WM_TIMER = 0x0113, WM_STOP = 0x8054;
    const int EventCap = 256, ChangeCap = 32;
    delegate IntPtr HookProc(int code, IntPtr message, IntPtr data);
    static HookProc keyboardProc = Keyboard, mouseProc = Mouse;
    static IntPtr keyboardHook, mouseHook, marker;
    static UIntPtr sampleTimer;
    static uint threadId, lastInput;
    static int callbackErrors, stopPostError;
    static long keyboardCount, mouseCount, changeCount, polls;
    static bool stopPosted;
    static ulong previousPoll, maxPollGap;
    static List<object> events = new List<object>(), changes = new List<object>();
    static Dictionary<string, object> initial, final;

    [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize, dwTime; }
    // Layout only: payload fields are NEVER marshalled/read. Offsets are derived
    // for the running architecture, including x64 mouse extra-info alignment.
    [StructLayout(LayoutKind.Sequential)] struct KBDLL {
        public uint vk, scan, flags, time; public IntPtr extra;
    }
    [StructLayout(LayoutKind.Sequential)] struct MSLL {
        public int x, y; public uint mouseData, flags, time; public IntPtr extra;
    }
    static int Offset(bool keyboard, string field) {
        return Marshal.OffsetOf(keyboard ? typeof(KBDLL) : typeof(MSLL), field).ToInt32();
    }
    [StructLayout(LayoutKind.Sequential)] struct MSG {
        public IntPtr hwnd; public uint message; public UIntPtr wParam; public IntPtr lParam;
        public uint time; public int x, y; public uint lPrivate;
    }
    [StructLayout(LayoutKind.Sequential)] struct USEROBJECTFLAGS { public int inherit, reserved; public uint flags; }
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetWindowsHookEx(int kind, HookProc proc, IntPtr module, uint thread);
    [DllImport("user32.dll", SetLastError=true)] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr message, IntPtr data);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr GetModuleHandle(string name);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll")] static extern ulong GetTickCount64();
    [DllImport("user32.dll", SetLastError=true)] static extern bool GetLastInputInfo(ref LASTINPUTINFO value);
    [DllImport("user32.dll")] static extern IntPtr GetProcessWindowStation();
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr GetThreadDesktop(uint thread);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll", SetLastError=true)] static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", EntryPoint="GetUserObjectInformationW", SetLastError=true)] static extern bool ObjectFlags(IntPtr handle, int index, ref USEROBJECTFLAGS flags, uint length, out uint needed);
    [DllImport("user32.dll", EntryPoint="GetUserObjectInformationW", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool ObjectName(IntPtr handle, int index, StringBuilder name, uint length, out uint needed);
    [DllImport("user32.dll", SetLastError=true)] static extern UIntPtr SetTimer(IntPtr hwnd, UIntPtr id, uint interval, IntPtr proc);
    [DllImport("user32.dll", SetLastError=true)] static extern bool KillTimer(IntPtr hwnd, UIntPtr id);
    [DllImport("user32.dll", SetLastError=true)] static extern int GetMessage(out MSG message, IntPtr hwnd, uint min, uint max);
    [DllImport("user32.dll")] static extern bool PeekMessage(out MSG message, IntPtr hwnd, uint min, uint max, uint remove);
    [DllImport("user32.dll", SetLastError=true)] static extern bool PostThreadMessage(uint thread, uint message, UIntPtr wParam, IntPtr lParam);
    // Same boot/logon/session epoch as bridge.ps1; raw identity stays in memory.
    [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(int infoClass, IntPtr info, int size, out int returned);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token, int infoClass, IntPtr info, int size, out int returned);
    static string GetEpoch() {
        IntPtr boot = Marshal.AllocHGlobal(32), stats = Marshal.AllocHGlobal(128), token = IntPtr.Zero;
        try {
            int returned;
            if (NtQuerySystemInformation(90, boot, 32, out returned) != 0 || returned < 16)
                throw new InvalidOperationException();
            byte[] guid = new byte[16]; Marshal.Copy(boot, guid, 0, 16);
            if (new Guid(guid) == Guid.Empty) throw new InvalidOperationException();
            if (!OpenProcessToken(GetCurrentProcess(), 8, out token) ||
                !GetTokenInformation(token, 10, stats, 128, out returned) || returned < 16)
                throw new InvalidOperationException();
            string identity = new Guid(guid).ToString("D") + ":" +
                System.Diagnostics.Process.GetCurrentProcess().SessionId + ":" +
                Marshal.ReadInt32(stats, 8) + ":" + Marshal.ReadInt32(stats, 12);
            using (var sha = System.Security.Cryptography.SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(identity))).Replace("-", "").ToLowerInvariant();
        } finally {
            if (token != IntPtr.Zero) CloseHandle(token);
            Marshal.FreeHGlobal(boot); Marshal.FreeHGlobal(stats);
        }
    }
    static Exception NativeError() { return new Win32Exception(Marshal.GetLastWin32Error()); }
    static string DesktopName(IntPtr handle) {
        uint needed; StringBuilder name = new StringBuilder(256);
        if (!ObjectName(handle, 2, name, 512, out needed)) throw NativeError();
        return name.ToString(); // compare privately, never output
    }
    static Dictionary<string, object> Sample() {
        LASTINPUTINFO value = new LASTINPUTINFO(); value.cbSize = (uint)Marshal.SizeOf(typeof(LASTINPUTINFO));
        if (!GetLastInputInfo(ref value)) throw NativeError();
        return new Dictionary<string, object> {
            {"observed_tick64", GetTickCount64()}, {"last_input_tick_dword", value.dwTime}
        };
    }
    static void Poll() {
        Dictionary<string, object> item = Sample();
        ulong now = (ulong)item["observed_tick64"]; uint tick = (uint)item["last_input_tick_dword"];
        if (previousPoll != 0 && now - previousPoll > maxPollGap) maxPollGap = now - previousPoll;
        previousPoll = now; polls++;
        if (initial == null) { initial = item; lastInput = tick; }
        else if (tick != lastInput) {
            changeCount++; if (changes.Count < ChangeCap) changes.Add(item); lastInput = tick;
        }
        final = item;
    }
    static void Record(bool keyboard, IntPtr message, IntPtr data) {
        uint msg = unchecked((uint)message.ToInt64()); string kind;
        if (keyboard) {
            keyboardCount++;
            if (msg == 0x0100 || msg == 0x0104) kind = "key-down";
            else if (msg == 0x0101 || msg == 0x0105) kind = "key-up";
            else kind = "keyboard-other";
        } else {
            mouseCount++;
            if (msg == 0x0200) kind = "mouse-move";
            else if (msg == 0x0201 || msg == 0x0204 || msg == 0x0207 || msg == 0x020B) kind = "mouse-button-down";
            else if (msg == 0x0202 || msg == 0x0205 || msg == 0x0208 || msg == 0x020C) kind = "mouse-button-up";
            else if (msg == 0x020A || msg == 0x020E) kind = "mouse-wheel";
            else kind = "mouse-other";
        }
        if (events.Count >= EventCap) return;
        uint flags = unchecked((uint)Marshal.ReadInt32(data, Offset(keyboard, "flags")));
        uint tick = unchecked((uint)Marshal.ReadInt32(data, Offset(keyboard, "time")));
        bool matches = Marshal.ReadIntPtr(data, Offset(keyboard, "extra")) == marker;
        bool injected = (flags & (keyboard ? 0x10U : 0x01U)) != 0;
        bool lower = (flags & 0x02U) != 0;
        string category = lower && !injected ? "inconsistent_flags" :
            injected ? (matches ? "own_marker" : "other_injected") :
            (matches ? "inconsistent_marker" : "unattributed_noninjected");
        events.Add(new Dictionary<string, object> {
            {"kind", kind}, {"callback_tick_dword", tick}, {"observed_tick64", GetTickCount64()},
            {"injected", injected}, {"lower_il_injected", lower},
            {"marker_matches", matches}, {"category", category}
        });
    }
    static IntPtr Keyboard(int code, IntPtr message, IntPtr data) {
        try { if (code >= 0) Record(true, message, data); } catch { callbackErrors++; }
        return CallNextHookEx(IntPtr.Zero, code, message, data);
    }
    static IntPtr Mouse(int code, IntPtr message, IntPtr data) {
        try { if (code >= 0) Record(false, message, data); } catch { callbackErrors++; }
        return CallNextHookEx(IntPtr.Zero, code, message, data);
    }
    public static object Run(long privateMarker, int durationMs) {
        if (privateMarker <= 0 || (IntPtr.Size == 4 && privateMarker > Int32.MaxValue) ||
            durationMs < 1 || durationMs > 35000) throw new InvalidOperationException();
        marker = new IntPtr(privateMarker);
        Dictionary<string, object> report = new Dictionary<string, object>();
        Dictionary<string, object> cleanup = new Dictionary<string, object>();
        Timer deadline = null; IntPtr inputDesktop = IntPtr.Zero;
        report["version"] = 1; report["ready"] = false; report["status"] = "failed";
        report["epoch"] = null; report["requested_duration_ms"] = durationMs;
        report["event_cap"] = EventCap; report["change_cap"] = ChangeCap;
        report["keyboard_hook_installed"] = false; report["mouse_hook_installed"] = false;
        try {
            threadId = GetCurrentThreadId();
            uint needed; USEROBJECTFLAGS flags = new USEROBJECTFLAGS();
            if (!ObjectFlags(GetProcessWindowStation(), 1, ref flags, (uint)Marshal.SizeOf(typeof(USEROBJECTFLAGS)), out needed))
                throw NativeError();
            inputDesktop = OpenInputDesktop(0, false, 1);
            if (inputDesktop == IntPtr.Zero) throw NativeError();
            if ((flags.flags & 1) == 0 || DesktopName(inputDesktop) != DesktopName(GetThreadDesktop(threadId)))
                throw new InvalidOperationException();
            report["epoch"] = GetEpoch();
            MSG message; PeekMessage(out message, IntPtr.Zero, 0, 0, 0);
            IntPtr module = GetModuleHandle(null); if (module == IntPtr.Zero) throw NativeError();
            keyboardHook = SetWindowsHookEx(13, keyboardProc, module, 0);
            if (keyboardHook == IntPtr.Zero) throw NativeError();
            report["keyboard_hook_installed"] = true;
            mouseHook = SetWindowsHookEx(14, mouseProc, module, 0);
            if (mouseHook == IntPtr.Zero) throw NativeError();
            report["mouse_hook_installed"] = true;
            sampleTimer = SetTimer(IntPtr.Zero, UIntPtr.Zero, 50, IntPtr.Zero);
            if (sampleTimer == UIntPtr.Zero) throw NativeError();
            Poll(); ulong start = GetTickCount64();
            deadline = new Timer(delegate(object ignored) {
                bool ok = PostThreadMessage(threadId, WM_STOP, UIntPtr.Zero, IntPtr.Zero);
                if (!ok) stopPostError = Marshal.GetLastWin32Error();
                stopPosted = ok;
            }, null, durationMs, Timeout.Infinite);
            // A flushed readiness frame ONLY after both hooks, counter baseline
            // and deadline exist. Caller must receive this before ordinary input.
            report["ready"] = true;
            Console.WriteLine("{\"event\":\"probe_ready\",\"version\":1,\"epoch\":\"" +
                report["epoch"] + "\",\"observed_tick64\":" + initial["observed_tick64"] + "}");
            Console.Out.Flush();
            while (true) {
                int result = GetMessage(out message, IntPtr.Zero, 0, 0);
                if (result < 0) throw NativeError();
                if (result == 0) throw new InvalidOperationException();
                if (message.message == WM_STOP) break;
                if (message.message == WM_TIMER && message.wParam == sampleTimer) Poll();
                if (GetTickCount64() - start >= (ulong)durationMs) break;
            }
            Poll(); report["status"] = "completed";
        } catch (Exception error) {
            // No exception messages: compiler/runtime errors may contain paths.
            report["error_type"] = error.GetType().Name;
        } finally {
            if (deadline != null) deadline.Dispose();
            cleanup["deadline_timer_disposed"] = true;
            cleanup["sample_timer_killed"] = sampleTimer == UIntPtr.Zero || KillTimer(IntPtr.Zero, sampleTimer);
            cleanup["mouse_unhooked"] = mouseHook == IntPtr.Zero || UnhookWindowsHookEx(mouseHook);
            cleanup["keyboard_unhooked"] = keyboardHook == IntPtr.Zero || UnhookWindowsHookEx(keyboardHook);
            cleanup["input_desktop_handle_closed"] = inputDesktop == IntPtr.Zero || CloseDesktop(inputDesktop);
            report["initial"] = initial; report["final"] = final;
            report["counter_changes"] = changes; report["counter_change_count"] = changeCount;
            report["counter_changes_dropped"] = Math.Max(0L, changeCount - changes.Count);
            report["sample_count"] = polls; report["max_sample_gap_ms"] = maxPollGap;
            report["events"] = events; report["keyboard_callback_count"] = keyboardCount;
            report["mouse_callback_count"] = mouseCount;
            report["events_dropped"] = Math.Max(0L, keyboardCount + mouseCount - events.Count);
            report["callback_errors"] = callbackErrors;
            report["deadline_stop_posted"] = stopPosted; report["deadline_post_error"] = stopPostError;
            report["cleanup"] = cleanup;
        }
        return report;
    }
}
'@
$provider = $null
$tempFiles = $null
$tempRecords = @()
$outerStatus = 'failed'
$exitCode = 1
try {
    $config = [Console]::ReadLine() | ConvertFrom-Json
    if (($config.marker -isnot [int] -and $config.marker -isnot [long]) -or $config.marker -le 0 -or
        ($config.duration_ms -isnot [int] -and $config.duration_ms -isnot [long]) -or
        $config.duration_ms -lt 1 -or $config.duration_ms -gt 35000) { throw 'Invalid private probe config' }
    # Reuse the original helper's explicit CodeDOM temporary-file ownership.
    $provider = New-Object Microsoft.CSharp.CSharpCodeProvider
    $parameters = New-Object System.CodeDom.Compiler.CompilerParameters
    $parameters.GenerateExecutable = $false
    $parameters.GenerateInMemory = $true
    $parameters.IncludeDebugInformation = $false
    $tempFiles = [System.CodeDom.Compiler.TempFileCollection]::new([System.IO.Path]::GetTempPath(), $true)
    $parameters.TempFiles = $tempFiles
    [void]$parameters.ReferencedAssemblies.Add('System.dll')
    $compiled = $provider.CompileAssemblyFromSource($parameters, [string[]]@($source))
    if ($compiled.Errors.HasErrors) {
        [Console]::WriteLine('{"event":"compile_failed"}')
        throw 'Diagnostic not started'
    }
    $type = $compiled.CompiledAssembly.GetType('InputProbe', $true)
    $result = $type.GetMethod('Run').Invoke($null, [object[]]@([long]$config.marker, [int]$config.duration_ms))
    [Console]::WriteLine((@{event='probe_result'; result=$result} | ConvertTo-Json -Depth 10 -Compress))
    $outerStatus = $result['status']
    if ($outerStatus -eq 'completed') { $exitCode = 0 }
} catch {
    [Console]::WriteLine((@{event='helper_error'; error_type=$_.Exception.GetType().Name} | ConvertTo-Json -Compress))
} finally {
    try { if ($null -ne $provider) { $provider.Dispose() } } finally {
        if ($null -ne $tempFiles) {
            # Only this disposable compiler's fresh collection may be removed.
            foreach ($ownedPath in @($tempFiles)) {
                $existed = [System.IO.File]::Exists($ownedPath)
                $cleanupError = $null
                try { if ($existed) { [System.IO.File]::Delete($ownedPath) } } catch { $cleanupError = $_.Exception.GetType().Name }
                $tempRecords += [ordered]@{existed_before_cleanup=$existed; exists_after_cleanup=[System.IO.File]::Exists($ownedPath); error_type=$cleanupError}
            }
            $tempFiles.Dispose()
        }
        [Console]::WriteLine((@{event='probe_final'; status=$outerStatus; compiler_temp_files=$tempRecords; input_calls=0; clipboard_access=0; gui_actions=0; halt_access=0} | ConvertTo-Json -Depth 6 -Compress))
    }
}
exit $exitCode