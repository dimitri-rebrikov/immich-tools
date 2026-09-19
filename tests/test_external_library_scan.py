# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for external_library_scan.py. No network access: the HTTP layer is faked.

Run with:  uv run tests/test_external_library_scan.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:  # the script imports immich_api from the repo root
    sys.path.insert(0, str(SCRIPT_ROOT))

SCRIPT_PATH = SCRIPT_ROOT / "external_library_scan.py"
_spec = importlib.util.spec_from_file_location("external_library_scan", SCRIPT_PATH)
external_library_scan = importlib.util.module_from_spec(_spec)
sys.modules["external_library_scan"] = external_library_scan
_spec.loader.exec_module(external_library_scan)

FAMILY = "59f55eb0-32e5-4037-b53e-5e41c1f2d9b3"
PHOTOS = "11111111-2222-3333-4444-555555555555"
REFRESHED = "2026-09-19T14:18:34.975Z"
STATS = {"photos": 52958, "videos": 1069, "usage": 247135360484, "total": 54027}


def library(library_id: str, name: str, *, refreshed_at=REFRESHED, updated_at="2026-09-19T14:18:34.978Z") -> dict:
    return {
        "id": library_id,
        "ownerId": "me",
        "name": name,
        "importPaths": [f"/photos/{name}"],
        "exclusionPatterns": [],
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": updated_at,
        "refreshedAt": refreshed_at,
        "assetCount": 0,  # Immich does not populate this for external libraries
    }


def queue_state(*, is_paused=False, **counts) -> dict:
    statistics = {"active": 0, "completed": 0, "failed": 0, "delayed": 0, "waiting": 0, "paused": 0}
    statistics.update(counts)
    return {"name": "library", "isPaused": is_paused, "statistics": statistics}


def idle(**counts) -> dict:
    return queue_state(completed=20, **counts)


