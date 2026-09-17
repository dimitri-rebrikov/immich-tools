# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Tests for favorite_rated.py. No network access: the HTTP layer is faked.

Run with:  uv run tests/test_favorite_rated.py
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
from urllib.parse import urlsplit

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "favorite_rated.py"
_spec = importlib.util.spec_from_file_location("favorite_rated", SCRIPT_PATH)
favorite_rated = importlib.util.module_from_spec(_spec)
sys.modules["favorite_rated"] = favorite_rated
_spec.loader.exec_module(favorite_rated)


def asset(
    asset_id: str,
    *,
    rating=4,
    favorite=False,
    owner="me",
    visibility="timeline",
    name="IMG_0001.jpg",
) -> dict:
    return {
        "id": asset_id,
        "ownerId": owner,
        "isFavorite": favorite,
        "visibility": visibility,
        "type": "IMAGE",
        "originalFileName": name,
        "localDateTime": "2026-01-01T00:00:00.000Z",
        "exifInfo": {"rating": rating},
    }


class FakeImmich:
    """Serves canned pages and records every request the script makes."""

    def __init__(self, *, version=(3, 3, 1), me="me", pages=None, search_status=200, patch_statuses=None):
        self.version = version
        self.user_id = me
        self.pages = pages if pages is not None else [[]]
        self.search_status = search_status
        self.patch_statuses = list(patch_statuses or [])
        self.search_bodies: list[dict] = []
        self.patch_bodies: list[dict] = []
        self.methods: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body, timeout):
        path = urlsplit(url).path
        self.methods.append((method, path))
        payload = json.loads(body) if body else None

        if path == "/api/server/version":
            major, minor, patch = self.version
            return 200, json.dumps({"major": major, "minor": minor, "patch": patch}).encode()
        if path == "/api/users/me":
            return 200, json.dumps({"id": self.user_id}).encode()
        if path == "/api/search/metadata":
            self.search_bodies.append(payload)
            index = len(self.search_bodies) - 1
            page = self.pages[index] if index < len(self.pages) else []
            next_cursor = f"cursor-{index + 2}" if index + 1 < len(self.pages) else None
            return self.search_status, json.dumps(
                {"assets": {"items": page, "nextCursor": next_cursor, "total": len(page)}}
            ).encode()
        if path == "/api/assets" and method == "PATCH":
            self.patch_bodies.append(payload)
            status = self.patch_statuses[len(self.patch_bodies) - 1] if len(self.patch_statuses) >= len(self.patch_bodies) else 204
            error = b'{"message":"forbidden"}' if status >= 400 else b""
            return status, error
        raise AssertionError(f"unexpected call: {method} {path}")

    @property
    def paths(self):
        return [path for _, path in self.methods]


def run(server: FakeImmich, *extra: str):
    """Run main() against the fake server, capturing stdout/stderr and the exit code."""
    out, err = io.StringIO(), io.StringIO()
    argv = ["--url", "https://immich.test", "--api-key", "test-key", *extra]
    with contextlib.redirect_stdout(out):
        code = favorite_rated.main(
            argv,
            transport=server,
            sleep=lambda _seconds: None,
            log=favorite_rated.Logger(stream=err),
        )
    return code, out.getvalue(), err.getvalue()


class SearchPayloadTest(unittest.TestCase):
    def test_uses_structured_filter_and_cursor_fields_only(self):
        server = FakeImmich(pages=[[asset("a1")]])
        code, out, _ = run(server, "--min-rating", "4")

        self.assertEqual(code, 0)
        body = server.search_bodies[0]
        self.assertEqual(
            body["filter"],
            {
                "type": {"eq": "IMAGE"},
                "rating": {"gte": 4},
                "isFavorite": {"eq": False},
                "trashedAt": {"eq": None},
            },
        )
        self.assertEqual(body["orderBy"], {"field": "fileCreatedAt", "direction": "desc"})
        self.assertTrue(body["withExif"])
        self.assertEqual(body["size"], 250)
        # deprecated flat fields must never be mixed with the structured shape
        for deprecated in ("rating", "isFavorite", "page", "type", "trashedAfter"):
            self.assertNotIn(deprecated, body)
        self.assertIn("WOULD FAVORITE", out)


class CandidateSelectionTest(unittest.TestCase):
    def test_only_owned_unfavorited_rated_images_become_candidates(self):
        server = FakeImmich(
            pages=[
                [
                    asset("own-ok", rating=3),
                    asset("partner", rating=5, owner="partner"),
                    asset("already-fav", rating=5, favorite=True),
                    asset("unrated", rating=None),
                    asset("too-low", rating=2),
                    asset("locked", rating=5, visibility="locked"),
                    asset("video", rating=5),  # server filter handles type, still rated/owned
                ]
            ]
        )
        code, out, err = run(server)
        self.assertEqual(code, 0)
        self.assertIn("candidates: 2", out)
        self.assertIn("not-owned=1", out)
        self.assertIn("already-favorite=1", out)
        self.assertIn("locked=1", out)
        self.assertIn("rating=2", out)
        self.assertEqual(server.patch_bodies, [])

    def test_limit_stops_scanning_after_enough_candidates(self):
        server = FakeImmich(pages=[[asset("a1"), asset("a2")], [asset("a3")]])
        code, out, _ = run(server, "--limit", "1")
        self.assertEqual(code, 0)
        self.assertEqual(len(server.search_bodies), 1)
        self.assertIn("candidates: 1", out)

    def test_cursor_pagination_walks_until_next_cursor_is_null(self):
        server = FakeImmich(pages=[[asset("a1")], [asset("a2")]])
        code, out, _ = run(server)
        self.assertEqual(code, 0)
        self.assertEqual(len(server.search_bodies), 2)
        self.assertNotIn("cursor", server.search_bodies[0])
        self.assertEqual(server.search_bodies[1]["cursor"], "cursor-2")
        self.assertEqual(server.search_bodies[1]["filter"], server.search_bodies[0]["filter"])
        self.assertIn("scanned 2 page(s), 2 asset(s)", out)


