import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from regit import cli, daemon  # noqa: E402
from regit.store import RegitError, Shadow, find_project, load_projects, register  # noqa: E402
from regit.timeparse import parse_duration, parse_spec  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.proj = base / "proj"
        self.proj.mkdir()
        self._old_env = os.environ.get("REGIT_HOME")
        os.environ["REGIT_HOME"] = str(self.home)
        self._old_cwd = os.getcwd()
        os.chdir(self.proj)

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_env is None:
            os.environ.pop("REGIT_HOME", None)
        else:
            os.environ["REGIT_HOME"] = self._old_env
        self.tmp.cleanup()

    def write(self, name, text):
        p = self.proj / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def shadow(self):
        sh = Shadow(self.proj, self.home)
        sh.init()
        register(sh.workdir, self.home)
        return sh

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()


class TimeParseTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration("14m"), 840)
        self.assertEqual(parse_duration("14min"), 840)
        self.assertEqual(parse_duration("14"), 840)
        self.assertEqual(parse_duration("2h30m"), 9000)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration("90s"), 90)
        self.assertIsNone(parse_duration("abc"))
        self.assertIsNone(parse_duration(""))

    def test_specs(self):
        now = 1_000_000.0
        self.assertEqual(parse_spec("14m", now).value, now - 840)
        self.assertEqual(parse_spec("@3", now).kind, "index")
        self.assertEqual(parse_spec("deadbeef", now).kind, "rev")
        clock = parse_spec("00:00", time.time())
        self.assertEqual(clock.kind, "time")
        self.assertLessEqual(clock.value, time.time())
        with self.assertRaises(ValueError):
            parse_spec("sometime after lunch")