class FakeImmich:
    """Serves canned libraries/queue state and records every request the script makes."""

    def __init__(
        self,
        *,
        libraries=None,
        states=None,
        version=(3, 3, 1),
        version_status=200,
        libraries_status=200,
        queue_status=200,
        scan_statuses=None,
        statistics_status=200,
        statistics=None,
        statistics_status_after_scan=200,
        refresh_on_scan=None,
        refresh_trigger="scan",
    ):
        self.libraries = [library(FAMILY, "Family"), library(PHOTOS, "Photos")] if libraries is None else libraries
        self.states = list(states) if states is not None else [idle()]
        self.version = version
        self.version_status = version_status
        self.libraries_status = libraries_status
        self.queue_status = queue_status
        self.scan_statuses = list(scan_statuses or [])
        # one dict for every library or a per-id map; kept per-id internally
        raw_statistics = STATS if statistics is None else statistics
        if raw_statistics and all(isinstance(value, dict) for value in raw_statistics.values()):
            self.statistics = {key: dict(value) for key, value in raw_statistics.items()}
        else:
            self.statistics = {library["id"]: dict(raw_statistics) for library in self.libraries}
        self.statistics_status = statistics_status
        self.statistics_status_after_scan = statistics_status_after_scan
        # {library_id: {"refreshedAt": ..., "statistics": {...}}} applied on the refresh trigger
        self.refresh_on_scan = refresh_on_scan or {}
        self.refresh_trigger = refresh_trigger
        self.statistics_reads: list[str] = []

        self.methods: list[tuple[str, str]] = []
        self.scans: list[str] = []
        self.queue_reads = 0
        self.sleeps: list[float] = []
        self.clock = 0.0

    def __call__(self, method, url, headers, body, timeout):
        path = urlsplit(url).path
        self.methods.append((method, path))

        if path == "/api/server/version":
            if self.version_status >= 400:
                return self.version_status, b'{"message":"unauthorized"}'
            major, minor, patch = self.version
            return 200, json.dumps({"major": major, "minor": minor, "patch": patch}).encode()
        if path == "/api/libraries" and method == "GET":
            if self.libraries_status >= 400:
                return self.libraries_status, b'{"message":"forbidden"}'
            return 200, json.dumps(self.libraries).encode()
        if path.startswith("/api/libraries/") and path.endswith("/statistics") and method == "GET":
            library_id = path.split("/")[3]
            self.statistics_reads.append(library_id)
            status = self.statistics_status_after_scan if library_id in self.scans else self.statistics_status
            if status >= 400:
                return status, b'{"message":"forbidden"}'
            return 200, json.dumps(self.statistics.get(library_id, {})).encode()
        if path.startswith("/api/libraries/") and path.endswith("/scan") and method == "POST":
            if body:
                raise AssertionError(f"scan request should have no body, got {body!r}")
            library_id = path.split("/")[3]
            self.scans.append(library_id)
            status = self.scan_statuses[len(self.scans) - 1] if len(self.scan_statuses) >= len(self.scans) else 204
            if status < 400 and self.refresh_trigger == "scan":
                self.apply_refresh()
            return status, b'{"message":"invalid library"}' if status >= 400 else b""
        if path == "/api/queues/library" and method == "GET":
            state = self.states[min(self.queue_reads, len(self.states) - 1)]
            self.queue_reads += 1
            if self.queue_status >= 400:
                return self.queue_status, b'{"message":"forbidden"}'
            counts = state["statistics"]
            if self.refresh_trigger == "idle" and not counts["active"] and not counts["waiting"]:
                self.apply_refresh()  # the scan we are watching just finished
            return 200, json.dumps(state).encode()
        raise AssertionError(f"unexpected call: {method} {path}")

    def apply_refresh(self) -> None:
        for library_id, change in self.refresh_on_scan.items():
            for record in self.libraries:
                if record["id"] == library_id:
                    record["refreshedAt"] = change["refreshedAt"]
                    record["updatedAt"] = change["refreshedAt"]
            if change.get("statistics"):
                self.statistics[library_id] = dict(change["statistics"])

    @property
    def paths(self):
        return [path for _, path in self.methods]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds  # tests run on fake time, so --wait-timeout is deterministic

    def now(self) -> float:
        return self.clock


def run(server: FakeImmich, *extra: str):
    """Run main() against the fake server, capturing stdout/stderr and the exit code."""
    out, err = io.StringIO(), io.StringIO()
    argv = ["--url", "https://immich.test", "--api-key", "test-key", *extra]
    with contextlib.redirect_stdout(out):
        code = external_library_scan.main(
            argv,
            transport=server,
            sleep=server.sleep,
            now=server.now,
            log=external_library_scan.Logger(stream=err),
        )
    return code, out.getvalue(), err.getvalue()


class DryRunTest(unittest.TestCase):
    def test_dry_run_lists_every_library_and_posts_nothing(self):
        server = FakeImmich()
        code, out, _ = run(server)

        self.assertEqual(code, 0)
        self.assertIn(f"WOULD SCAN Family ({FAMILY})", out)
        self.assertIn(f"WOULD SCAN Photos ({PHOTOS})", out)
        self.assertIn("DRY RUN: no scans started (add --apply to start)", out)
        self.assertEqual(server.scans, [])
        self.assertNotIn("/api/queues/library", server.paths)

    def test_dry_run_with_selection_only_lists_the_chosen_library(self):
        server = FakeImmich()
        code, out, _ = run(server, "--library", "Photos")

        self.assertEqual(code, 0)
        self.assertIn("WOULD SCAN Photos", out)
        self.assertNotIn("Family", out)
        self.assertEqual(server.scans, [])
        self.assertEqual(server.statistics_reads, [PHOTOS])  # no wasted call for the others

    def test_dry_run_shows_media_counts_and_refresh_times(self):
        server = FakeImmich(
            libraries=[library(FAMILY, "Family", updated_at="2026-09-18T09:00:00.000Z")],
            statistics={FAMILY: {"photos": 1, "videos": 2, "usage": 3, "total": 3}},
        )
        code, out, _ = run(server)

        self.assertEqual(code, 0)
        self.assertIn(
            f"WOULD SCAN Family ({FAMILY}) - 3 media (1 photos, 2 videos), "
            "refreshed=2026-09-19 14:18:34Z updated=2026-09-18 09:00:00Z",
            out,
        )

    def test_dry_run_never_waits(self):
        server = FakeImmich(states=[queue_state(active=1)])
        code, out, _ = run(server, "--wait")

        self.assertEqual(code, 0)
        self.assertIn("DRY RUN: no scans started", out)
        self.assertEqual(server.sleeps, [])
        self.assertNotIn("/api/queues/library", server.paths)

    def test_empty_library_list_is_not_an_error(self):
        server = FakeImmich(libraries=[])
        code, out, _ = run(server, "--apply")

        self.assertEqual(code, 0)
        self.assertIn("no external libraries found", out)
        self.assertEqual(server.scans, [])


