# tool-computer-use

Exports explicit `computer_use.version=1` intent via `native_tool_spec`/ToolSpec extras.
Intent contains local screenshot/input operations, confirmed local limitations and
cached real screenshot-pixel dimensions when known. Window/geometry helpers also
remain available as ordinary `desktop` functions. No model or vendor wire facts live here.

Executes existing flat canonical calls and strict `{version: 1, actions: [...],
capture_after: bool}` batches. The whole batch is validated before disclosure/input;
durations are 0–60 seconds, coordinates screenshot pixels and scroll amounts wheel
notches. Writes pass read-only, approval, coexistence, halt and target-binding guards.
Cancellation may leave an uncertain worker in flight, but never schedules later input;
its band token is released only when that worker settles. Known keypress progress is
retained, without rollback/replay claims.

A successful `capture_after` batch returns a full final screenshot, reusing only a
terminal full screenshot. Earlier screenshots or cropped zooms are not sufficient.
Errors retain partial progress and are never disguised by recapture. ToolResult and
`__amplifier_computer_use__` text/images markers remain the result boundary.

Providers own model/endpoint support, native declarations, vendor unit translation,
safety requirements and replay. The companion hook projects copied generic histories.
Physical scaling, monitor origins, private capture storage, held-input cleanup and SSH
transport remain local mechanisms. Fresh live provider wire qualification is **UNMET
under Root**; retained historical evidence does not qualify neutral v1.
