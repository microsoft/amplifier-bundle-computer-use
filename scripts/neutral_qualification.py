"""Test-only neutral evidence recorder/consumer. Hashes bind bytes, not live truth.

Provider owners supply native declaration/result/replay receipts; this module never
inspects protocol fields. Importing it does not initialize a session or a display.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata as metadata
import json
import re
import sysconfig
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

MAX_AGE_DAYS = 30
SCENARIOS = {
    "session": {
        "canonical_capture",
        "marker_recognition",
        "image_projection",
        "adjacent_history",
        "caller_purity",
        "identity_unchanged",
        "cleanup",
    },
    "provider": {"native_declaration", "native_result", "native_replay"},
    "policy": {
        "read_only_zero_input",
        "no_approval_zero_input",
        "local_approved_input_capture",
    },
    "offline": {"installed_sdk", "native_composition"},
}
IDENTITY_ROLES = {"core", "provider", "sdk", "tool", "hook", "loop", "context"}
HEX = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")


class QualificationError(ValueError):
    """Unqualified, missing or unbound evidence; never a warning/skip."""


def require(condition, message):
    if not condition:
        raise QualificationError(message)


def object_keys(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), f"malformed {label}")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_manifest(root, paths):
    root = Path(root).resolve()
    return {Path(p).relative_to(root).as_posix(): sha256(p) for p in sorted(paths)}


def production_manifest(root):
    root = Path(root).resolve()
    return file_manifest(root, (root / "modules").rglob("*.py"))


def script_manifest(root):
    root = Path(root).resolve()
    return file_manifest(
        root,
        [
            root / "scripts" / name
            for name in ("neutral_qualification.py", "qualify_neutral_session.py")
        ],
    )


def safe_path(root, relative):
    require(isinstance(relative, str) and bool(relative), "missing artifact path")
    p = PurePosixPath(relative)
    require(
        not p.is_absolute()
        and ".." not in p.parts
        and "\\" not in relative
        and ":" not in relative
        and p.as_posix() == relative,
        "unsafe artifact path",
    )
    root = Path(root).resolve()
    path = root.joinpath(*p.parts).resolve()
    require(
        path.is_relative_to(root) and path != root and path.is_file(),
        "missing or escaped artifact",
    )
    return path


def artifact(root, relative, kind):
    path = safe_path(root, relative)
    return {"kind": kind, "path": relative, "sha256": sha256(path)}


def package_identity(distribution, module, *, installed=True):
    """Normal metadata and actually imported Python bytes; no provider-private API."""
    package = importlib.import_module(module)
    path = Path(package.__file__).resolve()
    if installed:
        roots = {Path(sysconfig.get_path(k)).resolve() for k in ("purelib", "platlib")}
        require(
            any(path.is_relative_to(root) for root in roots),
            "module is not normal installed code",
        )
    dist = metadata.distribution(distribution)
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    require(
        not direct.get("dir_info", {}).get("editable", False) or not installed,
        "editable runtime dependency",
    )
    revision = direct.get("vcs_info", {}).get("commit_id")
    files = list(path.parent.rglob("*.py")) if path.name == "__init__.py" else [path]
    return {
        "distribution": distribution,
        "module": module,
        "version": dist.version,
        "revision": revision,
        "manifest": file_manifest(path.parent, files),
    }


def manifest_check(value):
    require(isinstance(value, dict) and bool(value), "missing code manifest")
    for path, digest in value.items():
        require(
            isinstance(path, str)
            and not PurePosixPath(path).is_absolute()
            and ".." not in PurePosixPath(path).parts
            and "\\" not in path
            and ":" not in path,
            "unsafe manifest path",
        )
        require(
            isinstance(digest, str) and HEX.fullmatch(digest),
            "malformed manifest digest",
        )


def identity_check(identities):
    object_keys(identities, IDENTITY_ROLES, "identity roles")
    for value in identities.values():
        object_keys(
            value,
            {"distribution", "module", "version", "revision", "manifest"},
            "identity",
        )
        for name in ("distribution", "module", "version"):
            require(
                isinstance(value[name], str) and bool(value[name]),
                "missing package identity",
            )
        require(
            value["revision"] is None
            or (
                isinstance(value["revision"], str)
                and REVISION.fullmatch(value["revision"])
            ),
            "bad package revision",
        )
        manifest_check(value["manifest"])


def timestamp(value, now):
    require(isinstance(value, str), "malformed timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QualificationError("malformed timestamp") from exc
    require(parsed.tzinfo is not None, "timestamp lacks timezone")
    require(
        timedelta(0) <= now - parsed <= timedelta(days=MAX_AGE_DAYS),
        "future or stale evidence",
    )
    return parsed


def binding_key(binding):
    return (binding["provider"], binding["profile"], binding["mode"])


def binding_check(binding):
    object_keys(
        binding, {"provider", "profile", "mode", "identities", "source"}, "binding"
    )
    require(
        isinstance(binding["provider"], str) and bool(binding["provider"]),
        "missing opaque provider id",
    )
    require(binding["profile"] in {"floor", "current"}, "wrong profile")
    require(
        binding["mode"] in {"live", "offline"}
        and (binding["profile"] != "floor" or binding["mode"] == "offline"),
        "floor is offline-only",
    )
    identity_check(binding["identities"])
    manifest_check(binding["source"])
    for role in ("tool", "hook"):
        package = binding["identities"][role]
        prefix = f"modules/{role}-computer-use/{package['module']}/"
        actual = {
            p[len(prefix) :]: h
            for p, h in binding["source"].items()
            if p.startswith(prefix)
        }
        require(
            actual == package["manifest"], "loaded production code not bound to source"
        )


def receipt(
    binding,
    owner,
    started_at,
    finished_at,
    status,
    scenarios,
    captures=(),
    *,
    observations=None,
    evidence_class=None,
):
    """Build an envelope only from supplied observations; does not grant PASS."""
    return {
        "schema": 1,
        "owner": owner,
        "binding": binding,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "scenarios": scenarios,
        "captures": list(captures),
        "observations": observations,
        "evidence_class": evidence_class or binding["mode"],
    }


def finite(value, label, *, positive=False):
    require(
        type(value) in (int, float)
        and value == value
        and abs(value) != float("inf")
        and value >= 0
        and (not positive or value > 0),
        f"invalid {label}",
    )


def public_bounds_check(value):
    object_keys(
        value,
        {
            "limits",
            "calls",
            "deadline_seconds",
            "elapsed_seconds",
            "configured",
            "effective_wire",
            "count_policy",
        },
        "public bounds",
    )
    for mapping in (value["limits"], value["calls"]):
        object_keys(
            mapping, {"complete", "request_budget", "estimate"}, "public call counts"
        )
        require(
            all(type(v) is int and v >= 0 for v in mapping.values()),
            "invalid public call count",
        )
    require(
        all(value["calls"][k] <= value["limits"][k] for k in value["calls"]),
        "public quota exceeded",
    )
    require(value["calls"]["complete"] >= 2, "no adjacent generation observations")
    finite(value["deadline_seconds"], "deadline", positive=True)
    finite(value["elapsed_seconds"], "elapsed")
    require(
        value["elapsed_seconds"] <= value["deadline_seconds"],
        "execution deadline exceeded",
    )
    require(
        value["configured"] == {"max_retries": 0, "auto_continue": False},
        "wrong requested generation configuration",
    )
    require(
        value["effective_wire"] == "UNKNOWN",
        "public counts cannot attest wire settings",
    )
    require(
        isinstance(value["count_policy"], dict) and bool(value["count_policy"]),
        "missing opaque provider count policy",
    )


def observations_check(owner, value, captures):
    if owner == "session":
        object_keys(
            value,
            {"public_bounds", "cycles", "owned_display", "cleanup", "supervision"},
            "session observations",
        )
        public_bounds_check(value["public_bounds"])
        supervision = value["supervision"]
        object_keys(
            supervision,
            {
                "worker_pid",
                "process_group",
                "deadline_seconds",
                "elapsed_seconds",
                "timed_out",
                "group_absent",
            },
            "external supervision",
        )
        require(
            type(supervision["worker_pid"]) is int
            and supervision["worker_pid"] > 0
            and supervision["process_group"] == supervision["worker_pid"]
            and supervision["timed_out"] is False
            and supervision["group_absent"] is True,
            "unproved external supervision",
        )
        finite(supervision["deadline_seconds"], "supervisor deadline", positive=True)
        finite(supervision["elapsed_seconds"], "supervisor elapsed")
        require(
            supervision["elapsed_seconds"]
            <= supervision["deadline_seconds"]
            == value["public_bounds"]["deadline_seconds"],
            "overall supervision deadline exceeded",
        )
        object_keys(
            value["owned_display"],
            {"pid", "process_group", "display", "scope"},
            "owned display",
        )
        for k in ("pid", "process_group"):
            require(
                type(value["owned_display"][k]) is int
                and value["owned_display"][k] > 0,
                "missing measured process ownership",
            )
        require(
            re.fullmatch(r":\d+", value["owned_display"]["display"] or "")
            and value["owned_display"]["scope"] == "owned-X11-only",
            "ambiguous desktop",
        )
        require(
            value["cleanup"]
            == {
                "session": "closed",
                "backend": "closed",
                "tk": "closed",
                "xvfb": "absent",
            },
            "unproved cleanup",
        )
        require(
            isinstance(value["cycles"], list) and len(value["cycles"]) == 2,
            "missing adjacent cycles",
        )
        hashes = {c["sha256"] for c in captures}
        prior = set()
        for cycle in value["cycles"]:
            object_keys(
                cycle,
                {"captures", "projected", "recognized", "prior_history_retained"},
                "cycle",
            )
            require(
                isinstance(cycle["captures"], list)
                and bool(cycle["captures"])
                and set(cycle["captures"]) <= hashes,
                "unbound cycle captures",
            )
            require(
                isinstance(cycle["projected"], list)
                and set(cycle["captures"]) <= set(cycle["projected"]),
                "missing projected pixels",
            )
            require(
                cycle["recognized"] is True
                and cycle["prior_history_retained"] is True
                and prior <= set(cycle["projected"]),
                "marker/history not observed",
            )
            prior.update(cycle["captures"])
        require(prior == hashes, "capture cycle mismatch")
    elif owner == "provider":
        object_keys(
            value,
            {
                "observer",
                "generation",
                "count",
                "generation_retries",
                "auto_continue",
                "count_policy",
                "evidence",
            },
            "provider-owned observations",
        )
        require(
            isinstance(value["observer"], str) and bool(value["observer"]),
            "missing independent provider observer",
        )
        require(
            value["generation_retries"] == 0
            and type(value["generation_retries"]) is int
            and value["auto_continue"] is False,
            "missing effective generation controls",
        )
        for kind in ("generation", "count"):
            object_keys(
                value[kind],
                {
                    "http_calls",
                    "maximum_http_calls",
                    "attempt_timeout_seconds",
                    "retry_outcomes",
                },
                "wire bounds",
            )
            row = value[kind]
            require(
                type(row["http_calls"]) is int
                and type(row["maximum_http_calls"]) is int
                and 0 < row["http_calls"] <= row["maximum_http_calls"],
                "unknown or exceeded actual HTTP bound",
            )
            finite(row["attempt_timeout_seconds"], "wire timeout", positive=True)
            require(
                isinstance(row["retry_outcomes"], list) and bool(row["retry_outcomes"]),
                "missing observed retry outcomes",
            )
        require(
            isinstance(value["count_policy"], dict) and bool(value["count_policy"]),
            "missing provider-owned count policy",
        )
        require(
            isinstance(value["evidence"], list) and bool(value["evidence"]),
            "missing independent protocol evidence",
        )
    elif owner == "policy":
        object_keys(
            value,
            {
                "read_only_inputs",
                "unapproved_inputs",
                "approved_inputs",
                "final_capture",
            },
            "policy observations",
        )
        require(
            type(value["read_only_inputs"]) is int
            and value["read_only_inputs"] == 0
            and type(value["unapproved_inputs"]) is int
            and value["unapproved_inputs"] == 0,
            "refusal delivered input",
        )
        require(
            type(value["approved_inputs"]) is int
            and value["approved_inputs"] > 0
            and value["final_capture"] in {c["sha256"] for c in captures},
            "missing approved input/final capture",
        )
    else:
        object_keys(
            value, {"tests", "failed", "skipped", "evidence"}, "offline observations"
        )
        require(
            type(value["tests"]) is int
            and value["tests"] > 0
            and type(value["failed"]) is int
            and value["failed"] == 0
            and type(value["skipped"]) is int
            and value["skipped"] == 0,
            "offline checks incomplete",
        )
        require(
            isinstance(value["evidence"], list) and bool(value["evidence"]),
            "missing offline run evidence",
        )


def record(rows, producer, source, *, now=None):
    """Preserve failures. Validation against independent expectations is separate."""
    now = now or datetime.now(timezone.utc)
    passed = bool(rows) and all(row.get("status") == "PASS" for row in rows)
    return {
        "schema": 1,
        "kind": "neutral-qualification",
        "producer": producer,
        "source": source,
        "created_at": now.isoformat(),
        "status": "PASS" if passed else "FAIL",
        "rows": rows,
    }


def reference_check(ref, root):
    object_keys(ref, {"kind", "path", "sha256"}, "artifact reference")
    require(
        isinstance(ref["sha256"], str) and HEX.fullmatch(ref["sha256"]),
        "bad artifact digest",
    )
    path = safe_path(root, ref["path"])
    require(sha256(path) == ref["sha256"], "artifact hash mismatch")
    return path


def load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: require(False, "nonfinite JSON"),
        )
    except (OSError, ValueError) as exc:
        raise QualificationError("missing or malformed evidence JSON") from exc


def validate(document, expected, now=None, *, evidence_root, allow_test_controls=False):
    """Fail closed. Expected bindings come from independently qualified installed rows.

    A cryptographic digest cannot certify a live run. The independent provider,
    Session and policy receipts are mandatory, not HTTP/model metadata substitutes.
    """
    now = now or datetime.now(timezone.utc)
    object_keys(expected, {"producer", "source", "bindings"}, "expectations")
    object_keys(
        document,
        {"schema", "kind", "producer", "source", "created_at", "status", "rows"},
        "qualification",
    )
    require(
        type(document["schema"]) is int
        and document["schema"] == 1
        and document["kind"] == "neutral-qualification",
        "wrong evidence schema",
    )
    object_keys(document["producer"], {"revision", "scripts"}, "producer")
    require(
        isinstance(document["producer"]["revision"], str)
        and REVISION.fullmatch(document["producer"]["revision"]),
        "bad producer revision",
    )
    manifest_check(document["producer"]["scripts"])
    manifest_check(document["source"])
    require(
        document["producer"] == expected["producer"]
        and document["source"] == expected["source"],
        "source or producer mismatch",
    )
    created = timestamp(document["created_at"], now)
    require(document["status"] == "PASS", "qualification did not pass")
    require(
        isinstance(document["rows"], list)
        and isinstance(expected["bindings"], list)
        and bool(expected["bindings"]),
        "missing qualification rows",
    )
    wanted = {}
    for binding in expected["bindings"]:
        binding_check(binding)
        key = binding_key(binding)
        require(
            key not in wanted and binding["source"] == expected["source"],
            "duplicate or unbound expectation",
        )
        wanted[key] = binding
    providers = {k[0] for k in wanted}
    require(
        set(wanted)
        == {
            (p, profile, mode)
            for p in providers
            for profile, mode in (
                ("current", "live"),
                ("current", "offline"),
                ("floor", "offline"),
            )
        },
        "incomplete profile matrix",
    )
    observed = set()
    for row in document["rows"]:
        object_keys(row, {"binding", "status", "artifacts"}, "row")
        binding_check(row["binding"])
        key = binding_key(row["binding"])
        require(
            key not in observed and key in wanted and row["binding"] == wanted[key],
            "wrong or duplicate profile/identity binding",
        )
        observed.add(key)
        require(
            row["status"] == "PASS" and isinstance(row["artifacts"], list),
            "failed qualification row",
        )
        owners = {"session", "provider", "policy"} if key[2] == "live" else {"offline"}
        seen = set()
        count_policies = []
        for ref in row["artifacts"]:
            path = reference_check(ref, evidence_root)
            require(
                ref["kind"] in owners and ref["kind"] not in seen,
                "wrong or duplicate receipt owner",
            )
            seen.add(ref["kind"])
            data = load_json(path)
            object_keys(
                data,
                {
                    "schema",
                    "owner",
                    "binding",
                    "started_at",
                    "finished_at",
                    "status",
                    "scenarios",
                    "captures",
                    "observations",
                    "evidence_class",
                },
                "receipt",
            )
            require(
                data["evidence_class"] == row["binding"]["mode"]
                or (allow_test_controls and data["evidence_class"] == "test-control"),
                "not genuine live/offline evidence",
            )
            require(
                type(data["schema"]) is int
                and data["schema"] == 1
                and data["owner"] == ref["kind"]
                and data["binding"] == row["binding"],
                "receipt binding mismatch",
            )
            start, end = (
                timestamp(data["started_at"], now),
                timestamp(data["finished_at"], now),
            )
            require(
                start <= end <= created and data["status"] == "PASS",
                "failed or disordered receipt",
            )
            object_keys(data["scenarios"], SCENARIOS[ref["kind"]], "required scenarios")
            require(
                all(v == "PASS" for v in data["scenarios"].values()),
                "missing/failed required observation",
            )
            require(isinstance(data["captures"], list), "bad capture references")
            for capture in data["captures"]:
                captured = reference_check(capture, evidence_root)
                require(
                    capture["kind"] == "capture"
                    and captured.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
                    "not a PNG artifact",
                )
            if ref["kind"] == "session":
                require(
                    len(data["captures"]) >= 2
                    and len({v["sha256"] for v in data["captures"]}) >= 2,
                    "missing distinct adjacent captures",
                )
            observations_check(ref["kind"], data["observations"], data["captures"])
            if ref["kind"] in {"session", "provider"}:
                bounds = data["observations"].get("public_bounds", data["observations"])
                count_policies.append(bounds["count_policy"])
            for evidence in data["observations"].get("evidence", []):
                reference_check(evidence, evidence_root)
        require(seen == owners, "missing independent required receipt")
        require(
            not count_policies
            or (len(count_policies) == 2 and count_policies[0] == count_policies[1]),
            "provider count policy mismatch",
        )
    require(observed == set(wanted), "missing required qualification")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    consumer = sub.add_parser(
        "validate", help="no network/display; rejects absent or unbound proof"
    )
    consumer.add_argument("document", type=Path)
    consumer.add_argument("expected", type=Path)
    consumer.add_argument("--evidence-root", type=Path, required=True)
    producer = sub.add_parser(
        "record", help="assemble supplied sanitized rows, never invent observations"
    )
    producer.add_argument("rows", type=Path)
    producer.add_argument("expected", type=Path)
    producer.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    expected = load_json(args.expected)
    if args.command == "validate":
        validate(load_json(args.document), expected, evidence_root=args.evidence_root)
        print("PASS: bound neutral qualification; floor remains offline-only")
    else:
        document = record(
            load_json(args.rows), expected["producer"], expected["source"]
        )
        args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(document["status"] + ": recorded only; independent validation required")


if __name__ == "__main__":
    main()
