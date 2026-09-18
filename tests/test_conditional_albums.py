# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Tests for conditional_albums.py. No network access: the HTTP layer is faked.

Run with:  uv run tests/test_conditional_albums.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:  # the script imports immich_api from the repo root
    sys.path.insert(0, str(SCRIPT_ROOT))

SCRIPT_PATH = SCRIPT_ROOT / "conditional_albums.py"
_spec = importlib.util.spec_from_file_location("conditional_albums", SCRIPT_PATH)
conditional_albums = importlib.util.module_from_spec(_spec)
sys.modules["conditional_albums"] = conditional_albums
_spec.loader.exec_module(conditional_albums)


def asset(asset_id: str) -> dict:
    """A trimmed search result item - the tool only reads `id`."""
    return {"id": asset_id, "originalFileName": f"{asset_id}.jpg", "visibility": "timeline"}


def album_record(album_id: str, name: str, assets=(), asset_count=None) -> dict:
    return {
        "id": album_id,
        "albumName": name,
        "assetCount": len(assets) if asset_count is None else asset_count,
        "assets": list(assets),
    }


def normalize_pages(pages):
    """Accept [[page, ...]] for a single payload search, or [[[page, ...]], ...] per search."""
    if pages and all(entry and isinstance(entry[0], dict) for entry in pages):
        return [pages]
    return pages


