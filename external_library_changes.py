# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Detect whether external library directories changed since the last recorded snapshot.

This is the *condition* half of a dagu DAG around `external_library_scan.py`: `--check` walks
the trees (recursively, so a change deep inside the library is found), compares them against
the state file and reports whether anything changed since the last `--record`. The DAG runs the
scan only when it did, and records the new state only after the scan succeeded - so a failed
scan can never swallow a change.

Modes:
    --check   read-only. Compares the trees with the state file and prints the verdict as a
              trailing `changed=yes` / `changed=no` line. Nothing is written, not even when
              the state file is missing.
    --record  writes the current trees as the new reference state (dry run unless --apply).
              Run it *after* a successful scan.
    --status  reads the state file only (no tree walk): paths, timestamps, file counts.

The verdict is deliberately *not* in the exit code: dagu marks a step that exits non-zero as
failed, so the DAG gates on the `changed=` line (dagu `output:` + `preconditions`), while this
exit code stays free for real errors. In a shell, use `-q` and grep instead:
`external_library_changes.py --check -q | grep -q '^changed=yes'`.

Exit codes: 0 the verdict was determined (changed or not), 1 the run failed (a root could not
be read, the state file could not be written), 2 configuration error (missing or unusable
arguments, a --path that is not a directory).

What is compared per file: size and mtime in nanoseconds; `--list` names the reason (resized vs.
same size, newer mtime). No file content is read and no hash is computed. Symlinks are never
followed - they are tracked as leaves, so retargeting a link is a change while changes behind it
are invisible. Directories are tracked by name only, which is what makes added and removed
*empty* directories visible.

Ignored: the noise of NAS and OS folders (`@eaDir`, `.DS_Store`, `Thumbs.db`, `*.tmp`) plus
every `--exclude` pattern, matched with shell wildcards against the path relative to the root and
against the bare file name. The state file itself is ignored, even inside a watched tree.

A missing state file is not an error: the first `--check` reports `changed=yes` (reason
`no-state`), the DAG scans once and `--record --apply` creates the file. An unusable state file
(broken JSON, other tool/version, other paths) behaves the same way - fail open, one scan too
many beats a missed change - and only logs a warning.

Usage:
    uv run external_library_changes.py --path /srv/photos --state photos.state.json --check
    uv run external_library_changes.py --path /srv/photos --state photos.state.json --check -q
    uv run external_library_changes.py --path /srv/photos --state photos.state.json --check --list --json
    uv run external_library_changes.py --path /srv/photos --state photos.state.json --record --apply
    uv run external_library_changes.py --state photos.state.json --status

    # dagu: the scan step only starts when the detect step said changed=yes
    steps:
      - id: detect
        run: uv run external_library_changes.py --path /srv/photos --state photos.state.json --check
        output: CHANGED
      - id: scan
        depends: detect
        preconditions:
          - condition: ${CHANGED}
            expected: changed=yes
        run: uv run external_library_scan.py --apply --wait
      - id: record
        depends: scan
        run: uv run external_library_changes.py --path /srv/photos --state photos.state.json --record --apply

No Immich API and no external dependency is involved, so this script runs without credentials.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
from pathlib import Path

from immich_api import Logger

REPORT_TOOL = "external_library_changes"
STATE_VERSION = 1
DEFAULT_EXCLUDES = ("@eaDir", ".DS_Store", "Thumbs.db", "*.tmp")  # NAS/OS noise, always ignored
MAX_LISTED = 20  # detail entries per change kind, for --list (human output and JSON report)
DETAIL_KEYS = ("added", "removed", "modified", "dirs_added", "dirs_removed")
DETAIL_MARKERS = {
    "added": "+",
    "removed": "-",
    "modified": "~",
    "dirs_added": "+ dir",
    "dirs_removed": "- dir",
}


def normalize_roots(values: list[str]) -> list[str]:
    """Absolute, de-duplicated roots in the order they were given."""
    roots: list[str] = []
    for value in values:
        root = os.path.abspath(os.path.expanduser(value))
        if root not in roots:
            roots.append(root)
    return roots


