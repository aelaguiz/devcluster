"""The nightly disk cleanup and its keep list.

The agent here is a shell script that does what the real one would: reads its brief, removes
folders through `devcluster cleanup rm`, and writes summary.json. Nothing outside the fake
machines' folders is ever a removal target; the guards that cover the rest of the disk are
checked without removing anything.
"""

import datetime
import fcntl
import json
import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from devcluster import config as configs
from devcluster.cleanup import Guards, report
from tests.fakes import ClusterTest, git, wrap_git

MACHINES = {
    "laptop": 'mode = "on"\ndescription = "The owner\'s laptop"\n',
    "server": 'mode = "on"\n',
}


def at(hour, minute=0):
    """A local time today."""
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))


def day(offset=0):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


def agent_script(body="", summary=None):
    summary = summary if summary is not None else {"status": "ok", "said": "cleared the dead checkouts"}
    write = f"cat > \"$DEVCLUSTER_CLEANUP_RUN/summary.json\" <<'EOF'\n{json.dumps(summary)}\nEOF\n" if summary else ""
    return f'#!/bin/sh\ncat > "$DEVCLUSTER_CLEANUP_RUN/brief-read.md"\necho "$DEVCLUSTER_MACHINE" > "$DEVCLUSTER_CLEANUP_RUN/machine.txt"\n{body}\n{write}'


class CleanupTest(ClusterTest):
    def setUp(self):
        super().setUp()
        self.notes = {"files/cleanup/cluster.md": "Every machine: caches regenerate.\n", "files/cleanup/laptop.md": "The laptop: Time Machine.\n"}

    def cluster(self, cleanup=None, keep=None, cluster=""):
        cleanup = cleanup if cleanup is not None else textwrap.dedent("""\
            command = '"$HOME/agent"'
            notes = "files/cleanup/cluster.md"
            [machines.laptop]
            notes = "files/cleanup/laptop.md"
            """)
        files = dict(self.notes)
        if cleanup:
            files["cleanup.toml"] = cleanup
        if keep is not None:
            files["keep.toml"] = keep
        self.world.config(cluster, MACHINES, files)
        self.laptop = self.world.machine("laptop")
        return self.laptop

    def agent(self, machine, body="", summary=None):
        path = machine.path("agent")
        path.write_text(agent_script(body, summary))
        path.chmod(0o755)

    def latest(self, machine):
        return Path(os.path.realpath(machine.dc / "state" / "cleanup" / "latest"))

    def folder(self, machine, rel, files=("a.bin",)):
        path = machine.path(rel)
        path.mkdir(parents=True)
        for name in files:
            (path / name).write_text("x" * 5000)
        return path

    def keep_entry(self, machine, path, until, who="the graders", why="still reading it"):
        return f'[[keep]]\nmachine = "{machine}"\npath = "{path}"\nwho = "{who}"\nwhy = "{why}"\nuntil = "{until}"\n\n'


