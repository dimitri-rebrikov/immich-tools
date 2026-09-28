# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for external_library_changes.py. No network and no real library: temp directories only.

Run with:  uv run tests/test_external_library_changes.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPT_ROOT) not in sys.path:  # the script imports immich_api from the repo root
    sys.path.insert(0, str(SCRIPT_ROOT))

SCRIPT_PATH = SCRIPT_ROOT / "external_library_changes.py"
_spec = importlib.util.spec_from_file_location("external_library_changes", SCRIPT_PATH)
external_library_changes = importlib.util.module_from_spec(_spec)
sys.modules["external_library_changes"] = external_library_changes
_spec.loader.exec_module(external_library_changes)

MTIME = 1_700_000_000_000_000_000  # a fixed mtime in ns, so mtime-only changes are deterministic


def run(*argv: str):
    """Run main() with captured streams. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out):
        code = external_library_changes.main(
            list(argv), log=external_library_changes.Logger(stream=err), now=time.time
        )
    return code, out.getvalue(), err.getvalue()


class TreeTestCase(unittest.TestCase):
    """A temp tree with one watched library and a state file next to it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.library = self.base / "library"
        self.library.mkdir()
        self.state = self.base / "state.json"

    def write(self, relative: str, content: str = "x", *, mtime_ns: int | None = None) -> Path:
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def check(self, *extra: str):
        return run("--path", str(self.library), "--state", str(self.state), "--check", *extra)

    def record(self, *extra: str):
        return run("--path", str(self.library), "--state", str(self.state), "--record", *extra)

    def record_for_real(self, *extra: str):
        return run("--path", str(self.library), "--state", str(self.state), "--record", "--apply", *extra)

    def status(self, *extra: str):
        return run("--state", str(self.state), "--status", *extra)

    @staticmethod
    def verdict(out: str) -> str:
        lines = [line for line in out.splitlines() if line.startswith("changed=")]
        return lines[-1] if lines else ""