class ApplyTest(unittest.TestCase):
    def test_dry_run_never_writes(self):
        server = FakeImmich(pages=[[asset("a1"), asset("a2")]])
        code, out, _ = run(server)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertNotIn("/api/assets", server.paths)

    def test_apply_batches_requests_and_sends_favorite_true(self):
        server = FakeImmich(pages=[[asset(f"a{index}") for index in range(600)], [asset(f"b{index}") for index in range(600)]])
        code, out, _ = run(server, "--apply", "--batch-size", "500")

        self.assertEqual(code, 0)
        self.assertEqual([len(body["ids"]) for body in server.patch_bodies], [500, 500, 200])
        for body in server.patch_bodies:
            self.assertEqual(body["isFavorite"], True)
            self.assertEqual(set(body) , {"ids", "isFavorite"})
        self.assertIn("favorited: 1200 asset(s) in 3 batch(es)", out)

    def test_failed_batch_is_reported_and_remaining_batches_still_run(self):
        server = FakeImmich(
            pages=[[asset(f"a{index}") for index in range(1200)]],
            patch_statuses=[204, 403, 204],
        )
        code, out, err = run(server, "--apply", "--batch-size", "500")

        self.assertEqual(code, 1)
        self.assertEqual(len(server.patch_bodies), 3)
        self.assertIn("HTTP 403", out)
        self.assertIn("asset.update", out)
        self.assertIn("favorited: 700 asset(s)", out)

    def test_read_error_is_not_a_traceback(self):
        server = FakeImmich(search_status=401)
        code, _, err = run(server)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", err)
        self.assertNotIn("Traceback", err)

    def test_quiet_still_reports_errors_on_stderr(self):
        server = FakeImmich(search_status=401)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            code = favorite_rated.main(
                ["--url", "https://immich.test", "--api-key", "test-key", "--quiet"],
                transport=server,
                sleep=lambda _seconds: None,
                log=favorite_rated.Logger(quiet=True, stream=err),
            )
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", err.getvalue())

    def test_version_below_3_2_aborts_before_searching(self):
        server = FakeImmich(version=(3, 1, 4), pages=[[asset("a1")]])
        code, _, err = run(server)
        self.assertEqual(code, 2)
        self.assertIn("3.2.0", err)
        self.assertNotIn("/api/search/metadata", server.paths)


class ReportAndRevertTest(unittest.TestCase):
    def test_report_is_written_and_revert_un_favorites_the_same_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = str(Path(tmp) / "report.json")
            server = FakeImmich(pages=[[asset("a1"), asset("a2")]])
            code, out, _ = run(server, "--apply", "--json-report", report)
            self.assertEqual(code, 0)

            payload = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(payload["tool"], "favorite_rated")
            self.assertEqual(payload["min_rating"], 3)
            self.assertEqual([entry["id"] for entry in payload["assets"]], ["a1", "a2"])
            self.assertEqual(payload["assets"][0]["rating"], 4)

            revert_server = FakeImmich()
            code, out, _ = run(revert_server, "--revert", report, "--apply")
            self.assertEqual(code, 0)
            self.assertEqual(len(revert_server.patch_bodies), 1)
            self.assertEqual(revert_server.patch_bodies[0], {"ids": ["a1", "a2"], "isFavorite": False})
            self.assertIn("reverted: 2 asset(s)", out)

    def test_revert_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = str(Path(tmp) / "report.json")
            server = FakeImmich(pages=[[asset("a1")]])
            run(server, "--apply", "--json-report", report)

            revert_server = FakeImmich()
            code, out, _ = run(revert_server, "--revert", report)
            self.assertEqual(code, 0)
            self.assertIn("DRY RUN", out)
            self.assertEqual(revert_server.patch_bodies, [])


class HelpersTest(unittest.TestCase):
    def test_skip_reason_ignores_non_numeric_and_boolean_ratings(self):
        self.assertEqual(favorite_rated.skip_reason(asset("a", rating=None), "me", 3), "rating")
        self.assertEqual(favorite_rated.skip_reason(asset("a", rating=True), "me", 3), "rating")
        self.assertIsNone(favorite_rated.skip_reason(asset("a", rating=3), "me", 3))

    def test_normalize_url_adds_scheme_and_strips_slashes(self):
        self.assertEqual(favorite_rated.normalize_url("immich.test/api/"), "https://immich.test/api")
        self.assertEqual(favorite_rated.normalize_url("http://10.0.0.5:2283/"), "http://10.0.0.5:2283")

    def test_missing_credentials_exit_with_usage_error(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = favorite_rated.main(["--url", "", "--api-key", ""])
        self.assertEqual(code, 2)
        self.assertIn("required", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
