"""Opt-in local OCR observations for bounded selectors; execution stays in computer.

OCR clicks, typing into current focus, bounded key presses and scrolling are exposed.
OCR regions are not accessibility controls. Before input starts, the pixel
fingerprint conservatively rejects screen changes. Typing's own changes are
allowed after that check; authority and coexistence guards remain per-keystroke.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import csv
import hashlib
import io
import json
import subprocess
import time
from contextvars import ContextVar

from amplifier_core import ToolResult
from PIL import Image

CURRENT_PROOF = ContextVar("computer_observed_proof", default=None)


class StaleObservation(RuntimeError):
    pass


def check_observed_proof(binding):
    """Called by the canonical executor after approval/presence waits, before input."""
    proof = CURRENT_PROOF.get()
    if proof is not None:
        proof["owner"].validate(proof, binding)


def observed_typing_guard(guard, binding):
    """Wrap only observed typing, preserving the ordinary guard unchanged."""
    proof = CURRENT_PROOF.get()
    if proof is None:
        return guard
    return _ObservedTypingGuard(guard, binding, proof)


class _ObservedTypingGuard:
    def __init__(self, guard, binding, proof):
        self.guard = guard
        self.binding = binding
        self.proof = proof
        self.started = False

    def before_event(self, **kwargs):
        # The canonical guard may wait for human presence or target availability.
        # Validate only AFTER it returns and immediately before backend input.
        self.guard.before_event(**kwargs)
        if not self.started:
            self.proof["owner"].validate(self.proof, self.binding)
        else:
            try:
                self.proof["owner"].validate_authority(self.proof, self.binding)
            except StaleObservation as exc:
                # A prefix may already have landed: report unknown, never stale
                # (which could invite a retry of the entire string).
                raise RuntimeError(
                    "Observed typing interrupted after input began"
                ) from exc
        self.started = True

    def after_event(self):
        self.guard.after_event()


class ObservedControl:
    def __init__(self, computer):
        self.computer = computer
        self.observations = {}
        self.lock = asyncio.Lock()

    def capture(self, binding):
        if binding.is_remote or binding.backend.name != "linux-x11":
            raise RuntimeError(
                "Observed computer spike currently requires a local Linux X11 target"
            )
        if self.computer._binding is not binding:
            raise StaleObservation("Target changed")
        _, encoded = self.computer._run("screenshot", {}, binding=binding)
        raw = base64.b64decode(encoded)
        with Image.open(io.BytesIO(raw)) as img:
            fingerprint = hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()
            width, height = img.size
        # Refuse a capture whose dimensions differ from executor model geometry.
        if (width, height) != (
            binding.display.model_width,
            binding.display.model_height,
        ):
            raise RuntimeError(
                "Capture geometry does not match the execution coordinate space"
            )
        return raw, fingerprint, width, height

    def observe(self, binding):
        raw, digest, width, height = self.capture(binding)
        # Automatic segmentation retains colored button labels that sparse-text
        # segmentation can discard as graphics. Targets remain observed regions.
        output = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "3", "tsv"],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        ).stdout.decode()
        groups = {}
        for row in csv.DictReader(io.StringIO(output), delimiter="\t"):
            if (
                row.get("level") != "5"
                or not row.get("text", "").strip()
                or float(row["conf"]) < 60
            ):
                continue
            group = tuple(row[k] for k in ("block_num", "par_num", "line_num"))
            groups.setdefault(group, []).append(row)
        elements, candidates = [], []
        for rows in list(groups.values())[:120]:
            left, top = (
                min(int(r["left"]) for r in rows),
                min(int(r["top"]) for r in rows),
            )
            right = max(int(r["left"]) + int(r["width"]) for r in rows)
            bottom = max(int(r["top"]) + int(r["height"]) for r in rows)
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                continue
            name = " ".join(r["text"] for r in rows)[:240]
            identity = "ocr-" + str(len(elements))
            elements.append(
                {
                    "id": identity,
                    "name": name,
                    "rect": [left, top, right - left, bottom - top],
                    "source": "ocr-region",
                }
            )
            candidates.append(
                {
                    "id": identity + ":click",
                    "operation": "CLICK",
                    "name": name,
                    "target": identity,
                }
            )
        candidates.append(
            {
                "id": "focused:type",
                "operation": "TYPE_TEXT",
                "name": "Type into the currently focused field; first focus the intended field",
            }
        )
        candidates.extend(
            {"id": "key:" + key, "operation": "KEY", "name": "Press " + key, "key": key}
            for key in (
                "Tab",
                "Return",
                "Escape",
                "ctrl+a",
                "ctrl+s",
                "BackSpace",
                "Down",
                "Up",
            )
        )
        candidates.extend(
            {
                "id": "scroll:" + direction,
                "operation": "SCROLL",
                "name": "Scroll " + direction,
                "direction": direction,
            }
            for direction in ("up", "down")
        )
        candidates.extend(
            {"id": x.lower(), "operation": x} for x in ("WAIT", "DONE", "BLOCKED")
        )
        state = {
            "text": "\n".join(e["name"] for e in elements),
            "elements": elements,
            "width": width,
            "height": height,
            "source": "local-x11-ocr",
            "pixel_sha256": digest,
        }
        revision = hashlib.sha256(
            json.dumps(state, sort_keys=True).encode()
        ).hexdigest()
        observed = {
            "revision": revision,
            "state": state,
            "candidates": candidates,
            "truncated": len(groups) > 120,
            "created_at": time.monotonic(),
        }
        self.observations[revision] = {
            "observation": observed,
            "binding": binding,
            "digest": digest,
        }
        while len(self.observations) > 16:
            self.observations.pop(next(iter(self.observations)))
        return copy.deepcopy(observed)

    def validate_authority(self, proof, binding):
        if (
            not proof["active"]
            or binding is not proof["saved"]["binding"]
            or self.computer._binding is not binding
        ):
            raise StaleObservation("Observed action revoked or target changed")

    def validate(self, proof, binding):
        self.validate_authority(proof, binding)
        saved = proof["saved"]
        if (
            not proof.get("validated")
            and time.monotonic() - saved["observation"]["created_at"] > 15
        ):
            raise StaleObservation("Observation expired")
        if self.capture(binding)[1] != saved["digest"]:
            raise StaleObservation("Screen changed since selection")
        # Subsequent checks surround an exact-action approval wait. Always
        # recapture and compare pixels; do not expire solely while a human reads.
        proof["validated"] = True

    async def execute(self, arguments):
        async with self.lock:
            binding = self.computer._binding
            try:
                if arguments.get("action") == "observe":
                    await asyncio.to_thread(self.computer._ensure_announced)
                    binding = self.computer._binding
                    observed = await asyncio.to_thread(self.observe, binding)
                    return ToolResult(success=True, output=observed)
                if set(arguments) - {"action", "revision", "candidate", "text"}:
                    raise ValueError(
                        "Observed desktop actions accept only revision and candidate"
                    )
                saved = self.observations.get(arguments.get("revision"))
                candidate = (
                    next(
                        (
                            c
                            for c in saved["observation"]["candidates"]
                            if c["id"] == arguments.get("candidate")
                        ),
                        None,
                    )
                    if saved
                    else None
                )
                if candidate is None:
                    raise StaleObservation("Candidate was not observed")
                if candidate["operation"] == "WAIT":
                    canonical = {"action": "wait", "duration": 0.2}
                elif candidate["operation"] == "TYPE_TEXT":
                    text = arguments.get("text")
                    if not isinstance(text, str) or len(text) > 10000:
                        raise ValueError("Invalid text")
                    canonical = {"action": "type", "text": text}
                elif candidate["operation"] == "KEY":
                    canonical = {"action": "key", "text": candidate["key"]}
                elif candidate["operation"] == "SCROLL":
                    canonical = {
                        "action": "scroll",
                        "scroll_direction": candidate["direction"],
                        "scroll_amount": 3,
                    }
                elif candidate["operation"] == "CLICK":
                    target = next(
                        e
                        for e in saved["observation"]["state"]["elements"]
                        if e["id"] == candidate["target"]
                    )
                    x, y, w, h = target["rect"]
                    canonical = {
                        "action": "left_click",
                        "coordinate": [round(x + w / 2), round(y + h / 2)],
                    }
                else:
                    raise ValueError(
                        "DONE/BLOCKED are controller outcomes, not desktop actions"
                    )
                proof = {"owner": self, "saved": saved, "active": True}
                await asyncio.to_thread(self.validate, proof, binding)
                token = CURRENT_PROOF.set(proof)
                try:
                    # Canonical validation, exact-action approval, announcement,
                    # read-only checks, presence/halt, target pin and ledger all run.
                    result = await self.computer.execute(canonical)
                finally:
                    proof["active"] = False
                    CURRENT_PROOF.reset(token)
                if result.success:
                    return ToolResult(success=True, output={"status": "completed"})
                error = result.error if isinstance(result.error, dict) else {}
                status = (
                    "stale"
                    if error.get("type") == "StaleObservation"
                    else "denied"
                    if error.get("type") == "PermissionError"
                    else "unknown"
                )
                return ToolResult(
                    success=True,
                    output={
                        "status": status,
                        "reason": "canonical_computer_"
                        + error.get("type", "unconfirmed"),
                    },
                )
            except asyncio.CancelledError:
                raise
            except StaleObservation:
                return ToolResult(
                    success=True,
                    output={"status": "stale", "reason": "observation_changed"},
                )
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error={
                        "type": type(exc).__name__,
                        "message": "Observed computer operation unavailable; use the ordinary computer tool for recovery.",
                    },
                )
