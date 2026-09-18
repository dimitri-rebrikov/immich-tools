# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Sync Immich albums with the results of saved search payloads.

Reads a JSON object that maps an album name to one or more Immich search
payloads - the body the Immich frontend POSTs to `/api/search/metadata`:

    {
      "Sommer 1996": [
        {"visibility": "timeline", "page": 1, "withExif": true,
         "takenAfter": "1995-01-01T00:00:00.000Z",
         "takenBefore": "1997-12-31T23:59:59.999Z", "isFavorite": true}
      ],
      "Ostfildern": [
        {"filter": {"city": {"eq": "Ostfildern"}}}
      ]
    }

Every album in that file is processed in order. The union of all search
results of an album is its desired content: a missing album is created, missing
assets are added, and assets that are in the album but no longer match the
search are removed (--no-remove keeps them).

Payloads are passed through untouched; only `size` is set and pagination is
driven by the script (cursor for the structured v3 shape, `page`/`nextPage` for
the deprecated flat shape). The album content is read with a search on
`filter.albumIds`, because the v3 album API does not return asset ids. Assets
that are invisible to search (locked, trashed) are never touched.

Planning is all-or-nothing: if any album fails to plan (bad input, API error,
ambiguous name), nothing at all is written. The apply phase is per album, so a
failing album does not stop the others.

Only asset metadata (JSON) is fetched - image bytes are never downloaded.
Dry run by default: nothing is written unless --apply is passed.

Usage:
    IMMICH_URL=https://immich.example.com IMMICH_API_KEY=xxx uv run conditional_albums.py --file albums.json
    IMMICH_URL=... IMMICH_API_KEY=... uv run conditional_albums.py --file albums.json --apply
    uv run conditional_albums.py --json '{"Test": [{"filter": {"isFavorite": {"eq": true}}}]}'

Required API key permissions: asset.read, album.read, album.create,
albumAsset.create, albumAsset.delete
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from immich_api import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PAGE_SIZE,
    MIN_SUPPORTED_VERSION,
    ImmichClient,
    ImmichError,
    Logger,
    bounded_int,
    make_default_transport,
    non_negative_int,
    normalize_url,
    positive_int,
)

REPORT_TOOL = "conditional_albums"
NEW_SHAPE_FIELDS = ("filter", "orderBy", "cursor")  # v3 search shape (v3.2.0+)
HARMLESS_ERRORS = {"duplicate"}  # already in the album - not a failure
PREVIEW_LIMIT = 10  # asset ids printed per album before "... (+N more)"


class SpecError(ValueError):
    """Invalid album specification; the message is safe to show a user."""


@dataclass
class AlbumPlan:
    """Desired vs. current album content, computed without writing anything."""

    name: str
    album_id: str | None
    matched: list[str]
    contents: list[str]
    to_add: list[str]
    to_remove: list[str]
    asset_count: int | None = None