class CheckTest(TreeTestCase):
    def test_first_check_reports_changed_and_writes_nothing(self):
        self.write("photo.jpg")

        code, out, _ = self.check()

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=yes")
        self.assertIn("changes: not compared (no-state)", out)
        self.assertFalse(self.state.exists())

    def test_check_after_record_is_unchanged(self):
        self.write("photo.jpg")
        self.record_for_real()

        code, out, _ = self.check()

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=no")

    def test_check_never_rewrites_the_state_file(self):
        self.write("photo.jpg")
        self.record_for_real()
        before = self.state.read_bytes()

        self.check("--list")

        self.assertEqual(self.state.read_bytes(), before)

    def test_deeply_nested_change_is_found(self):
        self.write("2026/urlaub/alt/tief/img.jpg", "x")
        self.record_for_real()

        self.write("2026/urlaub/alt/tief/img.jpg", "xx")
        code, out, _ = self.check()

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=yes")
        self.assertIn("changes: added=0 removed=0 modified=1 dirs_added=0 dirs_removed=0", out)

    def test_added_and_removed_files_are_counted(self):
        self.write("bleibt.jpg")
        self.write("weg.jpg")
        self.record_for_real()

        self.write("neu/tief/dazu.jpg")
        (self.library / "weg.jpg").unlink()
        _, out, _ = self.check()

        self.assertIn("changes: added=1 removed=1 modified=0 dirs_added=2 dirs_removed=0", out)
        self.assertEqual(self.verdict(out), "changed=yes")

    def test_renamed_file_counts_as_added_and_removed(self):
        self.write("alt.jpg")
        self.record_for_real()

        os.replace(self.library / "alt.jpg", self.library / "neu.jpg")
        _, out, _ = self.check()

        self.assertIn("changes: added=1 removed=1 modified=0", out)

    def test_removed_directory_tree_is_reported(self):
        self.write("ordner/tief/img.jpg")
        self.record_for_real()

        (self.library / "ordner" / "tief" / "img.jpg").unlink()
        (self.library / "ordner" / "tief").rmdir()
        (self.library / "ordner").rmdir()
        _, out, _ = self.check()

        self.assertIn("changes: added=0 removed=1 modified=0 dirs_added=0 dirs_removed=2", out)

    def test_same_size_with_a_newer_mtime_is_a_change(self):
        self.write("img.jpg", "x", mtime_ns=MTIME)
        self.record_for_real()

        self.write("img.jpg", "x", mtime_ns=MTIME + 1_000_000)
        code, out, _ = self.check("--list")

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=yes")
        self.assertIn("modified=1", out)
        self.assertIn("(same size, newer mtime)", out)

    def test_resized_file_is_a_change_even_with_a_restored_mtime(self):
        self.write("img.jpg", "x", mtime_ns=MTIME)
        self.record_for_real()

        self.write("img.jpg", "xxxx", mtime_ns=MTIME)
        _, out, _ = self.check("--list")

        self.assertEqual(self.verdict(out), "changed=yes")
        self.assertIn("(1 -> 4 bytes)", out)

    def test_empty_directories_are_tracked(self):
        self.record_for_real()

        (self.library / "neu" / "tief").mkdir(parents=True)
        _, out, _ = self.check()

        self.assertIn("changes: added=0 removed=0 modified=0 dirs_added=2 dirs_removed=0", out)
        self.assertEqual(self.verdict(out), "changed=yes")

        self.record_for_real()
        (self.library / "neu" / "tief").rmdir()
        (self.library / "neu").rmdir()
        _, out, _ = self.check()

        self.assertIn("changes: added=0 removed=0 modified=0 dirs_added=0 dirs_removed=2", out)
        self.assertEqual(self.verdict(out), "changed=yes")

    def test_default_excludes_hide_nas_and_os_noise(self):
        self.write("photo.jpg")
        self.record_for_real()

        self.write(".DS_Store", "x")
        self.write("@eaDir/thumb.jpg", "x")
        self.write("Thumbs.db", "x")
        self.write("upload.tmp", "x")
        code, out, _ = self.check()

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=no")

    def test_custom_exclude_hides_changes_only_when_it_is_used_on_both_sides(self):
        self.write("photo.jpg")
        self.write("notes.txt", "x")
        self.record_for_real("--exclude", "*.txt")

        self.write("notes.txt", "xxxx")
        _, out, _ = self.check("--exclude", "*.txt")

        self.assertEqual(self.verdict(out), "changed=no")

        _, out, _ = self.check()  # without the pattern the file is new again

        self.assertEqual(self.verdict(out), "changed=yes")

    def test_state_file_inside_the_watched_tree_is_ignored(self):
        self.write("photo.jpg")
        self.state = self.library / "state.json"
        self.record_for_real()

        code, out, _ = self.check()

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=no")

    def test_unusable_state_fails_open_with_a_warning(self):
        self.write("photo.jpg")
        self.state.write_text("not json at all", encoding="utf-8")

        code, out, err = self.check()

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=yes")
        self.assertIn("changes: not compared (state-unreadable)", out)
        self.assertIn("warning: ignoring the unusable state file", err)
        self.assertNotIn("Traceback", err)

    def test_state_of_another_path_reports_paths_changed(self):
        self.write("photo.jpg")
        self.record_for_real()
        second = self.base / "second"
        second.mkdir()

        code, out, _ = run("--path", str(second), "--state", str(self.state), "--check")

        self.assertEqual(code, 0)
        self.assertEqual(self.verdict(out), "changed=yes")
        self.assertIn("changes: not compared (paths-changed)", out)

    def test_quiet_prints_only_the_verdict(self):
        self.write("photo.jpg")

        code, out, _ = self.check("-q")

        self.assertEqual(code, 0)
        self.assertEqual(out, "changed=yes\n")

    def test_list_caps_the_details(self):
        self.write("photo.jpg")
        self.record_for_real()

        for index in range(25):
            self.write(f"neu/img_{index:02d}.jpg")
        _, out, _ = self.check("--list")

        added_files = [
            line for line in out.splitlines()
            if line.startswith(f"  + {os.path.join(str(self.library), 'neu', 'img_')}")
        ]
        self.assertEqual(len(added_files), 20)
        self.assertIn("  + ... and 5 more", out)
        # the new directory is listed as well, with its own marker
        self.assertIn(f"  + dir {os.path.join(str(self.library), 'neu')}", out)

    def test_json_is_the_only_stdout_document(self):
        self.write("photo.jpg")
        self.record_for_real()
        self.write("neu.jpg")

        code, out, err = self.check("--json", "--list")

        self.assertEqual(code, 0)
        report = json.loads(out)  # the whole stdout parses as one document
        self.assertEqual(report["tool"], "external_library_changes")
        self.assertEqual(report["mode"], "check")
        self.assertIs(report["changed"], True)
        self.assertEqual(report["reason"], "changed")
        self.assertEqual(report["changes"]["added"], 1)
        self.assertEqual(report["paths"][0]["changes"]["added"], 1)
        self.assertEqual(report["previous"]["records"], 1)
        self.assertIn("neu.jpg", report["details"][str(self.library)]["added"])
        self.assertIn("changed=yes", err)  # the human lines moved to stderr
        self.assertNotIn("changed=yes", out)

    def test_json_reports_an_unchanged_tree(self):
        self.write("photo.jpg")
        self.record_for_real()

        _, out, _ = self.check("--json")

        report = json.loads(out)
        self.assertIs(report["changed"], False)
        self.assertEqual(report["reason"], "unchanged")
        self.assertEqual(report["changes"]["total"], 0)
        self.assertNotIn("details", report)  # --list was not asked for

    def test_json_details_cap_and_count_omissions(self):
        self.write("photo.jpg")
        self.record_for_real()
        for index in range(25):
            self.write(f"neu/img_{index:02d}.jpg")

        _, out, _ = self.check("--json", "--list")

        details = json.loads(out)["details"][str(self.library)]
        self.assertEqual(len(details["added"]), 20)
        self.assertEqual(details["omitted"], {"added": 5})

    def test_several_paths_are_tracked_separately(self):
        second = self.base / "second"
        second.mkdir()
        self.write("photo.jpg")
        (second / "video.mp4").write_text("v", encoding="utf-8")
        run("--path", str(self.library), "--path", str(second), "--state", str(self.state), "--record", "--apply")

        (second / "video.mp4").write_text("vv", encoding="utf-8")
        code, out, _ = run(
            "--path", str(self.library), "--path", str(second), "--state", str(self.state), "--check", "--json"
        )

        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(len(report["paths"]), 2)
        self.assertEqual(report["changes"]["modified"], 1)
        changed_root = [entry for entry in report["paths"] if entry["changes"]["modified"] == 1]
        self.assertEqual(changed_root[0]["path"], str(second))

    def test_the_whole_dag_cycle_works(self):
        self.write("a/b/img.jpg", "x")

        self.assertEqual(self.verdict(self.check()[1]), "changed=yes")  # detect: no state yet
        self.assertEqual(self.record_for_real()[0], 0)  # record after the scan
        self.assertEqual(self.verdict(self.check()[1]), "changed=no")  # detect again: quiet

        self.write("a/b/img.jpg", "xx")
        self.assertEqual(self.verdict(self.check()[1]), "changed=yes")
        self.assertEqual(self.record_for_real()[0], 0)
        self.assertEqual(self.verdict(self.check()[1]), "changed=no")


