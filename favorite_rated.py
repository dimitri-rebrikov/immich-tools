# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Auto-favorite Immich images that carry a star rating.

Finds images owned by the API key's user with a star rating >= --min-rating
(default 3) that are not marked as favorite yet, and sets the favorite flag.

Targets the current Immich search API (v3.2+, structured `filter` + cursor
pagination) and writes with `PATCH /api/assets`. Only asset metadata (JSON) is
fetched - image bytes are never downloaded.

Dry run by default: nothing is written unless --apply is passed.

Usage:
    IMMICH_URL=https://immich.example.com IMMICH_API_KEY=xxx uv run favorite_rated.py
    IMMICH_URL=... IMMICH_API_KEY=... uv run favorite_rated.py --apply --limit 10
    uv run favorite_rated.py --revert favorites-2026-09-16.json --apply

Required API key permissions: asset.read, asset.update, user.read
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MIN_SUPPORTED_VERSION = (3, 2, 0)  # MetadataSearchDto.filter was added in v3.2.0
DEFAULT_PAGE_SIZE = 250  # server allows 1..1000
DEFAULT_BATCH_SIZE = 500
RETRY_STATUS = {429, 500, 502, 503, 504}
REPORT_TOOL = "favorite_rated"


class ImmichError(RuntimeError):
    """A failed Immich API call, with a message that is safe to show a user."""

    def __init__(self, status: int, path: str, detail: str = "") -> None:
        hint = ""
        if status == 401:
            hint = " (check --api-key)"
        elif status == 403:
            hint = " (API key lacks a needed permission such as asset.read/asset.update/user.read, or an asset is not owned by the key's user)"
        label = f"HTTP {status}" if status else "request failed"
        message = f"{label} on {path}{hint}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.status = status
        self.path = path
        self.detail = detail


class Logger:
    """Progress goes to stderr, results go to stdout."""

    def __init__(self, quiet: bool = False, verbose: bool = False, stream=None) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.stream = stream if stream is not None else sys.stderr

    def __call__(self, message: str, *, debug: bool = False, force: bool = False) -> None:
        """`debug` needs --verbose; `force` wins over --quiet (errors must always be visible)."""
        if debug and not self.verbose:
            return
        if not debug and self.quiet and not force:
            return
        print(message, file=self.stream, flush=True)


def _decode(raw: bytes | None) -> str:
    return raw.decode("utf-8", "replace") if raw else ""


def _parse_json(raw: bytes | None, status: int, path: str):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImmichError(status, path, f"invalid JSON response: {exc}") from exc


def make_default_transport(insecure: bool = False):
    """Return a transport(method, url, headers, body, timeout) -> (status, bytes)."""
    context = ssl._create_unverified_context() if insecure else None

    def transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:  # still a real HTTP answer
            return error.code, error.read()

    return transport


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        transport=None,
        sleep=time.sleep,
        log: Logger | None = None,
    ) -> None:
        self.base_url = normalize_url(base_url)
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.transport = transport or make_default_transport(False)
        self.sleep = sleep
        self.log = log or Logger()

    def request(self, method: str, path: str, payload: dict | None = None):
        url = f"{self.base_url}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"accept": "application/json", "x-api-key": self.api_key}
        if body is not None:
            headers["content-type"] = "application/json"

        attempt = 0
        while True:
            self.log(f"{method} {url}{f' ({len(body)} bytes)' if body else ''}", debug=True)
            try:
                status, raw = self.transport(method, url, headers, body, self.timeout)
            except (urllib.error.URLError, OSError) as exc:
                if attempt < self.retries:
                    attempt += 1
                    self.log(f"  network error ({exc}), retry {attempt}/{self.retries}", debug=True)
                    self.sleep(2 ** (attempt - 1))
                    continue
                raise ImmichError(0, path, f"network error: {exc}") from exc

            if status in RETRY_STATUS and attempt < self.retries:
                attempt += 1
                self.log(f"  HTTP {status}, retry {attempt}/{self.retries}", debug=True)
                self.sleep(2 ** (attempt - 1))
                continue
            if status >= 400:
                raise ImmichError(status, path, _decode(raw).strip().replace("\n", " ")[:300])
            return _parse_json(raw, status, path)

    def server_version(self) -> tuple[int, int, int]:
        info = self.request("GET", "/api/server/version") or {}
        return (
            int(info.get("major", 0) or 0),
            int(info.get("minor", 0) or 0),
            int(info.get("patch", 0) or 0),
        )

    def me(self) -> str:
        info = self.request("GET", "/api/users/me") or {}
        user_id = info.get("id")
        if not user_id:
            raise ImmichError(200, "/api/users/me", "response has no user id")
        return str(user_id)

    def search(self, payload: dict) -> dict:
        return self.request("POST", "/api/search/metadata", payload) or {}

    def update_assets(self, ids: list[str], favorite: bool) -> None:
        self.request("PATCH", "/api/assets", {"ids": ids, "isFavorite": favorite})


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not urlsplit(url).scheme:
        url = f"https://{url}"
    return url