class Run(CleanupTest):
    def test_the_agent_removes_dead_folders_and_kept_ones_stay(self):
        laptop = self.cluster()
        dead = self.folder(laptop, "workspace/task-1")
        kept = self.folder(laptop, "workspace/results")
        self.world.commit("config", {"keep.toml": self.keep_entry("laptop", os.path.realpath(kept), day(7))})
        laptop.sync()
        self.agent(laptop, f'devcluster cleanup rm "{dead}" "{kept}" > "$DEVCLUSTER_CLEANUP_RUN/rm.txt"')
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        last = result.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("freed "), last)
        self.assertIn(": cleared the dead checkouts", last)
        self.assertFalse(dead.exists())
        self.assertTrue(kept.exists())
        run = self.latest(laptop)
        removed = [json.loads(line) for line in (run / "removed.jsonl").read_text().splitlines()]
        self.assertEqual([r["path"] for r in removed], [os.path.realpath(dead)])
        self.assertGreater(removed[0]["bytes"], 0)
        self.assertIn(f"refused, it's kept ({os.path.realpath(kept)})", (run / "rm.txt").read_text())
        self.assertEqual((run / "machine.txt").read_text().strip(), "laptop")
        record = json.loads((run / "result.json").read_text())
        self.assertEqual(record["status"], "ok")
        self.assertEqual([v["path"] for v in record["volumes"]], ["/"])
        self.assertIsInstance(record["volumes"][0]["after_free"], int)

    def test_the_brief_says_what_the_run_needs(self):
        laptop = self.cluster()
        kept = self.folder(laptop, "data/line-b")
        self.world.commit("config", {"keep.toml": self.keep_entry("laptop", os.path.realpath(kept), day(3)) + self.keep_entry("laptop", "/gone/old", day(-1)) + self.keep_entry("server", "/srv/x", day(3))})
        laptop.sync()
        self.agent(laptop)
        laptop.run("cleanup")
        run = self.latest(laptop)
        brief = (run / "brief-read.md").read_text()
        self.assertEqual(brief, (run / "brief.md").read_text())
        self.assertIn("# Disk cleanup on laptop", brief)
        self.assertIn("The owner's laptop", brief)
        self.assertIn(f"Read the skill first: {laptop.dc / 'engine/skills/disk-cleanup/SKILL.md'}", brief)
        config = laptop.dc / "config"
        self.assertLess(brief.index(str(config / "files/cleanup/cluster.md")), brief.index(str(config / "files/cleanup/laptop.md")))
        self.assertIn(f"{os.path.realpath(kept)} until {day(3)}: the graders; still reading it", brief)
        self.assertNotIn("/gone/old", brief)
        self.assertNotIn("/srv/x", brief)
        self.assertIn(f"Run folder: {run}", brief)
        self.assertIn("Previous run: none", brief)
        self.assertIn("devcluster cleanup rm", brief)
        self.assertIn("devcluster keep add", brief)
        self.assertIn("summary.json", brief)
        self.assertNotIn("dry run", brief.lower())
        self.assertEqual((run / "keep.txt").read_text().split(), [os.path.realpath(kept)])
        laptop.run("cleanup")
        self.assertIn(f"Previous run: {run}", (self.latest(laptop) / "brief.md").read_text())

    def test_a_dry_run_removes_nothing_and_says_what_would_go(self):
        laptop = self.cluster()
        dead = self.folder(laptop, "workspace/task-1")
        self.agent(laptop, f'devcluster cleanup rm "{dead}"')
        result = laptop.run("cleanup", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(dead.exists())
        last = result.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith("dry run: would remove 1 folder, "), last)
        run = self.latest(laptop)
        self.assertIn("This is a dry run. Change nothing.", (run / "brief.md").read_text())
        would = [json.loads(line) for line in (run / "would-remove.jsonl").read_text().splitlines()]
        self.assertEqual(would[0]["path"], os.path.realpath(dead))
        self.assertFalse((run / "removed.jsonl").exists())

    def test_a_missing_summary_fails_the_run(self):
        laptop = self.cluster()
        self.agent(laptop, "exit 3", summary={})
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed: the agent wrote no summary.json (the agent exited 3)", result.stdout)

    def test_a_summary_with_no_known_status_fails_the_run(self):
        laptop = self.cluster()
        self.agent(laptop, summary={"status": "great", "said": "all good"})
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("summary.json's status is 'great'", result.stdout)

    def test_the_agent_saying_it_failed_fails_the_run(self):
        laptop = self.cluster()
        self.agent(laptop, summary={"status": "failed", "said": "the snapshot came back empty"})
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.strip().startswith("failed: the snapshot came back empty; freed"), result.stdout)

    def test_a_warning_is_reported_and_the_run_counts(self):
        laptop = self.cluster()
        self.agent(laptop, summary={"status": "warning", "said": "the VM didn't answer, so its disk wasn't checked"})
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 0)
        self.assertIn("; warning: the VM didn't answer, so its disk wasn't checked", result.stdout)

    def test_an_agent_that_exits_with_an_error_is_a_warning(self):
        laptop = self.cluster()
        body = """cat > "$DEVCLUSTER_CLEANUP_RUN/summary.json" <<'EOF'\n{"status": "ok", "said": "cleared the dead checkouts"}\nEOF\nexit 7"""
        self.agent(laptop, body=body, summary={})
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 0)
        self.assertIn("warning: the agent exited 7 after writing its summary; cleared the dead checkouts", result.stdout)

    def test_a_volume_still_under_its_floor_is_a_warning(self):
        laptop = self.cluster(cleanup='command = \'"$HOME/agent"\'\n[machines.laptop]\nfloors = { "/" = "100%" }\n')
        self.agent(laptop)
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 0)
        self.assertIn("warning: cleared the dead checkouts; / is still under its 100% floor with ", result.stdout)
        self.assertEqual(json.loads((self.latest(laptop) / "result.json").read_text())["status"], "warning")

    def test_a_floor_that_is_met_is_no_warning(self):
        laptop = self.cluster(cleanup='command = \'"$HOME/agent"\'\n[machines.laptop]\nfloors = { "/" = "1K" }\n')
        self.agent(laptop)
        result = laptop.run("cleanup")
        self.assertNotIn("warning", result.stdout)
        self.assertIn("freed ", result.stdout)

    def test_a_missing_drive_is_skipped_and_named(self):
        laptop = self.cluster(cleanup='command = \'"$HOME/agent"\'\n[machines.laptop]\nfloors = { "/" = "1K", "/devcluster-no-such-drive" = "10%" }\n')
        self.agent(laptop)
        result = laptop.run("cleanup")
        self.assertIn("/devcluster-no-such-drive isn't mounted, so it wasn't cleaned", result.stdout)
        self.assertIn("/devcluster-no-such-drive: not mounted", (self.latest(laptop) / "brief.md").read_text())

    def test_a_run_past_its_time_is_stopped_with_its_agent(self):
        laptop = self.cluster(cleanup='command = \'"$HOME/agent"\'\ntimeout = 2\n')
        self.agent(laptop, 'sleep 60 &\necho $! > "$DEVCLUSTER_CLEANUP_RUN/child.pid"\nwait', summary={})
        began = time.time()
        result = laptop.run("cleanup")
        self.assertLess(time.time() - began, 40)
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed: stopped after 2s, before it wrote its summary", result.stdout)
        child = int((self.latest(laptop) / "child.pid").read_text())
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_no_command_means_no_run(self):
        laptop = self.cluster(cleanup="")
        result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cleanup.toml names no command", result.stdout)

    def test_a_second_run_waits_for_the_first(self):
        laptop = self.cluster()
        self.agent(laptop)
        folder = laptop.dc / "state" / "cleanup"
        folder.mkdir(parents=True)
        with open(folder / "run.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.write("4242\n")
            held.flush()
            result = laptop.run("cleanup")
        self.assertEqual(result.returncode, 0)
        self.assertIn("didn't start: another cleanup run is going on this machine (process 4242)", result.stdout)
        self.assertFalse((folder / "runs").exists() and any((folder / "runs").iterdir()))

    def test_old_run_folders_are_removed(self):
        laptop = self.cluster(cleanup='command = \'"$HOME/agent"\'\nkeep_days = 14\n')
        self.agent(laptop)
        runs = laptop.dc / "state" / "cleanup" / "runs"
        old, recent = runs / "20200101T000000Z", runs / "20200102T000000Z"
        for path, age in ((old, 20), (recent, 3)):
            path.mkdir(parents=True)
            stamp = time.time() - age * 86400
            os.utime(path, (stamp, stamp))
        laptop.run("cleanup")
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(self.latest(laptop).exists())

    def test_the_nightly_item_shows_the_runs_last_line(self):
        laptop = self.cluster(cluster='[[nightly]]\nname = "disk-cleanup"\nrun = \'"$HOME/bin/devcluster" cleanup\'\ntimeout = 6000\n')
        self.agent(laptop)
        laptop.sync(now=at(2))
        done = laptop.dc / "state" / "nightly" / "disk-cleanup" / "exit"
        for _ in range(200):
            if done.exists():
                break
            time.sleep(0.1)
        laptop.sync(now=at(2, 5))
        item = self.assertState(laptop, "disk-cleanup", "ok", ": freed ")
        self.assertIn("cleared the dead checkouts", item["summary"])


class Remove(CleanupTest):
    def test_outside_a_run_every_guard_still_holds(self):
        laptop = self.cluster()
        kept = self.folder(laptop, "workspace/results")
        plain = self.folder(laptop, "workspace/old-task")
        self.world.commit("config", {"keep.toml": self.keep_entry("laptop", os.path.realpath(kept), day(1))})
        laptop.sync()
        result = laptop.run("cleanup", "rm", str(kept), str(laptop.path("workspace")), str(laptop.path("nothing-here")), str(plain))
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"refused, it's kept ({os.path.realpath(kept)}): {kept}", result.stdout)
        self.assertIn("refused, it's a home folder, a workspace root or a place task folders live, not a task folder", result.stdout)
        self.assertIn(f"refused, it doesn't exist: {laptop.path('nothing-here')}", result.stdout)
        self.assertIn(f"removed {os.path.realpath(plain)} (", result.stdout)
        self.assertTrue(kept.exists())
        self.assertFalse(plain.exists())

    def test_an_expired_entry_keeps_nothing(self):
        laptop = self.cluster()
        old = self.folder(laptop, "workspace/finished")
        self.world.commit("config", {"keep.toml": self.keep_entry("laptop", os.path.realpath(old), day(-1))})
        laptop.sync()
        result = laptop.run("cleanup", "rm", str(old))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(old.exists())

    def test_the_dry_run_flag_before_rm_removes_nothing(self):
        laptop = self.cluster()
        plain = self.folder(laptop, "workspace/old-task")
        result = laptop.run("cleanup", "--dry-run", "rm", str(plain))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("would remove", result.stdout)
        self.assertTrue(plain.exists())

    def test_a_link_is_refused_and_what_it_points_to_stays(self):
        laptop = self.cluster()
        store = self.folder(laptop, "store")
        link = laptop.path("workspace/policies")
        link.parent.mkdir(parents=True)
        link.symlink_to(store)
        result = laptop.run("cleanup", "rm", str(link), str(link) + "/")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("refused, it's a link"), 2)
        self.assertTrue((store / "a.bin").exists())