class ApplyTest(unittest.TestCase):
    def test_apply_scans_every_library(self):
        server = FakeImmich()
        code, out, _ = run(server, "--apply")

        self.assertEqual(code, 0)
        self.assertEqual(server.scans, [FAMILY, PHOTOS])
        self.assertIn(f"QUEUED Family ({FAMILY}) - 54027 media (52958 photos, 1069 videos)", out)
        self.assertIn(f"QUEUED Photos ({PHOTOS})", out)
        self.assertIn("scan queued: 2 of 2", out)
        self.assertEqual(server.methods.count(("POST", f"/api/libraries/{FAMILY}/scan")), 1)

    def test_apply_scans_only_the_selected_library_by_name(self):
        server = FakeImmich()
        code, out, _ = run(server, "--apply", "--library", "Photos")

        self.assertEqual(code, 0)
        self.assertEqual(server.scans, [PHOTOS])
        self.assertIn(f"QUEUED Photos ({PHOTOS})", out)
        self.assertIn("scan queued: 1 of 1", out)

    def test_selection_accepts_an_uppercase_uuid_and_is_deduplicated(self):
        server = FakeImmich()
        code, _, _ = run(server, "--apply", "--library", FAMILY.upper(), "--library", "Family")

        self.assertEqual(code, 0)
        self.assertEqual(server.scans, [FAMILY])

    def test_one_failing_scan_request_does_not_stop_the_others(self):
        server = FakeImmich(scan_statuses=[400, 204])
        code, out, err = run(server, "--apply")

        self.assertEqual(code, 1)
        self.assertEqual(server.scans, [FAMILY, PHOTOS])
        self.assertIn(f"QUEUED Photos ({PHOTOS})", out)
        self.assertNotIn("QUEUED Family", out)
        self.assertIn("scan queued: 1 of 2", out)
        self.assertIn("error: scan request failed for Family", err)
        self.assertNotIn("Traceback", err)

    def test_statistics_are_read_before_the_scans_start(self):
        server = FakeImmich()
        code, _, _ = run(server, "--apply")

        self.assertEqual(code, 0)
        first_scan = server.methods.index(("POST", f"/api/libraries/{FAMILY}/scan"))
        statistics_calls = [
            index for index, (method, path) in enumerate(server.methods) if path.endswith("/statistics")
        ]
        self.assertTrue(statistics_calls)
        self.assertLess(max(statistics_calls), first_scan)

    def test_nothing_queued_is_reported_when_every_post_fails(self):
        server = FakeImmich(scan_statuses=[400, 400])
        code, out, _ = run(server, "--apply")

        self.assertEqual(code, 1)
        self.assertIn("scan queued: none", out)


