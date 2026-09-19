# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Scan Immich external libraries, optionally waiting for the scan to finish.

Starts one scan per external library with `POST /api/libraries/{id}/scan` and can
follow it with `--wait`, which polls `GET /api/queues/library` until the scanner
queue is idle. `--status` reports that queue instead of scanning anything.

Only current endpoints are used. The deprecated `PUT /api/jobs/{queue}` scan-all
command is deliberately avoided, so "all libraries" is a per-library fan-out:
GET /api/libraries, then one POST each. Consequence worth knowing: a per-library
scan does not queue the cleanup of libraries stuck in deletion, which the
scan-all job triggers (the nightly cron job still does that).

Every library is reported with its media counts from
`GET /api/libraries/{id}/statistics` and with its `refreshedAt`/`updatedAt`
timestamps, so `--status` answers "is a scan due?" and a scan run shows what was
known before it started. When `--wait` ran to completion the libraries are read
again, so each line then also shows `refreshed=<before> -> <after>`: proof that
the scan actually refreshed them. The `assetCount` of the list response is not
used: it is not populated for external libraries (it reads 0 while statistics
reports the real totals).

Job counts reported by `--status` and `--wait` are cumulative for the queue, not
for one scan, so a non-zero `failed` may predate this run.

Dry run by default: nothing is queued unless --apply is passed.

Usage:
    IMMICH_URL=https://immich.example.com IMMICH_API_KEY=xxx uv run external_library_scan.py
    uv run external_library_scan.py --apply --wait
    uv run external_library_scan.py --apply --library Photos --library 59f55eb0-32e5-4037-b53e-5e41c1f2d9b3
    uv run external_library_scan.py --status
    uv run external_library_scan.py --status --wait --json

Required API key permissions: library.read, library.update, library.statistics, queue.read
All of those endpoints are admin-only, so the API key must belong to an admin user.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from immich_api import (
    ImmichClient,
    ImmichError,
    Logger,
    make_default_transport,
    non_negative_int,
    normalize_url,
    positive_int,
)

REPORT_TOOL = "external_library_scan"
QUEUE = "library"  # QueueName.Library: where external library scans run
DEFAULT_POLL_INTERVAL = 5
DEFAULT_WAIT_TIMEOUT = 3600
PENDING_KEYS = ("active", "waiting", "delayed", "paused")  # queued or running, i.e. not finished


class LibraryClient(ImmichClient):
    """The shared client plus the external library and queue endpoints this tool needs."""

    permissions = (
        "library.read, library.update, library.statistics, queue.read "
        "(the API key must belong to an admin user)"
    )

    def libraries(self) -> list[dict]:
        """GET /api/libraries - non-deleted external libraries (id, name, paths, timestamps)."""
        return self.request("GET", "/api/libraries") or []

    def statistics(self, library_id: str) -> dict:
        """GET /api/libraries/{id}/statistics - {photos, videos, usage, total}."""
        return self.request("GET", f"/api/libraries/{library_id}/statistics") or {}

    def scan_library(self, library_id: str) -> None:
        """POST /api/libraries/{id}/scan - queue the crawl and asset check for one library (204)."""
        self.request("POST", f"/api/libraries/{library_id}/scan")

    def queue(self, name: str) -> dict:
        """GET /api/queues/{name} - {"name", "isPaused", "statistics": {...}}."""
        state = self.request("GET", f"/api/queues/{name}")
        if not state:
            raise ImmichError(200, f"/api/queues/{name}", "response has no queue state")
        return state


def counts_of(state: dict) -> dict:
    return state.get("statistics") or {}


def pending_jobs(state: dict) -> int:
    """Jobs that still have to run. Immich's own wait helper only looks at active + waiting."""
    counts = counts_of(state)
    return sum(int(counts.get(key) or 0) for key in PENDING_KEYS)


def format_queue_state(state: dict) -> str:
    counts = counts_of(state)
    paused = str(bool(state.get("isPaused"))).lower()
    return (
        f"queue {state.get('name') or QUEUE}: isPaused={paused} "
        f"active={counts.get('active', 0)} waiting={counts.get('waiting', 0)} "
        f"delayed={counts.get('delayed', 0)} paused={counts.get('paused', 0)} "
        f"failed={counts.get('failed', 0)} completed={counts.get('completed', 0)}"
    )