class Guarding(unittest.TestCase):
    """The guards on their own, with nothing removed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="devcluster-guards-")
        root = Path(os.path.realpath(self._tmp.name))
        self.home = root / "home"
        self.data = root / "data"
        for path in ("home/workspace/task", "home/workspace/results/round-1", "data/runs/a", "data/mounted/inside", "outside/x"):
            (root / path).mkdir(parents=True)
        (self.home / "link").symlink_to(root / "outside")
        self.root = root
        self.guards = Guards(str(self.home), [str(self.home), str(self.data)],
                             [str(self.home / "workspace/results")], {str(self.data / "mounted"), "/"})

    def tearDown(self):
        self._tmp.cleanup()

    def refusal(self, rel):
        return self.guards.refusal(str(self.root / rel))

    def test_a_dead_task_folder_passes(self):
        self.assertIsNone(self.refusal("home/workspace/task"))
        self.assertIsNone(self.refusal("data/runs/a"))

    def test_kept_folders_their_parents_and_what_is_inside_them_are_refused(self):
        self.assertTrue(self.refusal("home/workspace/results").startswith("it's kept"))
        self.assertTrue(self.refusal("home/workspace/results/round-1").startswith("it's inside something kept"))
        self.assertTrue(self.refusal("home/workspace").startswith("it's a home folder"))
        self.guards.never.discard(str(self.home / "workspace"))
        self.assertTrue(self.refusal("home/workspace").startswith("it holds something kept"))

    def test_mounts_and_what_holds_them_are_refused(self):
        self.assertEqual(self.refusal("data/mounted"), "it's a mount")
        self.assertEqual(self.refusal("data"), "it's a home folder, a workspace root or a place task folders live, not a task folder")
        self.guards.places.append(str(self.root))
        self.guards.never.discard(str(self.data))
        self.assertEqual(self.refusal("data"), "it holds a mount")

    def test_a_path_through_a_link_is_refused(self):
        self.assertIn("link", self.refusal("home/link/.") or "")
        self.assertTrue(self.refusal("home/link/x").startswith("it goes through the link"), self.refusal("home/link/x"))
        self.assertTrue(self.refusal("home/workspace/task/../task").startswith("it has '..' in it"))

    def test_other_capitals_name_the_same_folder(self):
        if not os.path.exists(str(self.root / "home/WORKSPACE")):
            self.skipTest("this disk tells capitals apart, so other capitals name another folder")
        self.assertTrue(self.refusal("home/WORKSPACE").startswith("it's a home folder"), self.refusal("home/WORKSPACE"))
        self.assertTrue(self.refusal("home/workspace/RESULTS").startswith("it's kept"), self.refusal("home/workspace/RESULTS"))
        self.assertTrue(self.refusal("home/Workspace/Results/round-1").startswith("it's inside something kept"))

    def test_home_root_links_and_folders_outside_task_places_are_refused(self):
        self.assertTrue(self.refusal("home").startswith("it's a home folder"))
        self.assertTrue(self.guards.refusal("/").startswith("it's a home folder"))
        self.assertTrue(self.refusal("home/link").startswith("it's a link"))
        self.assertEqual(self.refusal("outside/x"), "it's outside the places task folders live")
        self.assertEqual(self.refusal("home/nothing"), "it doesn't exist")


class Report(unittest.TestCase):
    def test_lines(self):
        gib = 2 ** 30
        volumes = [{"path": "/", "floor": "150G", "before_free": 52 * gib, "after_free": 281 * gib, "total": 900 * gib},
                   {"path": "/data", "floor": "10%", "before_free": 300 * gib, "after_free": 595 * gib, "total": 983 * gib}]
        line, status = report(volumes, {"status": "ok", "said": "cleared 137 dead folders"}, None, False, (0, 0))
        self.assertEqual(line, "freed 524 GiB (/ 229 GiB, /data 295 GiB): cleared 137 dead folders")
        self.assertEqual(status, "ok")
        volumes[0]["after_free"] = 100 * gib
        line, status = report(volumes, {"status": "ok", "said": "done"}, None, False, (0, 0))
        self.assertEqual(status, "warning")
        self.assertIn("/ is still under its 150G floor with 100 GiB free", line)


class KeepList(CleanupTest):
    def setUp(self):
        super().setUp()
        self.cluster()
        self.server = self.world.machine("server")
        self.results = self.folder(self.laptop, "workspace/results")

    def origin_keep(self):
        checkout = self.world.work / "config"
        git(checkout, "pull", "--quiet", "--ff-only")
        path = checkout / "keep.toml"
        return configs.read_keep(path, {"laptop", "server"}, []) if path.exists() else []

    def add(self, path, until, why="still reading it", machine=None, **extra):
        args = ["keep", "add", str(path), "--until", until, "--who", "the graders", "--why", why]
        if machine:
            args += ["--machine", machine]
        return self.laptop.run(*args, **extra)

    def test_add_reaches_every_machine(self):
        result = self.add(self.results, day(7))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"kept {os.path.realpath(self.results)} on laptop until {day(7)}", result.stdout)
        entries = self.origin_keep()
        self.assertEqual([(e["machine"], e["path"], e["until"].isoformat()) for e in entries], [("laptop", os.path.realpath(self.results), day(7))])
        self.assertEqual(self.laptop.git(".devcluster/config", "status", "--porcelain"), "")
        self.server.sync()
        listed = self.server.run("keep", "list", "--all")
        self.assertIn(os.path.realpath(self.results), listed.stdout)
        self.assertIn("the graders: still reading it", listed.stdout)

    def test_adding_again_renews_and_expired_entries_drop_out(self):
        self.world.commit("config", {"keep.toml": self.keep_entry("server", "/srv/old", day(-2))})
        self.laptop.sync()
        self.add(self.results, day(3))
        result = self.add(self.results, day(10), why="the reruns need it too")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entries = self.origin_keep()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["until"].isoformat(), day(10))
        self.assertEqual(entries[0]["why"], "the reruns need it too")

    def test_a_dry_run_writes_nothing_and_records_the_keep(self):
        run_dir = self.folder(self.laptop, "dry-run")
        result = self.add(self.results, day(7), DEVCLUSTER_CLEANUP_DRY_RUN="1", DEVCLUSTER_CLEANUP_RUN=str(run_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dry run, nothing written", result.stdout)
        self.assertEqual(self.origin_keep(), [])
        self.assertIn(os.path.realpath(self.results), (run_dir / "would-keep.jsonl").read_text())

    def test_remove(self):
        self.add(self.results, day(3))
        result = self.laptop.run("keep", "remove", str(self.results))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.origin_keep(), [])
        again = self.laptop.run("keep", "remove", str(self.results))
        self.assertEqual(again.returncode, 1)
        self.assertIn("isn't on the keep list", again.stderr)

    def test_remove_works_after_the_folder_is_gone(self):
        self.add(self.results, day(3))
        for path in self.results.iterdir():
            path.unlink()
        self.results.rmdir()
        result = self.laptop.run("keep", "remove", str(self.results))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.origin_keep(), [])

    def test_another_machine_and_a_path_that_only_exists_there(self):
        result = self.add("/srv/runs/line-b", day(5), machine="server")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.origin_keep()[0]["machine"], "server")
        missing = self.add(self.laptop.path("not-here"), day(5))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("doesn't exist on this machine", missing.stderr)

    def test_a_date_that_has_passed_or_a_bad_one_is_refused(self):
        self.assertIn("has passed", self.add(self.results, day(-1)).stderr)
        self.assertIn("isn't one", self.add(self.results, "next week").stderr)
        self.assertEqual(self.origin_keep(), [])

    def test_when_another_machine_pushes_first_both_changes_land(self):
        admin = self.world.work / "config"
        real_push = f'(cd "{admin}" && "$real" pull --quiet --ff-only && echo other > other.txt && "$real" add other.txt && "$real" commit --quiet -m Other && "$real" push --quiet origin HEAD)'
        path = wrap_git(self.laptop, '*" push "*', before=real_push)
        result = self.add(self.results, day(4), PATH=path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.laptop.path("wrapped-fired").exists())
        self.assertEqual(len(self.origin_keep()), 1)
        self.assertTrue((admin / "other.txt").exists())
        log = git(admin, "log", "--format=%s", "-3")
        self.assertEqual(log.splitlines()[:2], [f"Keep {os.path.realpath(self.results)} on laptop until {day(4)}: still reading it", "Other"])
        self.assertEqual(self.laptop.git(".devcluster/config", "rev-list", "--count", "HEAD", "--not", "--remotes"), "0")

    def test_a_push_that_fails_undoes_its_commit(self):
        self.laptop.git(".devcluster/config", "remote", "set-url", "--push", "origin", str(self.world.root / "no-such-remote.git"))
        result = self.add(self.results, day(4))
        self.assertEqual(result.returncode, 1)
        self.assertIn("couldn't push the keep list", result.stderr)
        self.assertEqual(self.laptop.git(".devcluster/config", "rev-list", "--count", "HEAD", "--not", "--remotes"), "0")
        self.assertFalse((self.laptop.dc / "config" / "keep.toml").exists())

    def test_a_commit_that_fails_leaves_nothing_staged(self):
        hook = self.laptop.dc / "config" / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.add(self.results, day(4))
        self.assertEqual(result.returncode, 1)
        self.assertIn("couldn't commit the keep list", result.stderr)
        self.assertEqual(self.laptop.git(".devcluster/config", "status", "--porcelain"), "")
        hook.unlink()
        again = self.add(self.results, day(4))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def test_uncommitted_changes_to_the_list_here_stop_a_write(self):
        (self.laptop.dc / "config" / "keep.toml").write_text("# someone's edit\n")
        result = self.add(self.results, day(4))
        self.assertEqual(result.returncode, 1)
        self.assertIn("has changes nobody committed", result.stderr)


class CheckConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="devcluster-config-")
        self.root = Path(self._tmp.name)
        (self.root / "machines").mkdir()
        (self.root / "machines" / "laptop.toml").write_text("")
        (self.root / "cluster.toml").write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def errors(self, **files):
        for name, text in files.items():
            (self.root / name.replace("_", ".")).write_text(textwrap.dedent(text))
        try:
            configs.load(self.root)
        except configs.ConfigError as error:
            return error.errors
        return []

    def test_a_good_keep_list_and_cleanup_file_load(self):
        (self.root / "files").mkdir()
        (self.root / "files" / "notes.md").write_text("notes\n")
        self.assertEqual(self.errors(
            keep_toml=f'[[keep]]\nmachine = "laptop"\npath = "/data/x"\nwho = "a"\nwhy = "b"\nuntil = "{day(-30)}"\n',
            cleanup_toml='command = "agent"\nnotes = "files/notes.md"\n[machines.laptop]\nnotes = "files/notes.md"\nfloors = { "/" = "150G", "/data" = "10%" }\n',
        ), [])
        config = configs.load(self.root)
        self.assertEqual(config.keep[0]["until"].isoformat(), day(-30))
        self.assertEqual(config.cleanup_machines["laptop"]["floors"]["/"], "150G")

    def test_keep_list_problems(self):
        errors = self.errors(keep_toml="""\
            [[keep]]
            machine = "desktop"
            path = "data/x"
            who = "a"
            why = "b"
            until = "soon"

            [[keep]]
            machine = "laptop"
            path = "/data/y"
            why = "b"
            until = "2026-01-01"
            colour = "red"
            """)
        text = "\n".join(errors)
        self.assertIn("machine 'desktop' has no machines/desktop.toml", text)
        self.assertIn("path must be a full path", text)
        self.assertIn("'soon' isn't one", text)
        self.assertIn("/data/y: unknown field 'colour'", text)
        self.assertIn("/data/y: missing 'who'", text)

    def test_cleanup_file_problems(self):
        errors = self.errors(cleanup_toml="""\
            command = "agent"
            notes = "files/missing.md"
            colour = "red"
            [machines.desktop]
            [machines.laptop]
            floors = { "/" = "lots", "data" = "10%" }
            """)
        text = "\n".join(errors)
        self.assertIn("cleanup.toml: notes files/missing.md isn't in the config repo", text)
        self.assertIn("cleanup.toml: unknown setting 'colour'", text)
        self.assertIn("machines.desktop: there is no machines/desktop.toml", text)
        self.assertIn("the floor for / is a size", text)
        self.assertIn("'data' isn't one", text)


if __name__ == "__main__":
    unittest.main()