def normalize_excludes(patterns: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*DEFAULT_EXCLUDES, *patterns)))


def is_excluded(relative: str, name: str, excludes: tuple[str, ...]) -> bool:
    """Shell wildcards, matched against the path below the root and against the bare name."""
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) for pattern in excludes)


def walk_tree(root: str, excludes: tuple[str, ...], log: Logger, skip: frozenset[str] = frozenset()) -> dict:
    """Collect `{relative path: (size, mtime_ns)}` for every file plus every directory.

    Uses `os.scandir` and `DirEntry.stat`, so no file is opened. Unreadable directories are
    logged and skipped; `root_failed` says whether the root itself was unreadable, which the
    caller has to treat as a hard error (an unreadable root would otherwise look like "every
    file was deleted"). `skip` holds normalized absolute paths that are ignored, which is how
    the state file stays invisible even when it lives inside a watched tree.
    """
    files: dict[str, tuple[int, int]] = {}
    dirs: list[str] = []
    unreadable = 0
    root_failed = False
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as exc:
            unreadable += 1
            root_failed = root_failed or directory == root
            log(f"warning: cannot read {directory}: {exc}", force=True)
            continue
        for entry in entries:
            if os.path.normcase(entry.path) in skip:
                continue
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if is_excluded(relative, entry.name, excludes):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(relative)
                    pending.append((entry.path, relative))
                    continue
                info = entry.stat(follow_symlinks=False)  # symlinks stay leaves, never followed
            except OSError as exc:
                unreadable += 1
                log(f"warning: cannot stat {entry.path}: {exc}", force=True)
                continue
            files[relative] = (int(info.st_size), int(info.st_mtime_ns))
    dirs.sort()
    return {"files": files, "dirs": dirs, "unreadable": unreadable, "root_failed": root_failed}


def normalize_skip(state_path: Path) -> frozenset[str]:
    """The state file and its temp sibling must never count as library content."""
    names = [str(state_path), f"{state_path}.tmp"]
    return frozenset(os.path.normcase(os.path.abspath(name)) for name in names)


def scan_roots(
    roots: list[str], excludes: tuple[str, ...], log: Logger, skip: frozenset[str]
) -> tuple[dict, list[str], int]:
    """Walk every root. Returns (snapshots by root, roots that failed, unreadable directories)."""
    snapshots: dict[str, dict] = {}
    failed: list[str] = []
    unreadable = 0
    for root in roots:
        snapshot = walk_tree(root, excludes, log, skip)
        unreadable += snapshot["unreadable"]
        if snapshot["root_failed"]:
            failed.append(root)
        snapshots[root] = {"files": snapshot["files"], "dirs": snapshot["dirs"]}
        log(f"walked {root}: {len(snapshot['files'])} files, {len(snapshot['dirs'])} dirs", debug=True)
    return snapshots, failed, unreadable


