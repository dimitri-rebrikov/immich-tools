# /// script
# requires-python = ">=3.11"
# dependencies = ["smbprotocol"]
# ///
"""Snapshot an SMB share over SMB2/3 - no mount, no root, no FUSE.

Walks a share with `QUERY_DIRECTORY`/`FileDirectoryInformation` requests. Those responses already
contain size and mtime per entry, so the walk costs **one request per directory** plus the listing
payload instead of one round trip per file. That is the difference that matters on a slow SMB share:
a mount-based walk (cifs, FUSE/rclone) turns every `stat` into its own round trip, which is where
seconds per thousand files come from.

The lighter information class is deliberate: the walk needs file name, size, mtime and the directory
flag, which is exactly what `FileDirectoryInformation` returns. Asking for
`FileIdBothDirectoryInformation` (which also carries the FileId and the 8.3 name) measured no faster
(23.6 s vs 23.2 s for 54 336 files) - the server's cost per entry dominates anyway.

The result is a state document that `external_library_snapshot_compare.py` compares against the
persisted baseline - the producer only walks, the comparer only compares:

    {"tool": "external_library_smb_snapshot", "version": 1, "generated_at": "...",
     "source": "smb://nas/photos/2026", "paths": [{"path": "/srv/photos",
     "files": {"2026/09/img.jpg": [4821337, 1758051234123456789]}, "dirs": ["2026", "2026/09"]}]}

`size` is bytes, `mtime` is nanoseconds since the Unix epoch (converted from the SMB FILETIME),
exactly like the `os.stat().st_mtime_ns` the local producer reports. Both sources describe the same
server metadata, but the SMB path rounds to microseconds, so **switching a library between the two
producers reports every file as `modified` once** and then stays quiet again.

What is walked: same rules as the local producer `external_library_snapshot.py` - symlinks/reparse
points are never followed (they are leaves), directories are tracked by name (so empty ones are
visible), and the
`--exclude` patterns are applied to the path below the root and to the bare name. Excluded
*directories* are not descended into, which is the only way to save real work.

Three requests cost one directory (`CREATE`, `QUERY_DIRECTORY`, `CLOSE`). Measured on a NAS that caps
one session at about two requests in flight: 1 038 directories with 54 336 files took 79 s over one
session, 39 s over 16 threads on that same session and 22 s over 4-8 sessions - the knee is there,
16 or 32 sessions are no faster (the NAS saturates at ~2 400 entries/s). The credit window the walk
asks for with SMB2 ECHO (`-v` reports the grant) does not lift a per-session cap, more sessions do.

Usage:
    SMB_USER=immich SMB_PASSWORD=... uv run external_library_smb_snapshot.py \\
        --host nas --share photos --key /srv/photos --out photos.snapshot.json

    # straight into the change gate, without a file in between
    uv run external_library_smb_snapshot.py --host nas --share photos -q | \\
        uv run external_library_snapshot_compare.py --state photos.state.json --current - -q

Authentication is NTLM by default (`--user`/`--password`, env `SMB_USER`/`SMB_PASSWORD`, or
`--password-file`); Kerberos works when the `smbprotocol[kerberos]` extras and a ticket are present.
Only SMB2/3 is supported - SMB1 servers cannot be reached.

Exit codes: 0 the snapshot was written, 1 the share or the start directory could not be read,
2 configuration error (missing or unusable arguments, no credentials, unwritable --out path).
"""

from __future__ import annotations

import argparse
import fnmatch
import inspect
import json
import os
import queue
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from immich_api import Logger, positive_int

REPORT_TOOL = "external_library_smb_snapshot"
SNAPSHOT_VERSION = 1
# Must stay in sync with external_library_snapshot.py; test_external_library_snapshot.py asserts
# that both tuples are equal, so the two producers cannot drift apart silently.
DEFAULT_EXCLUDES = ("@eaDir", ".DS_Store", "Thumbs.db", "*.tmp")
MAX_LISTED = 20

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400  # symlink/junction: a leaf, never followed
STATUS_NO_MORE_FILES = 0x80000006
# One worker keeps one request in flight per session, so a small window is enough; the rest is
# headroom. Servers grant what they like - `-v` reports the answer.
SESSION_CREDITS = 16
FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000  # 1601-01-01 -> 1970-01-01 in 100ns ticks
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# (name, size, mtime_ns, is_dir) for one directory entry
Entry = tuple[str, int, int, bool]


