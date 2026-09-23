# hook-computer-use

The `provider:request` handler prepares native computer integration only when
the real `computer` tool is mounted. A headless session with only
`computer_use_unavailable`, or a child session without computer access, skips
provider lookup and probing. This prevents an irrelevant native-passthrough
warning during ordinary file/function work.

Tool presence is checked again each turn, not cached: activating a desktop later
in the same session enables the normal probe on the next request. A failed tool
lookup still warns and proceeds with the existing provider compatibility checks;
it is not treated as confirmed absence.

When `computer` is present, the existing native capability probes, compatibility
checks, and unsupported-provider warnings remain unchanged. Skipping a probe
does not assert that a provider supports native computer use.

Screenshot expansion runs on independent request copies before both
`request_budget()` (when present) and `complete()`. Budget preflight may serialize
the full request before completion, so wrapping completion alone is too late.
The wrapper preserves synchronous or awaitable budget results and named request
options. Canonical messages and the caller's request remain unchanged.

When the capability probe selects the exact bare `computer` dialect, its native
call output requires one image. An explicit successful ToolResult containing the
known single-screenshot marker becomes a single image block. The full original
result, including appended text, accompanies it as labelled untrusted tool
reference data with the call ID and `ephemeral` metadata. It is a request-only
user-content carrier, not a system/developer instruction or a new user turn.
Errors, ambiguous envelopes, missing files, multiple images and already mixed
text/image results are left unchanged for the provider to validate or reject.
The provider's existing image-byte validation is not changed or bypassed.

Every retained native computer call requires its screenshot output. For this
exact dialect, `max_inline_images` therefore does not replace older screenshots
with text. This can increase request size and image-token cost; ordinary context
limits still apply, and missing older image files fail closed. Other supported
dialects retain the existing screenshot recency policy. This hook does not invent
images, recapture screenshots, replay tools or clear a safety halt.

`tests/test_native_screenshot_preflight.py` includes retained screenshot-result
fixtures and offline regressions for both root and delegated results, unchanged
history, repeated budget/dispatch calls and more than three screenshots. Optional
provider integration cases run when `amplifier_module_provider_openai` is on the
test import path; use the intended provider revision explicitly when qualifying
a runtime combination. They stop before transport and do not establish live
endpoint, model, desktop, or physical-device acceptance.

Interactive applications with piped runtime stdin may register the boolean
coordinator capability `approval.interactive` before module initialization.
`True` means the app can deliver the standard `ask_user` approval to a person;
it does not approve any action. `False` explicitly disables interactive prompting.
Missing capability retains the legacy stdin-TTY check; malformed values or lookup
failure fail closed. The gate still defaults to deny and never infers unattended
write permission. This separates approval transport from write authorization.
