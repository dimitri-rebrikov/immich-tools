# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for external_library_smb_snapshot.py.

No SMB server and no `smbprotocol` package needed: the walk takes an injected directory lister and
the SMB specific parts are only reached through `RemoteTree`, so everything here is pure logic.

Run with:  uv run tests/test_external_library_smb_snapshot.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timezone
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


smb = load("external_library_smb_snapshot")
compare = load("external_library_snapshot_compare")

MTIME = 1_700_000_000_000_000_000  # ns, also used as a raw FILETIME below
EXCLUDES = smb.normalize_excludes([])

# A canned share: the root holds a year directory, a file, and the @eaDir noise a NAS creates.
TREE: dict[str, list] = {
    "": [("2026", 0, MTIME, True), ("lost.jpg", 10, MTIME, False), ("@eaDir", 0, MTIME, True)],
    "2026": [("09", 0, MTIME, True), ("b.tmp", 5, MTIME, False)],
    "2026/09": [("img.jpg", 1234, MTIME, False)],
}

# Enough directories to hand one to each of several sessions.
WIDE: dict[str, list] = {
    "": [(f"d{i}", 0, MTIME, True) for i in range(6)],
    **{f"d{i}": [(f"f{i}.jpg", i, MTIME, False)] for i in range(6)},
}


def lister(relative: str):
    """Serve TREE; listing an excluded directory fails the test (pruning must prevent it)."""
    if relative == "@eaDir":
        raise AssertionError("an excluded directory must never be listed")
    return list(TREE.get(relative, [])), False


class FakeField:
    def __init__(self, value):
        self._value = value

    def get_value(self):
        return self._value


class FakeInfo(dict):
    """Mimics the smbprotocol directory-information structure used by entry_of."""

    def __init__(self, name: str, *, size=0, mtime=smb.FILETIME_EPOCH_OFFSET, attributes=0):
        super().__init__(
            file_name=FakeField(name.encode("utf-16-le")),
            end_of_file=FakeField(size),
            last_write_time=FakeField(mtime),
            file_attributes=FakeField(attributes),
        )


class FakeTree:
    """What main() expects from RemoteTree: list_dir() and close()."""

    def __init__(self, tree=TREE, fail_start=False):
        self.tree = tree
        self.fail_start = fail_start
        self.closed = False

    def list_dir(self, relative: str):
        if self.fail_start and relative == "":
            return [], True
        return lister(relative)

    def close(self):
        self.closed = True


def run_smb(*argv: str, tree: FakeTree | None = None, factory=None):
    """Run main() against a fake tree, capturing stdout/stderr and the exit code.

    The logger is built here the way main() would build it, because main() only creates one when
    none is injected - so an injected logger has to carry the -q/-v flags itself.
    """
    out, err = io.StringIO(), io.StringIO()
    fake = tree if tree is not None else FakeTree()
    with contextlib.redirect_stdout(out):
        code = smb.main(
            list(argv),
            log=smb.Logger(quiet="-q" in argv, verbose="-v" in argv, stream=err),
            now=lambda: 1_000.0,
            tree_factory=factory or (lambda *args, **kwargs: fake),
        )
    return code, out.getvalue(), err.getvalue()


