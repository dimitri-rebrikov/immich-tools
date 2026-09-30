# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for external_library_snapshot_compare.py.

The comparer only reads two documents and writes nothing, so almost every test works on synthetic
states. Two tests at the end run the real producer through it, including how a DAG advances the
baseline (the producer writes its state straight into the state file).

Run with:  uv run tests/test_external_library_snapshot_compare.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:  # the scripts import immich_api from the repo root
    sys.path.insert(0, str(SCRIPT_ROOT))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare = load("external_library_snapshot_compare")
producer = load("external_library_snapshot")

ROOT = "/srv/photos"
DAY = 86_400_000_000_000


def state_of(files=None, dirs=None, *, root=ROOT, tool=producer.REPORT_TOOL):
    """A state document as a producer writes it."""
    return {
        "tool": tool,
        "version": 1,
        "generated_at": "2026-09-30T12:00:00+0200",
        "source": f"dir:{root}",
        "unreadable": 0,
        "elapsed_seconds": 1.5,
        "paths": [{"path": root, "files": files or {}, "dirs": dirs or []}],
    }


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.state = self.directory / "photos.state.json"
        self.current = self.directory / "photos.now.json"

    def write(self, path: Path, content) -> str:
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return str(path)

    def set_baseline(self, baseline) -> str:
        return self.write(self.state, baseline)

    def compare(self, current, *extra, state=None):
        """Write `current` and compare it with whatever baseline is in place."""
        return self.run_compare(
            "--state", state if state is not None else str(self.state),
            "--current", self.write(self.current, current),
            *extra,
        )

    def run_compare(self, *argv, stdin_text=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            logger = compare.Logger(quiet="-q" in argv, stream=err)
            if stdin_text is None:
                code = compare.main(list(argv), log=logger, now=lambda: 0.0)
            else:
                with unittest.mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
                    code = compare.main(list(argv), log=logger, now=lambda: 0.0)
        return code, out.getvalue(), err.getvalue()

    def test_a_missing_baseline_reports_no_state_and_changed(self):
        code, out, err = self.compare(state_of({"a.jpg": [1, DAY]}), state=str(self.directory / "nope.json"))

        self.assertEqual(code, 0)
        self.assertIn("no usable state", out)
        self.assertIn("not compared (no-state)", out)
        self.assertIn("changed=yes", out)
        self.assertEqual(err, "")  # nothing to compare against is not a warning

    def test_an_unchanged_tree_is_unchanged(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}, ["2026"]))

        code, out, _ = self.compare(state_of({"a.jpg": [1, DAY]}, ["2026"]))

        self.assertEqual(code, 0)
        self.assertIn("changes: added=0 removed=0 modified=0 dirs_added=0 dirs_removed=0", out)
        self.assertIn("changed=no", out)

    def test_the_verdict_compares_both_states_field_by_field(self):
        self.set_baseline(state_of({"stay.jpg": [5, DAY], "gone.jpg": [1, DAY], "edit.jpg": [1, DAY]}, ["old"]))

        code, out, _ = self.compare(
            state_of({"stay.jpg": [5, DAY], "edit.jpg": [4, DAY], "new.jpg": [2, DAY]}, ["new"])
        )

        self.assertEqual(code, 0)
        self.assertIn("added=1 removed=1 modified=1 dirs_added=1 dirs_removed=1", out)
        self.assertIn("changed=yes", out)

    def test_list_names_the_reason_per_file(self):
        self.set_baseline(state_of({"resized.jpg": [1, DAY], "retagged.jpg": [9, DAY]}))

        _, out, _ = self.compare(state_of({"resized.jpg": [4, DAY], "retagged.jpg": [9, DAY + 1]}), "--list")

        self.assertIn(f"  ~ {os.path.join(ROOT, 'resized.jpg')} (1 -> 4 bytes)", out)
        self.assertIn(f"  ~ {os.path.join(ROOT, 'retagged.jpg')} (same size, newer mtime)", out)

    def test_a_new_directory_is_listed_as_such(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}))

        _, out, _ = self.compare(state_of({"a.jpg": [1, DAY]}, ["2026"]), "--list")

        self.assertIn(f"  + dir {os.path.join(ROOT, '2026')}", out)

    def test_a_different_set_of_paths_counts_as_changed(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}))

        _, out, _ = self.compare(state_of({"a.jpg": [1, DAY]}, root="/srv/videos"))

        self.assertIn("paths-changed", out)
        self.assertIn("changed=yes", out)

    def test_an_unusable_baseline_fails_open_with_a_warning(self):
        self.set_baseline("{not json")

        code, out, err = self.compare(state_of({"a.jpg": [1, DAY]}))

        self.assertEqual(code, 0)
        self.assertIn("changed=yes", out)
        self.assertIn("baseline-unreadable", out)
        self.assertIn("warning: cannot parse the baseline state", err)

    def test_a_baseline_without_paths_fails_open(self):
        self.set_baseline({"tool": producer.REPORT_TOOL, "paths": []})

        _, out, err = self.compare(state_of({"a.jpg": [1, DAY]}))

        self.assertIn("baseline-malformed", out)
        self.assertIn("changed=yes", out)
        self.assertIn("no usable 'paths' list", err)

    def test_any_producer_may_write_the_baseline(self):
        """A baseline is a state like any other - including one from the SMB producer."""
        self.set_baseline(state_of({"a.jpg": [1, DAY]}, tool="external_library_smb_snapshot"))

        code, out, _ = self.compare(state_of({"a.jpg": [1, DAY]}))

        self.assertEqual(code, 0)
        self.assertIn("changed=no", out)
        self.assertIn("external_library_smb_snapshot", out)

    def test_an_unusable_current_state_is_a_configuration_error(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}))

        for current in ("missing.json", self.write(self.current, "{broken"), self.write(self.current, {"paths": []})):
            code, out, err = self.run_compare("--state", str(self.state), "--current", current)
            self.assertEqual(code, 2, current)
            self.assertEqual(out, "")
            self.assertIn("is unusable", err)

    def test_the_current_state_can_come_from_stdin(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}))

        code, out, _ = self.run_compare(
            "--state", str(self.state), "--current", "-", "-q",
            stdin_text=json.dumps(state_of({"a.jpg": [1, DAY]})),
        )

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "changed=no")

    def test_quiet_prints_only_the_verdict(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}))

        _, out, _ = self.compare(state_of({"a.jpg": [99, DAY]}), "-q")

        self.assertEqual(out.strip(), "changed=yes")

    def test_the_report_carries_both_sides(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}))

        _, out, _ = self.compare(state_of({"a.jpg": [2, DAY]}), "--json", "--list")

        report = json.loads(out)
        self.assertEqual(report["tool"], compare.REPORT_TOOL)
        self.assertEqual(report["state"], str(self.state))
        self.assertEqual(report["current"], str(self.current))
        self.assertEqual(report["changed"], True)
        self.assertEqual(report["reason"], "changed")
        self.assertEqual(report["producer"]["tool"], producer.REPORT_TOOL)
        self.assertEqual(report["paths"][0]["changes"]["modified"], 1)
        self.assertEqual(report["changes"]["total"], 1)
        self.assertEqual(report["details"][ROOT]["modified"], ["a.jpg"])

    def test_the_report_of_an_unusable_baseline_says_so(self):
        self.set_baseline("{broken")

        _, out, _ = self.compare(state_of({"a.jpg": [1, DAY]}), "--json")

        report = json.loads(out)
        self.assertEqual(report["changed"], True)
        self.assertEqual(report["reason"], "baseline-unreadable")
        self.assertEqual(report["changes"]["total"], 0)

    def test_nothing_is_written_anywhere(self):
        self.set_baseline(state_of({"a.jpg": [1, DAY]}))
        current = self.write(self.current, state_of({"a.jpg": [2, DAY]}))
        before = sorted((entry.name, entry.stat().st_mtime_ns) for entry in self.directory.iterdir())

        self.run_compare("--state", str(self.state), "--current", current)

        after = sorted((entry.name, entry.stat().st_mtime_ns) for entry in self.directory.iterdir())
        self.assertEqual(after, before)


