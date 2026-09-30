# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for external_library_snapshot.py.

The producer has no HTTP layer and no SMB, so everything runs against temp directories. The last
test runs the produced document through the comparer: the listing must reach the state file unchanged,
otherwise the two scripts would drift apart.

Run with:  uv run tests/test_external_library_snapshot.py
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
if str(SCRIPT_ROOT) not in sys.path:  # the scripts import immich_api from the repo root
    sys.path.insert(0, str(SCRIPT_ROOT))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


snapshot = load("external_library_snapshot")
compare = load("external_library_snapshot_compare")
smb = load("external_library_smb_snapshot")


def run_snapshot(*argv: str):
    """Run main() with captured stdout/stderr and a frozen clock."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out):
        code = snapshot.main(
            list(argv),
            log=snapshot.Logger(quiet="-q" in argv, verbose="-v" in argv, stream=err),
            now=lambda: 1_000.0,
        )
    return code, out.getvalue(), err.getvalue()


class TreeTest(unittest.TestCase):
    """The listing itself: what ends up in `files` and `dirs`."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "photos"
        (self.root / "2026" / "09").mkdir(parents=True)
        (self.root / "2026" / "empty").mkdir()
        (self.root / "2026" / "09" / "img.jpg").write_bytes(b"x" * 10)
        (self.root / "lost.jpg").write_bytes(b"y" * 20)
        (self.root / "@eaDir").mkdir()
        (self.root / "@eaDir" / "thumb.jpg").write_bytes(b"z")
        (self.root / "scratch.tmp").write_bytes(b"z")

    def walk(self, *argv):
        return run_snapshot("--path", str(self.root), *argv)

    def test_files_carry_size_and_mtime_in_nanoseconds(self):
        code, out, _ = self.walk()

        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["tool"], "external_library_snapshot")
        self.assertEqual(document["version"], snapshot.SNAPSHOT_VERSION)
        self.assertEqual(document["source"], f"dir:{self.root}")
        self.assertEqual(document["unreadable"], 0)
        self.assertEqual([entry["path"] for entry in document["paths"]], [str(self.root)])

        entry = document["paths"][0]
        info = (self.root / "lost.jpg").stat()
        self.assertEqual(entry["files"]["lost.jpg"], [info.st_size, info.st_mtime_ns])
        self.assertEqual(sorted(entry["files"]), ["2026/09/img.jpg", "lost.jpg"])
        self.assertEqual(entry["dirs"], ["2026", "2026/09", "2026/empty"])

    def test_the_usual_noise_is_ignored(self):
        _, out, _ = self.walk()

        files = json.loads(out)["paths"][0]["files"]
        self.assertNotIn("scratch.tmp", files)
        self.assertNotIn("@eaDir/thumb.jpg", files)

    def test_exclude_prunes_directories(self):
        _, out, _ = self.walk("--exclude", "2026")

        entry = json.loads(out)["paths"][0]
        self.assertEqual(list(entry["files"]), ["lost.jpg"])
        self.assertEqual(entry["dirs"], [])

    def test_one_exclude_flag_can_carry_several_patterns(self):
        _, out, _ = self.walk("--exclude", "2026, scratch.tmp")

        entry = json.loads(out)["paths"][0]
        self.assertEqual(list(entry["files"]), ["lost.jpg"])
        self.assertEqual(entry["dirs"], [])

    def test_comma_separated_excludes_ignore_empty_entries_and_spaces(self):
        self.assertEqual(
            snapshot.normalize_excludes([" a , ,b"]),
            (*snapshot.DEFAULT_EXCLUDES, "a", "b"),
        )

    def test_a_deeply_nested_file_is_found(self):
        deep = self.root / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "deep.jpg").write_bytes(b"x")

        _, out, _ = self.walk()

        self.assertIn("a/b/c/d/deep.jpg", json.loads(out)["paths"][0]["files"])

    def test_symlinks_stay_leaves(self):
        try:
            os.symlink(self.root / "2026", self.root / "link")
        except (OSError, NotImplementedError):  # Windows without developer mode
            self.skipTest("symlinks are not available here")

        _, out, _ = self.walk()

        entry = json.loads(out)["paths"][0]
        self.assertIn("link", entry["files"])  # a link is a file, never a directory
        self.assertNotIn("link", entry["dirs"])
        self.assertNotIn("link/img.jpg", entry["files"])

    def test_the_snapshot_file_never_ends_up_in_the_snapshot(self):
        target = self.root / "snapshot.json"

        self.assertEqual(self.walk("--out", str(target))[0], 0)
        self.assertEqual(self.walk("--out", str(target))[0], 0)  # the second run sees the first file

        entry = json.loads(target.read_text(encoding="utf-8"))["paths"][0]
        self.assertNotIn("snapshot.json", entry["files"])

    def test_several_paths_are_listed_separately(self):
        second = Path(self.folder.name) / "videos"
        second.mkdir()
        (second / "clip.mp4").write_bytes(b"v")

        _, out, _ = run_snapshot("--path", str(self.root), "--path", str(second))

        document = json.loads(out)
        self.assertEqual([entry["path"] for entry in document["paths"]], [str(self.root), str(second)])
        self.assertEqual(document["source"], "dir")
        self.assertEqual(list(document["paths"][1]["files"]), ["clip.mp4"])

    @unittest.skipIf(os.name == "nt", "chmod does not make a directory unreadable on Windows")
    def test_an_unreadable_subdirectory_is_counted_but_not_fatal(self):
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "hidden.jpg").write_bytes(b"x")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)

        code, out, err = self.walk()

        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["unreadable"], 1)
        self.assertIn("cannot read", err)

    @unittest.skipIf(os.name == "nt", "chmod does not make a directory unreadable on Windows")
    def test_an_unreadable_root_fails_without_writing_a_snapshot(self):
        self.root.chmod(0o000)
        self.addCleanup(self.root.chmod, 0o700)
        target = Path(self.folder.name) / "doc.json"

        code, _, err = run_snapshot("--path", str(self.root), "--out", str(target))

        self.assertEqual(code, 1)
        self.assertIn("the snapshot is not written", err)
        self.assertFalse(target.exists())  # no half empty tree can be recorded


class CliTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "photos"
        self.root.mkdir()
        (self.root / "a.jpg").write_bytes(b"x" * 10)

    def test_out_writes_a_file_and_list_previews_on_stderr(self):
        target = Path(self.folder.name) / "doc.json"

        code, out, err = run_snapshot("--path", str(self.root), "--out", str(target), "--list")

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("snapshot: 1 file, 0 dirs", err)
        self.assertIn("  a.jpg", err)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["paths"][0]["files"].keys(), {"a.jpg"})

    def test_quiet_silences_progress(self):
        _, out, err = run_snapshot("--path", str(self.root), "-q")

        self.assertEqual(err, "")
        self.assertIn("a.jpg", out)

    def test_a_path_that_is_not_a_directory_is_a_configuration_error(self):
        code, out, err = run_snapshot("--path", str(self.root / "nope"))

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("is not a directory", err)

    def test_out_into_a_missing_directory_is_a_configuration_error(self):
        code, _, err = run_snapshot("--path", str(self.root), "--out", str(self.root / "nope" / "doc.json"))

        self.assertEqual(code, 2)
        self.assertIn("is not a directory", err)

    def test_default_excludes_match_the_smb_producer(self):
        self.assertEqual(snapshot.DEFAULT_EXCLUDES, smb.DEFAULT_EXCLUDES)


class CompatibilityTest(unittest.TestCase):
    def test_the_document_survives_the_comparer_round_trip(self):
        """Producer -> comparer must not change a single entry of the listing."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "photos"
            (root / "2026" / "09").mkdir(parents=True)
            (root / "2026" / "09" / "img.jpg").write_bytes(b"x" * 10)
            (root / "lost.jpg").write_bytes(b"y" * 20)
            document = Path(folder) / "doc.json"

            code, _, _ = run_snapshot("--path", str(root), "--out", str(document))
            self.assertEqual(code, 0)

            listings, meta, problem = compare.load_state(
                str(document), compare.Logger(stream=io.StringIO()), role="current"
            )
            self.assertIsNone(problem)
            self.assertEqual(meta["tool"], snapshot.REPORT_TOOL)

            produced = json.loads(document.read_text(encoding="utf-8"))["paths"][0]
            listing = listings[produced["path"]]
            self.assertEqual({name: list(size) for name, size in listing["files"].items()}, produced["files"])
            self.assertEqual(listing["dirs"], produced["dirs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