def resolve_libraries(libraries: list[dict], selectors: list[str]) -> tuple[list[dict], list[str]]:
    """Match --library values against ids (case-insensitive) or exact names.

    Returns the chosen libraries plus one message per unusable selector. Every selector is
    checked before anything is queued, so a typo can never lead to a partial scan.
    """
    if not selectors:
        return list(libraries), []

    known = ", ".join(sorted(str(library.get("name") or "?") for library in libraries)) or "none"
    chosen: list[dict] = []
    problems: list[str] = []

    for selector in selectors:
        wanted = selector.strip()
        matches = [library for library in libraries if str(library.get("id", "")).lower() == wanted.lower()]
        if not matches:
            matches = [library for library in libraries if library.get("name") == wanted]
        if not matches:
            problems.append(f"no external library matches {wanted!r} (known libraries: {known})")
        elif len(matches) > 1:
            ids = ", ".join(str(library.get("id")) for library in matches)
            problems.append(f"{len(matches)} libraries are named {wanted!r}, use an id instead: {ids}")
        elif matches[0] not in chosen:
            chosen.append(matches[0])
    return chosen, problems


def format_timestamp(value) -> str:
    """Immich ISO 8601 UTC -> '2026-09-19 14:18:34Z'; null becomes 'never'."""
    if not value:
        return "never"
    text = str(value)
    if text.endswith("Z") and len(text) >= 19:
        return f"{text[:19].replace('T', ' ')}Z"
    return text


def format_counts(statistics: dict | None) -> str:
    statistics = statistics or {}
    return (
        f"{statistics.get('total', 0)} media "
        f"({statistics.get('photos', 0)} photos, {statistics.get('videos', 0)} videos)"
    )


def format_media(entry: dict) -> str:
    if entry.get("statisticsError") or not entry.get("statistics"):
        return "media counts unknown"
    return format_counts(entry["statistics"])


def describe_library(entry: dict) -> str:
    return (
        f"{entry['name']} ({entry['id']}) - {format_media(entry)}, "
        f"refreshed={format_timestamp(entry.get('refreshedAt'))} "
        f"updated={format_timestamp(entry.get('updatedAt'))}"
    )


def describe_refreshed(entry: dict) -> str:
    """The line printed after a finished scan: what the library holds now, and when it was refreshed."""
    after = entry.get("after") or {}
    counts = format_counts(after.get("statistics")) if after.get("statistics") else "media counts unknown"
    return (
        f"{entry['name']} ({entry['id']}) - {counts}, "
        f"refreshed={format_timestamp(entry.get('refreshedAt'))} -> {format_timestamp(after.get('refreshedAt'))}"
    )


def library_entries(client: LibraryClient, libraries: list[dict], log: Logger) -> list[dict]:
    """Add each library's media counts. A failing statistics call only degrades that one entry."""
    entries: list[dict] = []
    for library in libraries:
        entry = {
            "id": library.get("id"),
            "name": library.get("name"),
            "refreshedAt": library.get("refreshedAt"),
            "updatedAt": library.get("updatedAt"),
            "statistics": None,
            "statisticsError": None,
            "scanQueued": False,
            "scanError": None,
            "after": None,
        }
        try:
            entry["statistics"] = client.statistics(entry["id"]) or None
        except ImmichError as exc:
            entry["statisticsError"] = str(exc)
            log(f"warning: no media counts for {entry['name']} ({entry['id']}): {exc}")
        entries.append(entry)
    return entries


def refresh_entries(client: LibraryClient, entries: list[dict], log: Logger, *, only_queued: bool = True) -> None:
    """Re-read the queued libraries after a finished scan and store the new state as `after`."""
    try:
        by_id = {str(library.get("id")): library for library in client.libraries()}
    except ImmichError as exc:
        log(f"warning: cannot re-read the libraries after the scan: {exc}")
        return

    for entry in entries:
        if only_queued and not entry["scanQueued"]:
            continue
        library = by_id.get(str(entry["id"])) or {}
        after = {
            "refreshedAt": library.get("refreshedAt"),
            "updatedAt": library.get("updatedAt"),
            "statistics": None,
            "statisticsError": None,
        }
        try:
            after["statistics"] = client.statistics(entry["id"]) or None
        except ImmichError as exc:
            after["statisticsError"] = str(exc)
            log(f"warning: no media counts after the scan for {entry['name']} ({entry['id']}): {exc}")
        entry["after"] = after