class IntegrationTest(unittest.TestCase):
    """The real producer through the real comparer, in the shape a DAG uses."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.tree = self.directory / "photos"
        self.tree.mkdir()
        (self.tree / "a.jpg").write_bytes(b"x" * 10)
        self.state = self.directory / "photos.state.json"
        self.now = self.directory / "photos.now.json"

    def produce(self, target):
        """Run the producer into a file - what a DAG step does."""
        with contextlib.redirect_stdout(io.StringIO()):
            code = producer.main(
                ["--path", str(self.tree), "-q", "--out", str(target)],
                log=producer.Logger(quiet=True, stream=io.StringIO()),
                now=lambda: 0.0,
            )
        self.assertEqual(code, 0)
        return target

    def compare(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            code = compare.main(
                ["--state", str(self.state), "--current", str(self.now), *extra],
                log=compare.Logger(quiet="-q" in extra, stream=err),
                now=lambda: 0.0,
            )
        return code, out.getvalue(), err.getvalue()

    def test_produce_compare_produce_compare(self):
        self.produce(self.now)

        code, out, _ = self.compare()
        self.assertEqual(code, 0)
        self.assertIn("changed=yes", out)  # no baseline yet
        self.assertIn(f"current: {self.tree} (1 file, 0 dirs)", out)

        self.produce(self.state)  # the action ran, the producer writes the new baseline
        self.assertIn("changed=no", self.compare()[1])

        (self.tree / "b.jpg").write_bytes(b"y")
        self.produce(self.now)
        code, out, _ = self.compare("--list")
        self.assertIn("changed=yes", out)
        self.assertIn(f"  + {os.path.join(str(self.tree), 'b.jpg')}", out)

    def test_the_pipeline_form(self):
        self.produce(self.state)

        listing = io.StringIO()
        with contextlib.redirect_stdout(listing):
            self.assertEqual(
                producer.main(
                    ["--path", str(self.tree), "-q"],
                    log=producer.Logger(quiet=True, stream=io.StringIO()),
                    now=lambda: 0.0,
                ),
                0,
            )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with unittest.mock.patch.object(sys, "stdin", io.StringIO(listing.getvalue())):
                code = compare.main(
                    ["--state", str(self.state), "--current", "-", "-q"],
                    log=compare.Logger(quiet=True, stream=io.StringIO()),
                    now=lambda: 0.0,
                )

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "changed=no")


if __name__ == "__main__":
    unittest.main(verbosity=2)
