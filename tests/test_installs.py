"""Installs: the check after them, what they wrote, edits made by hand since, and going back
to the last good version when one fails."""

import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from devcluster import config as configs
from tests.fakes import ClusterTest, wrap_git
from tests.test_engine import write_config

# The skill folder is removed and copied again, as most skill installs do, so every file is
# rewritten even when its contents don't change.
INSTALL = textwrap.dedent('''\
    #!/bin/sh
    set -e
    if [ -e "$HOME/.agent-writes" ]; then
        # Stands in for another agent writing into the shared folder while the install runs.
        n=$(( $(cat "$HOME/.agent-count" 2>/dev/null || echo 0) + 1 )); echo $n > "$HOME/.agent-count"
        mkdir -p "$HOME/.skills" && echo "theirs $n" > "$HOME/.skills/theirs.txt"
    fi
    [ ! -e "$HOME/.fail-install" ] || { echo "told to fail" >&2; exit 9; }
    rm -rf "$HOME/.skills/demo"
    mkdir -p "$HOME/.skills/demo"
    cp -R skill/. "$HOME/.skills/demo/"
    echo run >> "$HOME/.install-count"
    ''')
CHECK = '#!/bin/sh\ndiff -r skill "$HOME/.skills/demo"\n'
BROKEN_CHECK = '#!/bin/sh\necho "the skill does not load" >&2\nexit 1\n'


