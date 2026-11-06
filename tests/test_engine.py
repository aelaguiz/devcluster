"""The config, devcluster updating itself, one run at a time, the timer files, and status."""

import fcntl
import plistlib
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from devcluster import config as configs
from devcluster.install import launchd_plist, systemd_units
from devcluster.paths import Layout
from devcluster.status import render
from tests.fakes import ROOT, ClusterTest, git


def write_config(root, cluster, machines, files=None):
    root = Path(root)
    (root / "machines").mkdir(parents=True)
    (root / "cluster.toml").write_text(textwrap.dedent(cluster))
    for name, text in machines.items():
        (root / "machines" / f"{name}.toml").write_text(textwrap.dedent(text))
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


class ConfigChecks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_applies_on_skip_and_replacement(self):
        write_config(self.root, """\
            [[repo]]
            path = "~/w/notes"
            url = "git@github.com:me/notes.git"

            [[tool]]
            name = "skills"
            path = "~/w/skills"
            url = "git@github.com:me/skills.git"
            install = "make install"

            [[cli]]
            name = "codex"
            version = "1.0"
            check = "codex --version"
            install = "npm i -g codex@{version}"
            on = ["laptop"]
            """, {
            "laptop": """\
                mode = "on"
                [[repo]]
                name = "skills"
                path = "~/w/skills"
                url = "git@github.com:me/skills.git"
                install = "make install"
                """,
            "server": 'skip = ["notes"]',
        })
        loaded = configs.load(self.root)
        laptop = loaded.plan("laptop")
        self.assertEqual([(i.kind, i.name) for i in laptop], [("repo", "notes"), ("repo", "skills"), ("cli", "codex")])
        self.assertEqual([(i.kind, i.name) for i in loaded.plan("server")], [("tool", "skills")])
        self.assertEqual(loaded.machines["laptop"].mode, "on")
        self.assertEqual(loaded.machines["server"].mode, "dry-run")

    def test_mistakes_are_all_reported(self):
        write_config(self.root, """\
            [devcluster]
            engin = "v1"

            [[repo]]
            path = "~/w/notes"
            urll = "x"
            on = ["nowhere"]

            [[file]]
            name = "rules"
            source = "../outside"
            targets = ["~/x"]
            """, {"laptop": 'mode = "yes"\nskip = ["ghost"]'})
        with self.assertRaises(configs.ConfigError) as caught:
            configs.load(self.root)
        text = "\n".join(caught.exception.errors)
        for expected in ("devcluster.engin", "unknown field 'urll'", "missing 'url'", "source must be a path inside",
                         'mode must be "dry-run" or "on"', "skip names 'ghost'"):
            self.assertIn(expected, text)

    def test_on_must_name_a_machine(self):
        write_config(self.root, '[[repo]]\npath = "~/a"\nurl = "u"\non = ["nowhere"]\n', {"laptop": ""})
        with self.assertRaises(configs.ConfigError) as caught:
            configs.load(self.root)
        self.assertIn("on names 'nowhere'", caught.exception.errors[0])

    def test_duplicate_names(self):
        write_config(self.root, '[[repo]]\npath = "~/a/x"\nurl = "u"\n[[tool]]\npath = "~/b/x"\nurl = "v"\n', {"laptop": ""})
        with self.assertRaises(configs.ConfigError) as caught:
            configs.load(self.root)
        self.assertIn("two items are named 'x'", caught.exception.errors[0])

    def test_example_config_is_valid(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "config"
        loaded = configs.load(example)
        self.assertTrue(loaded.machines)


class TimerFiles(unittest.TestCase):
    def test_launchd_plist(self):
        layout = Layout("/Users/me/.devcluster")
        plist = plistlib.loads(launchd_plist(layout, 120, "/opt/homebrew/bin:/usr/bin:/bin"))
        self.assertEqual(plist["ProgramArguments"], ["/Users/me/.devcluster/engine/bin/devcluster", "sync", "--quiet"])
        self.assertEqual(plist["StartInterval"], 120)
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertTrue(plist["LowPriorityIO"])
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin:/bin")

    def test_systemd_units(self):
        service, timer = systemd_units(Layout("/home/me/.devcluster"), 120, "/usr/bin:/bin")
        self.assertIn("ExecStart=/home/me/.devcluster/engine/bin/devcluster sync --quiet", service)
        self.assertIn("IOSchedulingClass=idle", service)
        self.assertIn('Environment="PATH=/usr/bin:/bin"', service)
        self.assertIn("OnUnitActiveSec=120", timer)
        self.assertIn("WantedBy=timers.target", timer)


class StatusText(unittest.TestCase):
    def test_render(self):
        reports = {
            "laptop": {"finished": 1000, "mode": "on", "items": [
                {"kind": "repo", "name": "notes", "state": "ok", "summary": "up to date"},
                {"kind": "repo", "name": "work", "state": "blocked", "summary": "conflicts in a.txt"},
            ]},
            "server": {"finished": 1000 - 3600, "mode": "dry-run", "items": [
                {"kind": "cli", "name": "codex", "state": "would-change", "summary": "would install 1.0"},
            ]},
        }
        text = render(reports, ["laptop", "server", "studio"], 900, clock=1000)
        self.assertIn("1 need you, 1 OK", text)
        self.assertIn("60 min ago (stale)", text)
        self.assertIn("studio", text)
        self.assertIn("never reported", text)
        self.assertIn("Needs you:\n  laptop  repo work: conflicts in a.txt", text)
        self.assertIn("Would change (dry run):\n  server  cli codex: would install 1.0", text)


class Engine(ClusterTest):
    def test_updates_itself_to_the_version_the_config_names_and_restarts(self):
        engine = self.world.engine()
        source = self.world.work / "engine"
        old = git(source, "rev-parse", "HEAD")
        (source / "src" / "devcluster" / "__init__.py").write_text('"""devcluster."""\n\n__version__ = "9.9.9"\n')
        git(source, "commit", "--quiet", "-am", "New version")
        git(source, "push", "--quiet", "origin", "main")
        new = git(source, "rev-parse", "HEAD")
        self.assertTrue(engine)
        self.world.config(f'[devcluster]\nengine = "{old}"\n', {"laptop": ""})
        laptop = self.world.machine("laptop")
        git(laptop.dc / "engine", "checkout", "--quiet", "--detach", old)
        report = laptop.sync()
        self.assertEqual(report["engine"], old)
        self.world.commit("config", {"cluster.toml": f'[devcluster]\nengine = "{new}"\n'})
        report = laptop.sync()
        self.assertEqual(report["engine"], new)
        self.assertEqual(report["devcluster"], "9.9.9")
        engine_item = laptop.item("engine")
        self.assertEqual(engine_item["state"], "changed")
        self.assertEqual(laptop.item("config")["state"], "changed")
        self.world.commit("config", {"machines/laptop.toml": f'engine = "{old}"\n'})
        self.assertEqual(laptop.sync()["engine"], old, "a machine's own version wins")

    def test_one_run_at_a_time(self):
        self.world.config("", {"laptop": ""})
        laptop = self.world.machine("laptop")
        (laptop.dc / "state").mkdir(exist_ok=True)
        with open(laptop.dc / "state" / "sync.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = laptop.run("sync")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Another devcluster sync is running", result.stdout)
        self.assertFalse((laptop.dc / "state" / "last.json").exists())

    def test_config_error_stops_the_run_and_is_reported(self):
        self.world.config("[[repo]]\npath = 3\n", {"laptop": 'mode = "on"'})
        laptop = self.world.machine("laptop")
        result = laptop.run("sync")
        self.assertEqual(result.returncode, 2)
        report = laptop.report()
        self.assertTrue(report["errors"])
        self.assertEqual(report["mode"], "dry-run")
        status = laptop.run("status")
        self.assertIn("config error", status.stdout)

    def test_machine_missing_from_the_config(self):
        self.world.config("", {"laptop": ""})
        stranger = self.world.machine("stranger")
        self.assertEqual(stranger.run("sync").returncode, 2)
        self.assertIn("no machines/stranger.toml", stranger.report()["errors"][0])

    def test_install_lays_out_the_machine(self):
        self.world.config("", {"laptop": ""})
        laptop = self.world.machine("laptop")
        self.assertTrue((laptop.dc / "engine" / "bin" / "devcluster").exists())
        self.assertTrue((laptop.dc / "config" / "cluster.toml").exists())
        self.assertEqual((laptop.dc / "machine").read_text().strip(), "laptop")
        shim = (laptop.home / "bin" / "devcluster").read_text()
        self.assertIn(str(laptop.dc / "engine" / "bin" / "devcluster"), shim)

    def test_status_shows_every_machine_and_skips_unchanged_pushes(self):
        url = self.world.remote("work", {"a.txt": "a\n"})
        self.world.config(f'[[repo]]\npath = "~/work"\nurl = "{url}"\n', {"laptop": 'mode = "on"', "server": "", "studio": ""})
        laptop, server = self.world.machine("laptop"), self.world.machine("server")
        laptop.sync()  # clones work
        server.sync()
        laptop.sync()  # nothing to do
        bare = self.world.github / "config.git"
        pushed = git(self.world.root, "--git-dir", str(bare), "rev-parse", "status/laptop")
        laptop.sync()  # still nothing to do, inside the heartbeat
        self.assertEqual(git(self.world.root, "--git-dir", str(bare), "rev-parse", "status/laptop"), pushed, "an unchanged run pushed again")
        observer = self.world.machine("observer", install=False)
        observer.dc.mkdir()
        git(self.world.root, "clone", "--quiet", str(bare), str(observer.dc / "config"))
        shown = subprocess.run([str(ROOT / "bin" / "devcluster"), "status"], env=observer.env(), capture_output=True, text=True).stdout
        self.assertIn("laptop", shown)
        self.assertIn("server", shown)
        self.assertIn("never reported", shown)  # studio
        self.assertIn("Would change (dry run):\n  server  repo work: would clone", shown)
        self.assertEqual(self.world.head("config"), git(self.world.work / "config", "rev-parse", "HEAD"), "status pushes never touch main")
