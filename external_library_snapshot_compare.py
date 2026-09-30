# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare two states of the same directory trees and report what changed.

A *state* is one document listing the trees, written by a producer:

    {"tool": "external_library_snapshot", "version": 1, "generated_at": "2026-09-30T12:00:00+0200",
     "paths": [{"path": "/srv/photos", "files": {"2026/09/img.jpg": [4821337, 1758051234123456789]},
                "dirs": ["2026", "2026/09"]}]}

Two of them are compared, both as a file, the current one also as `-` (stdin):

    --state FILE      the persisted state from the last successful run
    --current FILE|-  the state produced now

Producers are separate scripts, one per source (`external_library_snapshot.py` for local directories,
`external_library_smb_snapshot.py` for a share without a mount). This script only compares and never
writes anything - it stays a pure function of its two inputs.

Usage:
    # the DAG: produce, compare, act, then produce into the state file again
    uv run external_library_snapshot.py --path /srv/photos -q | \
        uv run external_library_snapshot_compare.py --state photos.state.json --current - -q
    # ... run the action, then let the producer overwrite the baseline
    uv run external_library_snapshot.py --path /srv/photos --out photos.state.json

    # keep the walk on disk when a human wants the details afterwards
    uv run external_library_snapshot.py --path /srv/photos --out photos.now.json
    uv run external_library_snapshot_compare.py --state photos.state.json --current photos.now.json --list

The verdict is the trailing `changed=yes|no` line (`changed` in the --json report), not the exit code:
a step that exits non-zero counts as failed in most schedulers, so a DAG gates on the line
(`output:` plus `preconditions: [{condition: ${CHANGED}, expected: changed=yes}]`, in a shell
`... -q | grep -q '^changed=yes'`).

Exit codes: 0 the verdict was determined, 2 configuration error (an unusable or missing state).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from immich_api import Logger

REPORT_TOOL = "external_library_snapshot_compare"
MAX_LISTED = 20  # detail entries per change kind, for --list (human output and JSON report)
DETAIL_KEYS = ("added", "removed", "modified", "dirs_added", "dirs_removed")
DETAIL_MARKERS = {
    "added": "+",
    "removed": "-",
    "modified": "~",
    "dirs_added": "+ dir",
    "dirs_removed": "- dir",
}


def is_state_shaped(document) -> bool:
    """Every state needs `paths` with `path`, `files` and `dirs` - whoever produced it."""
    if not isinstance(document, dict):
        return False
    paths = document.get("paths")
    if not isinstance(paths, list) or not paths:
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("files"), dict)
        and isinstance(entry.get("dirs"), list)
        for entry in paths
    )


