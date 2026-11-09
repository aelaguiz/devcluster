"""The fixer: when an item stays stuck, devcluster saves everything local, starts an agent,
checks its work on the next run, and tells the owner only when the fix needs them.

The agent here is a shell script that edits the checkout the way an agent would, then
writes its result. Notifying the owner and SSH are scripts that record what they were given.
"""

import json
import os
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from devcluster import fixer as fixers
from devcluster.context import Context
from devcluster.results import BLOCKED, FAILED, ItemResult
from tests.fakes import ClusterTest, base_env, git

CLUSTER = """\
[[repo]]
name = "work"
path = "~/workspace/work"
url = "{url}"
"""

FIXER = """\
command = '"$HOME/agent"'
notify = '"$HOME/notify"'
"""

T0 = 1_800_000_000


class FixerTest(ClusterTest):
    def setUp(self):
        super().setUp()
        self.url = self.world.remote("work", {"README.md": "hello\n", "notes.txt": "one\n"})

    def cluster(self, machines=None, fixer=FIXER, cluster=None):
        files = {"fixer.toml": textwrap.dedent(fixer)} if fixer else {}
        self.world.config(cluster or CLUSTER.format(url=self.url), machines or {"laptop": 'mode = "on"'}, files)

    def recluster(self, cluster=None, fixer=None):
        files = {}
        if cluster:
            files["cluster.toml"] = textwrap.dedent(cluster)
        if fixer:
            files["fixer.toml"] = textwrap.dedent(fixer)
        self.world.commit("config", files)

    def agent(self, machine, script, outcome="fixed", summary="did what it needed", **extra):
        """The fake agent: copies its brief, runs a shell snippet in the checkout, writes a result."""
        result = json.dumps({"outcome": outcome, "summary": summary, **extra})
        path = machine.path("agent")
        path.write_text(f"#!/bin/sh\ncat > \"$DEVCLUSTER_FIXER_DIR/brief-read.md\"\n{script}\ncat > \"$DEVCLUSTER_FIXER_RESULT\" <<'EOF'\n{result}\nEOF\n")
        path.chmod(0o755)
        notify = machine.path("notify")
        notify.write_text('#!/bin/sh\n{ echo "SUBJECT: $DEVCLUSTER_SUBJECT"; cat; echo "FILE: $DEVCLUSTER_MESSAGE"; } >> "$HOME/notified.txt"\n')
        notify.chmod(0o755)

    def runs(self, machine):
        root = machine.dc / "state" / "fixer"
        return sorted(p for p in root.iterdir() if p.is_dir() and p.name != "messages") if root.is_dir() else []

    def wait(self, machine, count=1):
        """Wait for the background agent runs to finish."""
        for _ in range(200):
            if sum(1 for run in self.runs(machine) if (run / "exit").exists()) >= count:
                return self.runs(machine)
            time.sleep(0.1)
        self.fail(f"the fixer on {machine.name} didn't finish: {[list(r.iterdir()) for r in self.runs(machine)]}")

    def messages(self, machine):
        path = machine.path("notified.txt")
        if not path.exists():
            return []
        return [line[len("SUBJECT: "):] for line in path.read_text().splitlines() if line.startswith("SUBJECT: ")]

    def block(self, machine):
        """Origin adds notes2.txt while this machine has a different copy it never committed."""
        self.world.commit("work", {"notes2.txt": "origin's copy\n"})
        (machine.path("workspace/work") / "notes2.txt").write_text("this machine's copy\n")