class StoreTests(Base):
    def test_snapshot_only_on_change(self):
        sh = self.shadow()
        self.write("a.txt", "v1")
        self.assertIsNotNone(sh.snapshot())
        self.assertIsNone(sh.snapshot())
        self.write("a.txt", "v2")
        self.assertIsNotNone(sh.snapshot())
        self.assertEqual(len(sh.snapshots()), 2)

    def test_empty_folder_no_snapshot(self):
        sh = self.shadow()
        self.assertIsNone(sh.snapshot())

    def test_does_not_touch_real_git(self):
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
        self.write("a.txt", "v1")
        sh = self.shadow()
        sh.snapshot()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.proj,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status.strip(), "?? a.txt")  # untouched, not staged
        self.assertFalse((self.proj / ".regitignore").exists())
        # the real .git is not snapshotted
        files = sh.out("ls-tree", "-r", "--name-only", sh.head()).split()
        self.assertEqual(files, ["a.txt"])

    def test_honours_gitignore_and_defaults(self):
        self.write(".gitignore", "secret.txt\n")
        self.write("secret.txt", "x")
        self.write("node_modules/x.js", "x")
        self.write("keep.txt", "x")
        self.write(".regitignore", "*.tmp\n")
        self.write("junk.tmp", "x")
        sh = self.shadow()
        sh.snapshot()
        files = set(sh.out("ls-tree", "-r", "--name-only", sh.head()).split())
        self.assertEqual(files, {".gitignore", ".regitignore", "keep.txt"})

    def test_restore_by_relative_time(self):
        sh = self.shadow()
        now = time.time()
        f = self.write("app.py", "good code")
        sh.snapshot(now=now - 20 * 60)
        self.write("app.py", "slightly worse")
        sh.snapshot(now=now - 10 * 60)
        self.write("app.py", "EVERYTHING BROKEN")

        snap = sh.resolve("14m", "app.py", now=now)
        safety = sh.restore(snap, ["app.py"])
        self.assertEqual(f.read_text(), "good code")
        # the broken state is kept, so the restore can be undone
        self.assertIsNotNone(safety)
        sh.restore(safety, ["app.py"])
        self.assertEqual(f.read_text(), "EVERYTHING BROKEN")

    def test_restore_only_touches_requested_file(self):
        sh = self.shadow()
        a = self.write("a.txt", "a1")
        b = self.write("b.txt", "b1")
        sh.snapshot()
        a.write_text("a2")
        b.write_text("b2")
        snap = sh.snapshots()[0]
        sh.restore(snap, ["a.txt"])
        self.assertEqual(a.read_text(), "a1")
        self.assertEqual(b.read_text(), "b2")

    def test_restore_deleted_file(self):
        sh = self.shadow()
        f = self.write("gone.txt", "still here")
        sh.snapshot()
        f.unlink()
        snap = sh.resolve(None, "gone.txt")
        sh.restore(snap, ["gone.txt"])
        self.assertEqual(f.read_text(), "still here")

    def test_default_spec_is_previous_different_version(self):
        sh = self.shadow()
        self.write("a.txt", "v1")
        self.write("other.txt", "o1")
        sh.snapshot()
        self.write("a.txt", "v2")
        sh.snapshot()
        self.write("other.txt", "o2")  # unrelated change -> extra snapshot
        sh.snapshot()
        snap = sh.resolve(None, "a.txt")
        self.assertEqual(sh.out("show", f"{snap.sha}:a.txt"), "v1")

    def test_restore_missing_in_snapshot_errors(self):
        sh = self.shadow()
        self.write("a.txt", "x")
        snap = sh.snapshot()
        self.write("new.txt", "y")
        with self.assertRaises(RegitError):
            sh.restore(snap, ["new.txt"])

    def test_index_spec_scoped_to_file(self):
        sh = self.shadow()
        self.write("a.txt", "1")
        sh.snapshot()
        self.write("b.txt", "x")
        sh.snapshot()
        self.write("a.txt", "2")
        sh.snapshot()
        self.assertEqual(len(sh.snapshots("a.txt")), 2)
        snap = sh.resolve("@2", "a.txt")
        self.assertEqual(sh.out("show", f"{snap.sha}:a.txt"), "1")

    def test_prune(self):
        sh = self.shadow()
        now = time.time()
        for i, age in enumerate([30, 20, 10, 1]):
            self.write("a.txt", f"v{i}")
            sh.snapshot(now=now - age * 86400)
        removed = sh.prune(15 * 86400, now=now)
        self.assertEqual(removed, 2)
        snaps = sh.snapshots()
        self.assertEqual(len(snaps), 2)
        self.assertEqual(sh.out("show", f"{snaps[0].sha}:a.txt"), "v3")
        self.assertEqual(sh.out("show", f"{snaps[1].sha}:a.txt"), "v2")
        # timestamps survive the rewrite
        self.assertAlmostEqual(snaps[1].ts, now - 10 * 86400, delta=1)

    def test_prune_keeps_newest_even_if_old(self):
        sh = self.shadow()
        self.write("a.txt", "x")
        sh.snapshot(now=time.time() - 100 * 86400)
        self.assertEqual(sh.prune(86400), 0)
        self.assertEqual(len(sh.snapshots()), 1)

    def test_find_project_deepest(self):
        inner = self.proj / "sub"
        inner.mkdir()
        register(self.proj, self.home)
        register(inner, self.home)
        self.assertEqual(find_project(inner / "x.py", self.home), inner)
        self.assertEqual(find_project(self.proj / "y.py", self.home), self.proj)
        self.assertIsNone(find_project(self.proj.parent, self.home))

    def test_path_outside_project(self):
        sh = self.shadow()
        with self.assertRaises(RegitError):
            sh.relpath(self.proj.parent / "x")