def print_library_lines(entries: list[dict], out, verb: str = "", describe=describe_library) -> None:
    for entry in entries:
        prefix = f"{verb} " if verb else ""
        out(f"  {prefix}{describe(entry)}")


def wait_for_scan(client: LibraryClient, args, now, out, log: Logger) -> tuple[dict, float | None, int]:
    """Poll the library queue until it drains. Returns (last state, elapsed seconds, exit code)."""
    timeout = args.wait_timeout or "none"
    out(f"waiting for queue {QUEUE} to finish (poll {args.poll_interval}s, timeout {timeout})")

    started = now()
    deadline = None if not args.wait_timeout else started + args.wait_timeout
    state = client.queue(QUEUE)

    while True:
        if not pending_jobs(state):
            elapsed = now() - started
            counts = counts_of(state)
            out(
                f"queue {QUEUE} is idle after {elapsed:.0f}s - "
                f"{counts.get('completed', 0)} completed, {counts.get('failed', 0)} failed (queue totals)"
            )
            return state, elapsed, 0
        if state.get("isPaused"):
            out(format_queue_state(state))
            log(f"error: queue {QUEUE} is paused and cannot finish; resume it in Administration -> Queues", force=True)
            return state, None, 1
        if deadline is not None and now() >= deadline:
            out(format_queue_state(state))
            log(f"error: timed out waiting {args.wait_timeout}s for queue {QUEUE} to finish", force=True)
            return state, None, 1
        log(f"  {QUEUE}: {pending_jobs(state)} job(s) pending", debug=True)
        client.sleep(args.poll_interval)
        state = client.queue(QUEUE)