class FakeImmich:
    """Serves the search/album endpoints and records every request.

    Album content reads (`filter.albumIds`) are answered from the album state, so
    adds and removals are visible on the next call. Payload searches consume the
    `pages` queue: one entry per payload search, each entry a list of its pages.
    """

    def __init__(
        self,
        *,
        version=(3, 3, 1),
        albums=None,
        pages=None,
        hidden_assets=(),
        no_permission=(),
        search_status=None,
        add_statuses=None,
        remove_statuses=None,
        albums_status=200,
        create_status=201,
    ):
        self.version = version
        self.albums: dict[str, list[dict]] = albums if albums is not None else {}
        self.pages = normalize_pages(pages) if pages is not None else [[[]]]  # [search][page][item]
        self.hidden_assets = set(hidden_assets)  # in the album but invisible to search (locked)
        self.no_permission = set(no_permission)
        self.search_status = search_status
        self.add_statuses = list(add_statuses or [])
        self.remove_statuses = list(remove_statuses or [])
        self.albums_status = albums_status
        self.create_status = create_status

        self.methods: list[tuple[str, str]] = []
        self.search_bodies: list[dict] = []
        self.payload_searches: list[dict] = []
        self.content_requests: list[str] = []
        self.create_requests: list[dict] = []
        self.add_requests: list[tuple[str, list[str]]] = []
        self.remove_requests: list[tuple[str, list[str]]] = []

    # --- HTTP ---------------------------------------------------------------

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        payload = json.loads(body) if body else None
        self.methods.append((method, path))

        if path == "/api/server/version":
            major, minor, patch = self.version
            return 200, json.dumps({"major": major, "minor": minor, "patch": patch}).encode()
        if path == "/api/search/metadata":
            return self.search(payload)
        if path == "/api/albums" and method == "GET":
            if self.albums_status >= 400:
                return self.albums_status, b'{"message":"forbidden"}'
            name = (query.get("name") or [""])[0]
            return 200, json.dumps([self.public(record) for record in self.albums.get(name, [])]).encode()
        if path == "/api/albums" and method == "POST":
            return self.create(payload)
        if path.startswith("/api/albums/") and method in {"PUT", "DELETE"}:
            return self.mutate(method, path.split("/")[3], payload)
        raise AssertionError(f"unexpected call: {method} {path}")

    def search(self, payload: dict) -> tuple[int, bytes]:
        self.search_bodies.append(payload)
        if self.search_status and self.search_status >= 400:
            return self.search_status, b'{"message":"search failed"}'

        album_filter = (payload.get("filter") or {}).get("albumIds")
        if album_filter:
            return self.content(album_filter["any"][0], payload)
        return self.payload_search(payload)

    def content(self, album_id: str, payload: dict) -> tuple[int, bytes]:
        self.content_requests.append(album_id)
        record = self.by_id(album_id)
        assets = [asset_id for asset_id in record["assets"] if asset_id not in self.hidden_assets] if record else []
        size = payload.get("size", 250)
        offset = int(payload["cursor"].split("-")[-1]) if "cursor" in payload else 0
        page = assets[offset : offset + size]
        more = offset + size < len(assets)
        body = {
            "items": [asset(asset_id) for asset_id in page],
            "nextCursor": f"content-{offset + size}" if more else None,
            "nextPage": None,
        }
        return 200, json.dumps({"assets": body}).encode()

    def payload_search(self, payload: dict) -> tuple[int, bytes]:
        index = len(self.payload_searches)
        self.payload_searches.append(payload)
        pages = self.pages[index] if index < len(self.pages) else [[]]
        page_index = self.page_index(payload)
        items = pages[page_index] if page_index < len(pages) else []
        legacy = not any(field in payload for field in ("filter", "orderBy", "cursor"))
        more = page_index + 1 < len(pages)
        body = {
            "items": items,
            "nextCursor": None if legacy else (f"cursor-{page_index + 2}" if more else None),
            "nextPage": (str(page_index + 2) if more else None) if legacy else None,
        }
        return 200, json.dumps({"assets": body}).encode()

    def page_index(self, payload: dict) -> int:
        """Which page of the current search this request is asking for (0-based)."""
        token = payload.get("cursor") or payload.get("page")
        if not token:
            return 0
        if isinstance(token, str):
            token = token.removeprefix("cursor-")
        return max(int(token) - 1, 0)

    def create(self, payload: dict) -> tuple[int, bytes]:
        self.create_requests.append(payload)
        if self.create_status >= 400:
            return self.create_status, b'{"message":"cannot create"}'
        record = album_record(f"new-{len(self.create_requests)}", payload["albumName"])
        self.albums.setdefault(payload["albumName"], []).append(record)
        return self.create_status, json.dumps(self.public(record)).encode()

    def mutate(self, method: str, album_id: str, payload: dict) -> tuple[int, bytes]:
        ids = list(payload["ids"])
        requests = self.add_requests if method == "PUT" else self.remove_requests
        statuses = self.add_statuses if method == "PUT" else self.remove_statuses
        requests.append((album_id, ids))
        status = statuses[len(requests) - 1] if len(statuses) >= len(requests) else 204
        if status >= 400:
            return status, b'{"message":"not found or no access"}'

        record = self.by_id(album_id)
        results = []
        for asset_id in ids:
            if asset_id in self.no_permission:
                results.append({"id": asset_id, "success": False, "error": "no_permission"})
            elif method == "PUT" and asset_id in record["assets"]:
                results.append({"id": asset_id, "success": False, "error": "duplicate"})
            elif method == "DELETE" and asset_id not in record["assets"]:
                results.append({"id": asset_id, "success": False, "error": "not_found"})
            else:
                (record["assets"].append if method == "PUT" else record["assets"].remove)(asset_id)
                results.append({"id": asset_id, "success": True})
        return 200, json.dumps(results).encode()

    # --- helpers ------------------------------------------------------------

    def public(self, record: dict) -> dict:
        """AlbumResponseDto: no assets array in v3, only a count."""
        return {"id": record["id"], "albumName": record["albumName"], "assetCount": record["assetCount"]}

    def by_id(self, album_id: str) -> dict:
        for records in self.albums.values():
            for record in records:
                if record["id"] == album_id:
                    return record
        raise AssertionError(f"unknown album id {album_id}")

    def contents(self, album_id: str) -> list[str]:
        return self.by_id(album_id)["assets"]

    def reset_searches(self, pages=None) -> None:
        """Start a fresh run: the payload search queue is consumed per run."""
        self.pages = normalize_pages(pages) if pages is not None else [[[]]]
        self.payload_searches.clear()

    @property
    def paths(self):
        return [path for _, path in self.methods]

    @property
    def writes(self):
        """Requests that change something (search is a POST but read-only)."""
        return [
            (method, path)
            for method, path in self.methods
            if method in {"POST", "PUT", "DELETE"} and path != "/api/search/metadata"
        ]