class WalkTest(unittest.TestCase):
    def test_collects_files_and_directories_and_skips_the_usual_noise(self):
        walk = smb.walk_remote("", lister, EXCLUDES, 1, smb.Logger(stream=io.StringIO()))

        self.assertEqual(sorted(walk["files"]), ["2026/09/img.jpg", "lost.jpg"])
        self.assertEqual(walk["files"]["2026/09/img.jpg"], (1234, MTIME))
        self.assertEqual(walk["dirs"], ["2026", "2026/09"])  # the @eaDir tree is not descended
        self.assertEqual(walk["unreadable"], 0)
        self.assertFalse(walk["root_failed"])

    def test_start_below_the_share_root_is_used_as_the_prefix(self):
        walk = smb.walk_remote("2026", lister, EXCLUDES, 1, smb.Logger(stream=io.StringIO()))

        # keys are relative to the start, so they match a mount of that subtree
        self.assertEqual(sorted(walk["files"]), ["09/img.jpg"])
        self.assertEqual(walk["dirs"], ["09"])

    def test_parallel_connections_produce_the_same_result(self):
        serial = smb.walk_remote("", lister, EXCLUDES, 1, smb.Logger(stream=io.StringIO()))
        parallel = smb.walk_remote("", lister, EXCLUDES, 4, smb.Logger(stream=io.StringIO()))

        self.assertEqual(serial, parallel)

    def test_sessions_are_used_one_after_the_other_and_each_directory_once(self):
        seen = [[], [], []]
        listers = []
        for index in range(3):
            def recorder(relative, index=index):
                seen[index].append(relative)
                return list(WIDE.get(relative, [])), False

            listers.append(recorder)

        walk = smb.walk_remote("", listers, EXCLUDES, 1, smb.Logger(stream=io.StringIO()))

        listed = sorted(relative for calls in seen for relative in calls)
        self.assertEqual(listed, ["", "d0", "d1", "d2", "d3", "d4", "d5"])  # each directory once
        counts = sorted(len(calls) for calls in seen)
        self.assertEqual(counts, [2, 2, 3])  # the sessions take turns, none of them is skipped
        self.assertEqual(len(walk["dirs"]), 6)
        self.assertEqual(len(walk["files"]), 6)

    def test_a_session_is_never_used_by_two_listings_at_once(self):
        busy = [False, False, False]
        calls = [0, 0, 0]

        def make(index):
            def lister(relative):
                self.assertFalse(busy[index], "a session must serve one listing at a time")
                busy[index] = True
                time.sleep(0.002)  # widen the window so the workers really overlap
                try:
                    calls[index] += 1
                    return list(WIDE.get(relative, [])), False
                finally:
                    busy[index] = False

            return lister

        walk = smb.walk_remote("", [make(i) for i in range(3)], EXCLUDES, 3, smb.Logger(stream=io.StringIO()))

        self.assertEqual(len(walk["dirs"]), 6)
        self.assertEqual(sorted(walk["files"]), [f"d{i}/f{i}.jpg" for i in range(6)])
        self.assertEqual(sum(calls), 7)  # the root plus six directories, none of them twice

    def test_custom_exclude_patterns_are_applied_to_files_and_directories(self):
        walk = smb.walk_remote("", lister, smb.normalize_excludes(["*.jpg"]), 1, smb.Logger(stream=io.StringIO()))

        self.assertEqual(sorted(walk["files"]), [])  # lost.jpg and 2026/09/img.jpg are gone

    def test_unreadable_subdirectory_is_counted_but_not_fatal(self):
        def broken(relative: str):
            return ([], True) if relative == "2026" else lister(relative)

        walk = smb.walk_remote("", broken, EXCLUDES, 1, smb.Logger(stream=io.StringIO()))

        self.assertEqual(walk["unreadable"], 1)  # the directory that failed to list
        self.assertFalse(walk["root_failed"])
        self.assertIn("lost.jpg", walk["files"])

    def test_unreadable_start_marks_the_root_as_failed(self):
        walk = smb.walk_remote("", lambda relative: ([], True), EXCLUDES, 1, smb.Logger(stream=io.StringIO()))

        self.assertTrue(walk["root_failed"])
        self.assertEqual(walk["files"], {})