def build_report(
    args,
    *,
    started: bool,
    libraries: list[dict],
    state: dict | None,
    waited: bool,
    elapsed: float | None,
) -> dict:
    return {
        "tool": REPORT_TOOL,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": args.url,
        "queue": QUEUE,
        "applied": bool(args.apply),
        "started": started,
        "libraries": libraries,
        "queueState": state,
        "waited": waited,
        "elapsed_seconds": None if elapsed is None else round(elapsed, 1),
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="external_library_scan.py",
        description="Scan Immich external libraries. Dry run unless --apply is given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Required API key permissions: library.read, library.update, library.statistics, queue.read\n"
            "All of those endpoints are admin-only, so the key must belong to an admin user.\n"
            "Endpoints: POST /api/libraries/{id}/scan, GET /api/libraries, GET /api/libraries/{id}/statistics,\n"
            "GET /api/queues/library. Nothing deprecated is used: 'all libraries' means one scan request\n"
            "per library, which (unlike the old scan-all job) does not queue the cleanup of libraries\n"
            "stuck in deletion.\n"
        ),
    )
    parser.add_argument("--url", default=os.environ.get("IMMICH_URL"), help="Immich base URL (env: IMMICH_URL)")
    parser.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="API key (env: IMMICH_API_KEY)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--status", action="store_true", help="report the library queue status instead of scanning")
    target.add_argument(
        "--library",
        action="append",
        metavar="ID|NAME",
        help="only this library, by uuid or exact name (repeatable)",
    )
    parser.add_argument("--apply", action="store_true", help="actually start the scans; without it the run is a dry run")
    parser.add_argument("--wait", action="store_true", help="wait for the library queue to finish")
    parser.add_argument(
        "--poll-interval",
        type=positive_int,
        default=DEFAULT_POLL_INTERVAL,
        metavar="SECONDS",
        help=f"seconds between queue status polls (default: {DEFAULT_POLL_INTERVAL})",
    )
    parser.add_argument(
        "--wait-timeout",
        type=non_negative_int,
        default=DEFAULT_WAIT_TIMEOUT,
        metavar="SECONDS",
        help=f"give up waiting after SECONDS; 0 waits forever (default: {DEFAULT_WAIT_TIMEOUT})",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON report on stdout, nothing else")
    parser.add_argument("--retries", type=non_negative_int, default=3, help="retries for 429/5xx/network errors (default: 3)")
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS", help="per-request timeout (default: 30)")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    return parser.parse_args(argv)


def main(argv=None, *, transport=None, sleep=time.sleep, log: Logger | None = None, now=time.monotonic) -> int:
    args = parse_args(argv)
    if not args.url or not args.api_key:
        print("error: --url/IMMICH_URL and --api-key/IMMICH_API_KEY are required", file=sys.stderr)
        return 2

    logger = log or Logger(quiet=args.quiet, verbose=args.verbose)
    base_url = normalize_url(args.url)
    client = LibraryClient(
        base_url,
        args.api_key.strip(),
        timeout=args.timeout,
        retries=args.retries,
        transport=transport if transport is not None else make_default_transport(args.insecure),
        sleep=sleep,
        log=logger,
    )
    logger(f"immich {base_url}")

    def out(line: str) -> None:
        """Results go to stdout - unless stdout is reserved for the --json report."""
        if args.json:
            logger(line)
        else:
            print(line)

    try:
        version = client.server_version()
    except ImmichError as exc:
        logger(f"error: cannot reach the Immich API: {exc}", force=True)
        return 2

    logger(f"server version {'.'.join(str(part) for part in version)}")

    if args.status:
        try:
            state = client.queue(QUEUE)
            libraries = client.libraries()
        except ImmichError as exc:
            logger(f"error: {exc}", force=True)
            return 2
        entries = library_entries(client, libraries, logger)
        out(format_queue_state(state))
        out(f"libraries: {len(entries)}")
        print_library_lines(entries, out)
        waited = False
        elapsed = None
        code = 0
        if args.wait:
            state, elapsed, code = wait_for_scan(client, args, now, out, logger)
            waited = True
            if code == 0:
                # The wait is over, so show what the libraries look like now.
                refresh_entries(client, entries, logger, only_queued=False)
                print_library_lines(entries, out, verb="REFRESHED", describe=describe_refreshed)
        if args.json:
            print(json.dumps(build_report(args, started=False, libraries=entries, state=state, waited=waited, elapsed=elapsed), indent=2))
        return code

    try:
        libraries = client.libraries()
    except ImmichError as exc:
        logger(f"error: {exc}", force=True)
        return 2

    if not libraries and not args.library:
        out("no external libraries found, nothing to scan")
        return 0

    chosen, problems = resolve_libraries(libraries, args.library or [])
    if problems:
        for problem in problems:
            logger(f"error: {problem}", force=True)
        logger("error: no scans started, fix the --library value(s) above", force=True)
        return 2

    entries = library_entries(client, chosen, logger)

    if not args.apply:
        print_library_lines(entries, out, verb="WOULD SCAN")
        out("DRY RUN: no scans started (add --apply to start)")
        if args.json:
            print(json.dumps(build_report(args, started=False, libraries=entries, state=None, waited=False, elapsed=None), indent=2))
        return 0

    # A library only counts as queued once its POST succeeded; failures do not stop the rest.
    for entry in entries:
        try:
            client.scan_library(entry["id"])
        except ImmichError as exc:
            entry["scanError"] = str(exc)
            logger(f"error: scan request failed for {entry['name']} ({entry['id']}): {exc}", force=True)
        else:
            entry["scanQueued"] = True

    queued = [entry for entry in entries if entry["scanQueued"]]
    print_library_lines(queued, out, verb="QUEUED")
    out(f"scan queued: {len(queued)} of {len(entries)}" if queued else "scan queued: none")

    state = None
    waited = False
    elapsed = None
    code = 0 if len(queued) == len(entries) else 1

    if args.wait:
        if not queued:
            out("nothing was queued, not waiting")
        else:
            state, elapsed, wait_code = wait_for_scan(client, args, now, out, logger)
            waited = True
            code = max(code, wait_code)
            if wait_code == 0:
                # The scans are done, so read the libraries again to show what changed.
                refresh_entries(client, entries, logger)
                print_library_lines(queued, out, verb="REFRESHED", describe=describe_refreshed)

    if args.json:
        print(json.dumps(build_report(args, started=bool(queued), libraries=entries, state=state, waited=waited, elapsed=elapsed), indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