def load_smbprotocol():
    """Import lazily: --help, the pure helpers and the unit tests work without the dependency."""
    try:
        from smbprotocol import connection, exceptions, file_info, open, session, tree  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit(
            "error: the smbprotocol package is required (uv run installs it from the PEP 723 "
            f"header): {exc}"
        ) from exc
    return connection, exceptions, file_info, open, session, tree


def optional_kwargs(session_cls, wanted: dict) -> dict:
    """Pass only the optional arguments this smbprotocol version actually accepts."""
    accepted = inspect.signature(session_cls.__init__).parameters
    return {key: value for key, value in wanted.items() if key in accepted and value not in (None, "")}


def session_credentials(session_cls, user: str, domain: str) -> tuple[str, dict]:
    """Username and Session kwargs that fit this smbprotocol version.

    Current releases dropped `domain_name` (the domain is part of the username there), older ones
    still take it, and encryption defaults to on - which a read-only metadata walk does not need.
    """
    accepted = inspect.signature(session_cls.__init__).parameters
    kwargs = optional_kwargs(session_cls, {"require_encryption": False})
    if domain:
        if "domain_name" in accepted:
            kwargs["domain_name"] = domain
        else:
            user = f"{domain}\\{user}"
    return user, kwargs


def normalize_excludes(patterns: list[str]) -> tuple[str, ...]:
    """The given patterns plus the built-in noise. One flag may carry several, comma separated."""
    split = [part.strip() for pattern in patterns for part in pattern.split(",")]
    return tuple(dict.fromkeys((*DEFAULT_EXCLUDES, *[part for part in split if part])))


def request_credits(connection, session, wanted: int, log: Logger) -> int | None:
    """Ask the server for room to keep more requests in flight, and report what it granted.

    SMB2 lets a client have only as many requests outstanding as the *credit window* allows, and
    smbprotocol asks for a single credit per request. A server that grants one credit per response
    therefore pins the pipeline depth at 1-2, no matter how many threads walk. ECHO is the
    documented way to request more (`credit_request`); a smaller grant is a server policy, so a
    refusal only costs speed and must never fail the walk.
    """
    try:
        granted = connection.echo(sid=session.session_id, credit_request=wanted)
    except Exception as exc:  # noqa: BLE001 - a window we did not get is a performance issue only
        log(f"notice: the server did not grant more credits: {exc}", debug=True)
        return None
    log(f"credits: {granted} (requested {wanted})", debug=True)
    return granted


def is_excluded(relative: str, name: str, excludes: tuple[str, ...]) -> bool:
    """Shell wildcards, matched against the path below the root and against the bare name."""
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) for pattern in excludes)


def unc_share(host: str, share: str) -> str:
    return "\\\\{}\\{}".format(host, share.strip("\\/"))


def to_smb_name(relative: str) -> str:
    """'' for the share root, otherwise the path below it with backslashes."""
    return relative.strip("/").replace("/", "\\")