class HandOff(FixerTest):
    def setUp(self):
        super().setUp()
        self.cluster()
        self.laptop = self.world.machine("laptop")
        self.laptop.sync(now=T0)

    def test_saves_everything_then_the_agent_fixes_it_and_the_status_says_so(self):
        laptop = self.laptop
        mine = laptop.commit("workspace/work", {"mine.txt": "a local commit\n"})
        (laptop.path("workspace/work") / "README.md").write_text("an edit nobody committed\n")
        self.block(laptop)
        self.agent(laptop, 'while [ ! -e "$HOME/go" ]; do sleep 0.1; done; rm notes2.txt', summary="removed this machine's notes2.txt, an older copy of origin's")
        status_before = laptop.git("workspace/work", "status", "--porcelain")

        laptop.sync(now=T0 + 120)
        self.assertState(laptop, "work", "blocked", "would overwrite")
        self.assertEqual(self.runs(laptop), [], "handed over on the first stuck run")

        laptop.sync(now=T0 + 240)
        item = self.assertState(laptop, "work", "busy", "an agent has been fixing it since")
        branches = git(self.world.root, "--git-dir", str(self.world.github / "work.git"), "for-each-ref", "--format=%(refname:short)", "refs/heads/devcluster/saved/")
        self.assertRegex(branches, r"^devcluster/saved/laptop/work/\d{8}-\d{4}$")
        self.assertIn(branches, item["summary"])

        # The saved commit holds every local thing, on top of the local commit.
        bare = ["--git-dir", str(self.world.github / "work.git")]
        self.assertEqual(git(self.world.root, *bare, "show", f"{branches}:notes2.txt"), "this machine's copy")
        self.assertEqual(git(self.world.root, *bare, "show", f"{branches}:README.md"), "an edit nobody committed")
        self.assertEqual(git(self.world.root, *bare, "rev-parse", f"{branches}^"), mine)
        # Saving changed nothing in the checkout.
        self.assertEqual(laptop.git("workspace/work", "status", "--porcelain"), status_before)

        laptop.path("go").touch()
        run = self.wait(laptop)[0]
        brief = (run / "brief-read.md").read_text()
        self.assertIn("would overwrite", brief)
        self.assertIn(branches, brief)
        self.assertIn("and it reached origin", brief)
        skill = brief.split("Read the fixer skill first: ")[1].split("\n")[0]
        self.assertTrue(Path(skill).is_file(), skill)

        laptop.sync(now=T0 + 360)
        item = self.assertState(laptop, "work", "changed", "an agent fixed it at")
        self.assertIn("removed this machine's notes2.txt", item["summary"])
        self.assertIn(branches, item["summary"])
        laptop.git("workspace/work", "merge-base", "--is-ancestor", mine, self.world.head("work"))
        self.assertEqual(self.messages(laptop), [])

        laptop.sync(now=T0 + 25 * 3600)
        self.assertNotIn("an agent fixed it", laptop.item("work")["summary"])

    def test_leaves_the_item_alone_while_the_agent_works(self):
        laptop = self.laptop
        self.block(laptop)
        self.agent(laptop, 'while [ ! -e "$HOME/go" ]; do sleep 0.1; done; rm notes2.txt')
        laptop.sync(now=T0 + 120)
        laptop.sync(now=T0 + 240)
        head = laptop.git("workspace/work", "rev-parse", "HEAD")
        self.world.commit("work", {"more.txt": "more\n"})
        laptop.sync(now=T0 + 360)
        self.assertState(laptop, "work", "busy", "an agent has been fixing it since")
        self.assertEqual(laptop.git("workspace/work", "rev-parse", "HEAD"), head, "the engine pulled while the agent worked")
        laptop.path("go").touch()
        self.wait(laptop)
        laptop.sync(now=T0 + 480)
        self.assertState(laptop, "work", "changed", "an agent fixed it")

    def test_owner_hears_once_when_the_agent_couldnt_fix_it(self):
        laptop = self.laptop
        self.block(laptop)
        self.agent(laptop, ":", outcome="could-not-fix", summary="both copies look current", details="notes2.txt differs in its second half")
        laptop.sync(now=T0 + 120)
        laptop.sync(now=T0 + 240)
        self.wait(laptop)
        for minute in (6, 8, 10, 30):
            laptop.sync(now=T0 + minute * 60)
            self.assertState(laptop, "work", "blocked", "would overwrite")
        self.assertEqual(self.messages(laptop), ["devcluster on laptop: work is stuck, and the agent couldn't fix it"])
        self.assertEqual(len(self.runs(laptop)), 1, "started the same fix twice")
        body = laptop.path("notified.txt").read_text()
        self.assertIn("both copies look current", body)
        self.assertIn("notes2.txt differs in its second half", body)
        self.assertIn("devcluster/saved/laptop/work/", body)

    def test_needs_owner_sends_the_agents_message(self):
        laptop = self.laptop
        self.block(laptop)
        self.agent(laptop, ":", outcome="needs-owner", summary="GitHub wants a new login", message="Run gh auth login on the laptop.")
        laptop.sync(now=T0 + 120)
        laptop.sync(now=T0 + 240)
        self.wait(laptop)
        laptop.sync(now=T0 + 360)
        laptop.sync(now=T0 + 480)
        self.assertEqual(self.messages(laptop), ["devcluster on laptop: work needs you"])
        self.assertIn("Run gh auth login on the laptop.", laptop.path("notified.txt").read_text())

    def test_an_agent_that_never_gets_going_is_started_again(self):
        laptop = self.laptop
        self.block(laptop)
        path = laptop.path("agent")
        self.agent(laptop, ":")
        path.write_text('#!/bin/sh\necho \'agent: account "team" is busy\' >&2\nexit 1\n')
        laptop.sync(now=T0 + 120)
        for run in range(1, 4):
            laptop.sync(now=T0 + 120 + run * 120)
            self.assertState(laptop, "work", "busy")
            self.wait(laptop, run)
        laptop.sync(now=T0 + 600)
        laptop.sync(now=T0 + 720)
        self.assertEqual(len(self.runs(laptop)), 3)
        self.assertEqual(self.messages(laptop), ["devcluster on laptop: the agent for work won't start"])
        self.assertIn('account "team" is busy', laptop.path("notified.txt").read_text())

    def test_stops_an_agent_that_runs_out_of_time(self):
        laptop = self.laptop
        self.block(laptop)
        self.agent(laptop, 'trap "" TERM; echo $$ > "$HOME/agent.pid"; sleep 300')  # ignores a polite stop
        laptop.sync(now=T0 + 120)
        laptop.sync(now=T0 + 240)
        pid_file = laptop.path("agent.pid")
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            time.sleep(0.1)
        pid = int(pid_file.read_text())
        laptop.sync(now=T0 + 600)
        self.assertState(laptop, "work", "busy")
        laptop.sync(now=T0 + 240 + 1801)
        self.assertState(laptop, "work", "blocked", "would overwrite")
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("the agent kept running past its time limit")
        body = laptop.path("notified.txt").read_text()
        self.assertIn("ran out of time after 30 minutes", body)

    def test_a_dry_run_never_stops_an_agent_and_turning_the_fixer_off_leaves_it_to_finish(self):
        laptop = self.laptop
        self.block(laptop)
        self.agent(laptop, 'while [ ! -e "$HOME/go" ]; do sleep 0.1; done; rm notes2.txt')
        laptop.sync(now=T0 + 120)
        laptop.sync(now=T0 + 240)
        laptop.sync("--dry-run", now=T0 + 240 + 1801)
        self.assertState(laptop, "work", "busy")
        self.recluster(fixer="# turned off\n")
        laptop.sync(now=T0 + 600)
        self.assertState(laptop, "work", "busy", "an agent has been fixing it since")
        self.assertEqual(self.runs(laptop)[0].joinpath("exit").exists(), False, "the agent was stopped")
        laptop.path("go").touch()
        self.wait(laptop)
        laptop.sync(now=T0 + 720)
        self.assertState(laptop, "work", "changed")

    def test_a_failing_item_waits_before_it_is_handed_over(self):
        laptop = self.laptop
        self.recluster(cluster=CLUSTER.format(url=self.url) + textwrap.dedent("""\
            [[cli]]
            name = "mytool"
            version = "2.0.0"
            check = "$HOME/bin/mytool --version"
            install = "echo 'mirror unreachable' >&2; exit 1"
            """))
        self.agent(laptop, 'printf "#!/bin/sh\\necho mytool 2.0.0\\n" > "$HOME/bin/mytool"; chmod +x "$HOME/bin/mytool"', summary="installed mytool 2.0.0 from the release page")
        for minute in (2, 4, 20):
            laptop.sync(now=T0 + minute * 60)
            self.assertState(laptop, "mytool", "failed")
        self.assertEqual(self.runs(laptop), [])
        laptop.sync(now=T0 + 33 * 60)
        self.assertState(laptop, "mytool", "busy", "an agent has been fixing it")
        self.wait(laptop)
        laptop.sync(now=T0 + 35 * 60)
        item = self.assertState(laptop, "mytool", "ok", "an agent fixed it at")
        self.assertNotIn("branch", item["summary"])