@dataclass
class AlbumResult:
    """What happened (or, in a dry run, what would happen) for one album."""

    plan: AlbumPlan
    album_id: str | None
    created: bool = False
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped_removals: int = 0
    errors: dict[str, list[str]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        """Per-asset failures that mean the desired state was not reached."""
        return sum(len(ids) for reason, ids in self.errors.items() if reason not in HARMLESS_ERRORS)

    @property
    def ok(self) -> bool:
        return not self.failures and self.failed == 0


class AlbumClient(ImmichClient):
    """The shared client plus the album endpoints this tool needs."""

    permissions = "album.read, album.create, albumAsset.create, albumAsset.delete, asset.read"

    def albums_named(self, name: str) -> list[dict]:
        query = urlencode({"name": name, "isOwned": "true"})
        return self.request("GET", f"/api/albums?{query}") or []

    def create_album(self, name: str) -> dict:
        album = self.request("POST", "/api/albums", {"albumName": name})
        if not isinstance(album, dict) or not album.get("id"):
            raise ImmichError(200, "/api/albums", "response has no album id")
        return album

    def add_album_assets(self, album_id: str, ids: list[str]) -> list[dict]:
        return self.request("PUT", f"/api/albums/{album_id}/assets", {"ids": ids}) or []

    def remove_album_assets(self, album_id: str, ids: list[str]) -> list[dict]:
        return self.request("DELETE", f"/api/albums/{album_id}/assets", {"ids": ids}) or []

    def album_asset_ids(self, album_id: str, page_size: int, log: Logger) -> list[str]:
        """Read the current album content.

        The v3 album responses have no assets array, so a search on
        `filter.albumIds` is the only way to list them. Such a filter is
        "album confined", which is why assets owned by other users are
        returned too. `trashedAt` has to be set explicitly: the v3 search does
        not exclude trashed assets on its own.
        """
        base = {
            "filter": {"albumIds": {"any": [album_id]}, "trashedAt": {"eq": None}},
            "withExif": False,
            "size": page_size,
        }
        return search_ids(client=self, base=base, legacy=False, log=log, what="album content")


def parse_spec(text: str) -> list[tuple[str, list[dict]]]:
    """Validate the album specification and return [(album name, [payload, ...])]."""
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"invalid JSON: {exc}") from exc

    if not isinstance(spec, dict):
        raise SpecError('top level must be a JSON object: {"Album name": [<search>, ...]}')
    if not spec:
        raise SpecError("no albums in the input")

    entries: list[tuple[str, list[dict]]] = []
    for name, payloads in spec.items():
        if not name.strip():
            raise SpecError("album name must not be empty")
        if name != name.strip():
            raise SpecError(f'album name "{name}" has leading or trailing whitespace')
        if not isinstance(payloads, list) or not payloads:
            raise SpecError(f'album "{name}": value must be a non-empty list of search payloads')
        for index, payload in enumerate(payloads, start=1):
            if not isinstance(payload, dict) or not payload:
                raise SpecError(f'album "{name}" search #{index}: must be a non-empty JSON object')
        entries.append((name, payloads))
    return entries


def read_spec(args) -> str:
    if args.json is not None:
        return args.json
    try:
        return Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read {args.file}: {exc}") from exc


def prepare_search(payload: dict, page_size: int, name: str, index: int, log: Logger) -> tuple[dict, bool]:
    """Return (search body, legacy pagination?) for one payload.

    The payload itself is passed through unchanged - it defines what matches.
    Only paging is normalized: `size` is set to --page-size, and a `page` field
    is dropped when it would be mixed with the structured shape (the server
    rejects that combination with a 400).
    """
    body = dict(payload)
    body["size"] = page_size
    legacy = not any(field in body for field in NEW_SHAPE_FIELDS)
    if legacy:
        body["page"] = 1
        return body, True

    if "page" in body:
        del body["page"]
        log(f'  album "{name}" search #{index}: removed "page", pagination uses the cursor')
    body.pop("cursor", None)  # a stale cursor would skip results
    return body, False


def page_assets(data: dict, legacy: bool) -> tuple[list[dict], str | None]:
    assets = data.get("assets") or {}
    items = assets.get("items") or []
    token = assets.get("nextPage") if legacy else assets.get("nextCursor")
    return items, token or None


def search_ids(*, client: ImmichClient, base: dict, legacy: bool, log: Logger, what: str) -> list[str]:
    """Walk every page of a search body and return the asset ids in order."""
    ids: list[str] = []
    pages = 0
    token: str | None = None
    page_number = 1
    seen: set[str] = set()

    while True:
        body = dict(base)
        if legacy:
            body["page"] = page_number  # never mix `page` into the structured shape
            body.pop("cursor", None)
        elif token:
            body["cursor"] = token

        data = client.search(body)
        items, token = page_assets(data, legacy)
        pages += 1
        ids.extend(item["id"] for item in items if isinstance(item, dict) and item.get("id"))
        log(f"  {what}: page {pages}, {len(ids)} asset(s) found", debug=True)

        if not items or not token or token in seen:
            break
        seen.add(token)
        if legacy:
            try:
                page_number = int(token)
            except ValueError:  # not a page number - stop instead of looping
                break
            continue

    return list(dict.fromkeys(ids))