class ParsingTest(unittest.TestCase):
    def test_filetime_conversion_handles_datetimes_and_raw_ticks(self):
        self.assertEqual(smb.filetime_to_ns(datetime(1970, 1, 1, tzinfo=timezone.utc)), 0)
        self.assertEqual(smb.filetime_to_ns(datetime(1970, 1, 1)), 0)  # naive means UTC
        self.assertEqual(smb.filetime_to_ns(datetime(1601, 1, 1, tzinfo=timezone.utc)), -(smb.FILETIME_EPOCH_OFFSET * 100))
        self.assertEqual(smb.filetime_to_ns(smb.FILETIME_EPOCH_OFFSET), 0)
        self.assertEqual(smb.filetime_to_ns(smb.FILETIME_EPOCH_OFFSET + 10), 1_000)  # 10 ticks = 1 us
        self.assertEqual(smb.filetime_to_ns(datetime(2026, 9, 28, 19, 31, 2, 123456, tzinfo=timezone.utc)) % 1_000_000, 456_000)

    def test_entry_of_reads_size_mtime_and_the_directory_flag(self):
        self.assertEqual(smb.entry_of(FakeInfo("a.jpg", size=99, attributes=0)), ("a.jpg", 99, 0, False))
        self.assertEqual(
            smb.entry_of(FakeInfo("dir", attributes=smb.FILE_ATTRIBUTE_DIRECTORY)),
            ("dir", 0, 0, True),
        )

    def test_entry_of_skips_dot_entries(self):
        self.assertIsNone(smb.entry_of(FakeInfo(".")))
        self.assertIsNone(smb.entry_of(FakeInfo("..")))

    def test_reparse_points_stay_leaves_even_when_they_are_directories(self):
        attributes = smb.FILE_ATTRIBUTE_DIRECTORY | smb.FILE_ATTRIBUTE_REPARSE_POINT

        self.assertEqual(smb.entry_of(FakeInfo("link", attributes=attributes)), ("link", 0, 0, False))

    def test_one_exclude_flag_can_carry_several_patterns(self):
        """Both producers must read the same form, or their noise lists drift apart."""
        self.assertEqual(
            smb.normalize_excludes([" a , ,b"]),
            (*smb.DEFAULT_EXCLUDES, "a", "b"),
        )

    def test_path_helpers(self):
        self.assertEqual(smb.unc_share("nas", "/photos/"), "\\\\nas\\photos")
        self.assertEqual(smb.to_smb_name(""), "")
        self.assertEqual(smb.to_smb_name("/2026/09"), "2026\\09")

    def test_session_credentials_fit_the_installed_smbprotocol_version(self):
        class NewSession:
            def __init__(self, connection, username=None, password=None, require_encryption=True,
                         hostname_override=None, auth_protocol="negotiate"):
                pass

        class OldSession:
            def __init__(self, connection, username=None, password=None, domain_name=None,
                         require_encryption=False):
                pass

        # current smbprotocol: no domain_name argument, so the domain goes into the username
        user, kwargs = smb.session_credentials(NewSession, "immich", "WORKGROUP")
        self.assertEqual(user, "WORKGROUP\\immich")
        self.assertEqual(kwargs, {"require_encryption": False})

        # older smbprotocol: the domain is a session argument
        user, kwargs = smb.session_credentials(OldSession, "immich", "WORKGROUP")
        self.assertEqual(user, "immich")
        self.assertEqual(kwargs, {"require_encryption": False, "domain_name": "WORKGROUP"})

        # no domain configured: nothing to pass either way
        self.assertEqual(smb.session_credentials(NewSession, "immich", ""), ("immich", {"require_encryption": False}))


class CliTest(unittest.TestCase):
    def test_missing_credentials_are_a_configuration_error(self):
        with unittest.mock.patch.dict(os.environ, {"SMB_USER": "", "SMB_PASSWORD": ""}, clear=False):
            code, out, err = run_smb("--host", "nas", "--share", "photos")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error: --user/SMB_USER", err)

    def test_snapshot_is_written_to_stdout_with_the_given_key(self):
        code, out, err = run_smb(
            "--host", "nas", "--share", "photos", "--user", "u", "--password", "p", "--key", "/srv/photos"
        )

        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["tool"], "external_library_smb_snapshot")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["paths"][0]["path"], "/srv/photos")
        self.assertEqual(sorted(document["paths"][0]["files"]), ["2026/09/img.jpg", "lost.jpg"])
        self.assertEqual(document["paths"][0]["dirs"], ["2026", "2026/09"])
        self.assertEqual(document["source"], "smb://nas/photos")
        self.assertEqual(document["unreadable"], 0)
        self.assertIn("snapshot: 2 files, 2 dirs", err)

    def test_key_and_source_take_the_subdir_into_account(self):
        _, out, _ = run_smb("--host", "nas", "--share", "photos", "--subdir", "2026/09/", "--user", "u", "--password", "p")

        document = json.loads(out)
        self.assertEqual(document["paths"][0]["path"], "//nas/photos/2026/09")
        self.assertEqual(document["source"], "smb://nas/photos/2026/09")
        self.assertEqual(sorted(document["paths"][0]["files"]), ["img.jpg"])

    def test_quiet_silences_progress(self):
        code, out, err = run_smb("--host", "nas", "--share", "photos", "--user", "u", "--password", "p", "-q")

        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["paths"])
        self.assertEqual(err, "")

    def test_out_writes_a_file_and_list_previews_on_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "photos.snapshot.json"
            code, out, err = run_smb(
                "--host", "nas", "--share", "photos", "--user", "u", "--password", "p",
                "--out", str(target), "--list",
            )

            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            self.assertTrue(json.loads(target.read_text(encoding="utf-8"))["paths"])
            self.assertIn("lost.jpg", err)

    def test_out_into_a_missing_directory_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as folder:
            code, _, err = run_smb(
                "--host", "nas", "--share", "photos", "--user", "u", "--password", "p",
                "--out", str(Path(folder) / "nope" / "s.json"),
            )

        self.assertEqual(code, 2)
        self.assertIn("error: cannot write the snapshot", err)

    def test_unreadable_start_fails_without_writing_a_snapshot(self):
        code, out, err = run_smb(
            "--host", "nas", "--share", "photos", "--user", "u", "--password", "p",
            tree=FakeTree(fail_start=True),
        )

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("error: cannot read smb://nas/photos", err)

    def test_password_file_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / "cred"
            secret.write_text("p\n", encoding="utf-8")
            code, _, err = run_smb(
                "--host", "nas", "--share", "photos", "--user", "u", "--password-file", str(secret)
            )

        self.assertEqual(code, 0)
        self.assertIn("snapshot:", err)