class SelectionTest(unittest.TestCase):
    def test_unknown_selector_fails_before_any_post(self):
        server = FakeImmich()
        code, _, err = run(server, "--apply", "--library", "Nope")

        self.assertEqual(code, 2)
        self.assertEqual(server.scans, [])
        self.assertIn("no external library matches 'Nope'", err)
        self.assertIn("known libraries: Family, Photos", err)

    def test_ambiguous_name_lists_the_candidate_ids(self):
        server = FakeImmich(libraries=[library(FAMILY, "Family"), library(PHOTOS, "Family")])
        code, _, err = run(server, "--apply", "--library", "Family")

        self.assertEqual(code, 2)
        self.assertEqual(server.scans, [])
        self.assertIn("use an id instead", err)
        self.assertIn(FAMILY, err)
        self.assertIn(PHOTOS, err)

    def test_every_bad_selector_is_reported_at_once(self):
        server = FakeImmich()
        code, _, err = run(server, "--apply", "--library", "Nope", "--library", "Nada")

        self.assertEqual(code, 2)
        self.assertIn("'Nope'", err)
        self.assertIn("'Nada'", err)
        self.assertEqual(server.scans, [])


class StatusTest(unittest.TestCase):
    def test_status_reports_counts_and_never_scans(self):
        server = FakeImmich(states=[queue_state(active=1, waiting=2, completed=57)])
        code, out, _ = run(server, "--status")

        self.assertEqual(code, 0)
        self.assertIn("queue library: isPaused=false active=1 waiting=2", out)
        self.assertIn("completed=57", out)
        self.assertEqual(server.scans, [])

    def test_status_lists_every_library_with_counts_and_timestamps(self):
        server = FakeImmich(states=[idle()])
        code, out, _ = run(server, "--status")

        self.assertEqual(code, 0)
        self.assertIn("libraries: 2", out)
        self.assertIn(
            f"Family ({FAMILY}) - 54027 media (52958 photos, 1069 videos), "
            "refreshed=2026-09-19 14:18:34Z updated=2026-09-19 14:18:34Z",
            out,
        )
        self.assertEqual(server.statistics_reads, [FAMILY, PHOTOS])

    def test_status_reports_a_library_that_was_never_refreshed(self):
        server = FakeImmich(libraries=[library(FAMILY, "Family", refreshed_at=None)], states=[idle()])
        code, out, _ = run(server, "--status")

        self.assertEqual(code, 0)
        self.assertIn("refreshed=never", out)

    def test_status_with_wait_follows_a_running_scan(self):
        server = FakeImmich(states=[queue_state(active=1), queue_state(active=1), queue_state(completed=57)])
        code, out, _ = run(server, "--status", "--wait")

        self.assertEqual(code, 0)
        self.assertIn("is idle after 5s", out)
        self.assertEqual(server.sleeps, [5])
        self.assertEqual(server.scans, [])

    def test_status_and_library_are_mutually_exclusive(self):
        server = FakeImmich()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as caught:
                run(server, "--status", "--library", "Family")

        self.assertEqual(caught.exception.code, 2)
        self.assertIn("not allowed with", err.getvalue())
        self.assertEqual(server.scans, [])


