"""Shared HTTP plumbing for the immich-tools scripts.

Not a standalone script: it has no CLI and no PEP 723 header. Imported by
`favorite_rated.py` and `conditional_albums.py` so the API client, retry logic
and argument helpers exist only once.

Only asset/API-key mechanics live here. Endpoint-specific call wrappers belong
to the script that needs them.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MIN_SUPPORTED_VERSION = (3, 2, 0)  # MetadataSearchDto.filter (structured search) was added in v3.2.0
DEFAULT_PAGE_SIZE = 250  # server allows 1..1000
DEFAULT_BATCH_SIZE = 500
RETRY_STATUS = {429, 500, 502, 503, 504}


class ImmichError(RuntimeError):
    """A failed Immich API call, with a message that is safe to show a user."""

    def __init__(self, status: int, path: str, detail: str = "", hint: str = "") -> None:
        if status == 401:
            hint = " (check --api-key)"
        elif status == 403:
            needed = hint or "asset.read/asset.update/user.read"
            hint = f" (the API key lacks a needed permission: {needed}, or the object is not accessible to its user)"
        else:
            hint = ""
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
    # Named in HTTP 403 messages; subclasses narrow this to the permissions they use.
    permissions = "asset.read/asset.update/user.read"

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
                raise ImmichError(status, path, _decode(raw).strip().replace("\n", " ")[:300], self.permissions)
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


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return number


def bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number

    return parse