class SessionTest(unittest.TestCase):
    """`--connections` opens one session per connection, because a single session is what a server caps."""

    def factory(self, trees: list, refuse: int | None = None):
        lock = threading.Lock()
        counter = {"built": 0}

        def build(*args, **kwargs):
            with lock:
                counter["built"] += 1
                index = counter["built"]
            if refuse is not None and index == refuse:
                raise OSError("too many sessions for this share")
            tree = FakeTree()
            trees.append(tree)
            return tree

        return build

    def walk_with(self, *extra: str, factory=None, refuse: int | None = None):
        trees: list = []
        code, out, err = run_smb(
            "--host", "nas", "--share", "photos", "--user", "u", "--password", "p",
            "--out", "-", *extra, factory=factory or self.factory(trees, refuse),
        )
        return code, out, err, trees

    @staticmethod
    def always_refusing(message: str):
        """A server that refuses every logon (wrong credentials, per-user connection limit reached)."""

        def build(*args, **kwargs):
            raise OSError(message)

        return build

    def test_each_connection_gets_its_own_session_and_all_of_them_are_closed(self):
        code, _, err, trees = self.walk_with("--connections", "3")

        self.assertEqual(code, 0)
        self.assertEqual(len(trees), 3)
        self.assertTrue(all(tree.closed for tree in trees))
        self.assertNotIn("sessions are usable", err)

    def test_a_refused_session_only_costs_a_warning(self):
        code, out, err, trees = self.walk_with("--connections", "4", refuse=3)

        self.assertEqual(code, 0)
        self.assertEqual(len(trees), 3)
        self.assertTrue(all(tree.closed for tree in trees))
        self.assertIn("warning: only 3 of 4 sessions are usable", err)
        self.assertIn("too many sessions for this share", err)
        self.assertIn("snapshot:", err)
        self.assertIn('"unreadable":0', out)

    def test_no_usable_session_is_a_runtime_error(self):
        code, out, err, trees = self.walk_with(
            "--connections", "3", factory=self.always_refusing("no logon possible")
        )

        self.assertEqual(code, 1)
        self.assertEqual(trees, [])
        self.assertIn("error: cannot connect to", err)
        self.assertIn("nas", err)
        self.assertIn("no logon possible", err)
        self.assertEqual(out, "")  # nothing is written, so no half-empty tree can be recorded

    def test_a_single_connection_still_reports_a_refused_logon(self):
        code, out, err, _ = self.walk_with(refuse=1)

        self.assertEqual(code, 1)
        self.assertIn("error: cannot connect to", err)
        self.assertEqual(out, "")