class DryRun(FixerTest):
    def test_never_hands_off_in_dry_run(self):
        self.cluster(machines={"laptop": 'mode = "dry-run"'})
        laptop = self.world.machine("laptop")
        git(laptop.home, "clone", "--quiet", self.url, str(laptop.path("workspace/work")), env=base_env(laptop.home))
        self.block(laptop)
        self.agent(laptop, ":")
        for minute in range(0, 40, 2):
            laptop.sync(now=T0 + minute * 60)
        self.assertEqual(self.runs(laptop), [])


    def test_never_hands_off_an_item_tried_in_dry_run(self):
        self.cluster(cluster=CLUSTER.format(url=self.url) + 'mode = "dry-run"\n')
        laptop = self.world.machine("laptop")
        git(laptop.home, "clone", "--quiet", self.url, str(laptop.path("workspace/work")), env=base_env(laptop.home))
        self.block(laptop)
        self.agent(laptop, ":")
        for minute in range(0, 40, 2):
            laptop.sync(now=T0 + minute * 60)
        self.assertEqual(laptop.report()["mode"], "on")
        self.assertEqual(self.runs(laptop), [])


class Saving(FixerTest):
    """The save step on its own, on checkouts the engine itself didn't make."""

    def checkout(self, *args):
        path = self.world.root / "checkout"
        git(self.world.root, "clone", "--quiet", *args, self.url, str(path))
        return path

    def save(self, path, in_way=()):
        item = SimpleNamespace(kind="repo", name="work", get=lambda key, default=None: {"path": str(path)}.get(key, default))
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        env.update({k: v for k, v in base_env().items() if k.startswith("GIT_")})
        from devcluster.git import Git
        return fixers.save(Git(path, env), "laptop", item, T0, in_way)

    def test_a_sparse_checkout_records_no_deletions(self):
        self.world.commit("work", {"inside/a.txt": "a\n", "outside/b.txt": "b\n"})
        path = self.checkout()
        git(path, "sparse-checkout", "set", "inside")
        self.assertFalse((path / "outside").exists())
        (path / "inside" / "a.txt").write_text("edited\n")
        (path / "inside" / "new.txt").write_text("new\n")
        (path / "outside").mkdir()
        (path / "outside" / "note.txt").write_text("a note outside the sparse folders\n")
        index_before = (path / ".git" / "index").read_bytes()
        saved = self.save(path)
        self.assertTrue(saved["pushed"], saved)
        files = git(path, "ls-tree", "-r", "--name-only", saved["commit"]).splitlines()
        self.assertEqual(sorted(files), ["README.md", "inside/a.txt", "inside/new.txt", "notes.txt", "outside/b.txt", "outside/note.txt"])
        self.assertEqual(git(path, "show", f"{saved['commit']}:inside/a.txt"), "edited")
        self.assertEqual((path / ".git" / "index").read_bytes(), index_before, "the real index changed")
        self.assertEqual(git(path, "rev-parse", f"{saved['commit']}^"), git(path, "rev-parse", "HEAD"))

    def test_an_ignored_file_in_the_way_is_saved_too(self):
        self.world.commit("work", {".gitignore": "*.log\n"})
        path = self.checkout()
        (path / "run.log").write_text("ignored, but in the way\n")
        (path / "other.log").write_text("ignored and not in the way\n")
        saved = self.save(path, in_way=["run.log"])
        files = git(path, "ls-tree", "-r", "--name-only", saved["commit"]).splitlines()
        self.assertIn("run.log", files)
        self.assertNotIn("other.log", files)

    def test_what_was_staged_is_kept_when_the_file_changed_again(self):
        path = self.checkout()
        (path / "notes.txt").write_text("staged\n")
        git(path, "add", "notes.txt")
        (path / "notes.txt").write_text("changed again\n")
        (path / "README.md").write_text("staged, then deleted\n")
        git(path, "add", "README.md")
        (path / "README.md").unlink()
        saved = self.save(path)
        self.assertEqual(git(path, "show", f"{saved['commit']}:notes.txt"), "changed again")
        self.assertEqual(git(path, "show", f"{saved['commit']}^2:notes.txt"), "staged")
        self.assertEqual(git(path, "show", f"{saved['commit']}^2:README.md"), "staged, then deleted")
        self.assertEqual(git(path, "rev-parse", f"{saved['commit']}^1"), git(path, "rev-parse", "HEAD"))

    def test_nothing_staged_means_one_parent(self):
        path = self.checkout()
        (path / "notes.txt").write_text("edited\n")
        saved = self.save(path)
        self.assertEqual(git(path, "rev-list", "--parents", "-n", "1", saved["commit"]).count(" "), 1)

    def test_a_branch_that_cant_be_made_is_an_error_not_a_save(self):
        path = self.checkout()
        git(path, "branch", "devcluster")
        (path / "new.txt").write_text("new\n")
        saved = self.save(path)
        self.assertNotIn("branch", saved)
        self.assertIn("couldn't name a branch", saved["error"])

    def test_ignored_files_are_copied_where_only_this_user_can_read_them(self):
        self.world.commit("work", {".gitignore": "local/\n"})
        path = self.checkout()
        (path / "local").mkdir()
        (path / "local" / "token").write_text("not a real token\n")
        from devcluster.git import Git
        folder = self.world.root / "copy" / "ignored-files"
        self.assertEqual(fixers.copy_ignored(Git(path), folder), 1)
        self.assertEqual((folder / "local" / "token").read_text(), "not a real token\n")
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_a_refused_push_keeps_the_branch_here_and_says_so(self):
        hook = self.world.github / "work.git" / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'file over the size limit' >&2\nexit 1\n")
        hook.chmod(0o755)
        path = self.checkout()
        (path / "big.bin").write_text("pretend this is big\n")
        saved = self.save(path)
        self.assertFalse(saved["pushed"])
        self.assertIn("file over the size limit", saved["error"])
        self.assertEqual(git(path, "rev-parse", f"refs/heads/{saved['branch']}"), saved["commit"])


    def test_a_public_origin_keeps_the_branch_here_and_says_why(self):
        path = self.checkout()
        (path / "plans.md").write_text("private notes nobody meant to publish\n")
        item = SimpleNamespace(kind="tool", name="work", get=lambda key, default=None: {"path": str(path)}.get(key, default))
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        env.update({k: v for k, v in base_env().items() if k.startswith("GIT_")})
        from devcluster.git import Git
        saved = fixers.save(Git(path, env), "laptop", item, T0, push=False, here="its origin is public, so it stays on this machine")
        self.assertFalse(saved["pushed"])
        self.assertIn("origin is public", saved["error"])
        self.assertEqual(git(path, "show", f"{saved['branch']}:plans.md"), "private notes nobody meant to publish")
        on_origin = git(self.world.root, "--git-dir", str(self.world.github / "work.git"), "for-each-ref", "refs/heads/devcluster/")
        self.assertEqual(on_origin, "")