class RecordTest(TreeTestCase):
    def test_record_without_apply_is_a_dry_run(self):
        self.write("photo.jpg")

        code, out, _ = self.record()

        self.assertEqual(code, 0)
        self.assertIn("WOULD RECORD 1 file, 0 dirs in 1 path ->", out)
        self.assertIn("DRY RUN: state not written (add --apply to write)", out)
        self.assertFalse(self.state.exists())

    def test_apply_writes_the_state_and_reports_it(self):
        self.write("photo.jpg")

        code, out, _ = self.record_for_real()

        self.assertEqual(code, 0)
        self.assertIn(f"RECORDED 1 file, 0 dirs -> {self.state}", out)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["tool"], "external_library_changes")
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["records"], 1)
        self.assertEqual(state["paths"][0]["path"], str(self.library))
        self.assertEqual(state["paths"][0]["files"], {"photo.jpg": [1, state["paths"][0]["files"]["photo.jpg"][1]]})
        self.assertFalse((self.state.with_name("state.json.tmp")).exists())

    def test_record_reports_what_changed_since_the_last_record(self):
        self.write("photo.jpg")
        self.record_for_real()
        self.write("neu.jpg", "xx")

        _, out, _ = self.record_for_real()

        self.assertIn("changes since the last record: added=1 removed=0 modified=0 dirs_added=0 dirs_removed=0", out)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["records"], 2)

    def test_record_counts_records_and_keeps_created_at(self):
        self.record_for_real()
        first = json.loads(self.state.read_text(encoding="utf-8"))

        self.record_for_real()
        second = json.loads(self.state.read_text(encoding="utf-8"))

        self.assertEqual(second["records"], 2)
        self.assertEqual(second["created_at"], first["created_at"])

    def test_record_does_not_advance_the_state_when_it_cannot_write(self):
        self.write("photo.jpg")
        code, _, err = run(
            "--path", str(self.library),
            "--state", str(self.base / "nope" / "state.json"),
            "--record",
            "--apply",
        )

        self.assertEqual(code, 1)
        self.assertIn("error: cannot write the state file", err)
        self.assertNotIn("Traceback", err)

    @unittest.skipIf(os.name == "nt", "chmod does not make a directory unreadable on Windows")
    def test_unreadable_root_fails_instead_of_recording_an_empty_tree(self):
        self.write("photo.jpg")
        self.record_for_real()
        before = self.state.read_bytes()
        os.chmod(self.library, 0o000)
        self.addCleanup(os.chmod, self.library, 0o700)

        code, _, err = self.record_for_real()

        self.assertEqual(code, 1)
        self.assertIn("error: cannot read the directory", err)
        self.assertEqual(self.state.read_bytes(), before)