class StatisticsTest(unittest.TestCase):
    def test_a_failing_statistics_call_degrades_to_unknown_counts(self):
        server = FakeImmich(statistics_status=403, states=[idle()])
        code, out, err = run(server, "--status")

        self.assertEqual(code, 0)  # a missing count is not a failed run
        self.assertIn("media counts unknown", out)
        self.assertIn("warning: no media counts for Family", err)
        self.assertIn("HTTP 403", err)
        self.assertNotIn("Traceback", err)

    def test_a_failing_statistics_call_does_not_block_scanning(self):
        server = FakeImmich(statistics_status=403)
        code, out, err = run(server, "--apply")

        self.assertEqual(code, 0)
        self.assertEqual(server.scans, [FAMILY, PHOTOS])
        self.assertIn("QUEUED Family", out)
        self.assertIn("media counts unknown", out)

    def test_timestamps_and_counts_are_formatted_for_people(self):
        self.assertEqual(external_library_scan.format_timestamp("2026-09-19T14:18:34.975Z"), "2026-09-19 14:18:34Z")
        self.assertEqual(external_library_scan.format_timestamp("2026-09-19T14:18:34Z"), "2026-09-19 14:18:34Z")
        self.assertEqual(external_library_scan.format_timestamp(None), "never")
        self.assertEqual(external_library_scan.format_timestamp(""), "never")
        self.assertEqual(external_library_scan.format_timestamp("2026-09-19T14:18:34+02:00"), "2026-09-19T14:18:34+02:00")

        self.assertEqual(
            external_library_scan.format_media({"statistics": {"total": 5, "photos": 4, "videos": 1}}),
            "5 media (4 photos, 1 videos)",
        )
        self.assertEqual(
            external_library_scan.format_media({"statistics": None, "statisticsError": "HTTP 403"}),
            "media counts unknown",
        )


class WaitTest(unittest.TestCase):
    def test_wait_polls_until_the_queue_drains(self):
        server = FakeImmich(
            states=[queue_state(active=1), queue_state(active=1, waiting=2), queue_state(completed=57)]
        )
        code, out, _ = run(server, "--apply", "--wait")

        self.assertEqual(code, 0)
        self.assertEqual(server.sleeps, [5, 5])
        self.assertEqual(server.queue_reads, 3)
        self.assertIn("is idle after 10s", out)
        self.assertIn("57 completed, 0 failed", out)

    def test_wait_uses_the_poll_interval(self):
        server = FakeImmich(states=[queue_state(active=1), queue_state(completed=3)])
        code, _, _ = run(server, "--apply", "--wait", "--poll-interval", "2")

        self.assertEqual(code, 0)
        self.assertEqual(server.sleeps, [2])

    def test_wait_returns_immediately_when_the_queue_is_already_idle(self):
        server = FakeImmich(states=[queue_state(completed=4)])
        code, out, _ = run(server, "--apply", "--wait")

        self.assertEqual(code, 0)
        self.assertEqual(server.sleeps, [])
        self.assertIn("is idle after 0s", out)

    def test_wait_times_out_and_reports_the_last_counts(self):
        server = FakeImmich(states=[queue_state(active=1, waiting=3)])
        code, out, err = run(server, "--apply", "--wait", "--wait-timeout", "30", "--poll-interval", "10")

        self.assertEqual(code, 1)
        self.assertEqual(server.sleeps, [10, 10, 10])
        self.assertIn("active=1 waiting=3", out)
        self.assertIn("timed out waiting 30s", err)
        self.assertNotIn("Traceback", err)

    def test_paused_queue_fails_fast_with_a_resume_hint(self):
        server = FakeImmich(states=[queue_state(is_paused=True, active=1)])
        code, out, err = run(server, "--apply", "--wait")

        self.assertEqual(code, 1)
        self.assertEqual(server.sleeps, [])
        self.assertIn("isPaused=true", out)
        self.assertIn("queue library is paused", err)

    def test_wait_only_watches_the_library_queue(self):
        server = FakeImmich(states=[queue_state(active=1), queue_state(completed=1)])
        code, _, _ = run(server, "--apply", "--wait")

        self.assertEqual(code, 0)
        self.assertNotIn("/api/queues", server.paths)  # never the all-queues listing
        self.assertEqual(server.queue_reads, 2)


