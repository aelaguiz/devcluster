"""Repos worked on directly on main, synced both ways."""

import shutil
import textwrap

from tests.fakes import ClusterTest, git, wrap_git

CLUSTER = """\
[[repo]]
name = "work"
path = "~/workspace/work"
url = "{url}"
"""


class RepoSync(ClusterTest):
    def setUp(self):
        super().setUp()
        self.url = self.world.remote("work", {"README.md": "hello\n", "notes.txt": "one\n"})
        self.world.config(CLUSTER.format(url=self.url), {"laptop": 'mode = "on"', "server": 'mode = "on"'})
        self.laptop = self.world.machine("laptop")
        self.laptop.sync()  # clones it
        self.assertState(self.laptop, "work", "changed", "cloned it")

    def test_pulls_when_behind(self):
        new = self.world.commit("work", {"README.md": "hello again\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "pulled 1 commit")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), new)

    def test_pushes_when_ahead(self):
        mine = self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "pushed 1 commit")
        self.assertEqual(self.world.head("work"), mine)

    def test_merges_both_ways_and_every_machine_gets_every_commit(self):
        server = self.world.machine("server")
        server.sync()
        mine = self.laptop.commit("workspace/work", {"laptop.txt": "a\n"})
        theirs = server.commit("workspace/work", {"server.txt": "b\n"})
        server.sync()
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "merged 1 commit from origin")
        server.sync()
        heads = {self.world.head("work"), self.laptop.git("workspace/work", "rev-parse", "HEAD"), server.git("workspace/work", "rev-parse", "HEAD")}
        self.assertEqual(len(heads), 1)
        for machine in (self.laptop, server):
            for commit in (mine, theirs):
                machine.git("workspace/work", "merge-base", "--is-ancestor", commit, "HEAD")

    def test_conflict_changes_nothing(self):
        self.world.commit("work", {"notes.txt": "origin's line\n"})
        mine = self.laptop.commit("workspace/work", {"notes.txt": "laptop's line\n"})
        before = self.world.head("work")
        self.laptop.sync()
        item = self.assertState(self.laptop, "work", "blocked", "conflicts in notes.txt")
        self.assertIn("nothing was changed", item["summary"])
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), mine)
        self.assertEqual(self.laptop.git("workspace/work", "status", "--porcelain"), "")
        self.assertFalse((self.laptop.path("workspace/work/.git/MERGE_HEAD")).exists())
        self.assertEqual(self.world.head("work"), before)

    def test_uncommitted_change_in_the_way_blocks_and_survives(self):
        self.world.commit("work", {"notes.txt": "two\n"})
        (self.laptop.path("workspace/work/notes.txt")).write_text("my unsaved edit\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite uncommitted changes in notes.txt")
        self.assertEqual(self.laptop.path("workspace/work/notes.txt").read_text(), "my unsaved edit\n")

    def test_untracked_file_in_the_way_blocks_and_survives(self):
        self.world.commit("work", {"new.txt": "from origin\n"})
        self.laptop.path("workspace/work/new.txt").write_text("mine, not committed\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite untracked files in new.txt")
        self.assertEqual(self.laptop.path("workspace/work/new.txt").read_text(), "mine, not committed\n")

    def test_identical_untracked_copy_is_cleared(self):
        new = self.world.commit("work", {"new.txt": "from origin\n"})
        self.laptop.path("workspace/work/new.txt").write_text("from origin\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "pulled 1 commit, after clearing 1 file here that was identical to the incoming one")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), new)
        self.assertEqual(self.laptop.git("workspace/work", "status", "--porcelain"), "")
        self.assertEqual(list(self.laptop.path("workspace/work/.git/devcluster").glob("cleared-*")), [])

    def test_identical_uncommitted_change_is_cleared(self):
        new = self.world.commit("work", {"notes.txt": "two\n"})
        self.laptop.path("workspace/work/notes.txt").write_text("two\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "pulled 1 commit, after clearing 1 file")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), new)
        self.assertEqual(self.laptop.git("workspace/work", "status", "--porcelain"), "")

    def test_one_differing_file_blocks_and_only_it_is_named(self):
        before = self.laptop.git("workspace/work", "rev-parse", "HEAD")
        self.world.commit("work", {"same.txt": "same\n", "new.txt": "from origin\n"})
        self.laptop.path("workspace/work/same.txt").write_text("same\n")
        self.laptop.path("workspace/work/new.txt").write_text("mine\n")
        self.laptop.sync()
        item = self.assertState(self.laptop, "work", "blocked", "would overwrite untracked files in new.txt (1 more file in the way is identical to the incoming one)")
        self.assertEqual(item["detail"], "new.txt")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), before)
        self.assertEqual(self.laptop.path("workspace/work/same.txt").read_text(), "same\n")
        self.assertEqual(self.laptop.path("workspace/work/new.txt").read_text(), "mine\n")

    def test_a_mix_of_kinds_is_named_by_kind(self):
        self.world.commit("work", {"notes.txt": "two\n", "new.txt": "from origin\n"})
        self.laptop.path("workspace/work/notes.txt").write_text("my unsaved edit\n")
        self.laptop.path("workspace/work/new.txt").write_text("mine\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite uncommitted changes and untracked files in ")
        self.assertEqual(self.laptop.path("workspace/work/notes.txt").read_text(), "my unsaved edit\n")
        self.assertEqual(self.laptop.path("workspace/work/new.txt").read_text(), "mine\n")

    def test_a_staged_third_version_blocks(self):
        self.world.commit("work", {"notes.txt": "two\n"})
        self.laptop.path("workspace/work/notes.txt").write_text("staged\n")
        self.laptop.git("workspace/work", "add", "notes.txt")
        self.laptop.path("workspace/work/notes.txt").write_text("two\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite uncommitted changes in notes.txt")
        self.assertEqual(self.laptop.git("workspace/work", "show", ":notes.txt"), "staged")
        self.assertEqual(self.laptop.path("workspace/work/notes.txt").read_text(), "two\n")

    def test_a_staged_executable_bit_blocks(self):
        self.world.commit("work", {"notes.txt": "two\n"})
        self.laptop.git("workspace/work", "update-index", "--chmod=+x", "notes.txt")
        self.laptop.path("workspace/work/notes.txt").write_text("two\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite uncommitted changes in notes.txt")
        self.assertTrue(self.laptop.git("workspace/work", "ls-files", "--stage", "notes.txt").startswith("100755 "))

    def test_different_line_endings_block(self):
        self.laptop.git("workspace/work", "config", "core.autocrlf", "input")
        self.world.commit("work", {"new.txt": "hello\n"})
        self.laptop.path("workspace/work/new.txt").write_bytes(b"hello\r\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite untracked files in new.txt")
        self.assertEqual(self.laptop.path("workspace/work/new.txt").read_bytes(), b"hello\r\n")

    def test_a_name_with_brackets_is_not_a_pattern(self):
        self.world.commit("work", {"pages/[id].txt": "a\n", "pages/i.txt": "one\n"})
        self.laptop.sync()
        new = self.world.commit("work", {"pages/[id].txt": "b\n"})
        self.laptop.path("workspace/work/pages/[id].txt").write_text("b\n")
        self.laptop.path("workspace/work/pages/i.txt").write_text("staged\n")
        self.laptop.git("workspace/work", "add", "pages/i.txt")
        self.laptop.path("workspace/work/pages/i.txt").write_text("not staged\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "after clearing 1 file")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), new)
        self.assertEqual(self.laptop.git("workspace/work", "show", ":pages/i.txt"), "staged")
        self.assertEqual(self.laptop.path("workspace/work/pages/i.txt").read_text(), "not staged\n")

    def test_a_different_executable_bit_blocks(self):
        self.world.commit("work", {"new.txt": "x\n"})
        self.laptop.path("workspace/work/new.txt").write_text("x\n")
        self.laptop.path("workspace/work/new.txt").chmod(0o755)
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite untracked files in new.txt")
        self.assertTrue(self.laptop.path("workspace/work/new.txt").stat().st_mode & 0o100)

    def test_a_file_changed_while_it_is_cleared_is_put_back(self):
        repo = self.laptop.path("workspace/work")
        edit = f'for f in "{repo}"/.git/devcluster/cleared-*/new.txt; do echo changed > "$f"; done'
        path = wrap_git(self.laptop, '*" hash-object --no-filters -- "*/cleared-*', before=edit)
        before = self.laptop.git("workspace/work", "rev-parse", "HEAD")
        self.world.commit("work", {"new.txt": "from origin\n"})
        self.laptop.path("workspace/work/new.txt").write_text("from origin\n")
        self.laptop.sync(PATH=path)
        self.assertState(self.laptop, "work", "blocked", "would overwrite untracked files in new.txt")
        self.assertEqual(self.laptop.path("workspace/work/new.txt").read_text(), "changed\n")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), before)
        self.assertEqual(list(self.laptop.path("workspace/work/.git/devcluster").glob("cleared-*")), [])

    def test_identical_copy_is_cleared_through_a_merge(self):
        self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.world.commit("work", {"new.txt": "from origin\n"})
        self.laptop.path("workspace/work/new.txt").write_text("from origin\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "merged 1 commit from origin, after clearing 1 file")
        self.assertEqual(self.laptop.git("workspace/work", "status", "--porcelain"), "")
        self.assertEqual(self.world.head("work"), self.laptop.git("workspace/work", "rev-parse", "HEAD"))

    def test_uncommitted_change_elsewhere_is_kept_through_a_merge(self):
        self.world.commit("work", {"README.md": "origin\n"})
        self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.laptop.path("workspace/work/notes.txt").write_text("unsaved\n")
        self.laptop.path("workspace/work/scratch.txt").write_text("untracked\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "merged 1 commit")
        self.assertEqual(self.laptop.path("workspace/work/notes.txt").read_text(), "unsaved\n")
        self.assertEqual(self.laptop.path("workspace/work/scratch.txt").read_text(), "untracked\n")
        self.assertEqual(self.laptop.path("workspace/work/README.md").read_text(), "origin\n")

    def test_mid_merge_is_left_alone(self):
        self.world.commit("work", {"notes.txt": "origin\n"})
        self.laptop.commit("workspace/work", {"notes.txt": "laptop\n"})
        self.laptop.git("workspace/work", "fetch", "--quiet")
        self.laptop.git("workspace/work", "merge", "origin/main", check=False)
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "in the middle of a merge")

    def test_held_git_lock_is_busy(self):
        self.world.commit("work", {"README.md": "x\n"})
        self.laptop.path("workspace/work/.git/index.lock").write_text("")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "busy")

    def test_other_branch_is_left_alone(self):
        self.laptop.git("workspace/work", "checkout", "--quiet", "-b", "feature")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "checked out on feature, not main")

    def test_wrong_origin_is_left_alone(self):
        other = self.world.remote("other", {"a": "b"})
        self.laptop.git("workspace/work", "remote", "set-url", "origin", other)
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "but the list says")

    def test_dry_run_predicts_without_changing(self):
        self.world.commit("work", {"notes.txt": "origin's line\n"})
        mine = self.laptop.commit("workspace/work", {"notes.txt": "laptop's line\n"})
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "work", "blocked", "conflicts in notes.txt")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), mine)

        self.laptop.git("workspace/work", "reset", "--quiet", "--hard", "HEAD~1")
        self.laptop.commit("workspace/work", {"other.txt": "x\n"})
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "work", "would-change", "the merge is clean")
        self.assertEqual(self.laptop.report()["mode"], "dry-run")

    def test_push_race_is_retried(self):
        # A pre-push hook stands in for another machine pushing between our fetch and our push.
        rival = self.world.work / "rival"
        git(self.world.root, "clone", "--quiet", self.url, str(rival))
        hook = self.laptop.path("workspace/work/.git/hooks/pre-push")
        hook.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            [ -e "{rival}/raced" ] && exit 0
            touch "{rival}/raced"
            cd "{rival}" && echo r > rival.txt && git add rival.txt && git commit --quiet -m rival && git push --quiet origin main
            """))
        hook.chmod(0o755)
        mine = self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.laptop.sync()
        item = self.assertState(self.laptop, "work", "changed")
        self.assertIn("merged 1 commit from origin", item["summary"])
        head = self.world.head("work")
        self.laptop.git("workspace/work", "merge-base", "--is-ancestor", mine, head)
        self.assertTrue(self.laptop.path("workspace/work/rival.txt").exists())

    def test_new_machine_clones_and_dry_run_machine_does_not(self):
        self.world.commit("config", {"machines/studio.toml": ""})
        studio = self.world.machine("studio")
        studio.sync()
        self.assertState(studio, "work", "would-change", "would clone")
        self.assertFalse(studio.path("workspace/work").exists())

    def test_ignored_file_in_the_way_is_kept(self):
        self.laptop.path("workspace/work/.git/info/exclude").write_text("local-notes.md\n")
        self.laptop.path("workspace/work/local-notes.md").write_text("ignored, but mine\n")
        self.world.commit("work", {"local-notes.md": "now tracked on origin\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked")
        self.assertEqual(self.laptop.path("workspace/work/local-notes.md").read_text(), "ignored, but mine\n")

    def test_autostash_setting_does_not_change_what_happens(self):
        self.laptop.git("workspace/work", "config", "merge.autostash", "true")
        self.world.commit("work", {"notes.txt": "two\n"})
        self.laptop.path("workspace/work/notes.txt").write_text("my unsaved edit\n")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "would overwrite uncommitted changes in notes.txt")
        self.assertEqual(self.laptop.path("workspace/work/notes.txt").read_text(), "my unsaved edit\n")
        self.assertEqual(self.laptop.git("workspace/work", "stash", "list"), "")

    def test_branch_switched_during_the_fetch_is_left_alone(self):
        repo = self.laptop.path("workspace/work")
        path = wrap_git(self.laptop, f'*" -C {repo} fetch "*', after=f'"$real" -C "{repo}" checkout --quiet -b feature')
        self.world.commit("work", {"README.md": "new\n"})
        before = self.laptop.git("workspace/work", "rev-parse", "main")
        self.laptop.sync(PATH=path)
        self.assertState(self.laptop, "work", "blocked", "was switched to feature during the run")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "main"), before)
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "feature"), before)

    def test_branch_switched_just_before_the_update_is_reported(self):
        # The switch lands after the last check and before the fast-forward, so the
        # fast-forward moves the other branch. That only adds commits; it must be reported.
        repo = self.laptop.path("workspace/work")
        path = wrap_git(self.laptop, f'*" -C {repo} merge "*', before=f'"$real" -C "{repo}" checkout --quiet -b feature')
        new = self.world.commit("work", {"README.md": "new\n"})
        before = self.laptop.git("workspace/work", "rev-parse", "main")
        self.laptop.sync(PATH=path)
        self.assertState(self.laptop, "work", "blocked", "was switched to feature while devcluster updated it, so that got origin's commits instead of main")
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "main"), before)
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "feature"), new)

    def test_url_rewrites_are_understood(self):
        # The list names an SSH URL; this machine's Git rewrites it to where the repo really is.
        listed = "git@github.example:you/work.git"
        with open(self.laptop.path(".gitconfig"), "a") as config:
            config.write(f'[url "{self.world.github}/"]\n\tinsteadOf = git@github.example:you/\n')
        self.world.commit("config", {"cluster.toml": CLUSTER.format(url=listed)})
        self.world.commit("work", {"README.md": "new\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "pulled 1 commit")
        shutil.rmtree(self.laptop.path("workspace/work"))
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "cloned it")
        self.laptop.sync()
        self.assertState(self.laptop, "work", "ok")


SYNCED = """\
[[repo]]
name = "work"
path = "~/workspace/work"
url = "{url}"
sync = '"$HOME/sync.sh"'
install = 'echo done >> "$HOME/installed"'
"""

# The repo's own sync script, as a shared checkout ships one: here a plain pull and push.
PULL_AND_PUSH = "git pull --quiet --no-rebase --no-edit origin main && git push --quiet origin HEAD:main"


class RepoSyncCommand(ClusterTest):
    """A repo whose own command brings origin in and pushes, in place of devcluster's."""

    def setUp(self):
        super().setUp()
        self.url = self.world.remote("work", {"README.md": "hello\n", "notes.txt": "one\n"})
        self.world.config(SYNCED.format(url=self.url), {"laptop": 'mode = "on"'})
        self.laptop = self.world.machine("laptop")
        self.script(PULL_AND_PUSH)
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "cloned it")
        self.assertEqual(self.runs(), 0, "a clone needs no sync command")

    def script(self, body):
        path = self.laptop.path("sync.sh")
        path.write_text(f'#!/bin/sh\necho ran >> "$HOME/sync-ran"\n{body}\n')
        path.chmod(0o755)

    def runs(self):
        ran = self.laptop.path("sync-ran")
        return len(ran.read_text().splitlines()) if ran.exists() else 0

    def installs(self):
        done = self.laptop.path("installed")
        return len(done.read_text().splitlines()) if done.exists() else 0

    def test_up_to_date_runs_nothing(self):
        self.laptop.sync()
        self.assertState(self.laptop, "work", "ok", "up to date at")
        self.assertEqual(self.runs(), 0)

    def test_pulls_with_the_command_and_installs(self):
        new = self.world.commit("work", {"README.md": "hello again\n"})
        installed = self.installs()
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "pulled 1 commit; now at")
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.laptop.git("workspace/work", "rev-parse", "HEAD"), new)
        self.assertEqual(self.installs(), installed + 1)

    def test_pushes_with_the_command(self):
        mine = self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "pushed 1 commit")
        self.assertEqual(self.world.head("work"), mine)

    def test_merges_with_the_command(self):
        self.world.commit("work", {"theirs.txt": "b\n"})
        self.laptop.commit("workspace/work", {"mine.txt": "a\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "merged 1 commit from origin; pushed 1 commit")
        self.assertEqual(self.world.head("work"), self.laptop.git("workspace/work", "rev-parse", "HEAD"))

    def test_a_failing_command_fails_with_its_last_error_line(self):
        self.script('echo "starting" >&2; echo "error: cannot lock ref HEAD" >&2; exit 3')
        self.world.commit("work", {"README.md": "new\n"})
        self.laptop.sync()
        item = self.assertState(self.laptop, "work", "failed", "its sync command failed (exit 3): error: cannot lock ref HEAD")
        self.assertIn("starting", item["detail"])

    def test_origin_moving_again_straight_after_is_fine(self):
        # Another machine pushes the moment this one's sync finishes.
        self.script(PULL_AND_PUSH + ' && later=$(git commit-tree "HEAD^{tree}" -p HEAD -m later) && git push --quiet origin "$later:refs/heads/main"')
        self.world.commit("work", {"README.md": "new\n"})
        mine = self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "changed", "merged 1 commit from origin; pushed 1 commit")
        self.laptop.git("workspace/work", "merge-base", "--is-ancestor", mine, "origin/main")
        self.assertNotEqual(self.world.head("work"), self.laptop.git("workspace/work", "rev-parse", "HEAD"))

    def test_a_command_that_left_commits_behind_fails(self):
        self.script("true")
        self.world.commit("work", {"README.md": "new\n"})
        self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "failed", "its sync command succeeded, but origin's 1 commit didn't come in and this machine's 1 commit didn't reach origin")

    def test_dry_run_says_what_the_command_would_do_and_runs_nothing(self):
        self.world.commit("work", {"README.md": "new\n"})
        self.laptop.commit("workspace/work", {"mine.txt": "x\n"})
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "work", "would-change", "would run its sync command to pull 1 commit and push 1 commit, and run its install")
        self.assertEqual(self.runs(), 0)

    def test_still_needs_the_branch(self):
        self.laptop.git("workspace/work", "checkout", "--quiet", "-b", "feature")
        self.world.commit("work", {"README.md": "new\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "work", "blocked", "not main")
        self.assertEqual(self.runs(), 0)