def build_search_payload(min_rating: int, page_size: int, cursor: str | None = None) -> dict:
    """Search only returns own+partner assets, so ownership is re-checked client side."""
    payload = {
        "filter": {
            "type": {"eq": "IMAGE"},
            "rating": {"gte": min_rating},
            "isFavorite": {"eq": False},
            "trashedAt": {"eq": None},  # v3 search does not exclude trashed assets
        },
        "orderBy": {"field": "fileCreatedAt", "direction": "desc"},
        "withExif": True,
        "size": page_size,
    }
    if cursor:
        payload["cursor"] = cursor
    return payload


def skip_reason(asset: dict, user_id: str, min_rating: int) -> str | None:
    """None means the asset is a candidate; otherwise a reason it was skipped."""
    if asset.get("ownerId") != user_id:
        return "not-owned"
    if asset.get("visibility") == "locked":
        return "locked"
    if asset.get("isFavorite"):
        return "already-favorite"
    rating = (asset.get("exifInfo") or {}).get("rating")
    if isinstance(rating, bool) or not isinstance(rating, (int, float)):
        return "rating"
    if rating < min_rating:
        return "rating"
    return None


def collect_candidates(client: ImmichClient, user_id: str, args, log: Logger):
    candidates: list[dict] = []
    skipped = {"not-owned": 0, "locked": 0, "already-favorite": 0, "rating": 0}
    cursor = None
    pages = seen = 0

    while True:
        data = client.search(build_search_payload(args.min_rating, args.page_size, cursor))
        assets = data.get("assets") or {}
        items = assets.get("items") or []
        pages += 1
        seen += len(items)

        for asset in items:
            reason = skip_reason(asset, user_id, args.min_rating)
            if reason is None:
                candidates.append(asset)
            else:
                skipped[reason] = skipped.get(reason, 0) + 1

        log(f"page {pages}: {len(items)} asset(s), {len(candidates)} candidate(s) so far")

        if args.limit and len(candidates) >= args.limit:
            candidates = candidates[: args.limit]
            log(f"limit of {args.limit} candidate(s) reached, stopping")
            break
        cursor = assets.get("nextCursor")
        if not cursor or not items:
            break

    return candidates, {"pages": pages, "seen": seen, "skipped": skipped}


def apply_favorite(client: ImmichClient, ids: list[str], *, favorite: bool, batch_size: int, log: Logger) -> list[str]:
    failures: list[str] = []
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        try:
            client.update_assets(batch, favorite)
        except ImmichError as exc:
            failures.append(f"{len(batch)} asset(s) starting at {batch[0]}: {exc}")
            log(f"  batch {start // batch_size + 1} failed: {exc}")
        else:
            log(f"  batch {start // batch_size + 1}: {len(batch)} asset(s) set isFavorite={str(favorite).lower()}")
    return failures


def write_report(path: str, args, candidates: list[dict], applied: bool) -> None:
    report = {
        "tool": REPORT_TOOL,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "server": args.url,
        "min_rating": args.min_rating,
        "applied": applied,
        "count": len(candidates),
        "assets": [
            {
                "id": asset.get("id"),
                "rating": (asset.get("exifInfo") or {}).get("rating"),
                "fileName": asset.get("originalFileName"),
                "takenAt": asset.get("localDateTime"),
            }
            for asset in candidates
        ],
    }
    Path(path).write_text(json.dumps(report, indent=2), encoding="utf-8")


def describe(asset: dict) -> str:
    rating = (asset.get("exifInfo") or {}).get("rating")
    return f"rating={rating} {asset.get('localDateTime', '?')} {asset.get('originalFileName', '?')} {asset.get('id')}"