def plan_album(client: AlbumClient, name: str, payloads: list[dict], args, log: Logger) -> AlbumPlan:
    """Compute the desired and current content of one album. Read-only."""
    albums = client.albums_named(name)
    if len(albums) > 1:
        found = ", ".join(str(album.get("id")) for album in albums)
        raise SpecError(f'album "{name}" is ambiguous: {len(albums)} owned albums have that name ({found})')

    album = albums[0] if albums else None
    album_id = str(album["id"]) if album and album.get("id") else None

    matched: list[str] = []
    for index, payload in enumerate(payloads, start=1):
        body, legacy = prepare_search(payload, args.page_size, name, index, log)
        matched.extend(
            search_ids(
                client=client,
                base=body,
                legacy=legacy,
                log=log,
                what=f'album "{name}" search #{index}',
            )
        )
    matched = list(dict.fromkeys(matched))

    contents = client.album_asset_ids(album_id, args.page_size, log) if album_id else []
    matched_set = set(matched)
    contents_set = set(contents)

    asset_count = album.get("assetCount") if album else None
    if isinstance(asset_count, int) and asset_count > len(contents):
        log(
            f'warning: album "{name}": {asset_count - len(contents)} of {asset_count} asset(s) are not '
            "visible to search (locked or trashed); they are left untouched",
            force=True,
        )

    return AlbumPlan(
        name=name,
        album_id=album_id,
        matched=matched,
        contents=contents,
        to_add=[asset_id for asset_id in matched if asset_id not in contents_set],
        to_remove=[asset_id for asset_id in contents if asset_id not in matched_set],
        asset_count=asset_count if isinstance(asset_count, int) else None,
    )


def preview(ids: list[str]) -> str:
    shown = " ".join(ids[:PREVIEW_LIMIT])
    if len(ids) > PREVIEW_LIMIT:
        return f"{shown} ... (+{len(ids) - PREVIEW_LIMIT} more)"
    return shown


def write_chunk(client: AlbumClient, album_id: str, ids: list[str], *, add: bool, log: Logger):
    """Send one batch and return (confirmed ids, errors by reason, failures)."""
    verb = "add" if add else "remove"
    call = client.add_album_assets if add else client.remove_album_assets
    try:
        results = call(album_id, ids)
    except ImmichError as exc:
        log(f"  {verb} batch of {len(ids)} asset(s) failed: {exc}")
        return [], {}, [f"{len(ids)} asset(s) to {verb}: {exc}"]

    confirmed: list[str] = []
    reported: set[str] = set()
    errors: dict[str, list[str]] = {}
    for entry in results if isinstance(results, list) else []:
        asset_id = entry.get("id") if isinstance(entry, dict) else None
        if not asset_id:
            continue
        reported.add(str(asset_id))
        if entry.get("success"):
            confirmed.append(str(asset_id))
        else:
            reason = str(entry.get("error") or "unknown")
            errors.setdefault(reason, []).append(str(asset_id))

    unreported = [asset_id for asset_id in ids if asset_id not in reported]
    if unreported:
        errors.setdefault("unreported", []).extend(unreported)
    return confirmed, errors, []


def apply_album(client: AlbumClient, plan: AlbumPlan, args, log: Logger) -> AlbumResult:
    """Create/add/remove for one album. Failures are recorded, never raised."""
    result = AlbumResult(plan=plan, album_id=plan.album_id)
    if result.album_id is None:
        album = client.create_album(plan.name)
        result.album_id = str(album["id"])
        result.created = True
        log(f'album "{plan.name}": created {result.album_id}')

    if args.no_remove:
        result.skipped_removals = len(plan.to_remove)
    batches = [(plan.to_add, True)]
    if plan.to_remove and not args.no_remove:
        batches.append((plan.to_remove, False))
    for ids, add in batches:
        for start in range(0, len(ids), args.batch_size):
            confirmed, errors, failures = write_chunk(
                client, result.album_id, ids[start : start + args.batch_size], add=add, log=log
            )
            (result.added if add else result.removed).extend(confirmed)
            for reason, reasons_ids in errors.items():
                result.errors.setdefault(reason, []).extend(reasons_ids)
            result.failures.extend(failures)

    return result