class CliTests(Base):
    def test_full_flow(self):
        f = self.write("conf.yml", "port: 8080\n")
        code, out = self.run_cli("watch", ".")
        self.assertEqual(code, 0)
        self.assertIn("first snapshot", Shadow(self.proj).snapshots()[0].message)
        self.assertEqual(load_projects(), [self.proj.resolve()])

        f.write_text("port: 666\n")
        code, out = self.run_cli("snap", "-m", "experiment")
        self.assertIn("experiment", out)

        code, out = self.run_cli("log", "conf.yml")
        self.assertIn("@1", out)
        self.assertIn("@2", out)
        self.assertIn("<- current", out)

        f.write_text("totally broken\n")
        code, out = self.run_cli("restore", "conf.yml", "--at", "@2", "-y")
        self.assertEqual(code, 0, out)
        # @N is resolved against the log as shown, before the safety snapshot
        self.assertEqual(f.read_text(), "port: 8080\n")

        code, out = self.run_cli("status")
        self.assertIn(str(self.proj.resolve()), out)

    def test_restore_previous_by_default(self):
        f = self.write("a.txt", "good\n")
        self.run_cli("watch")
        f.write_text("bad\n")
        code, _ = self.run_cli("restore", "a.txt", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(f.read_text(), "good\n")

    def test_bad_at_spec_is_a_clean_error(self):
        self.write("a.txt", "x")
        self.run_cli("watch")
        code, _ = self.run_cli("diff", "a.txt", "--at", "yesterday-ish")
        self.assertEqual(code, 2)

    def test_auto_message_is_english(self):
        sh = self.shadow()
        self.write("a.txt", "x")
        self.write("b.txt", "y")
        self.assertEqual(sh.snapshot().message, "auto: 2 files: a.txt, b.txt")

    def test_unwatched_folder_errors(self):
        code, _ = self.run_cli("log")
        self.assertEqual(code, 2)

    def test_unwatch_purge(self):
        self.write("a.txt", "x")
        self.run_cli("watch")
        sh = Shadow(self.proj)
        self.assertTrue(sh.exists)
        self.run_cli("unwatch", "--purge")
        self.assertFalse(sh.exists)
        self.assertEqual(load_projects(), [])


class DaemonTests(Base):
    def test_once(self):
        self.write("a.txt", "x")
        register(self.proj.resolve(), self.home)  # daemon must init the repo itself
        code = daemon.run(interval=1, keep=None, once=True, home=self.home)
        self.assertEqual(code, 0)
        self.assertEqual(len(Shadow(self.proj, self.home).snapshots()), 1)
        self.assertIsNone(daemon.running_pid(self.home))


if __name__ == "__main__":
    unittest.main()


class GuiTests(Base):
    def setUp(self):
        super().setUp()
        import json
        import threading
        import urllib.request
        from regit import gui
        self.json, self.urllib = json, urllib.request
        self.write("a.txt", "v1\n")
        cli.main(["watch", "."])
        self.write("a.txt", "v2 broken\n")
        self.info = {}
        ready = threading.Event()
        threading.Thread(target=gui.serve, kwargs=dict(open_browser=False, ready=ready, info=self.info),
                         daemon=True).start()
        ready.wait(5)

    def tearDown(self):
        self.info["server"].shutdown()
        super().tearDown()

    def call(self, path, body=None, token=None):
        req = self.urllib.Request(
            f"http://127.0.0.1:{self.info['port']}{path}",
            data=None if body is None else self.json.dumps(body).encode(),
            headers={"X-Regit-Token": self.info["token"] if token is None else token},
        )
        try:
            with self.urllib.urlopen(req) as r:
                return r.status, self.json.loads(r.read())
        except self.urllib.HTTPError as e:
            return e.code, self.json.loads(e.read())

    def test_requires_token(self):
        code, _ = self.call("/api/projects", token="nope")
        self.assertEqual(code, 403)

    def test_browse_and_restore(self):
        proj = str(self.proj.resolve())
        code, j = self.call("/api/projects")
        self.assertEqual(code, 200)
        self.assertEqual(j["projects"][0]["path"], proj)
        code, j = self.call(f"/api/files?project={proj}")
        self.assertIn("a.txt", [f["path"] for f in j["files"]])
        code, j = self.call(f"/api/versions?project={proj}&path=a.txt")
        old = [v for v in j["versions"] if not v["current"]][0]
        self.assertEqual(old["message"], "first snapshot")
        code, j = self.call(f"/api/diff?project={proj}&path=a.txt&sha={old['sha']}")
        self.assertIn("+v1", j["diff"])
        self.assertEqual(j["content"], "v1\n")
        code, j = self.call("/api/restore", {"project": proj, "path": "a.txt", "sha": old["sha"]})
        self.assertEqual(code, 200, j)
        self.assertEqual((self.proj / "a.txt").read_text(), "v1\n")
        # the version list must now mark the restored version as "= now"
        code, j = self.call(f"/api/versions?project={proj}&path=a.txt")
        self.assertEqual(j["versions"][0]["current"], True)
        self.assertEqual(j["versions"][0]["message"].startswith("auto"), True)

    def test_unknown_project_rejected(self):
        code, j = self.call("/api/files?project=/etc")
        self.assertEqual(code, 400)
