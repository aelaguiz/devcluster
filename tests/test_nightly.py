"""Commands that run once a night, in the background, such as Git's maintenance."""

import time
import unittest

from devcluster.items.nightly import parse_hours, window
from tests.fakes import ClusterTest


def at(hour, minute=0, days=0):
    """A local time today, or `days` from today."""
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday + days, hour, minute, 0, 0, 0, -1))


class Nightly(ClusterTest):
    def start(self, run='echo "$DEVCLUSTER_REPOS" > "$HOME/repos"; echo run >> "$HOME/runs"; echo all tidy', extra="", mode="on"):
        self.url = self.world.remote("notes", {"a.txt": "a\n"})
        self.world.config(
            f'[[repo]]\npath = "~/notes"\nurl = "{self.url}"\n\n'
            f'[[nightly]]\nname = "tidy"\nrun = \'{run}\'\n{extra}',
            {"laptop": f'mode = "{mode}"'},
        )
        self.laptop = self.world.machine("laptop")

    def finish(self):
        """Wait for the command started in the background to finish."""
        done = self.laptop.dc / "state" / "nightly" / "tidy" / "exit"
        for _ in range(100):
            if done.exists():
                return
            time.sleep(0.1)
        self.fail("the nightly command didn't finish")

    def runs(self):
        path = self.laptop.path("runs")
        return len(path.read_text().split()) if path.exists() else 0

    def test_waits_for_its_hours(self):
        self.start()
        self.laptop.sync(now=at(14))
        self.assertState(self.laptop, "tidy", "ok", "first runs between 01:00 and 05:00")
        self.assertEqual(self.runs(), 0)

    def test_runs_once_a_night_in_the_background_with_the_repos(self):
        self.start()
        self.laptop.sync(now=at(2))
        self.assertState(self.laptop, "tidy", "changed", "started at 02:00")
        self.finish()
        self.laptop.sync(now=at(2, 5))
        self.assertState(self.laptop, "tidy", "ok", "ran at 02:00")
        self.assertIn(": all tidy", self.laptop.item("tidy")["summary"])
        self.assertEqual(self.laptop.path("repos").read_text().split(), [str(self.laptop.path("notes"))])
        self.laptop.sync(now=at(3))
        self.laptop.sync(now=at(14))
        self.assertState(self.laptop, "tidy", "ok", "ran at 02:00")
        self.assertEqual(self.runs(), 1)
        self.laptop.sync(now=at(1, 30, days=1))
        self.assertState(self.laptop, "tidy", "changed", "started at 01:30")
        self.finish()
        self.assertEqual(self.runs(), 2)

    def test_a_failed_run_shows_until_the_next_night(self):
        self.start(run="echo it broke >&2; exit 3")
        self.laptop.sync(now=at(2))
        self.finish()
        self.laptop.sync(now=at(9))
        item = self.assertState(self.laptop, "tidy", "failed", "its run at 02:00 exited 3: it broke; it runs again at 01:00")
        self.assertIn("it broke", item["detail"])

    def test_a_run_past_its_time_is_stopped(self):
        self.start(run="sleep 30", extra="timeout = 60\n")
        self.laptop.sync(now=at(2))
        self.laptop.sync(now=at(2, 0) + 30)
        self.assertState(self.laptop, "tidy", "ok", "running since 02:00")
        began = time.time()
        self.laptop.sync(now=at(2, 2))
        self.assertLess(time.time() - began, 20)
        self.assertState(self.laptop, "tidy", "failed", "stopped after 1m")

    def test_a_dry_run_starts_nothing(self):
        self.start(mode="dry-run")
        self.laptop.sync(now=at(2))
        self.assertState(self.laptop, "tidy", "would-change", "would start now")
        self.assertEqual(self.runs(), 0)

    def test_hours_past_midnight(self):
        self.start(extra='hours = "23-2"\n')
        self.laptop.sync(now=at(22))
        self.assertState(self.laptop, "tidy", "ok", "first runs between 23:00 and 02:00")
        self.laptop.sync(now=at(23, 30))
        self.assertState(self.laptop, "tidy", "changed", "started at 23:30")
        self.finish()
        self.laptop.sync(now=at(1, days=1))
        self.assertState(self.laptop, "tidy", "ok", "ran at 23:30")
        self.assertEqual(self.runs(), 1)


class Hours(unittest.TestCase):
    def test_reads_hours(self):
        self.assertEqual(parse_hours("1-5"), (1, 5))
        self.assertEqual(parse_hours("23-4"), (23, 4))
        for wrong in ("5", "1-1", "1-24", "a-b", 3):
            self.assertIsNone(parse_hours(wrong))

    def test_window(self):
        self.assertEqual(window(at(3), "1-5"), (at(1), at(5)))
        self.assertIsNone(window(at(5), "1-5"))
        self.assertEqual(window(at(1, days=1), "23-2"), (at(23), at(2, days=1)))
        self.assertEqual(window(at(23, 30), "23-2"), (at(23), at(2, days=1)))

    def test_the_config_names_bad_hours(self):
        import tempfile
        from pathlib import Path
        from devcluster import config
        with tempfile.TemporaryDirectory() as root:
            Path(root, "machines").mkdir()
            Path(root, "machines", "laptop.toml").write_text("")
            Path(root, "cluster.toml").write_text('[[nightly]]\nname = "tidy"\nrun = "true"\nhours = "5-5"\n')
            with self.assertRaises(config.ConfigError) as caught:
                config.load(root)
        self.assertIn('nightly tidy: hours says when it may start', caught.exception.errors[0])