def state_snapshot(entry: dict) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Read one recorded path entry. A single corrupt record only costs one extra `changed`."""
    files: dict[str, tuple[int, int]] = {}
    for name, value in (entry.get("files") or {}).items():
        try:
            files[str(name)] = (int(value[0]), int(value[1]))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    dirs = [str(item) for item in (entry.get("dirs") or []) if isinstance(item, str)]
    return files, dirs


def load_state(path: Path, log: Logger) -> tuple[dict | None, str | None]:
    """Read the state file. Returns (state, problem), `problem` is None when it is usable."""
    if not path.exists():
        return None, "no-state"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log(f"warning: ignoring the unusable state file {path}: {exc}", force=True)
        return None, "state-unreadable"
    if not is_state_shaped(state):
        log(
            f"warning: ignoring the state file {path}: not a "
            f"{REPORT_TOOL} version {STATE_VERSION} state",
            force=True,
        )
        return None, "state-unreadable"
    return state, None


def is_state_shaped(state) -> bool:
    if not isinstance(state, dict) or state.get("tool") != REPORT_TOOL or state.get("version") != STATE_VERSION:
        return False
    paths = state.get("paths")
    if not isinstance(paths, list) or not paths:
        return False
    for entry in paths:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return False
        if not isinstance(entry.get("files"), dict) or not isinstance(entry.get("dirs"), list):
            return False
    return True


def recorded_paths(state: dict | None) -> set[str]:
    return {entry["path"] for entry in (state or {}).get("paths", [])}


def compare_roots(state: dict | None, snapshots: dict) -> dict:
    """One diff per root, or None where there is nothing to compare against."""
    recorded = {entry["path"]: entry for entry in (state or {}).get("paths", [])}
    diffs: dict[str, dict | None] = {}
    for root, snapshot in snapshots.items():
        entry = recorded.get(root)
        if entry is None:
            diffs[root] = None
            continue
        old_files, old_dirs = state_snapshot(entry)
        diffs[root] = diff_trees(old_files, snapshot["files"], old_dirs, snapshot["dirs"])
    return diffs


def diff_trees(
    old_files: dict[str, tuple[int, int]],
    new_files: dict[str, tuple[int, int]],
    old_dirs: list[str],
    new_dirs: list[str],
) -> dict:
    added = sorted(set(new_files) - set(old_files))
    removed = sorted(set(old_files) - set(new_files))
    common = set(old_files) & set(new_files)
    modified = sorted(rel for rel in common if old_files[rel] != new_files[rel])
    dirs_added = sorted(set(new_dirs) - set(old_dirs))
    dirs_removed = sorted(set(old_dirs) - set(new_dirs))
    counts = {
        "added": len(added),
        "removed": len(removed),
        "modified": len(modified),
        "dirs_added": len(dirs_added),
        "dirs_removed": len(dirs_removed),
    }
    counts["total"] = sum(counts.values())
    return {
        "counts": counts,
        "added": added,
        "removed": removed,
        "modified": modified,
        "dirs_added": dirs_added,
        "dirs_removed": dirs_removed,
        "sizes": {rel: [old_files[rel][0], new_files[rel][0]] for rel in modified},
    }


def total_counts(diffs: dict) -> dict:
    counts = {key: 0 for key in DETAIL_KEYS}
    counts["total"] = 0
    for diff in diffs.values():
        if not diff:
            continue
        for key, value in diff["counts"].items():
            counts[key] += value
    return counts


def details_of(diff: dict) -> dict:
    """Capped detail lists plus the number of entries left out, so nothing is silently cut."""
    details: dict = {}
    omitted: dict[str, int] = {}
    for key in DETAIL_KEYS:
        items = diff[key]
        details[key] = items[:MAX_LISTED]
        if len(items) > MAX_LISTED:
            omitted[key] = len(items) - MAX_LISTED
    if omitted:
        details["omitted"] = omitted
    return details


def display_path(root: str, relative: str) -> str:
    return os.path.join(root, *relative.split("/"))


def modification_note(diff: dict, relative: str) -> str:
    old_size, new_size = diff["sizes"][relative]
    if old_size != new_size:
        return f" ({old_size} -> {new_size} bytes)"
    return " (same size, newer mtime)"


def print_details(diffs: dict, out) -> None:
    for root, diff in diffs.items():
        if not diff:
            continue
        details = details_of(diff)
        omitted = details.get("omitted", {})
        for key in DETAIL_KEYS:
            for relative in details[key]:
                note = modification_note(diff, relative) if key == "modified" else ""
                out(f"  {DETAIL_MARKERS[key]} {display_path(root, relative)}{note}")
            if key in omitted:
                out(f"  {DETAIL_MARKERS[key]} ... and {omitted[key]} more")


def count_text(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def format_counts(counts: dict) -> str:
    parts = [f"{key}={counts.get(key, 0)}" for key in DETAIL_KEYS]
    return " ".join(parts)


def format_timestamp(value) -> str:
    """State timestamps are written as `2026-09-28T18:04:11+0200`."""
    text = str(value or "never")
    return text.replace("T", " ", 1) if "T" in text else text


def format_age(seconds: float) -> str:
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def timestamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))


def describe_state(path: Path, state: dict | None) -> str:
    if state is None:
        return f"state: {path} (no usable snapshot)"
    return (
        f"state: {path} (record {state.get('records', 0)}, "
        f"updated {format_timestamp(state.get('updated_at'))})"
    )


def build_state(roots: list[str], snapshots: dict, *, previous: dict | None, now: float) -> dict:
    previous = previous or {}
    return {
        "tool": REPORT_TOOL,
        "version": STATE_VERSION,
        "created_at": previous.get("created_at") or timestamp(now),
        "updated_at": timestamp(now),
        "updated_ts": now,
        "records": int(previous.get("records") or 0) + 1,
        "paths": [
            {"path": root, "files": snapshots[root]["files"], "dirs": snapshots[root]["dirs"]}
            for root in roots
        ],
    }


def write_state(path: Path, state: dict) -> None:
    """Atomic: the temp file sits next to the target, so os.replace stays on one filesystem."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_report(
    args,
    *,
    mode: str,
    state_path: Path,
    snapshots: dict,
    diffs: dict,
    counts: dict,
    state: dict | None,
    changed: bool,
    reason: str,
    applied: bool,
    unreadable: int,
    elapsed: float,
    generated_at: str,
) -> dict:
    report = {
        "tool": REPORT_TOOL,
        "generated_at": generated_at,
        "mode": mode,
        "applied": applied,
        "state": str(state_path),
        "changed": changed,
        "reason": reason,
        "previous": None
        if state is None
        else {
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "records": state.get("records"),
        },
        "paths": [
            {
                "path": root,
                "files": len(snapshot["files"]),
                "dirs": len(snapshot["dirs"]),
                "changes": dict(diffs[root]["counts"]) if diffs.get(root) else None,
            }
            for root, snapshot in snapshots.items()
        ],
        "changes": dict(counts),
        "unreadable": unreadable,
        "elapsed_seconds": round(elapsed, 3),
    }
    if args.list:
        report["details"] = {root: details_of(diff) for root, diff in diffs.items() if diff}
    return report


