# hook-computer-use

Projects `__amplifier_computer_use__` markers into generic copied text/image histories
before both `request_budget()` and `complete()`. The caller's tools/messages stay
unchanged, sync or awaitable budgets and named request options are preserved, and older
screenshots collapse to text under `max_inline_screenshots`. Full error/progress/reference
text is retained; no vendor image-only projection or extra user-reference turn is created.

A no-op projection preserves the request, messages list and message identities. A
changed projection copies only the request shell and changed messages; it does not
mutate the caller or copy unrelated prompt content.

Hosts that fit and bind requests before provider dispatch can use the optional
`computer_use.prepare_request` coordinator capability. Its synchronous callable
returns `(projected_request, validate_or_none)`. Invoke it before fitting, then keep
the exact projected representation. For images, call the returned validator after
awaited metadata/admission and immediately before provider entry. It rechecks
original capture issuance, mounted tool identity, hook lifetime and file identity
without rereading image bytes. Failure must refuse that prepared dispatch, even
without diagnostics or numeric budget capture. It is not execution permission or
a budget receipt. Request/options equality and task/dispatch binding remain the
host's responsibility. Replacing the capability invalidates prepared host work.

The ordinary CLI/Agent provider wrappers remain available without this capability
consumer. Already-projected typed blocks and collapsed reference text are naturally
idempotent, so these wrappers do not replace the host's prepared object. No request
metadata, provider name, public token or skip flag grants a projection bypass.

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

## Optional session-media producer

A qualified host can bind `experimental.media.v1` before module initialization.
The screenshot producer requires integer `version == 1`, `enabled is True`, and a
callable `computer_use.media_policy(ref, *, purpose)` capability. It privately
ingests the already-captured PNG after ordinary action cleanup, then returns an
actual `ToolResult.output` mapping with `experimental.media.v1: {refs: [...]}`.
No marker string, image bytes or local screenshot path is emitted on that path.
The loop alone publishes surviving effective post-hook refs and checkpoints them.
Packaging failure never recaptures or replays completed input; cancelled batches
retain completed progress with no promotable refs.

The host's source-policy registry binds the same callback into its generic media
store policy verifier. It classifies original screenshot session/call identities
and invokes the original mounted tool's `_validate_media_ref(ref, *, purpose)`.
At producer `purpose="persist"` admission, the host must bind that classification
to the actual current tool call, not caller text. Validate again before resolve,
private preparation and every count/transport attempt. Ref possession, a digest,
`capture_id`/`binding_id` labels, or compatibility reporting is never consent.
Lost original issuance after restart, copy, fork or bounded eviction is unavailable,
not permission to hydrate a durable blob. Keep source classification on those
paths so loss of the owner cannot misclassify screenshots as unrestricted uploads.
Convert an initial source refusal to explicit unavailable text before counting;
after fitting, refuse rather than replace pixels or refresh capture consent.

Original capture time and two-hour expiry are fixed before capture starts, with a
private monotonic deadline too. Ingest, retry, resume, copy and fork cannot renew
them. Identical pixels can have independently expiring observations. Existing
target, machine, disclosure, policy binding, halt, pause and cleanup state remains
owned by the tool/host; media handling creates no capture or input authority.
This source base adds no native episode claims that it cannot establish.

Call `computer_use.prepare_request` **before generic hydration and counting**:
it applies `max_inline_screenshots` newest-first to genuine owner-issued screenshot
refs on a private canonical copy, preserving other producers' media and metadata.
Count/send must retain that same effective projection. The generic host, not a
second legacy provider wrapper, owns hydration and its private transport witnesses.
Historical marker projection remains available through the explicit preparation
capability. Without generic media, a mounted compatible legacy reader can still
use the established private marker path. Without either reader, new screenshots
return useful unavailable text and retain no fallback pixels.

### Historical screenshot recognition

`amplifier_module_hook_computer_use.legacy_media` exposes
`recognize_legacy_screenshot_marker(decoded_value)`. The host's existing bounded,
allowlisted owner parser supplies a decoded exact `{__amplifier_computer_use__: 1,
text: str, images: [nonempty_str, ...]}` mapping. A recognized marker returns a
`held` / `missing-original-capture-receipt` disposition, with `origin: None` and
zero converted pixels. Unknown versions/fields, wrappers and strings return None;
the host must retain its unknown-format protection, not infer unrestricted media.
The adapter does not parse arbitrary prose/JSON strings or open any image path.

The original legacy writer never persisted capture creation/expiry or its private
issuance witness. `legacy_screenshot_origin_for(snapshot)` therefore always
returns None for historical source-SHA/record/image-index lookups. Event dates,
file times, filename UUIDs and pixel digests cannot reconstruct that missing
receipt. Old records remain protected and unchanged; neither optimization nor
this adapter renews screen consent. Prospective structured refs follow their
existing media-store provenance instead.

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
Fresh live provider wire qualification is represented separately by the bounded
neutral qualification fixtures and their active expiry/package-binding checks.
The live native cases are screenshot-only, not general-operation or platform proof.