class CreditWindowTest(unittest.TestCase):
    """The SMB2 credit window caps how many requests may be in flight on one connection."""

    class FakeConnection:
        def __init__(self, granted=64, failure=None):
            self.granted = granted
            self.failure = failure
            self.calls = []

        def echo(self, sid=None, credit_request=None):
            self.calls.append((sid, credit_request))
            if self.failure:
                raise self.failure
            return self.granted

    class FakeSession:
        session_id = 1234

    def test_the_session_is_asked_for_the_credits(self):
        connection = self.FakeConnection(granted=64)
        errors = io.StringIO()

        granted = smb.request_credits(connection, self.FakeSession(), 32, smb.Logger(stream=errors))

        self.assertEqual(granted, 64)  # the server may grant less than asked, that is its policy
        self.assertEqual(connection.calls, [(1234, 32)])  # the session id is required by Windows

    def test_the_grant_is_only_reported_with_verbose(self):
        quiet, loud = io.StringIO(), io.StringIO()

        smb.request_credits(self.FakeConnection(), self.FakeSession(), 32, smb.Logger(stream=quiet))
        smb.request_credits(self.FakeConnection(), self.FakeSession(), 32, smb.Logger(stream=loud, verbose=True))

        self.assertEqual(quiet.getvalue(), "")
        self.assertIn("credits: 64", loud.getvalue())

    def test_a_refused_credit_request_is_not_fatal(self):
        connection = self.FakeConnection(failure=OSError("not supported"))
        errors = io.StringIO()

        granted = smb.request_credits(connection, self.FakeSession(), 32, smb.Logger(stream=errors, verbose=True))

        self.assertIsNone(granted)
        self.assertIn("did not grant more credits", errors.getvalue())

    def test_every_session_asks_for_the_same_credit_window(self):
        """The window is per session, so it must not grow with the number of sessions."""
        seen = []

        def factory(*args, **kwargs):
            seen.append(kwargs)
            return FakeTree()

        run_smb("--host", "nas", "--share", "photos", "--user", "u", "--password", "p",
                "--out", "-", factory=factory)
        self.assertEqual(seen, [{"credits": smb.SESSION_CREDITS}])

        seen.clear()
        run_smb("--host", "nas", "--share", "photos", "--user", "u", "--password", "p",
                "--connections", "8", "--out", "-", factory=factory)
        self.assertEqual(seen, [{"credits": smb.SESSION_CREDITS}] * 8)


class IntegrationTest(unittest.TestCase):
    """The produced document must reach the comparer and the state file without any translation."""

    def test_the_produced_document_becomes_the_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "photos.snapshot.json"
            state = Path(folder) / "photos.state.json"
            env = {"SMB_USER": "u", "SMB_PASSWORD": "p"}

            def produce(target: Path):
                with unittest.mock.patch.dict(os.environ, env, clear=False):
                    code, _, _ = run_smb(
                        "--host", "nas", "--share", "photos", "--user", "u", "--password", "p",
                        "--key", "/srv/photos", "--out", str(target),
                    )
                self.assertEqual(code, 0)

            def compare_against():
                capture, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(capture):
                    result = compare.main(
                        ["--state", str(state), "--current", str(snapshot)],
                        log=compare.Logger(stream=errors),
                        now=time.time,
                    )
                return result, capture.getvalue(), errors.getvalue()

            produce(snapshot)
            first, out_first, _ = compare_against()
            self.assertEqual(first, 0)
            self.assertIn("changed=yes", out_first)  # no baseline yet

            produce(state)  # the record step: the producer writes the state file
            second, out_second, _ = compare_against()
            self.assertEqual(second, 0)
            self.assertIn("changed=no", out_second)
            self.assertIn("current: /srv/photos (2 files, 2 dirs)", out_second)

            recorded = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(recorded["paths"][0]["path"], "/srv/photos")
            self.assertEqual(recorded["paths"][0]["files"]["2026/09/img.jpg"], [1234, MTIME])

    def test_every_path_of_the_document_is_compared(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / "s.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "tool": "external_library_smb_snapshot",
                        "paths": [
                            {"path": "/srv/photos", "files": {"a.jpg": [1, 2]}, "dirs": []},
                            {"path": "/srv/videos", "files": {"b.mp4": [3, 4]}, "dirs": []},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            capture, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(capture):
                code = compare.main(
                    ["--state", str(Path(folder) / "state.json"), "--current", str(snapshot), "--json"],
                    log=compare.Logger(stream=errors),
                    now=time.time,
                )

        self.assertEqual(code, 0)
        report = json.loads(capture.getvalue())
        self.assertEqual([entry["path"] for entry in report["paths"]], ["/srv/photos", "/srv/videos"])
        self.assertEqual(report["current"], str(snapshot))
        self.assertEqual(report["producer"]["tool"], "external_library_smb_snapshot")


if __name__ == "__main__":
    unittest.main(verbosity=2)