def run_check(args, roots, excludes, state_path: Path, logger: Logger, now) -> int:
    started = now()
    state, problem = load_state(state_path, logger)
    snapshots, failed, unreadable = scan_roots(roots, excludes, logger, normalize_skip(state_path))
    elapsed = now() - started

    if failed:
        for root in failed:
            logger(f"error: cannot read the directory {root}", force=True)
        return 1

    if state is None:
        changed, reason, diffs = True, problem or "no-state", {}
    elif recorded_paths(state) != set(snapshots):
        changed, reason, diffs = True, "paths-changed", {}
    else:
        diffs = compare_roots(state, snapshots)
        changes = total_counts(diffs)
        changed = changes["total"] > 0
        reason = "changed" if changed else "unchanged"
    counts = total_counts(diffs)

    def out(line: str) -> None:
        """Results go to stdout - unless stdout is reserved for the --json report."""
        if args.json:
            logger(line)
        else:
            print(line)

    def info(line: str) -> None:
        """Progress, so -q silences it (the verdict and the report are never silenced)."""
        if not args.quiet:
            out(line)

    info(describe_state(state_path, state))
    for root, snapshot in snapshots.items():
        info(
            f"watching: {root} "
            f"({count_text(len(snapshot['files']), 'file', 'files')}, "
            f"{count_text(len(snapshot['dirs']), 'dir', 'dirs')})"
        )
    if unreadable:
        info(f"skipped: {count_text(unreadable, 'unreadable directory', 'unreadable directories')}")
    if diffs:
        info(f"changes: {format_counts(counts)}")
    else:
        info(f"changes: not compared ({reason})")
    if args.list:
        print_details(diffs, out)
    out(f"changed={'yes' if changed else 'no'}")

    if args.json:
        print(
            json.dumps(
                build_report(
                    args,
                    mode="check",
                    state_path=state_path,
                    snapshots=snapshots,
                    diffs=diffs,
                    counts=counts,
                    state=state,
                    changed=changed,
                    reason=reason,
                    applied=False,
                    unreadable=unreadable,
                    elapsed=elapsed,
                    generated_at=timestamp(now()),
                ),
                indent=2,
            )
        )
    return 0