def run(server: FakeImmich, *extra: str):
    """Run main() against the fake server, capturing stdout/stderr and the exit code."""
    out, err = io.StringIO(), io.StringIO()
    argv = ["--url", "https://immich.test", "--api-key", "test-key", *extra]
    with contextlib.redirect_stdout(out):
        code = conditional_albums.main(
            argv,
            transport=server,
            sleep=lambda _seconds: None,
            log=conditional_albums.Logger(stream=err),
        )
    return code, out.getvalue(), err.getvalue()


def spec(**albums: list[dict]) -> str:
    return json.dumps(albums)


class PlanTest(unittest.TestCase):
    def test_dry_run_plans_adds_and_removals_without_writing(self):
        server = FakeImmich(
            albums={"Sommer": [album_record("a-1", "Sommer", ["x", "old"])]},
            pages=[[asset("x"), asset("y"), asset("z")]],
        )
        code, out, err = run(server, "--json", spec(**{"Sommer": [{"filter": {"isFavorite": {"eq": True}}}]}))

        self.assertEqual(code, 0)
        self.assertEqual(server.writes, [])
        self.assertIn('album "Sommer" (a-1): matched 3, in album 2', out)
        self.assertIn("WOULD ADD 2 asset(s): y z", out)
        self.assertIn("WOULD REMOVE 1 asset(s): old", out)
        self.assertIn("DRY RUN", out)
        self.assertIn("assets to add: 2, to remove: 1", out)

    def test_missing_album_is_planned_as_new(self):
        server = FakeImmich(pages=[[asset("a"), asset("b")]])
        code, out, _ = run(server, "--json", spec(**{"Neu": [{"filter": {"isFavorite": {"eq": True}}}]}))

        self.assertEqual(code, 0)
        self.assertIn('album "Neu" (missing): matched 2, in album 0', out)
        self.assertIn("WOULD CREATE the album and add 2 asset(s): a b", out)
        self.assertEqual(server.content_requests, [])  # nothing to read for a new album
        self.assertEqual(server.writes, [])

    def test_multiple_payloads_are_unioned_and_deduplicated(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["b"])]},
            pages=[[[asset("a"), asset("b")]], [[asset("b"), asset("c")]]],
        )
        code, out, _ = run(
            server,
            "--json",
            spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}, {"filter": {"rating": {"gte": 3}}}]}),
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(server.payload_searches), 2)
        self.assertIn("WOULD ADD 2 asset(s): a c", out)  # b is already in the album
        self.assertNotIn("WOULD REMOVE", out)

    def test_album_content_search_uses_album_ids_and_trashed_filter(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["a"])]},
            pages=[[asset("a")]],
        )
        code, _, _ = run(server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}))

        self.assertEqual(code, 0)
        content = server.search_bodies[-1]
        self.assertEqual(content["filter"], {"albumIds": {"any": ["x-1"]}, "trashedAt": {"eq": None}})
        self.assertEqual(server.content_requests, ["x-1"])
        self.assertFalse(content.get("page"))
        self.assertNotIn("cursor", content)

    def test_legacy_payload_paginates_with_next_page(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["a"])]},
            pages=[[asset("a")], [asset("b")]],
        )
        payload = {"visibility": "timeline", "page": 1, "isFavorite": True, "size": 5}
        code, _, _ = run(server, "--json", spec(**{"X": [payload]}))

        self.assertEqual(code, 0)
        first, second = server.payload_searches
        self.assertEqual([first["page"], second["page"]], [1, 2])
        self.assertEqual(second["visibility"], "timeline")  # payload passes through untouched
        self.assertEqual(second["size"], 250)  # ... except size
        self.assertNotIn("cursor", second)

    def test_structured_payload_drops_page_and_uses_cursor(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["a"])]},
            pages=[[asset("a")], [asset("b")]],
        )
        payload = {"filter": {"isFavorite": {"eq": True}}, "page": 4, "cursor": "stale"}
        code, _, err = run(server, "--json", spec(**{"X": [payload]}))

        self.assertEqual(code, 0)
        first, second = server.payload_searches
        self.assertNotIn("page", first)
        self.assertNotIn("cursor", first)  # the stale cursor was dropped
        self.assertEqual(second["cursor"], "cursor-2")
        self.assertIn('removed "page"', err)

    def test_page_size_flag_is_applied_to_every_search(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["a"])]},
            pages=[[asset("a")]],
        )
        code, _, _ = run(server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}), "--page-size", "1000")

        self.assertEqual(code, 0)
        self.assertEqual({body["size"] for body in server.search_bodies}, {1000})

    def test_album_content_is_read_page_by_page(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["a", "b", "c"])]},
            pages=[[asset("a"), asset("b"), asset("c")]],
        )
        code, out, _ = run(server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}), "--page-size", "2")

        self.assertEqual(code, 0)
        self.assertIn("in album 3", out)
        self.assertIn("assets to add: 0, to remove: 0", out)

    def test_asset_count_mismatch_warns_and_keeps_invisible_assets(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["keep", "locked"])]},
            pages=[[asset("keep")]],
            hidden_assets={"locked"},  # locked: counted by assetCount, never returned by search
        )
        code, out, err = run(server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply")

        self.assertEqual(code, 0)
        self.assertIn("1 of 2 asset(s) are not visible to search", err)
        self.assertIn("matched 1, in album 1", out)
        self.assertEqual(server.remove_requests, [])  # invisible assets are never removed
        self.assertEqual(server.contents("x-1"), ["keep", "locked"])
        self.assertIn("already up to date", out)


class ApplyTest(unittest.TestCase):
    def test_apply_creates_the_album_and_syncs_membership(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["old", "keep"])]},
            pages=[[asset("keep"), asset("new")]],
        )
        code, out, _ = run(server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply")

        self.assertEqual(code, 0)
        self.assertEqual(server.create_requests, [])
        self.assertEqual(server.add_requests, [("x-1", ["new"])])
        self.assertEqual(server.remove_requests, [("x-1", ["old"])])
        self.assertEqual(server.contents("x-1"), ["keep", "new"])
        self.assertIn("added 1 asset(s), removed 1 asset(s)", out)

    def test_apply_creates_a_missing_album_with_the_matching_assets(self):
        server = FakeImmich(pages=[[asset("a"), asset("b")]])
        code, out, _ = run(server, "--json", spec(**{"Neu": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply")

        self.assertEqual(code, 0)
        self.assertEqual(server.create_requests, [{"albumName": "Neu"}])
        self.assertEqual(server.add_requests, [("new-1", ["a", "b"])])
        self.assertEqual(server.contents("new-1"), ["a", "b"])
        self.assertIn("album \"Neu\" (created new-1)", out)
        self.assertIn("created the album", out)
        self.assertIn("added 2 asset(s)", out)

    def test_second_run_after_apply_is_idempotent(self):
        server = FakeImmich(pages=[[asset("a"), asset("b")]])
        document = spec(**{"Neu": [{"filter": {"isFavorite": {"eq": True}}}]})
        self.assertEqual(run(server, "--json", document, "--apply")[0], 0)
        writes = len(server.writes)

        server.reset_searches([[asset("a"), asset("b")]])
        code, out, _ = run(server, "--json", document, "--apply")

        self.assertEqual(code, 0)
        self.assertEqual(len(server.writes), writes)  # nothing new was written
        self.assertIn("already up to date", out)
        self.assertIn("assets added: 0", out)

    def test_no_remove_keeps_non_matching_assets(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["old", "keep"])]},
            pages=[[asset("keep"), asset("new")]],
        )
        code, out, _ = run(
            server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply", "--no-remove"
        )

        self.assertEqual(code, 0)
        self.assertEqual(server.remove_requests, [])
        self.assertEqual(server.contents("x-1"), ["old", "keep", "new"])
        self.assertIn("left 1 non-matching asset(s) in the album (--no-remove)", out)
        self.assertIn("skipped removals: 1", out)

    def test_updates_are_sent_in_batches(self):
        server = FakeImmich(pages=[[asset(f"a{index}") for index in range(1200)]])
        code, _, _ = run(
            server,
            "--json",
            spec(**{"Big": [{"filter": {"isFavorite": {"eq": True}}}]}),
            "--apply",
            "--batch-size",
            "500",
        )

        self.assertEqual(code, 0)
        self.assertEqual([len(ids) for _, ids in server.add_requests], [500, 500, 200])

    def test_no_permission_is_reported_and_exits_1(self):
        server = FakeImmich(pages=[[asset("mine"), asset("foreign")]], no_permission={"foreign"})
        code, out, _ = run(server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply")

        self.assertEqual(code, 1)
        self.assertIn("no_permission: 1 asset(s) foreign", out)
        self.assertIn("per-asset errors: no_permission=1", out)
        self.assertEqual(server.contents("new-1"), ["mine"])

    def test_duplicate_is_reported_but_not_a_failure(self):
        server = FakeImmich(
            albums={"X": [album_record("x-1", "X", ["keep", "locked"])]},
            pages=[[asset("keep"), asset("locked")]],
            hidden_assets={"locked"},  # in the album, invisible to search (locked)
        )
        code, out, err = run(server, "--json", spec(**{"X": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply")

        self.assertEqual(code, 0)  # duplicate means "already there"
        self.assertEqual(server.contents("x-1"), ["keep", "locked"])
        self.assertIn("duplicate: 1 asset(s) locked", out)
        self.assertIn("per-asset errors: duplicate=1", out)
        self.assertIn("1 of 2 asset(s) are not visible to search", err)

    def test_failing_batch_does_not_stop_the_other_albums(self):
        server = FakeImmich(
            albums={"A": [album_record("a-1", "A")], "B": [album_record("b-1", "B")]},
            pages=[[[asset("a1")]], [[asset("b1")]]],
            add_statuses=[403],
        )
        code, out, err = run(
            server,
            "--json",
            spec(**{"A": [{"filter": {"isFavorite": {"eq": True}}}], "B": [{"filter": {"isFavorite": {"eq": True}}}]}),
            "--apply",
        )

        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", err)
        self.assertIn("failed: 1 asset(s) to add", out)
        self.assertEqual(server.contents("b-1"), ["b1"])  # album B was still processed
        self.assertEqual(server.contents("a-1"), [])

    def test_albums_are_processed_in_file_order(self):
        server = FakeImmich(
            albums={"A": [album_record("a-1", "A")], "B": [album_record("b-1", "B")]},
            pages=[[[asset("a1")]], [[asset("b1")]]],
        )
        code, out, _ = run(
            server,
            "--json",
            spec(**{"A": [{"filter": {"isFavorite": {"eq": True}}}], "B": [{"filter": {"isFavorite": {"eq": True}}}]}),
            "--apply",
        )

        self.assertEqual(code, 0)
        self.assertLess(out.index('album "A"'), out.index('album "B"'))
        self.assertEqual(server.add_requests, [("a-1", ["a1"]), ("b-1", ["b1"])])


class ValidationTest(unittest.TestCase):
    def test_array_top_level_is_rejected(self):
        code, _, err = run(FakeImmich(), "--json", '[{"Album": []}]')
        self.assertEqual(code, 2)
        self.assertIn("top level must be a JSON object", err)

    def test_empty_album_list_is_rejected(self):
        code, _, err = run(FakeImmich(), "--json", '{"Album": []}')
        self.assertEqual(code, 2)
        self.assertIn("non-empty list of search payloads", err)

    def test_invalid_json_is_rejected(self):
        code, _, err = run(FakeImmich(), "--json", "{nope}")
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)

    def test_ambiguous_album_name_aborts_before_any_write(self):
        server = FakeImmich(
            albums={
                "OK": [album_record("ok-1", "OK", ["keep"])],
                "Dup": [album_record("dup-1", "Dup"), album_record("dup-2", "Dup")],
            },
            pages=[[asset("keep"), asset("new")]],
        )
        code, out, err = run(
            server,
            "--json",
            spec(**{"OK": [{"filter": {"isFavorite": {"eq": True}}}], "Dup": [{"filter": {"isFavorite": {"eq": True}}}]}),
            "--apply",
        )

        self.assertEqual(code, 2)
        self.assertIn('album "Dup" is ambiguous: 2 owned albums', err)
        self.assertIn("nothing was written", err)
        self.assertEqual(server.writes, [])
        self.assertEqual(server.contents("ok-1"), ["keep"])
        self.assertEqual(out, "")

    def test_album_lookup_403_names_the_album_permissions(self):
        server = FakeImmich(albums_status=403)
        code, _, err = run(server, "--json", spec(**{"A": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply")

        self.assertEqual(code, 2)
        self.assertIn("album.read", err)
        self.assertIn("albumAsset.create", err)
        self.assertEqual(server.writes, [])

    def test_search_failure_aborts_before_any_write(self):
        server = FakeImmich(
            albums={"A": [album_record("a-1", "A")]},
            pages=[[asset("a1")]],
            search_status=401,
        )
        code, _, err = run(server, "--json", spec(**{"A": [{"filter": {"isFavorite": {"eq": True}}}]}), "--apply")

        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", err)
        self.assertEqual(server.writes, [])

    def test_version_below_3_2_aborts_before_any_request(self):
        server = FakeImmich(version=(3, 1, 4), pages=[[asset("a")]])
        code, _, err = run(server, "--json", spec(**{"A": [{"filter": {"isFavorite": {"eq": True}}}]}))

        self.assertEqual(code, 2)
        self.assertIn("3.2.0", err)
        self.assertEqual(server.methods, [("GET", "/api/server/version")])

    def test_missing_credentials_exit_with_usage_error(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = conditional_albums.main(["--json", "{}", "--url", "", "--api-key", ""])
        self.assertEqual(code, 2)
        self.assertIn("required", err.getvalue())

    def test_file_and_json_are_mutually_exclusive_and_required(self):
        for argv in (["--url", "https://x", "--api-key", "k"], ["--url", "https://x", "--api-key", "k", "--file", "a", "--json", "{}"]):
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                conditional_albums.parse_args(argv)
            self.assertEqual(caught.exception.code, 2)


class ReportTest(unittest.TestCase):
    def test_report_contains_every_album(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = str(Path(tmp) / "report.json")
            server = FakeImmich(
                albums={"A": [album_record("a-1", "A", ["old", "keep"])], "B": [album_record("b-1", "B", ["b1"])]},
                pages=[[[asset("keep"), asset("new")]], [[asset("b1"), asset("b2")]]],
            )
            code, _, _ = run(
                server,
                "--json",
                spec(**{"A": [{"filter": {"isFavorite": {"eq": True}}}], "B": [{"filter": {"isFavorite": {"eq": True}}}]}),
                "--apply",
                "--json-report",
                report,
            )

            self.assertEqual(code, 0)
            payload = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(payload["tool"], "conditional_albums")
            self.assertTrue(payload["applied"])
            self.assertEqual([entry["name"] for entry in payload["albums"]], ["A", "B"])
            first = payload["albums"][0]
            self.assertEqual(first["albumId"], "a-1")
            self.assertEqual(first["added"], ["new"])
            self.assertEqual(first["removed"], ["old"])
            self.assertEqual(first["assetCount"], 2)

    def test_dry_run_report_is_written_and_marks_planned_creates(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = str(Path(tmp) / "report.json")
            server = FakeImmich(pages=[[asset("a")]])
            code, _, _ = run(
                server,
                "--json",
                spec(**{"Neu": [{"filter": {"isFavorite": {"eq": True}}}]}),
                "--json-report",
                report,
            )

            self.assertEqual(code, 0)
            payload = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertFalse(payload["applied"])
            self.assertEqual(payload["albums"][0]["created"], True)
            self.assertEqual(payload["albums"][0]["added"], ["a"])
            self.assertIsNone(payload["albums"][0]["albumId"])
            self.assertEqual(server.writes, [])


class HelpersTest(unittest.TestCase):
    def test_prepare_search_keeps_the_payload_and_only_sets_size(self):
        log = conditional_albums.Logger(quiet=True, stream=io.StringIO())
        payload = {"visibility": "timeline", "takenAfter": "1995-01-01T00:00:00.000Z", "page": 1}

        body, legacy = conditional_albums.prepare_search(payload, 250, "X", 1, log)

        self.assertTrue(legacy)
        self.assertEqual(body, {"visibility": "timeline", "takenAfter": "1995-01-01T00:00:00.000Z", "page": 1, "size": 250})
        self.assertEqual(payload.get("size"), None)  # the input payload is not modified

    def test_parse_spec_rejects_whitespace_in_names(self):
        with self.assertRaises(conditional_albums.SpecError):
            conditional_albums.parse_spec('{" Album ": [{"filter": {}}]}')

    def test_parse_spec_keeps_file_order(self):
        entries = conditional_albums.parse_spec('{"Z": [{"filter": {}}], "A": [{"rating": 1}]}')
        self.assertEqual([name for name, _ in entries], ["Z", "A"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
