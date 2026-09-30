# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Snapshot a directory tree - the local producer for the snapshot comparer.

Lists the trees below `--path` with `os.scandir`/`DirEntry.stat` (no file is opened, no content is
read) and writes one snapshot document:

    {"tool": "external_library_snapshot", "version": 1, "generated_at": "2026-09-30T18:20:00+0200",
     "source": "dir:/srv/photos", "unreadable": 0, "elapsed_seconds": 12.4,
     "paths": [{"path": "/srv/photos",
                "files": {"2026/09/img.jpg": [4821337, 1758051234123456789]},
                "dirs": ["2026", "2026/09"]}]}

`path` is the directory as you named it (made absolute): that string is the key the comparer stores in
its state, so it has to stay stable between runs. `size` is bytes, `mtime` is nanoseconds since the
Unix epoch - exactly what `os.stat().st_mtime_ns` returns.

The share counterpart is `external_library_smb_snapshot.py`: same document, different transport (SMB2,
no mount, no root). Both are read by `external_library_snapshot_compare.py`, which only compares
documents and never walks anything itself.

What is listed: files and directories below each `--path`. Symlinks are never followed (they are
leaves), directories are tracked by name (so empty ones stay visible), `--exclude` matches the path
below the root and the bare name, and the usual NAS/OS noise (@eaDir, .DS_Store, Thumbs.db, *.tmp) is
always ignored. The file this run writes is never part of the snapshot.

Usage:
    uv run external_library_snapshot.py --path /srv/photos --out photos.snapshot.json
    uv run external_library_snapshot.py --path /srv/photos -q | \\
        uv run external_library_snapshot_compare.py --state photos.state.json --snapshot-in - --check

Exit codes: 0 the snapshot was written, 1 a tree could not be read (nothing is written, so a half
empty tree can never be compared), 2 configuration error.
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

REPORT_TOOL = "external_library_snapshot"
SNAPSHOT_VERSION = 1
# The noise list is shared with the comparer and with the SMB producer; a test pins all three.
DEFAULT_EXCLUDES = ("@eaDir", ".DS_Store", "Thumbs.db", "*.tmp")
MAX_LISTED = 20


def normalize_roots(values: list[str]) -> list[str]:
    """Absolute, de-duplicated roots in the order they were given."""
    roots: list[str] = []
    for value in values:
        root = os.path.abspath(os.path.expanduser(value))
        if root not in roots:
            roots.append(root)
    return roots


def normalize_excludes(patterns: list[str]) -> tuple[str, ...]:
    """The given patterns plus the built-in noise. One flag may carry several, comma separated."""
    split = [part.strip() for pattern in patterns for part in pattern.split(",")]
    return tuple(dict.fromkeys((*DEFAULT_EXCLUDES, *[part for part in split if part])))


def is_excluded(relative: str, name: str, excludes: tuple[str, ...]) -> bool:
    """Shell wildcards, matched against the path below the root and against the bare name."""
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) for pattern in excludes)


def walk_tree(root: str, excludes: tuple[str, ...], log: Logger, skip: frozenset[str] = frozenset()) -> dict:
    """Collect `{relative path: (size, mtime_ns)}` for every file plus every directory.

    `skip` holds normalized absolute paths that are ignored, which is how the snapshot file stays
    invisible when it is written inside a watched tree. `root_failed` says whether the root itself
    was unreadable, which the caller has to treat as a hard error - an empty listing would otherwise
    look like "everything was deleted".
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
        log(f"listed {root}: {len(snapshot['files'])} files, {len(snapshot['dirs'])} dirs", debug=True)
    return snapshots, failed, unreadable


def timestamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))


def count_text(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def build_document(roots: list[str], snapshots: dict, *, unreadable: int, elapsed: float, generated_at: str) -> dict:
    return {
        "tool": REPORT_TOOL,
        "version": SNAPSHOT_VERSION,
        "generated_at": generated_at,
        "source": f"dir:{roots[0]}" if len(roots) == 1 else "dir",
        "paths": [
            {"path": root, "files": snapshot["files"], "dirs": snapshot["dirs"]}
            for root, snapshot in snapshots.items()
        ],
        "unreadable": unreadable,
        "elapsed_seconds": round(elapsed, 3),
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="external_library_snapshot.py",
        description=(
            "Snapshot local directory trees for external_library_snapshot_compare.py. The snapshot "
            "goes to stdout, progress goes to stderr."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 the snapshot was written, 1 a tree could not be read (nothing is written),\n"
            "2 configuration error.\n"
        ),
    )
    parser.add_argument(
        "--path",
        action="append",
        required=True,
        metavar="DIR",
        help="directory to list, repeatable; the value is also the key stored in the state",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "shell wildcard to ignore, matched against the path below the root and the file name "
            f"(repeatable; several patterns may be separated by commas; always ignored: "
            f"{', '.join(DEFAULT_EXCLUDES)})"
        ),
    )
    parser.add_argument("--out", default="-", metavar="FILE", help="write the snapshot here ('-' = stdout)")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every listed directory")
    parser.add_argument("--list", action="store_true", help="preview the first entries of the snapshot")
    return parser.parse_args(argv)


def main(argv=None, *, log: Logger | None = None, now=time.time) -> int:
    args = parse_args(argv)
    logger = log or Logger(quiet=args.quiet, verbose=args.verbose)
    roots = normalize_roots(args.path)
    for root in roots:
        if not os.path.isdir(root):
            logger(f"error: {root} is not a directory", force=True)
            return 2
    if args.out != "-":
        parent = Path(args.out).parent
        if not parent.is_dir():
            logger(f"error: cannot write the snapshot, {parent} is not a directory", force=True)
            return 2

    excludes = normalize_excludes(args.exclude)
    skip = frozenset() if args.out == "-" else frozenset({os.path.normcase(os.path.abspath(args.out))})
    started = now()
    snapshots, failed, unreadable = scan_roots(roots, excludes, logger, skip)
    elapsed = now() - started
    if failed:
        for root in failed:
            logger(f"error: cannot read {root} - the snapshot is not written", force=True)
        return 1

    document = build_document(
        roots, snapshots, unreadable=unreadable, elapsed=elapsed, generated_at=timestamp(now())
    )
    text = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as exc:
            logger(f"error: cannot write {args.out}: {exc}", force=True)
            return 1

    files = sum(len(snapshot["files"]) for snapshot in snapshots.values())
    dirs = sum(len(snapshot["dirs"]) for snapshot in snapshots.values())
    logger(
        f"snapshot: {count_text(files, 'file', 'files')}, {count_text(dirs, 'dir', 'dirs')} "
        f"in {elapsed:.1f}s"
        + (f", {count_text(unreadable, 'unreadable directory', 'unreadable directories')}" if unreadable else "")
    )
    if args.list:
        for snapshot in snapshots.values():
            for relative in list(snapshot["files"])[:MAX_LISTED]:
                logger(f"  {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