def run_record(args, roots, excludes, state_path: Path, logger: Logger, now) -> int:
    started = now()
    state, problem = load_state(state_path, logger)
    if state is None and problem == "state-unreadable":
        logger("warning: the previous state is lost, this record becomes the new baseline", force=True)
    snapshots, failed, unreadable = scan_roots(roots, excludes, logger, normalize_skip(state_path))
    elapsed = now() - started

    if failed:
        for root in failed:
            logger(f"error: cannot read the directory {root}, the state is left untouched", force=True)
        return 1

    diffs = {} if state is None or recorded_paths(state) != set(snapshots) else compare_roots(state, snapshots)
    counts = total_counts(diffs)
    changed = counts["total"] > 0
    files = sum(len(snapshot["files"]) for snapshot in snapshots.values())
    dirs = sum(len(snapshot["dirs"]) for snapshot in snapshots.values())

    def out(line: str) -> None:
        if args.json:
            logger(line)
        else:
            print(line)

    def info(line: str) -> None:
        if not args.quiet:
            out(line)

    info(describe_state(state_path, state))
    for root, snapshot in snapshots.items():
        info(
            f"watching: {root} "
            f"({count_text(len(snapshot['files']), 'file', 'files')}, "
            f"{count_text(len(snapshot['dirs']), 'dir', 'dirs')})"
        )
    if unreadable:
        info(f"skipped: {count_text(unreadable, 'unreadable directory', 'unreadable directories')}")
    if diffs:
        info(f"changes since the last record: {format_counts(counts)}")
    if args.list:
        print_details(diffs, out)

    if not args.apply:
        out(
            f"WOULD RECORD {count_text(files, 'file', 'files')}, {count_text(dirs, 'dir', 'dirs')} "
            f"in {count_text(len(snapshots), 'path', 'paths')} -> {state_path}"
        )
        out("DRY RUN: state not written (add --apply to write)")
        applied = False
    else:
        try:
            write_state(state_path, build_state(roots, snapshots, previous=state, now=now()))
        except OSError as exc:
            logger(f"error: cannot write the state file {state_path}: {exc}", force=True)
            return 1
        out(f"RECORDED {count_text(files, 'file', 'files')}, {count_text(dirs, 'dir', 'dirs')} -> {state_path}")
        applied = True

    if args.json:
        print(
            json.dumps(
                build_report(
                    args,
                    mode="record",
                    state_path=state_path,
                    snapshots=snapshots,
                    diffs=diffs,
                    counts=counts,
                    state=state,
                    changed=changed,
                    reason="recorded" if applied else "would-record",
                    applied=applied,
                    unreadable=unreadable,
                    elapsed=elapsed,
                    generated_at=timestamp(now()),
                ),
                indent=2,
            )
        )
    return 0