class StatusTest(TreeTestCase):
    def test_status_reads_the_state_without_walking(self):
        self.write("photo.jpg")
        self.record_for_real()
        self.write("spaeter.jpg")  # status must not see this: it never looks at the tree

        code, out, _ = self.status()

        self.assertEqual(code, 0)
        self.assertIn(f"state: {self.state}", out)
        self.assertIn("version 1", out)
        self.assertIn("records: 1", out)
        self.assertIn("paths: 1", out)
        self.assertIn(f"  {self.library} - 1 file, 0 dirs", out)
        self.assertNotIn("spaeter.jpg", out)

    def test_status_without_a_state_is_not_an_error(self):
        code, out, _ = self.status()

        self.assertEqual(code, 0)
        self.assertIn("(missing)", out)
        self.assertIn("no snapshot yet", out)

    def test_status_of_a_broken_state_fails(self):
        self.state.write_text("{", encoding="utf-8")

        code, _, err = self.status()

        self.assertEqual(code, 1)
        self.assertIn("is not a usable state file", err)


class ArgumentTest(TreeTestCase):
    def test_path_is_required(self):
        code, _, err = run("--state", str(self.state), "--check")

        self.assertEqual(code, 2)
        self.assertIn("error: --path is required", err)

    def test_a_path_that_is_not_a_directory_is_rejected(self):
        self.write("photo.jpg")

        code, _, err = run("--path", str(self.library / "photo.jpg"), "--state", str(self.state), "--check")

        self.assertEqual(code, 2)
        self.assertIn("error: not a directory:", err)

    def test_apply_is_only_valid_with_record(self):
        code, _, err = run("--path", str(self.library), "--state", str(self.state), "--check", "--apply")

        self.assertEqual(code, 2)
        self.assertIn("error: --apply only belongs to --record", err)

    def test_state_is_required(self):
        with self.assertRaises(SystemExit) as caught:
            run("--path", str(self.library), "--check")

        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