def state_trees(entry: dict) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Read one entry. A single corrupt record only costs one extra `changed`."""
    files: dict[str, tuple[int, int]] = {}
    for name, value in (entry.get("files") or {}).items():
        try:
            files[str(name)] = (int(value[0]), int(value[1]))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    dirs = [str(item) for item in (entry.get("dirs") or []) if isinstance(item, str)]
    return files, dirs


def load_state(source: str, log: Logger, *, role: str, stdin=None) -> tuple[dict | None, dict, str | None]:
    """Read one state. Returns (listings by path, producer metadata, problem)."""
    if source == "-":
        try:
            text = (stdin if stdin is not None else sys.stdin).read()
        except OSError as exc:
            log(f"warning: cannot read the {role} state from stdin: {exc}", force=True)
            return None, {}, f"{role}-unreadable"
    else:
        path = Path(source)
        if role == "baseline" and not path.exists():
            return None, {}, "no-state"  # the first run: nothing to compare against yet
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log(f"warning: cannot read the {role} state {source}: {exc}", force=True)
            return None, {}, f"{role}-unreadable"
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        log(f"warning: cannot parse the {role} state {source}: {exc}", force=True)
        return None, {}, f"{role}-unreadable"
    if not is_state_shaped(document):
        log(f"warning: ignoring the {role} state {source}: no usable 'paths' list", force=True)
        return None, {}, f"{role}-malformed"

    listings = {}
    for entry in document["paths"]:
        files, dirs = state_trees(entry)
        listings[entry["path"]] = {"files": files, "dirs": dirs}
    meta = {
        "tool": document.get("tool"),
        "source": document.get("source"),
        "generated_at": document.get("generated_at"),
        "elapsed_seconds": document.get("elapsed_seconds"),
        "unreadable": document.get("unreadable"),
    }
    return listings, meta, None


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


def compare_paths(baseline: dict, listings: dict) -> dict:
    """One diff per path, or None where the baseline has no entry for it."""
    diffs: dict[str, dict | None] = {}
    for root, listing in listings.items():
        recorded = baseline.get(root)
        if recorded is None:
            diffs[root] = None
            continue
        diffs[root] = diff_trees(recorded["files"], listing["files"], recorded["dirs"], listing["dirs"])
    return diffs


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
    return " ".join(f"{key}={counts.get(key, 0)}" for key in DETAIL_KEYS)


def timestamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))


def describe_baseline(path: str, baseline: dict | None, meta: dict) -> str:
    if baseline is None:
        return f"baseline: {path} (no usable state)"
    return (
        f"baseline: {path} ({meta.get('tool') or 'unknown producer'}, "
        f"{meta.get('generated_at') or 'no timestamp'}, "
        f"{count_text(len(baseline), 'path', 'paths')})"
    )


def build_report(
    args,
    *,
    listings: dict,
    producer: dict,
    diffs: dict,
    counts: dict,
    changed: bool,
    reason: str,
    generated_at: str,
) -> dict:
    report = {
        "tool": REPORT_TOOL,
        "generated_at": generated_at,
        "state": str(args.state),
        "current": str(args.current),
        "producer": producer,
        "changed": changed,
        "reason": reason,
        "paths": [
            {
                "path": root,
                "files": len(listing["files"]),
                "dirs": len(listing["dirs"]),
                "changes": dict(diffs[root]["counts"]) if diffs.get(root) else None,
            }
            for root, listing in listings.items()
        ],
        "changes": dict(counts),
    }
    if args.list:
        report["details"] = {root: details_of(diff) for root, diff in diffs.items() if diff}
    return report


def run_compare(args, logger: Logger, now) -> int:
    listings, producer, problem = load_state(args.current, logger, role="current")
    if listings is None:
        logger(f"error: the current state {args.current} is unusable ({problem})", force=True)
        return 2
    baseline, baseline_meta, baseline_problem = load_state(args.state, logger, role="baseline")
    return report_verdict(
        args,
        logger,
        now,
        baseline=baseline,
        baseline_meta=baseline_meta,
        meta_problem=baseline_problem,
        listings=listings,
        producer=producer,
    )


def report_verdict(
    args, logger: Logger, now, *, baseline, baseline_meta, meta_problem, listings, producer
) -> int:
    """Print the verdict (and the report) for one comparison."""
    if baseline is None:
        changed, reason, diffs = True, meta_problem or "no-state", {}
    elif set(baseline) != set(listings):
        changed, reason, diffs = True, "paths-changed", {}
    else:
        diffs = compare_paths(baseline, listings)
        changed = total_counts(diffs)["total"] > 0
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

    info(describe_baseline(args.state, baseline, baseline_meta))
    for root, listing in listings.items():
        info(
            f"current: {root} "
            f"({count_text(len(listing['files']), 'file', 'files')}, "
            f"{count_text(len(listing['dirs']), 'dir', 'dirs')})"
        )
    if producer.get("unreadable"):
        info(f"skipped: {count_text(int(producer['unreadable']), 'unreadable directory', 'unreadable directories')}")
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
                    listings=listings,
                    producer=producer,
                    diffs=diffs,
                    counts=counts,
                    changed=changed,
                    reason=reason,
                    generated_at=timestamp(now()),
                ),
                indent=2,
            )
        )
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="external_library_snapshot_compare.py",
        description=(
            "Compare two states of the same trees (the persisted baseline and the state produced now) "
            "and print `changed=yes|no`. Writes nothing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 the verdict was determined (changed or not), 2 configuration error (a\n"
            "missing or unusable state).\n"
            "The verdict is the trailing `changed=yes|no` line, not the exit code: a step that exits\n"
            "non-zero counts as failed in most schedulers. A DAG gates on the line via `output:` plus\n"
            "`preconditions: [{condition: ${CHANGED}, expected: changed=yes}]`, in a shell:\n"
            "--check -q | grep -q '^changed=yes'.\n"
        ),
    )
    parser.add_argument(
        "--state",
        required=True,
        metavar="FILE",
        help="the baseline to compare against (the state of the last successful run)",
    )
    parser.add_argument(
        "--current",
        required=True,
        metavar="FILE",
        help="the state produced for this run; '-' reads stdin",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=f"list the changed paths, at most {MAX_LISTED} per kind (also in the --json details)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON report on stdout, nothing else")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the verdict, no progress")
    parser.add_argument("-v", "--verbose", action="store_true", help="log what is compared")
    return parser.parse_args(argv)


def main(argv=None, *, log: Logger | None = None, now=time.time) -> int:
    args = parse_args(argv)
    logger = log or Logger(quiet=args.quiet, verbose=args.verbose)
    logger(f"comparing {args.current} against {args.state}", debug=True)
    return run_compare(args, logger, now)


if __name__ == "__main__":
    raise SystemExit(main())