def run_status(args, state_path: Path, logger: Logger, now) -> int:
    state, problem = load_state(state_path, logger)

    def out(line: str) -> None:
        if args.json:
            logger(line)
        else:
            print(line)

    if state is None:
        if problem == "no-state":
            out(f"state: {state_path} (missing)")
            out("no snapshot yet: the first --check reports changed=yes, then --record --apply creates it")
            if args.json:
                print(json.dumps(build_report(
                    args,
                    mode="status",
                    state_path=state_path,
                    snapshots={},
                    diffs={},
                    counts=total_counts({}),
                    state=None,
                    changed=True,
                    reason="no-state",
                    applied=False,
                    unreadable=0,
                    elapsed=0.0,
                    generated_at=timestamp(now()),
                ), indent=2))
            return 0
        logger(f"error: {state_path} is not a usable state file", force=True)
        return 1

    age = now() - float(state.get("updated_ts") or now())
    out(f"state: {state_path}")
    out(f"tool: {state.get('tool')} (version {state.get('version')})")
    out(f"created: {format_timestamp(state.get('created_at'))}")
    out(f"updated: {format_timestamp(state.get('updated_at'))} (age {format_age(age)})")
    out(f"records: {state.get('records', 0)}")
    out(f"paths: {len(state['paths'])}")
    for entry in state["paths"]:
        files = len(entry.get("files") or {})
        dirs = len(entry.get("dirs") or [])
        out(
            f"  {entry['path']} - {count_text(files, 'file', 'files')}, "
            f"{count_text(dirs, 'dir', 'dirs')}"
        )

    if args.json:
        print(json.dumps(build_report(
            args,
            mode="status",
            state_path=state_path,
            snapshots={},
            diffs={},
            counts=total_counts({}),
            state=state,
            changed=False,
            reason="status",
            applied=False,
            unreadable=0,
            elapsed=0.0,
            generated_at=timestamp(now()),
        ), indent=2))
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="external_library_changes.py",
        description=(
            "Report whether external library directories changed since the last recorded snapshot. "
            "--check writes nothing, --record advances the snapshot, --status only reads it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 the verdict was determined (changed or not), 1 the run failed (a root could\n"
            "not be read, the state file could not be written), 2 configuration error.\n"
            "The verdict itself is the trailing `changed=yes|no` line of --check (and the `changed`\n"
            "field of --json), not the exit code: dagu marks a step that exits non-zero as failed, so a\n"
            "DAG gates on the line via `output:` plus `preconditions: [{condition: ${CHANGED}, expected:\n"
            "changed=yes}]`. In a shell: --check -q | grep -q '^changed=yes'.\n"
            "Record after a successful scan, never before: the snapshot is what makes the change\n"
            "invisible, so a failed scan would be skipped forever.\n"
        ),
    )
    parser.add_argument(
        "--path",
        action="append",
        metavar="DIR",
        help="directory to watch, relative paths are resolved against the current directory (repeatable)",
    )
    parser.add_argument(
        "--state",
        required=True,
        metavar="FILE",
        help="snapshot file to compare against / write (required; keep it outside the watched trees)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        dest="mode",
        action="store_const",
        const="check",
        help="compare the trees with the state and print the verdict (default; writes nothing)",
    )
    mode.add_argument(
        "--record",
        dest="mode",
        action="store_const",
        const="record",
        help="write the current trees as the new reference state (dry run unless --apply)",
    )
    mode.add_argument(
        "--status",
        dest="mode",
        action="store_const",
        const="status",
        help="print the recorded state (paths, timestamps, counts) without walking anything",
    )
    parser.set_defaults(mode="check")
    parser.add_argument("--apply", action="store_true", help="let --record actually write the state file")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "shell wildcard to ignore, matched against the path below the root and the file name "
            f"(repeatable; always ignored: {', '.join(DEFAULT_EXCLUDES)})"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=f"list the changed paths, at most {MAX_LISTED} per kind (also in the --json details)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON report on stdout, nothing else")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the verdict, no progress")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every walked directory")
    return parser.parse_args(argv)


def main(argv=None, *, log: Logger | None = None, now=time.time) -> int:
    args = parse_args(argv)
    logger = log or Logger(quiet=args.quiet, verbose=args.verbose)
    state_path = Path(args.state)

    if args.apply and args.mode != "record":
        logger("error: --apply only belongs to --record", force=True)
        return 2
    if args.mode == "status":
        return run_status(args, state_path, logger, now)
    if not args.path:
        logger("error: --path is required (one directory per flag, repeatable)", force=True)
        return 2

    roots = normalize_roots(args.path)
    missing = [root for root in roots if not os.path.isdir(root)]
    if missing:
        for root in missing:
            logger(f"error: not a directory: {root}", force=True)
        return 2

    excludes = normalize_excludes(args.exclude)
    logger(f"watching {len(roots)} path(s) with {len(excludes)} exclude pattern(s)", debug=True)
    if args.mode == "record":
        return run_record(args, roots, excludes, state_path, logger, now)
    return run_check(args, roots, excludes, state_path, logger, now)


if __name__ == "__main__":
    raise SystemExit(main())