def revert_run(client: ImmichClient, args, log: Logger) -> int:
    try:
        payload = json.loads(Path(args.revert).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"error: cannot read report {args.revert}: {exc}", force=True)
        return 2

    ids = [entry.get("id") for entry in payload.get("assets") or [] if entry.get("id")]
    if not ids:
        print(f"nothing to revert in {args.revert}")
        return 0

    print(f"revert: {len(ids)} asset(s) from {args.revert} (server {payload.get('server', 'unknown')})")
    if not args.apply:
        print("DRY RUN: would remove the favorite flag from those assets (add --apply to write)")
        return 0

    failures = apply_favorite(client, ids, favorite=False, batch_size=args.batch_size, log=log)
    print(f"reverted: {len(ids) - sum(int(f.split()[0]) for f in failures)} asset(s), {len(failures)} failed batch(es)")
    return 1 if failures else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="favorite_rated.py",
        description="Favorite Immich images with a star rating >= --min-rating. Dry run unless --apply is given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Required API key permissions: asset.read, asset.update, user.read\n"
            "Needs Immich 3.2.0 or newer (structured search filter).\n"
        ),
    )
    parser.add_argument("--url", default=os.environ.get("IMMICH_URL"), help="Immich base URL (env: IMMICH_URL)")
    parser.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="API key (env: IMMICH_API_KEY)")
    parser.add_argument("--min-rating", type=_rating, default=3, metavar="N", help="minimum star rating 1-5 (default: 3)")
    parser.add_argument("--apply", action="store_true", help="actually write; without it the run is a dry run")
    parser.add_argument("--limit", type=_positive, metavar="N", help="stop after N candidates (canary runs)")
    parser.add_argument("--page-size", type=_bounded(1, 1000), default=DEFAULT_PAGE_SIZE, help="search page size, 1-1000 (default: 250)")
    parser.add_argument("--batch-size", type=_positive, default=DEFAULT_BATCH_SIZE, help="assets per update request (default: 500)")
    parser.add_argument("--retries", type=_non_negative, default=3, help="retries for 429/5xx/network errors (default: 3)")
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS", help="per-request timeout (default: 30)")
    parser.add_argument("--json-report", metavar="PATH", help="write the affected assets to PATH (usable with --revert)")
    parser.add_argument("--revert", metavar="PATH", help="un-favorite the assets listed in a previous --json-report file")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    return parser.parse_args(argv)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return number


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return number


def _rating(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 5:
        raise argparse.ArgumentTypeError("rating must be between 1 and 5")
    return number


def _bounded(low: int, high: int):
    def parse(value: str) -> int:
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number

    return parse


def main(argv=None, *, transport=None, sleep=time.sleep, log: Logger | None = None) -> int:
    args = parse_args(argv)
    if not args.url or not args.api_key:
        print("error: --url/IMMICH_URL and --api-key/IMMICH_API_KEY are required", file=sys.stderr)
        return 2

    logger = log or Logger(quiet=args.quiet, verbose=args.verbose)
    base_url = normalize_url(args.url)
    client = ImmichClient(
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
            f"error: Immich {wanted}+ required (structured search filter), found {'.'.join(str(part) for part in version)}",
            force=True,
        )
        return 2

    if args.revert:
        return revert_run(client, args, logger)

    try:
        user_id = client.me()
        candidates, stats = collect_candidates(client, user_id, args, logger)
    except ImmichError as exc:
        logger(f"error: {exc}", force=True)
        return 2

    skipped = stats["skipped"]
    print(f"scanned {stats['pages']} page(s), {stats['seen']} asset(s)")
    print(f"candidates: {len(candidates)}")
    print(
        "skipped: "
        f"not-owned={skipped.get('not-owned', 0)} "
        f"locked={skipped.get('locked', 0)} "
        f"already-favorite={skipped.get('already-favorite', 0)} "
        f"rating={skipped.get('rating', 0)}"
    )

    if not candidates:
        print("nothing to do")
        return 0

    verdict = "FAVORITED" if args.apply else "WOULD FAVORITE"
    for asset in candidates:
        print(f"  {verdict} {describe(asset)}")

    if not args.apply:
        print("DRY RUN: no changes made (add --apply to write)")
        return 0

    ids = [asset["id"] for asset in candidates]
    failures = apply_favorite(client, ids, favorite=True, batch_size=args.batch_size, log=logger)

    if args.json_report:
        write_report(args.json_report, args, candidates, applied=True)
        print(f"report written to {args.json_report}")

    written = len(ids) - sum(int(failure.split()[0]) for failure in failures)
    print(f"favorited: {written} asset(s) in {(len(ids) - 1) // args.batch_size + 1} batch(es)")
    for failure in failures:
        print(f"  failed: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