class AfterScanTest(unittest.TestCase):
    def scan_flips_one_library(self):
        return FakeImmich(
            libraries=[library(FAMILY, "ds photo", refreshed_at="2026-09-01T00:00:00.000Z")],
            statistics={FAMILY: {"photos": 0, "videos": 0, "usage": 0, "total": 0}},
            states=[queue_state(active=1), queue_state(completed=2)],
            refresh_on_scan={FAMILY: {"refreshedAt": REFRESHED, "statistics": STATS}},
        )

    def test_wait_prints_the_state_after_the_scan(self):
        server = self.scan_flips_one_library()
        code, out, _ = run(server, "--apply", "--wait")

        self.assertEqual(code, 0)
        self.assertIn(
            f"QUEUED ds photo ({FAMILY}) - 0 media (0 photos, 0 videos), refreshed=2026-09-01 00:00:00Z",
            out,
        )
        self.assertIn(
            f"REFRESHED ds photo ({FAMILY}) - 54027 media (52958 photos, 1069 videos), "
            "refreshed=2026-09-01 00:00:00Z -> 2026-09-19 14:18:34Z",
            out,
        )
        self.assertEqual(server.statistics_reads, [FAMILY, FAMILY])  # once before, once after

    def test_json_carries_the_state_after_the_scan(self):
        server = self.scan_flips_one_library()
        code, out, _ = run(server, "--apply", "--wait", "--json")

        self.assertEqual(code, 0)
        entry = json.loads(out)["libraries"][0]
        self.assertEqual(entry["statistics"]["total"], 0)  # before
        self.assertEqual(entry["after"]["refreshedAt"], REFRESHED)
        self.assertEqual(entry["after"]["statistics"]["total"], 54027)
        self.assertIsNone(entry["after"]["statisticsError"])

    def test_status_wait_reports_the_state_after_the_wait(self):
        # nothing is scanned here, so the change comes from the scan we are watching
        server = self.scan_flips_one_library()
        server.refresh_trigger = "idle"
        server.scans.append(FAMILY)  # as if another actor had queued it
        code, out, _ = run(server, "--status", "--wait")

        self.assertEqual(code, 0)
        self.assertIn(
            f"REFRESHED ds photo ({FAMILY}) - 54027 media (52958 photos, 1069 videos), "
            "refreshed=2026-09-01 00:00:00Z -> 2026-09-19 14:18:34Z",
            out,
        )

    def test_nothing_is_reread_without_wait(self):
        server = self.scan_flips_one_library()
        code, out, _ = run(server, "--apply")

        self.assertEqual(code, 0)
        self.assertNotIn("REFRESHED", out)
        self.assertEqual(server.statistics_reads, [FAMILY])

    def test_a_finished_wait_that_timed_out_is_not_reread(self):
        server = FakeImmich(states=[queue_state(active=1)], refresh_on_scan={FAMILY: {"refreshedAt": REFRESHED}})
        code, out, err = run(server, "--apply", "--wait", "--wait-timeout", "10", "--poll-interval", "5")

        self.assertEqual(code, 1)
        self.assertNotIn("REFRESHED", out)
        self.assertIn("timed out waiting 10s", err)

    def test_a_failing_post_scan_statistics_call_is_unknown(self):
        server = FakeImmich(
            states=[queue_state(active=1), queue_state(completed=2)],
            statistics_status_after_scan=403,
        )
        code, out, err = run(server, "--apply", "--wait")

        self.assertEqual(code, 0)
        self.assertIn("REFRESHED Family", out)
        self.assertIn("media counts unknown", out)
        self.assertIn("warning: no media counts after the scan for Family", err)
        self.assertNotIn("Traceback", err)

    def test_a_failed_scan_is_not_reread(self):
        server = FakeImmich(scan_statuses=[400, 204], refresh_on_scan={FAMILY: {"refreshedAt": REFRESHED}})
        code, out, _ = run(server, "--apply", "--wait")

        self.assertEqual(code, 1)
        self.assertNotIn("REFRESHED Family", out)
        self.assertIn("REFRESHED Photos", out)  # the one that did get scanned


