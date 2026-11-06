"""Repos worked on directly on main, synced both ways."""

import textwrap

from tests.fakes import ClusterTest, git

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