class PublicOrigins(unittest.TestCase):
    def test_network_origins_are_asked_over_https_without_credentials(self):
        cases = {
            "git@github.com:aelaguiz/aimgr.git": "https://github.com/aelaguiz/aimgr.git",
            "ssh://git@github.com/aelaguiz/aimgr.git": "https://github.com/aelaguiz/aimgr.git",
            "ssh://git@example.com:2222/team/tool": "https://example.com/team/tool",
            "https://x-access-token:secret@github.com/funcountry/psbrain.git": "https://github.com/funcountry/psbrain.git",
            "https://github.com/aelaguiz/agent-email.git": "https://github.com/aelaguiz/agent-email.git",
            "/srv/git/work.git": None,
            "file:///srv/git/work.git": None,
            "": None,
        }
        for url, expected in cases.items():
            self.assertEqual(fixers.anonymous_url(url), expected, url)

    def answers(self, code, err):
        calls = []

        def fake(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(ok=code == 0, code=code, out="", err=err)
        return fake, calls

    def ask(self, code, err, url="git@github.com:aelaguiz/aimgr.git"):
        fake, calls = self.answers(code, err)
        real = fixers.run_command
        fixers.run_command = fake
        try:
            return fixers.public_origin(url), calls
        finally:
            fixers.run_command = real

    def test_an_origin_anyone_can_read_is_public(self):
        public, calls = self.ask(0, "")
        self.assertTrue(public)
        args, kwargs = calls[0]
        self.assertIn("https://github.com/aelaguiz/aimgr.git", args)
        self.assertIn("credential.helper=", args)
        self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_an_origin_that_wants_a_login_is_private(self):
        public, _ = self.ask(128, "fatal: could not read Username for 'https://github.com': terminal prompts disabled")
        self.assertFalse(public)

    def test_an_unclear_answer_keeps_the_work_here(self):
        public, _ = self.ask(128, "fatal: unable to access 'https://github.com/aelaguiz/aimgr.git/': Could not resolve host: github.com")
        self.assertTrue(public)

    def test_a_local_origin_is_never_asked(self):
        public, calls = self.ask(0, "", url="/srv/git/work.git")
        self.assertFalse(public)
        self.assertEqual(calls, [])


class Config(unittest.TestCase):
    def load(self, fixer):
        from devcluster import config
        with tempfile.TemporaryDirectory() as root:
            Path(root, "machines").mkdir()
            Path(root, "machines", "laptop.toml").write_text("")
            Path(root, "cluster.toml").write_text("")
            Path(root, "fixer.toml").write_text(textwrap.dedent(fixer))
            try:
                return config.load(root), []
            except config.ConfigError as error:
                return None, error.errors

    def test_reads_settings_and_each_machines_table(self):
        loaded, errors = self.load('command = "agent"\ntimeout = 600\n[machines.laptop]\nssh = "me@laptop"\nwatched = false\ntimeout = 900\n')
        self.assertEqual(errors, [])
        chosen = fixers.settings(loaded, "laptop")
        self.assertEqual((chosen["command"], chosen["timeout"], chosen["wait"]), ("agent", 900, 1800))
        self.assertEqual(loaded.fixer_machines["laptop"]["ssh"], "me@laptop")

    def test_names_each_mistake(self):
        _, errors = self.load('command = "agent"\ntimout = 600\n[machines.desktop]\nssh = "me@desktop"\n[machines.laptop]\nwatched = "no"\n')
        self.assertEqual(errors, [
            "fixer.toml: unknown setting 'timout'",
            "fixer.toml: machines.desktop: there is no machines/desktop.toml",
            "fixer.toml: machines.laptop: watched must be true or false",
        ])

    def test_no_command_means_no_fixer(self):
        loaded, _ = self.load('notify = "mail me"\n')
        self.assertIsNone(fixers.settings(loaded, "laptop"))

    def test_a_machines_empty_command_means_no_agent_there(self):
        loaded, errors = self.load('command = "agent"\n[machines.laptop]\ncommand = ""\n')
        self.assertEqual(errors, [])
        self.assertIsNone(fixers.settings(loaded, "laptop"))
        _, errors = self.load('command = ""\n')
        self.assertEqual(errors, ["fixer.toml: command must be text"])


class Rules(unittest.TestCase):
    """When the fixer tries again, and when it tells the owner, without running anything."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        layout = SimpleNamespace(state=Path(self._tmp.name), config=Path(self._tmp.name))
        config = SimpleNamespace(machines={"laptop": SimpleNamespace(mode="on")}, fixer={}, fixer_machines={}, setting=lambda k, d: d)
        self.ctx = Context("laptop", False, config, T0, {})
        self.fixer = fixers.Fixer(layout, self.ctx, config, "laptop", dict(fixers.DEFAULTS, command="agent"))
        self.started, self.told = [], []
        self.fixer._start_item = lambda item, episode: self.started.append(episode["fp"]) or False
        self.fixer._notify_item = lambda item, episode, owner: self.told.append((episode["fp"], bool(owner))) or True
        self.item = SimpleNamespace(kind="repo", name="work", get=lambda key, default=None: default)

    def tearDown(self):
        self._tmp.cleanup()

    def run_once(self, state=BLOCKED, summary="pulling 3 commits would overwrite notes.txt", minutes=2):
        self.fixer.now = self.ctx.now = self.fixer.now + minutes * 60
        result = ItemResult("repo", "work", state, summary)
        self.fixer._track([result], set(), {"repo:work"})
        self.fixer._hand_off([self.item], [result])
        return self.fixer.state["episodes"].get("repo:work")

    def attempt(self, episode, outcome, fp=None):
        episode["attempts"].append({"at": self.fixer.now, "fp": fp or episode["fp"], "outcome": outcome, "summary": ""})

    def test_the_owner_hears_once_however_the_status_line_rewords_it(self):
        failing = ("installing 2.0.0 exited 1", "installing 2.0.0 failed at 10:02, and is tried again after 11:02")
        self.run_once(state=FAILED, summary=failing[0], minutes=0)
        for _ in range(40):
            for summary in (failing[1], failing[1], failing[0]):
                result = ItemResult("repo", "work", FAILED, summary, "mirror unreachable")
                self.fixer.now += 120
                self.fixer._track([result], set(), {"repo:work"})
                self.fixer._hand_off([self.item], [result])
                episode = self.fixer.state["episodes"]["repo:work"]
                if self.started and len(episode["attempts"]) < len(self.started):
                    self.attempt(episode, "could-not-fix")
        self.assertEqual(len(self.started), 1, "the same failure reworded got a second agent")
        self.assertEqual(len(self.told), 1)

    def test_digits_and_commit_ids_dont_make_a_new_problem(self):
        a = ItemResult("repo", "work", BLOCKED, "pulling 3 commits would overwrite notes.txt; now at 1a2b3c4")
        b = ItemResult("repo", "work", BLOCKED, "pulling 12 commits would overwrite notes.txt; now at 9f8e7d6")
        c = ItemResult("repo", "work", BLOCKED, "pulling 12 commits would overwrite plans.txt; now at 9f8e7d6")
        self.assertEqual(fixers.fingerprint(a), fixers.fingerprint(b))
        self.assertNotEqual(fixers.fingerprint(a), fixers.fingerprint(c))

    def test_a_new_problem_after_a_fix_gets_one_more_try_then_the_owner(self):
        episode = self.run_once()
        self.assertEqual(self.started, [])
        episode = self.run_once()
        self.assertEqual(len(self.started), 1)
        self.attempt(episode, "fixed")
        self.run_once(summary="merging would conflict in plans.txt")
        self.assertEqual(len(self.started), 2)
        self.attempt(episode, "fixed")
        self.run_once(summary="merging would conflict in other.txt")
        self.assertEqual(len(self.started), 2)
        self.assertEqual(len(self.told), 1)
        self.run_once(summary="merging would conflict in other.txt")
        self.assertEqual(len(self.told), 1, "told the owner twice about one problem")

    def test_waiting_backs_off_and_doesnt_count_as_a_try(self):
        self.run_once()
        episode = self.run_once()
        self.assertEqual(len(self.started), 1)
        self.fixer._record({"key": "repo:work", "started": self.fixer.now, "fp": episode["fp"], "dir": "x"}, {"outcome": "waiting", "summary": "someone is editing notes.txt"})
        self.run_once(minutes=10)
        self.assertEqual(len(self.started), 1)
        self.run_once(minutes=25)
        self.assertEqual(len(self.started), 2)
        self.fixer._record({"key": "repo:work", "started": self.fixer.now, "fp": episode["fp"], "dir": "x"}, {"outcome": "waiting", "summary": "still editing"})
        self.run_once(minutes=40)
        self.assertEqual(len(self.started), 2, "the second wait should be an hour")
        self.run_once(minutes=25)
        self.assertEqual(len(self.started), 3)
        self.assertEqual(self.told, [])

    def test_needs_owner_tells_the_owner_at_once(self):
        self.run_once()
        episode = self.run_once()
        self.attempt(episode, "needs-owner")
        self.run_once()
        self.assertEqual(self.told, [(episode["fp"], True)])

    def test_an_item_that_syncs_ends_the_episode(self):
        self.run_once()
        episode = self.run_once()
        self.attempt(episode, "fixed")
        self.fixer.now += 120
        self.fixer._track([ItemResult("repo", "work", "changed", "pulled 3 commits")], set(), {"repo:work"})
        self.assertNotIn("repo:work", self.fixer.state["episodes"])
        self.assertIn("repo:work", self.fixer.state["fixed"])

    def test_a_failing_item_is_handed_over_only_after_failed_after(self):
        for _ in range(15):  # the first at 2 minutes, the fifteenth at 30: 28 minutes failing
            self.run_once(state=FAILED, summary="couldn't fetch: timed out")
        self.assertEqual(self.started, [])
        self.run_once(state=FAILED, summary="couldn't fetch: timed out")
        self.assertEqual(len(self.started), 1)


class Watch(FixerTest):
    """A machine that stops reporting gets checked by the next machine in the list."""

    def setUp(self):
        super().setUp()
        self.fixer = FIXER + '\n[machines.server]\nssh = "agent@server.invalid"\n'
        self.cluster(machines={"laptop": 'mode = "on"', "server": 'mode = "on"'}, fixer=self.fixer)
        self.laptop = self.world.machine("laptop")
        self.server = self.world.machine("server")
        bin_dir = self.laptop.path("fake-bin")
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text('#!/bin/sh\necho "$*" >> "$HOME/ssh.log"\n[ -e "$HOME/ssh-down" ] && { echo "ssh: connect to host server.invalid port 22: Operation timed out" >&2; exit 255; }\nexit 0\n')
        (bin_dir / "ssh").chmod(0o755)
        self.path = f"{bin_dir}:{self.laptop.env()['PATH']}"
        self.ssh = str(bin_dir / "ssh")
        self.agent(self.laptop, ":", summary="restarted the timer on server")
        self.server.sync(now=T0)
        self.laptop.sync(now=T0, PATH=self.path, DEVCLUSTER_SSH=self.ssh)

    def test_checks_a_quiet_machine_over_ssh_and_starts_the_agent(self):
        self.laptop.sync(now=T0 + 600, PATH=self.path, DEVCLUSTER_SSH=self.ssh)
        self.assertEqual([i for i in self.laptop.report()["items"] if i["kind"] == "machine"], [])
        self.laptop.sync(now=T0 + 1500, PATH=self.path, DEVCLUSTER_SSH=self.ssh)
        item = self.assertState(self.laptop, "server", "busy", "an agent has been checking it over SSH")
        self.assertEqual(item["kind"], "machine")
        self.assertIn("BatchMode=yes", self.laptop.path("ssh.log").read_text())
        brief = (self.wait(self.laptop)[0] / "brief-read.md").read_text()
        self.assertIn("ssh agent@server.invalid", brief)
        self.server.sync(now=T0 + 1600)
        self.laptop.sync(now=T0 + 1700, PATH=self.path, DEVCLUSTER_SSH=self.ssh)
        self.assertEqual([i for i in self.laptop.report()["items"] if i["kind"] == "machine"], [])

    def test_an_unreachable_machine_sends_one_message_and_no_agent(self):
        self.laptop.path("ssh-down").touch()
        for minute in (25, 27, 29):
            self.laptop.sync(now=T0 + minute * 60, PATH=self.path, DEVCLUSTER_SSH=self.ssh)
            self.assertState(self.laptop, "server", "blocked", "can't be reached over SSH")
        self.assertEqual(self.messages(self.laptop), ["devcluster: server stopped reporting"])
        self.assertEqual(self.runs(self.laptop), [])

    def test_no_neighbour_is_checked_when_the_reports_cant_be_fetched(self):
        config = self.world.github / "config.git"
        config.rename(config.with_name("config-away.git"))
        try:
            self.laptop.sync(now=T0 + 1500, PATH=self.path, DEVCLUSTER_SSH=self.ssh)
        finally:
            config.with_name("config-away.git").rename(config)
        self.assertEqual([i for i in self.laptop.report()["items"] if i["kind"] == "machine"], [])
        self.assertFalse(self.laptop.path("ssh.log").exists())

    def test_a_machine_that_isnt_watched_is_left_alone(self):
        self.recluster(fixer=self.fixer + "watched = false\n")
        self.laptop.sync(now=T0 + 1500, PATH=self.path, DEVCLUSTER_SSH=self.ssh)
        self.assertEqual([i for i in self.laptop.report()["items"] if i["kind"] == "machine"], [])
        self.assertFalse(self.laptop.path("ssh.log").exists())


if __name__ == "__main__":
    unittest.main()