class JsonTest(unittest.TestCase):
    def test_json_report_is_the_only_stdout_output(self):
        server = FakeImmich(states=[queue_state(active=1), queue_state(completed=57)])
        code, out, _ = run(server, "--apply", "--wait", "--json")

        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["tool"], "external_library_scan")
        self.assertEqual(report["queue"], "library")
        self.assertTrue(report["applied"])
        self.assertTrue(report["started"])
        self.assertTrue(report["waited"])
        self.assertEqual(report["elapsed_seconds"], 5.0)
        self.assertEqual(report["server"], "https://immich.test")
        self.assertEqual([entry["scanQueued"] for entry in report["libraries"]], [True, True])
        self.assertEqual(report["queueState"]["statistics"]["completed"], 57)
        self.assertEqual(report["libraries"][0]["statistics"]["total"], 54027)
        self.assertEqual(report["libraries"][0]["refreshedAt"], REFRESHED)  # raw API value
        self.assertIn("generated_at", report)

    def test_json_dry_run_marks_nothing_as_applied(self):
        server = FakeImmich()
        code, out, _ = run(server, "--json")

        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertFalse(report["applied"])
        self.assertFalse(report["started"])
        self.assertIsNone(report["queueState"])
        self.assertEqual([entry["scanQueued"] for entry in report["libraries"]], [False, False])
        self.assertEqual(report["libraries"][0]["name"], "Family")
        self.assertEqual(
            set(report["libraries"][0]),
            {
                "id",
                "name",
                "refreshedAt",
                "updatedAt",
                "statistics",
                "statisticsError",
                "scanQueued",
                "scanError",
                "after",
            },
        )
        self.assertIsNone(report["libraries"][0]["after"])  # nothing was queued, so nothing re-read
        self.assertEqual(server.scans, [])

    def test_json_status_includes_the_libraries(self):
        server = FakeImmich(states=[queue_state(active=2, failed=1)])
        code, out, _ = run(server, "--status", "--json")

        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertFalse(report["applied"])
        self.assertFalse(report["started"])
        self.assertEqual(report["queueState"]["statistics"]["active"], 2)
        self.assertEqual(report["queueState"]["statistics"]["failed"], 1)
        self.assertEqual([entry["name"] for entry in report["libraries"]], ["Family", "Photos"])
        self.assertEqual(report["libraries"][0]["statistics"]["photos"], 52958)
        self.assertEqual([entry["scanQueued"] for entry in report["libraries"]], [False, False])

    def test_json_marks_a_failed_statistics_call(self):
        server = FakeImmich(statistics_status=403, states=[idle()])
        code, out, _ = run(server, "--status", "--json")

        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIsNone(report["libraries"][0]["statistics"])
        self.assertIn("HTTP 403", report["libraries"][0]["statisticsError"])


class ErrorTest(unittest.TestCase):
    def test_missing_credentials_exit_2(self):
        server = FakeImmich()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = external_library_scan.main([], transport=server, sleep=server.sleep, now=server.now)

        self.assertEqual(code, 2)
        self.assertIn("--url/IMMICH_URL and --api-key/IMMICH_API_KEY are required", err.getvalue())
        self.assertEqual(server.methods, [])

    def test_unreachable_api_exits_2_without_a_traceback(self):
        server = FakeImmich(version_status=401)
        code, _, err = run(server, "--status")

        self.assertEqual(code, 2)
        self.assertIn("cannot reach the Immich API", err)
        self.assertIn("HTTP 401", err)
        self.assertNotIn("Traceback", err)

    def test_unauthorized_library_list_exits_2(self):
        server = FakeImmich(libraries_status=403)
        code, _, err = run(server, "--apply")

        self.assertEqual(code, 2)
        self.assertIn("HTTP 403", err)
        self.assertIn("library.read, library.update, library.statistics, queue.read", err)
        self.assertEqual(server.scans, [])

    def test_queue_status_error_exits_2(self):
        server = FakeImmich(queue_status=403)
        code, _, err = run(server, "--status")

        self.assertEqual(code, 2)
        self.assertIn("HTTP 403", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