class Installs(ClusterTest):
    def setUp(self):
        super().setUp()
        self.url = self.world.remote("skills", {
            "skill/a.txt": "a1\n",
            "skill/b.txt": "b1\n",
            "install.sh": INSTALL,
            "check.sh": CHECK,
        })

    def start(self, kind="tool", extra="", others=""):
        self.world.config(
            f'[[{kind}]]\nname = "skills"\npath = "~/tools/skills"\nurl = "{self.url}"\n'
            f'install = "sh install.sh"\ncheck = "sh check.sh"\ninstalls_into = ["~/.skills"]\n{extra}{others}',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        return self.laptop.sync()

    def installed(self, name):
        path = self.laptop.path(f".skills/demo/{name}")
        return path.read_text() if path.exists() else None

    def own_files(self, name="skills"):
        return json.loads(self.laptop.path(f".devcluster/state/files/{name}.json").read_text())

    def kept(self, name="skills"):
        folder = self.laptop.path(f".devcluster/kept/{name}")
        return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()) if folder.exists() else []

    def head(self):
        return self.laptop.git("tools/skills", "rev-parse", "HEAD")

    # The check

    def test_check_runs_after_the_install_and_the_files_are_recorded(self):
        self.start()
        self.assertState(self.laptop, "skills", "changed", "ran its install, and its check passed")
        recorded = self.own_files()
        self.assertEqual(sorted(Path(p).name for p in recorded), ["a.txt", "b.txt"])
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "ok", "up to date at")

    def test_a_failing_check_takes_a_tool_back_to_its_last_good_commit(self):
        self.start()
        good = self.head()
        bad = self.world.commit("skills", {"skill/a.txt": "a2\n", "check.sh": BROKEN_CHECK})
        self.laptop.sync()
        item = self.assertState(self.laptop, "skills", "failed", "its check exited 1")
        self.assertIn("the files it changed were put back", item["summary"])
        self.assertIn(f"went back to {good[:7]}, its last good commit, and installed it again", item["summary"])
        self.assertIn("the skill does not load", item["detail"])
        self.assertEqual(self.head(), good)
        self.assertEqual(self.installed("a.txt"), "a1\n")

        # It stays on the good commit, and doesn't try the bad one again until the wait is over.
        report = self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", f"stays on {good[:7]}, its last good commit: {bad[:7]} failed its check")
        self.assertEqual(self.head(), good)
        self.laptop.sync(now=report["finished"] + 4000)
        self.assertState(self.laptop, "skills", "failed", "its check exited 1")
        self.assertEqual(self.head(), good)

        # A fixed commit moves it forward.
        fixed = self.world.commit("skills", {"check.sh": CHECK})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install, and its check passed")
        self.assertEqual(self.head(), fixed)
        self.assertEqual(self.installed("a.txt"), "a2\n")

    # A failed install

    def test_a_failed_install_puts_its_files_back_and_removes_what_it_added(self):
        # A repo, so going back to a good commit can't hide a file that wasn't put back.
        self.start(kind="repo")
        self.world.commit("skills", {"install.sh": textwrap.dedent('''\
            #!/bin/sh
            echo changed > "$HOME/.skills/demo/a.txt"
            rm "$HOME/.skills/demo/b.txt"
            mkdir -p "$HOME/.skills/demo/new"
            echo added > "$HOME/.skills/demo/new/c.txt"
            echo "copy failed" >&2
            exit 4
            ''')})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", "its install exited 4")
        self.assertState(self.laptop, "skills", "failed", "the file it added was moved to ~/.devcluster/state/removed/skills")
        self.assertEqual(self.installed("a.txt"), "a1\n")
        self.assertEqual(self.installed("b.txt"), "b1\n")
        self.assertFalse(self.laptop.path(".skills/demo/new").exists())
        # Moved, not deleted: another agent might have made it.
        aside = list(self.laptop.path(".devcluster/state/removed/skills").rglob("c.txt"))
        self.assertEqual([p.read_text() for p in aside], ["added\n"])

    def test_a_repo_keeps_its_checkout_where_it_is_and_only_the_files_go_back(self):
        self.start(kind="repo")
        bad = self.world.commit("skills", {"skill/a.txt": "a2\n", "check.sh": BROKEN_CHECK})
        self.laptop.sync()
        item = self.assertState(self.laptop, "skills", "failed", "its check exited 1")
        self.assertIn("the files it changed were put back", item["summary"])
        self.assertNotIn("went back", item["summary"])
        self.assertEqual(self.head(), bad)
        self.assertEqual(self.installed("a.txt"), "a1\n")

    def test_a_failure_on_the_first_install_leaves_the_tool_where_it_is(self):
        self.world.commit("skills", {"check.sh": BROKEN_CHECK})
        self.start()
        item = self.assertState(self.laptop, "skills", "failed", "its check exited 1")
        self.assertNotIn("went back", item["summary"])
        # The first install wrote files the item didn't own yet, so they were removed again.
        self.assertIsNone(self.installed("a.txt"))

    def test_a_file_the_item_never_installed_is_not_restored_or_removed(self):
        self.world.commit("skills", {"check.sh": BROKEN_CHECK})
        self.world.config(
            f'[[tool]]\nname = "skills"\npath = "~/tools/skills"\nurl = "{self.url}"\n'
            'install = "sh install.sh"\ncheck = "sh check.sh"\ninstalls_into = ["~/.skills"]\n',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        self.laptop.path(".skills/demo").mkdir(parents=True)
        self.laptop.path(".skills/demo/a.txt").write_text("someone else's\n")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed")
        # The install overwrote it, and devcluster had no copy of its own to put back: it stays.
        self.assertEqual(self.installed("a.txt"), "a1\n")

    def test_putting_back_a_symlinked_folder_never_removes_files_through_it(self):
        # The item's install had made ~/.skills/demo a link to a folder elsewhere. A failed
        # install replaced the link with a real folder of new files. Putting the link back
        # must not remove those files through it, from the folder it points to.
        self.world.commit("skills", {"install.sh": textwrap.dedent('''\
            #!/bin/sh
            mkdir -p "$HOME/work/demo" "$HOME/.skills"
            echo source > "$HOME/work/demo/SKILL.md"
            ln -sfn "$HOME/work/demo" "$HOME/.skills/demo"
            '''), "check.sh": "#!/bin/sh\ntrue\n"})
        self.start(kind="repo")
        self.assertTrue(self.laptop.path(".skills/demo").is_symlink())
        self.world.commit("skills", {"install.sh": textwrap.dedent('''\
            #!/bin/sh
            rm -rf "$HOME/.skills/demo"
            mkdir -p "$HOME/.skills/demo"
            echo new > "$HOME/.skills/demo/SKILL.md"
            exit 1
            ''')})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", "its install exited 1")
        self.assertTrue(self.laptop.path(".skills/demo").is_symlink())
        self.assertEqual(self.laptop.path("work/demo/SKILL.md").read_text(), "source\n")

    def test_a_commit_made_while_the_install_runs_is_left_where_it_is(self):
        self.start()
        tool = self.laptop.path("tools/skills")
        self.world.commit("skills", {"skill/a.txt": "a2\n", "check.sh": BROKEN_CHECK + "\n"})
        # The failing check is where an agent commits in the checkout, mid-install.
        self.world.commit("skills", {"check.sh": textwrap.dedent(f'''\
            #!/bin/sh
            cd "{tool}" && echo mine > mine.txt && git add mine.txt && git commit --quiet -m mine
            exit 1
            ''')})
        self.laptop.sync()
        item = self.assertState(self.laptop, "skills", "failed", "while the install ran, so devcluster left it there")
        self.assertEqual(self.laptop.git("tools/skills", "log", "-1", "--format=%s"), "mine")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "blocked", "that origin doesn't have")

    def test_going_back_is_tried_again_on_the_next_run_when_it_fails(self):
        self.start()
        good = self.head()
        bad = self.world.commit("skills", {"check.sh": BROKEN_CHECK})
        git_dir = self.laptop.path("tools/skills/.git")
        path = wrap_git(self.laptop, '*" checkout --quiet --no-overwrite-ignore --detach "*',
                        before=f'touch "{git_dir}/index.lock"', after=f'rm -f "{git_dir}/index.lock"')
        self.laptop.sync(PATH=path)
        self.assertState(self.laptop, "skills", "failed", f"couldn't go back to {good[:7]}")
        self.assertEqual(self.head(), bad)
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", f"went back to {good[:7]}, its last good commit, and installed it again")
        self.assertEqual(self.head(), good)

    def test_files_moved_aside_by_two_failures_in_the_same_second_are_all_kept(self):
        self.start()
        self.world.commit("skills", {"check.sh": BROKEN_CHECK})
        # The new commit's install fails, then so does the good commit's, in the same run, and
        # another agent's file appears in the shared folder during each.
        self.laptop.path(".fail-install").write_text("")
        self.laptop.path(".agent-writes").write_text("")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", "there too")
        aside = sorted(p.read_text() for p in self.laptop.path(".devcluster/state/removed/skills").rglob("theirs.txt"))
        self.assertEqual(aside, ["theirs 1\n", "theirs 2\n"])

    def test_a_tool_whose_install_is_removed_while_it_recovers_just_syncs(self):
        self.start()
        self.world.commit("skills", {"check.sh": BROKEN_CHECK})
        self.laptop.path(".fail-install").write_text("")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", "there too")
        config = self.world.work / "config" / "cluster.toml"
        text = config.read_text()
        for line in ('install = "sh install.sh"\n', 'check = "sh check.sh"\n', 'installs_into = ["~/.skills"]\n'):
            text = text.replace(line, "")
        self.world.commit("config", {"cluster.toml": text})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "moved from")

    def test_installing_the_good_commit_again_is_retried_until_it_works(self):
        self.start()
        good = self.head()
        self.world.commit("skills", {"check.sh": BROKEN_CHECK})
        self.laptop.path(".fail-install").write_text("")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", f"went back to {good[:7]}, its last good commit, but its install exited 9 there too")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", "but its install exited 9 there too")
        self.laptop.path(".fail-install").unlink()
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", f"stays on {good[:7]}, its last good commit, and installed it again")
        self.assertEqual(self.installed("a.txt"), "a1\n")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", f"stays on {good[:7]}, its last good commit: ")

    # Edits made by hand

    def test_an_edit_the_install_left_alone_is_still_noticed_later(self):
        self.start()
        self.laptop.path(".skills/demo/a.txt").write_text("my fix\n")
        # An install that only rewrites the file that changed.
        self.world.commit("skills", {"skill/b.txt": "b2\n", "install.sh": '#!/bin/sh\ncp skill/b.txt "$HOME/.skills/demo/b.txt"\n', "check.sh": "#!/bin/sh\ntrue\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.installed("a.txt"), "my fix\n")
        # A later install that does rewrite it keeps the edit first.
        self.world.commit("skills", {"skill/a.txt": "a3\n", "install.sh": INSTALL})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "blocked", "(~/.skills/demo/a.txt)")
        self.assertEqual(self.installed("a.txt"), "a3\n")
        self.assertEqual(len(self.kept()), 1)

    def test_an_edit_is_kept_aside_the_install_goes_ahead_and_it_needs_you_until_dealt_with(self):
        self.start()
        self.laptop.path(".skills/demo/a.txt").write_text("my fix\n")
        self.world.commit("skills", {"skill/a.txt": "a2\n"})
        self.laptop.sync()
        item = self.assertState(self.laptop, "skills", "blocked", "ran its install, and its check passed")
        self.assertIn("1 installed file was edited here after devcluster installed it (~/.skills/demo/a.txt)", item["summary"])
        self.assertIn("~/.devcluster/kept/skills", item["summary"])
        self.assertEqual(self.installed("a.txt"), "a2\n")
        kept = self.kept()
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].endswith(".skills/demo/a.txt"))
        stored = self.laptop.path(f".devcluster/kept/skills/{kept[0]}")
        self.assertEqual(stored.read_text(), "my fix\n")

        self.laptop.sync()
        self.assertState(self.laptop, "skills", "blocked", "up to date at")
        shutil.rmtree(self.laptop.path(".devcluster/kept/skills"))
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "ok")

    def test_an_edit_identical_to_the_new_install_is_not_kept(self):
        self.start()
        # Someone ran the next version's install by hand before devcluster got to it.
        self.laptop.path(".skills/demo/a.txt").write_text("a2\n")
        self.world.commit("skills", {"skill/a.txt": "a2\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.kept(), [])
        self.assertFalse(self.laptop.path(".devcluster/kept").exists() and any(self.laptop.path(".devcluster/kept").iterdir()))

    def test_an_edit_to_a_file_the_item_does_not_own_is_ignored(self):
        self.start()
        self.laptop.path(".skills/other.txt").write_text("not the tool's\n")
        self.world.commit("skills", {"skill/a.txt": "a2\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.laptop.path(".skills/other.txt").read_text(), "not the tool's\n")
        self.assertNotIn(str(self.laptop.path(".skills/other.txt")), self.own_files())

    def test_an_edit_is_still_there_after_a_repo_install_fails(self):
        self.start(kind="repo")
        self.laptop.path(".skills/demo/a.txt").write_text("my fix\n")
        self.world.commit("skills", {"skill/a.txt": "a2\n", "check.sh": BROKEN_CHECK})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed")
        # Putting the files back put the edit back too, so no copy of it is needed.
        self.assertEqual(self.installed("a.txt"), "my fix\n")
        self.assertEqual(self.kept(), [])

    def test_dry_run_says_an_edit_would_be_kept(self):
        self.start()
        self.laptop.path(".skills/demo/a.txt").write_text("my fix\n")
        self.world.commit("skills", {"skill/a.txt": "a2\n"})
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "skills", "would-change", "keeping a copy of 1 installed file edited here")
        self.assertEqual(self.installed("a.txt"), "my fix\n")

    # Folders shared by several tools

    def test_two_tools_installing_into_the_same_folder_each_own_only_their_files(self):
        more = self.world.remote("more", {
            "install.sh": '#!/bin/sh\nmkdir -p "$HOME/.skills/more" && echo m1 > "$HOME/.skills/more/m.txt"\n',
        })
        self.start(others=f'\n[[tool]]\nname = "more"\npath = "~/tools/more"\nurl = "{more}"\ninstall = "sh install.sh"\ninstalls_into = ["~/.skills"]\n')
        self.assertEqual(sorted(Path(p).name for p in self.own_files("skills")), ["a.txt", "b.txt"])
        self.assertEqual(sorted(Path(p).name for p in self.own_files("more")), ["m.txt"])
        # A new version of one doesn't count the other's files as edited.
        self.world.commit("skills", {"skill/a.txt": "a2\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed")
        self.assertState(self.laptop, "more", "ok")

    def test_a_star_in_installs_into_matches_each_folder(self):
        self.world.commit("skills", {"install.sh": textwrap.dedent('''\
            #!/bin/sh
            for profile in "$HOME"/.profiles/*/; do mkdir -p "$profile/skills" && cp skill/a.txt "$profile/skills/a.txt"; done
            ''')})
        self.world.config(
            f'[[tool]]\nname = "skills"\npath = "~/tools/skills"\nurl = "{self.url}"\n'
            'install = "sh install.sh"\ninstalls_into = ["~/.profiles/*/skills"]\n',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        for profile in ("one", "two"):
            self.laptop.path(f".profiles/{profile}").mkdir(parents=True)
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed")
        self.assertEqual(len(self.own_files()), 2)

    # Trying one item in dry run

    def test_an_item_in_dry_run_changes_nothing_while_the_rest_sync(self):
        notes = self.world.remote("notes", {"plan.md": "one\n"})
        self.start(extra='mode = "dry-run"\n', others=f'\n[[repo]]\nname = "notes"\npath = "~/notes"\nurl = "{notes}"\n')
        self.assertState(self.laptop, "skills", "would-change", "would clone")
        self.assertFalse(self.laptop.path("tools/skills").exists())
        self.assertState(self.laptop, "notes", "changed", "cloned it")
        self.assertEqual(self.laptop.report()["mode"], "on")


class InstallConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fields_are_checked(self):
        write_config(self.root, """\
            [[tool]]
            path = "~/a"
            url = "u"
            check = "true"
            installs_into = "~/.skills"
            mode = "maybe"

            [[repo]]
            path = "~/b"
            url = "v"
            installs_into = ["~/.skills"]
            """, {"laptop": ""})
        with self.assertRaises(configs.ConfigError) as caught:
            configs.load(self.root)
        text = "\n".join(caught.exception.errors)
        for expected in ("installs_into must be a list", 'mode must be "dry-run" or "on"',
                         "check runs after the install, so it needs an install",
                         "installs_into says where the install writes, so it needs an install"):
            self.assertIn(expected, text)

    def test_a_valid_tool(self):
        write_config(self.root, """\
            [[tool]]
            path = "~/a"
            url = "u"
            install = "make install"
            check = "make verify"
            installs_into = ["~/.skills", "~/.hermes/profiles/*/skills"]
            mode = "dry-run"
            """, {"laptop": ""})
        loaded = configs.load(self.root)
        self.assertEqual(loaded.items[0].get("mode"), "dry-run")