def dry_result(plan: AlbumPlan, args) -> AlbumResult:
    """The report/dry-run view of a plan: intended changes, nothing written."""
    return AlbumResult(
        plan=plan,
        album_id=plan.album_id,
        created=plan.album_id is None,
        added=plan.to_add,
        removed=[] if args.no_remove else plan.to_remove,
        skipped_removals=len(plan.to_remove) if args.no_remove else 0,
    )


def where(result: AlbumResult) -> str:
    if result.plan.album_id:  # the album already existed when it was planned
        return result.plan.album_id
    return f"created {result.album_id}" if result.album_id else "missing"


def print_plan(result: AlbumResult, args, out=print) -> None:
    """Dry-run output for one album."""
    plan = result.plan
    out(f'album "{plan.name}" ({where(result)}): matched {len(plan.matched)}, in album {len(plan.contents)}')
    if plan.album_id is None:
        if plan.to_add:
            out(f"  WOULD CREATE the album and add {len(plan.to_add)} asset(s): {preview(plan.to_add)}")
        else:
            out("  WOULD CREATE the empty album")
        return
    if plan.to_add:
        out(f"  WOULD ADD {len(plan.to_add)} asset(s): {preview(plan.to_add)}")
    if plan.to_remove:
        suffix = " (skipped: --no-remove)" if args.no_remove else ""
        out(f"  WOULD REMOVE {len(plan.to_remove)} asset(s): {preview(plan.to_remove)}{suffix}")
    if not plan.to_add and not plan.to_remove:
        out("  already up to date")


def print_result(result: AlbumResult, out=print) -> None:
    """Applied output for one album."""
    plan = result.plan
    out(f'album "{plan.name}" ({where(result)}): matched {len(plan.matched)}, in album {len(plan.contents)}')
    if result.created:
        out("  created the album")
    if result.added or result.removed:
        out(f"  added {len(result.added)} asset(s), removed {len(result.removed)} asset(s)")
    if result.skipped_removals:
        out(f"  left {result.skipped_removals} non-matching asset(s) in the album (--no-remove)")
    if not result.added and not result.removed and not result.created:
        out("  already up to date")
    for reason, ids in sorted(result.errors.items()):
        out(f"  {reason}: {len(ids)} asset(s) {preview(ids)}")


def summarize(results: list[AlbumResult], *, applied: bool) -> list[str]:
    created = sum(1 for result in results if result.created)
    lines = [f"albums: {len(results)}" + (f" ({created} created)" if applied and created else "")]
    error_counts: dict[str, int] = {}
    for result in results:
        lines.extend(f"  failed: {failure}" for failure in result.failures)
        for reason, ids in result.errors.items():
            error_counts[reason] = error_counts.get(reason, 0) + len(ids)

    if applied:
        lines.append(
            f"assets added: {sum(len(result.added) for result in results)}, "
            f"removed: {sum(len(result.removed) for result in results)}, "
            f"skipped removals: {sum(result.skipped_removals for result in results)}"
        )
    else:
        lines.append(
            f"assets to add: {sum(len(result.plan.to_add) for result in results)}, "
            f"to remove: {sum(len(result.plan.to_remove) for result in results)}"
        )
    if error_counts:
        detail = ", ".join(f"{reason}={count}" for reason, count in sorted(error_counts.items()))
        lines.append(f"per-asset errors: {detail}")
    return lines


def report_entry(result: AlbumResult) -> dict:
    """One album in the --json-report file."""
    entry = {
        "name": result.plan.name,
        "albumId": result.album_id,
        "created": result.created,
        "matched": len(result.plan.matched),
        "assetCount": result.plan.asset_count,
        "added": result.added,
        "removed": result.removed,
    }
    if result.skipped_removals:
        entry["skippedRemovals"] = result.skipped_removals
    if result.errors:
        entry["errors"] = {reason: ids for reason, ids in sorted(result.errors.items())}
    if result.failures:
        entry["failures"] = result.failures
    return entry