def filetime_to_ns(value) -> int:
    """SMB hands out FILETIME; smbprotocol surfaces it either as datetime or as the raw tick count."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        delta = moment - UNIX_EPOCH
        return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000
    return (int(value or 0) - FILETIME_EPOCH_OFFSET) * 100


def entry_of(info) -> Entry | None:
    """Turn one directory-information structure into (name, size, mtime_ns, is_dir)."""
    name = info["file_name"].get_value().decode("utf-16-le")
    if name in (".", ".."):
        return None
    attributes = int(info["file_attributes"].get_value() or 0)
    is_directory = bool(attributes & FILE_ATTRIBUTE_DIRECTORY) and not attributes & FILE_ATTRIBUTE_REPARSE_POINT
    return (name, int(info["end_of_file"].get_value() or 0), filetime_to_ns(info["last_write_time"].get_value()), is_directory)


def walk_remote(
    start: str,
    list_dir,
    excludes: tuple[str, ...],
    connections: int,
    log: Logger,
) -> dict:
    """Walk `start` (a relative path below the share root) using `list_dir(relative) -> (entries, failed)`.

    One code path for serial and parallel runs; `list_dir` must return only the entries of that
    directory, prefixing, filtering and the recursion happen here, which keeps the tricky part
    testable without a server. Unreadable directories are counted and skipped; an unreadable
    `start` sets `root_failed`, which the caller must treat as a hard error - an empty result would
    otherwise look like "everything was deleted".

    `list_dir` is either one lister or a sequence of them (one per session). With several, every
    listing checks one out and returns it, so a session is never used by two threads at once.
    """
    listers = list(list_dir) if isinstance(list_dir, (list, tuple)) else [list_dir]
    sessions = queue.SimpleQueue()
    for lister in listers:
        sessions.put(lister)

    files: dict[str, tuple[int, int]] = {}
    dirs: list[str] = []
    unreadable = 0
    root_failed = False

    def visit(relative: str, key: str) -> tuple[str, str, list[Entry], bool]:
        if len(listers) > 1:
            lister = sessions.get()
            try:
                entries, failed = lister(relative)
            finally:
                sessions.put(lister)
        else:
            entries, failed = listers[0](relative)
        return relative, key, entries, failed

    def collect(relative: str, key: str, entries: list[Entry], failed: bool) -> list[tuple[str, str]]:
        """Merge one directory. Returns the (share path, key) pairs of its subdirectories.

        Keys are built relative to `start`, not to the share root, so a snapshot of
        `--share photos --subdir 2026` matches what a mount of that subtree would report.
        """
        nonlocal unreadable, root_failed
        if failed:
            unreadable += 1
            root_failed = root_failed or relative == start
            log(f"warning: cannot list {relative or '<share root>'}", force=True)
            return []
        found: list[tuple[str, str]] = []
        for name, size, mtime, is_dir in entries:
            child_key = f"{key}/{name}" if key else name
            if is_excluded(child_key, name, excludes):
                continue
            if is_dir:
                dirs.append(child_key)
                found.append((f"{relative}/{name}" if relative else name, child_key))
            else:
                files[child_key] = (size, mtime)
        return found

    if connections <= 1:
        pending = [(start, "")]
        while pending:
            pending.extend(collect(*visit(*pending.pop())))
    else:
        with ThreadPoolExecutor(max_workers=connections) as pool:
            pending_futures = {pool.submit(visit, start, "")}
            while pending_futures:
                done, pending_futures = wait(pending_futures, return_when=FIRST_COMPLETED)
                for future in done:
                    for pair in collect(*future.result()):
                        pending_futures.add(pool.submit(visit, *pair))

    dirs.sort()
    log(f"walked {start or '<share root>'}: {len(files)} files, {len(dirs)} dirs", debug=True)
    return {"files": files, "dirs": dirs, "unreadable": unreadable, "root_failed": root_failed}


class RemoteTree:
    """One SMB2 session on a share. `list_dir` is meant to be used from one thread at a time."""

    def __init__(self, host, share, user, password, domain, port, timeout, log: Logger, credits: int = 0):
        connection, exceptions, file_info, open_module, session_module, tree_module = load_smbprotocol()
        self._constants = (open_module, file_info)
        self._exceptions = exceptions
        self._log = log
        self._connection = connection.Connection(uuid.uuid4(), host, port)
        self._connect(timeout)
        name, kwargs = session_credentials(session_module.Session, user, domain)
        self._session = session_module.Session(self._connection, name, password, **kwargs)
        self._session.connect()
        self._tree = tree_module.TreeConnect(self._session, unc_share(host, share))
        self._tree.connect()
        log(f"connected to {unc_share(host, share)} as {user}", debug=True)
        if credits:
            request_credits(self._connection, self._session, credits, log)

    def _connect(self, timeout) -> None:
        try:
            self._connection.connect(timeout=timeout)
        except TypeError:  # older smbprotocol releases have no timeout argument
            self._connection.connect()

    def list_dir(self, relative: str) -> tuple[list[Entry], bool]:
        """Entries of one directory plus a flag saying whether listing it failed."""
        open_module, file_info = self._constants
        handle = open_module.Open(self._tree, to_smb_name(relative))
        entries: list[Entry] = []
        try:
            handle.create(
                open_module.ImpersonationLevel.Impersonation,
                open_module.DirectoryAccessMask.FILE_LIST_DIRECTORY,
                file_info.FileAttributes.FILE_ATTRIBUTE_DIRECTORY,
                open_module.ShareAccess.FILE_SHARE_READ
                | open_module.ShareAccess.FILE_SHARE_WRITE
                | open_module.ShareAccess.FILE_SHARE_DELETE,
                open_module.CreateDisposition.FILE_OPEN,
                open_module.CreateOptions.FILE_DIRECTORY_FILE,
            )
            while True:
                try:
                    # name + size + mtime + attributes, no FileId and no 8.3 name: everything the
                    # walk needs, and the smallest listing the server can produce for it.
                    infos = handle.query_directory("*", file_info.FileInformationClass.FILE_DIRECTORY_INFORMATION)
                except self._exceptions.SMBResponseException as exc:
                    if int(getattr(exc.status, "value", exc.status)) == STATUS_NO_MORE_FILES:
                        break
                    raise
                if not infos:
                    break
                for info in infos:
                    entry = entry_of(info)
                    if entry is not None:
                        entries.append(entry)
        except self._exceptions.SMBResponseException as exc:
            self._log(f"warning: {to_smb_name(relative) or '<share root>'}: {exc}", force=True)
            return [], True
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - a failed close must not hide the listing result
                self._log(f"warning: closing {to_smb_name(relative) or '<share root>'} failed", force=True)
        return entries, False

    def close(self) -> None:
        for closer in (
            getattr(self._tree, "disconnect", None),
            getattr(self._session, "disconnect", None),
            getattr(self._connection, "disconnect", None),
        ):
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown errors are not worth failing the run
                pass


def build_snapshot(key: str, walk: dict, *, source: str, elapsed: float, generated_at: str) -> dict:
    return {
        "tool": REPORT_TOOL,
        "version": SNAPSHOT_VERSION,
        "generated_at": generated_at,
        "source": source,
        "paths": [{"path": key, "files": walk["files"], "dirs": walk["dirs"]}],
        "unreadable": walk["unreadable"],
        "elapsed_seconds": round(elapsed, 3),
    }


def timestamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))


def resolve_password(args) -> str | None:
    if args.password:
        return args.password
    if args.password_file:
        try:
            return Path(args.password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"error: cannot read --password-file {args.password_file}: {exc}", file=sys.stderr)
            return None
    return os.environ.get("SMB_PASSWORD")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="external_library_smb_snapshot.py",
        description=(
            "Snapshot an SMB share over SMB2/3 (no mount, no root) into a state document for "
            "external_library_snapshot_compare.py. The snapshot goes to stdout, progress goes to stderr."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 the snapshot was written, 1 the share or the start directory could not be\n"
            "read, 2 configuration error.\n"
            "The directory listing already carries size and mtime, so this walk needs one request per\n"
            "directory instead of one per file. It is still the same server: if the server itself is\n"
            "slow per entry (or the link is slow), this walk is slow as well.\n"
        ),
    )
    parser.add_argument("--host", required=True, help="SMB server hostname or ip")
    parser.add_argument("--share", required=True, help="share name (without slashes)")
    parser.add_argument(
        "--subdir",
        default="",
        help="path below the share to start at, e.g. photos/2026 (default: the share root)",
    )
    parser.add_argument(
        "--key",
        metavar="PATH",
        help=(
            "the path used as the key in the snapshot and in the state file; use the same value as "
            "the --path you would pass to a mount-based run (default: //host/share[/subdir])"
        ),
    )
    parser.add_argument("--user", default=os.environ.get("SMB_USER"), help="SMB user (env: SMB_USER)")
    parser.add_argument(
        "--password",
        default=None,
        help="SMB password (env: SMB_PASSWORD; prefer --password-file - argv ends up in the dagu log)",
    )
    parser.add_argument("--password-file", metavar="FILE", help="file holding the password (chmod 600)")
    parser.add_argument("--domain", default="", help="NTLM domain (default: none)")
    parser.add_argument("--port", type=positive_int, default=445, help="SMB port (default: 445)")
    parser.add_argument(
        "--connections",
        type=positive_int,
        default=1,
        metavar="N",
        help=(
            "sessions used for the walk (default: 1). Each keeps one listing in flight, so this "
            "is the lever when the round trip time is high or when the server caps a single "
            "session; -v reports the credit window each session got"
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "shell wildcard to ignore, matched against the path below the start and the file name "
            f"(repeatable; several patterns may be separated by commas; always ignored: "
            f"{', '.join(DEFAULT_EXCLUDES)})"
        ),
    )
    parser.add_argument("--timeout", type=float, default=60.0, metavar="SECONDS", help="connection timeout (default: 60)")
    parser.add_argument("--out", default="-", metavar="FILE", help="write the snapshot here ('-' = stdout)")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every listed directory")
    parser.add_argument("--list", action="store_true", help="print the first entries of the snapshot as a preview")
    return parser.parse_args(argv)


def close_all(trees: list) -> None:
    """Best effort teardown: a failing session must not hide the walk result."""
    for tree in trees:
        try:
            tree.close()
        except Exception:  # noqa: BLE001 - teardown errors are not worth failing the run
            pass


def open_trees(args, password: str, logger: Logger, tree_factory) -> tuple[list | None, str | None]:
    """One session per worker, because a single session is what the server usually caps.

    Each session has its own TCP connection, its own credit window and its own server-side context,
    so N of them are the only client-side lever left once more threads inside one session stop
    helping. Fewer usable sessions than asked for is a warning (a per-user connection limit, a
    server that refuses the Nth logon); no session at all is fatal for the run.
    """

    def build(_index: int):
        return tree_factory(
            args.host, args.share, args.user, password, args.domain, args.port, args.timeout, logger,
            credits=SESSION_CREDITS,
        )

    trees: list = []
    problem: str | None = None

    def remember(built) -> None:
        """Collect one attempt; the first error is kept, the successful sessions are used anyway."""
        nonlocal problem
        try:
            trees.append(built())
        except Exception as exc:  # noqa: BLE001 - a refused session must not stop the others
            problem = problem or (str(exc) or exc.__class__.__name__)

    if args.connections <= 1:
        remember(lambda: build(0))
    else:
        with ThreadPoolExecutor(max_workers=args.connections) as pool:
            attempts = [pool.submit(build, index) for index in range(args.connections)]
            for attempt in attempts:
                remember(attempt.result)

    if not trees:
        return None, problem or "no session could be opened"
    if problem:
        logger(
            f"warning: only {len(trees)} of {args.connections} sessions are usable: {problem}",
            force=True,
        )
    return trees, None


def main(argv=None, *, log: Logger | None = None, now=time.time, tree_factory=RemoteTree) -> int:
    args = parse_args(argv)
    logger = log or Logger(quiet=args.quiet, verbose=args.verbose)
    password = resolve_password(args)
    if not args.user or not password:
        logger("error: --user/SMB_USER and --password/--password-file/SMB_PASSWORD are required", force=True)
        return 2
    if args.out != "-":
        parent = Path(args.out).parent
        if not parent.is_dir():
            logger(f"error: cannot write the snapshot, {parent} is not a directory", force=True)
            return 2

    excludes = normalize_excludes(args.exclude)
    start = args.subdir.strip("/")
    key = args.key or f"//{args.host}/{args.share}" + (f"/{start}" if start else "")
    source = f"smb://{args.host}/{args.share}" + (f"/{start}" if start else "")

    started = now()
    trees, problem = open_trees(args, password, logger, tree_factory)
    if trees is None:
        logger(f"error: cannot connect to {unc_share(args.host, args.share)}: {problem}", force=True)
        return 1
    try:
        walk = walk_remote(start, [tree.list_dir for tree in trees], excludes, args.connections, logger)
    finally:
        close_all(trees)
    elapsed = now() - started

    if walk["root_failed"]:
        logger(f"error: cannot read {source} - the snapshot is not written", force=True)
        return 1

    snapshot = build_snapshot(key, walk, source=source, elapsed=elapsed, generated_at=timestamp(now()))
    document = json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n"
    if args.out == "-":
        sys.stdout.write(document)
    else:
        try:
            Path(args.out).write_text(document, encoding="utf-8")
        except OSError as exc:
            logger(f"error: cannot write {args.out}: {exc}", force=True)
            return 1

    logger(
        f"snapshot: {len(walk['files'])} files, {len(walk['dirs'])} dirs in {elapsed:.1f}s"
        + (f", {walk['unreadable']} unreadable directories" if walk["unreadable"] else "")
    )
    if args.list:
        for relative in list(walk["files"])[:MAX_LISTED]:
            logger(f"  {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
