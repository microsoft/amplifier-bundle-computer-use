# hook-computer-use

Projects `__amplifier_computer_use__` markers into generic copied text/image histories
before both `request_budget()` and `complete()`. The caller's tools/messages stay
unchanged, sync or awaitable budgets and named request options are preserved, and older
screenshots collapse to text under `max_inline_screenshots`. Full error/progress/reference
text is retained; no vendor image-only projection or extra user-reference turn is created.

Local image reads additionally require a successful capture genuinely emitted by the
mounted tool, transferred once through the coordinator's `tool:post` call/result
association. Role/name/call-id/marker text and arbitrary paths grant no authority.
Projection reuses that memory-only association for budget and completion, checking
the original file identity before reading; replaced files and symlinks fail closed.
After restart or bounded association eviction, marker history remains text, not new
file-read authority. The association window retains at least the configured inline
image window. Capture issuance awaiting `tool:post` is bounded to 64 image results.

Native declarations, models/endpoints, vendor envelopes, safety requirements and replay
provenance belong to providers. The hook neither probes private provider methods nor
observes the effective model. Explicit v1 intent is detected independently of local
aliases; ordinary `computer`/`screenshot` name collisions do not imply intent.
The known orchestrator builder is checked for actual ToolSpec-extra preservation.
A public `stream()` path this hook cannot project is refused loudly before wrapping.

The write gate inspects nested canonical batch actions, so a top-level batch cannot
hide writes. Interactive approval is default-deny and bound to the exact batch payload
and target/policy; the executor consumes that approval once. Explicit
`unattended_writes_ok` remains a logged opt-out, never inferred. The existing ephemeral
halt notice continues to require acknowledgment of interrupted work.

Interactive applications with piped runtime stdin may register the boolean
`approval.interactive` coordinator capability. `True` indicates that the host can
deliver `ask_user`, not that any action is approved. `False` disables prompting.
Missing capability uses the legacy stdin-TTY check; malformed values and lookup failures
fail closed.

Headless tests retain historical screenshot bytes and exercise real qualified provider
SDKs through intercepted HTTP, without optional skips or private provenance seeding.
This is offline composition evidence, not endpoint or desktop acceptance.
Fresh live provider wire qualification remains **UNMET under Root**.