def write_report(path: str, args, results: list[AlbumResult], applied: bool) -> None:
    report = {
        "tool": REPORT_TOOL,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": args.url,
        "applied": applied,
        "albums": [report_entry(result) for result in results],
    }
    Path(path).write_text(json.dumps(report, indent=2), encoding="utf-8")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="conditional_albums.py",
        description="Create/sync Immich albums from search payloads. Dry run unless --apply is given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Required API key permissions: asset.read, album.read, album.create, "
            "albumAsset.create, albumAsset.delete\n"
            "Needs Immich 3.2.0 or newer (structured search filter with albumIds).\n"
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", metavar="PATH", help="JSON file: {\"Album name\": [<search payload>, ...]}")
    source.add_argument("--json", metavar="JSON", help="the same document as an inline string")
    parser.add_argument("--url", default=os.environ.get("IMMICH_URL"), help="Immich base URL (env: IMMICH_URL)")
    parser.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="API key (env: IMMICH_API_KEY)")
    parser.add_argument("--apply", action="store_true", help="actually write; without it the run is a dry run")
    parser.add_argument("--no-remove", action="store_true", help="only add matching assets, never remove any")
    parser.add_argument("--page-size", type=bounded_int(1, 1000), default=DEFAULT_PAGE_SIZE, help="search page size, 1-1000 (default: 250)")
    parser.add_argument("--batch-size", type=positive_int, default=DEFAULT_BATCH_SIZE, help="assets per album update request (default: 500)")
    parser.add_argument("--json-report", metavar="PATH", help="write the planned/applied changes to PATH")
    parser.add_argument("--retries", type=non_negative_int, default=3, help="retries for 429/5xx/network errors (default: 3)")
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS", help="per-request timeout (default: 30)")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    return parser.parse_args(argv)


def main(argv=None, *, transport=None, sleep=time.sleep, log: Logger | None = None) -> int:
    args = parse_args(argv)
    if not args.url or not args.api_key:
        print("error: --url/IMMICH_URL and --api-key/IMMICH_API_KEY are required", file=sys.stderr)
        return 2

    logger = log or Logger(quiet=args.quiet, verbose=args.verbose)
    try:
        entries = parse_spec(read_spec(args))
    except SpecError as exc:
        logger(f"error: {exc}", force=True)
        return 2

    base_url = normalize_url(args.url)
    client = AlbumClient(
        base_url,
        args.api_key.strip(),
        timeout=args.timeout,
        retries=args.retries,
        transport=transport if transport is not None else make_default_transport(args.insecure),
        sleep=sleep,
        log=logger,
    )
    logger(f"immich {base_url}")

    try:
        version = client.server_version()
    except ImmichError as exc:
        logger(f"error: cannot reach the Immich API: {exc}", force=True)
        return 2

    logger(f"server version {'.'.join(str(part) for part in version)}")
    if version < MIN_SUPPORTED_VERSION:
        wanted = ".".join(str(part) for part in MIN_SUPPORTED_VERSION)
        logger(
            f"error: Immich {wanted}+ required (structured search filter with albumIds), "
            f"found {'.'.join(str(part) for part in version)}",
            force=True,
        )
        return 2

    # Planning is read-only and all-or-nothing: one bad album aborts before any write.
    plans: list[AlbumPlan] = []
    try:
        for name, payloads in entries:
            plans.append(plan_album(client, name, payloads, args, logger))
    except (ImmichError, SpecError) as exc:
        logger(f"error: {exc} (nothing was written)", force=True)
        return 2

    if not args.apply:
        results = [dry_result(plan, args) for plan in plans]
        for result in results:
            print_plan(result, args)
        print("DRY RUN: no changes made (add --apply to write)")
        for line in summarize(results, applied=False):
            print(line)
        if args.json_report:
            write_report(args.json_report, args, results, applied=False)
            print(f"report written to {args.json_report}")
        return 0

    results = []
    for plan in plans:
        result = apply_album(client, plan, args, logger)
        results.append(result)
        print_result(result)

    for line in summarize(results, applied=True):
        print(line)
    if args.json_report:
        write_report(args.json_report, args, results, applied=True)
        print(f"report written to {args.json_report}")

    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
