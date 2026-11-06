"""Tools, CLIs, secrets and files."""

import base64
import json

from tests.fakes import FAKE_SECRETS, SECRET_VALUE, ClusterTest, git, wrap_git

INSTALL = 'mkdir -p "$HOME/.skills" && cp skill.txt "$HOME/.skills/skill.txt" && echo run >> "$HOME/.install-count"'


class Tools(ClusterTest):
    def setUp(self):
        super().setUp()
        self.url = self.world.remote("skills", {"skill.txt": "v1\n", "install.sh": f"#!/bin/sh\n{INSTALL}\n"})

    def start(self, ref=None, extra=""):
        ref_line = f'ref = "{ref}"\n' if ref else ""
        self.world.config(
            f'[[tool]]\nname = "skills"\npath = "~/tools/skills"\nurl = "{self.url}"\ninstall = "sh install.sh"\n{ref_line}{extra}',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        return self.laptop.sync()

    def installs(self):
        path = self.laptop.path(".install-count")
        return len(path.read_text().split()) if path.exists() else 0

    def test_clones_installs_and_follows_main(self):
        self.start()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.laptop.path(".skills/skill.txt").read_text(), "v1\n")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "ok")
        self.assertEqual(self.installs(), 1)
        self.world.commit("skills", {"skill.txt": "v2\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.laptop.path(".skills/skill.txt").read_text(), "v2\n")
        self.assertEqual(self.installs(), 2)

    def test_failed_install_backs_off_then_retries_on_a_new_commit(self):
        self.world.commit("skills", {"install.sh": "#!/bin/sh\necho broken >&2\nexit 3\n"})
        self.start()
        item = self.assertState(self.laptop, "skills", "failed", "its install exited 3")
        self.assertIn("broken", item["detail"])
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "failed", "is tried again after")
        self.world.commit("skills", {"install.sh": f"#!/bin/sh\n{INSTALL}\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")

    def test_failed_install_retries_after_the_wait(self):
        self.world.commit("skills", {"install.sh": "#!/bin/sh\nexit 1\n"})
        self.start(extra="")
        self.laptop.sync(now=self.laptop.report()["finished"] + 4000)
        self.assertState(self.laptop, "skills", "failed", "its install exited 1")

    def test_local_commits_block(self):
        self.start()
        self.laptop.commit("tools/skills", {"local.txt": "edit\n"})
        self.world.commit("skills", {"skill.txt": "v2\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "blocked", "that origin doesn't have")

    def test_pinned_commit_moves_back_and_forward(self):
        first = self.world.head("skills")
        second = self.world.commit("skills", {"skill.txt": "v2\n"})
        self.start(ref=first)
        self.assertEqual(self.laptop.git("tools/skills", "rev-parse", "HEAD"), first)
        self.assertEqual(self.laptop.path(".skills/skill.txt").read_text(), "v1\n")
        self.world.commit("config", {"cluster.toml": self.world.work.joinpath("config/cluster.toml").read_text().replace(first, second)})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.laptop.path(".skills/skill.txt").read_text(), "v2\n")

    def test_branch_moved_back_resets_without_losing_local_edits(self):
        self.start()
        good = self.world.head("skills")
        self.world.commit("skills", {"skill.txt": "bad\n"})
        self.laptop.sync()
        checkout = self.world.work / "skills"
        git(checkout, "reset", "--quiet", "--hard", good)
        git(checkout, "push", "--quiet", "--force", "origin", "main")
        self.laptop.path("tools/skills/untracked.txt").write_text("keep me\n")
        bad = self.laptop.git("tools/skills", "rev-parse", "main")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "moved from")
        self.assertEqual(self.laptop.git("tools/skills", "rev-parse", "HEAD"), good)
        self.assertEqual(self.laptop.path("tools/skills/untracked.txt").read_text(), "keep me\n")
        # It detached at the good commit and left the branch where it was, so nothing on it is lost.
        self.assertEqual(self.laptop.git("tools/skills", "rev-parse", "main"), bad)
        self.assertEqual(self.laptop.git("tools/skills", "branch", "--show-current"), "")
        self.world.commit("skills", {"skill.txt": "v3\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.laptop.path(".skills/skill.txt").read_text(), "v3\n")

    def test_commit_made_during_a_move_is_kept_on_a_branch(self):
        first = self.world.head("skills")
        second = self.world.commit("skills", {"skill.txt": "v2\n"})
        self.start(ref=first)
        tool = self.laptop.path("tools/skills")
        commit = f'(cd "{tool}" && echo mine > mine.txt && "$real" add mine.txt && "$real" commit --quiet -m mine)'
        path = wrap_git(self.laptop, f'*" -C {tool} checkout "*', before=commit)
        self.world.commit("config", {"cluster.toml": self.world.work.joinpath("config/cluster.toml").read_text().replace(first, second)})
        self.laptop.sync(PATH=path)
        self.assertState(self.laptop, "skills", "blocked", "that commit is kept on branch devcluster-kept-")
        kept = self.laptop.git("tools/skills", "for-each-ref", "--format=%(refname:short)", "refs/heads/devcluster-kept-*")
        self.assertEqual(self.laptop.git("tools/skills", "show", f"{kept}:mine.txt"), "mine")
        self.assertEqual(self.laptop.git("tools/skills", "rev-parse", "HEAD"), second)
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "ran its install")
        self.assertEqual(self.laptop.path(".skills/skill.txt").read_text(), "v2\n")


class Clis(ClusterTest):
    def start(self, version, install='echo "{version}" > "$HOME/.fake-cli"'):
        self.world.config(
            f'[[cli]]\nname = "fakecli"\nversion = "{version}"\ncheck = "cat $HOME/.fake-cli"\ninstall = \'{install}\'\n',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")

    def test_installs_when_the_version_differs(self):
        self.start("1.2.3")
        self.laptop.path(".fake-cli").write_text("fakecli 1.2.2\n")
        self.laptop.sync()
        self.assertState(self.laptop, "fakecli", "changed", "installed 1.2.3 (had 1.2.2)")
        self.laptop.sync()
        self.assertState(self.laptop, "fakecli", "ok", "at 1.2.3")

    def test_a_shared_prefix_is_not_a_match(self):
        self.start("0.163.0")
        self.laptop.path(".fake-cli").write_text("codex-cli 0.163.0-alpha.5\n")
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "fakecli", "would-change", "has 0.163.0-alpha.5; would install 0.163.0")

    def test_missing_cli_installs(self):
        self.start("2.0")
        self.laptop.sync()
        self.assertState(self.laptop, "fakecli", "changed", "had not installed")

    def test_failed_install_backs_off(self):
        self.start("2.0", install="exit 9")
        self.laptop.sync()
        self.assertState(self.laptop, "fakecli", "failed", "installing 2.0 exited 9")
        self.laptop.sync()
        self.assertState(self.laptop, "fakecli", "failed", "tried again after")

    def test_install_that_does_not_fix_the_check_fails(self):
        self.start("2.0", install='echo "1.0" > "$HOME/.fake-cli"')
        self.laptop.sync()
        self.assertState(self.laptop, "fakecli", "failed", "its check still shows 1.0")


class Secrets(ClusterTest):
    def setUp(self):
        super().setUp()
        encoded = base64.b64encode(f"TOKEN={SECRET_VALUE}\n".encode()).decode()
        self.world.remote("secrets", {"bin/secrets": FAKE_SECRETS, "encrypted/app.env.enc": encoded, ".gitignore": "local/\n"})
        self.world.config(
            '[secrets]\npath = "~/secrets"\nurl = "' + str(self.world.github / "secrets.git") + '"\n'
            'check = "./bin/secrets check"\ndecrypt = "./bin/secrets decrypt"\n',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        self.laptop.sync()

    def assertNoLeak(self):
        state = self.laptop.dc / "state"
        for path in state.iterdir():
            self.assertNotIn(SECRET_VALUE, path.read_text(errors="replace"), path.name)
        status = git(self.laptop.dc / "config", "show", "origin/status/laptop:status.json", check=False)
        self.assertNotIn(SECRET_VALUE, status)

    def test_clones_and_decrypts(self):
        self.assertState(self.laptop, "secrets", "changed", "cloned it and decrypted")
        self.assertIn(SECRET_VALUE, self.laptop.path("secrets/local/app.env").read_text())
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "ok")

    def test_pulls_and_decrypts_a_new_secret(self):
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "changed", "pulled 1 commit and decrypted")
        self.assertEqual(self.laptop.path("secrets/local/new.env").read_text(), "NEW=1\n")

    def test_a_failed_decrypt_is_retried(self):
        self.laptop.path(".fail-decrypt").write_text("")
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "failed", "decrypting failed")
        self.laptop.path(".fail-decrypt").unlink()
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "changed", "retried decrypting")
        self.assertEqual(self.laptop.path("secrets/local/new.env").read_text(), "NEW=1\n")
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "ok")

    def test_a_failed_decrypt_is_not_retried_over_a_local_edit(self):
        self.laptop.path(".fail-decrypt").write_text("")
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "failed", "decrypting failed")
        self.laptop.path("secrets/local/app.env").write_text(f"TOKEN={SECRET_VALUE}-edited\n")
        self.laptop.path(".fail-decrypt").unlink()
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "blocked", "has changed since, so devcluster won't decrypt over it")
        self.assertIn("-edited", self.laptop.path("secrets/local/app.env").read_text())
        self.assertNoLeak()

    def test_unpublished_local_edit_blocks_the_pull(self):
        before = self.laptop.git("secrets", "rev-parse", "HEAD")
        self.laptop.path("secrets/local/app.env").write_text(f"TOKEN={SECRET_VALUE}-edited\n")
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "blocked", "its check failed here")
        self.assertEqual(self.laptop.git("secrets", "rev-parse", "HEAD"), before)
        self.assertIn("-edited", self.laptop.path("secrets/local/app.env").read_text())
        self.assertNoLeak()


class Files(ClusterTest):
    def setUp(self):
        super().setUp()
        self.world.config(
            '[[file]]\nname = "rules"\nsource = "files/AGENTS.md"\ntargets = ["~/.codex/AGENTS.md", "~/.claude/CLAUDE.md"]\n',
            {"laptop": 'mode = "on"'},
            {"files/AGENTS.md": "Rule one.\n"},
        )
        self.laptop = self.world.machine("laptop", install=True)

    def test_writes_then_keeps_in_step(self):
        self.laptop.sync()
        self.assertState(self.laptop, "rules", "changed", "wrote ~/.codex/AGENTS.md")
        self.assertEqual(self.laptop.path(".claude/CLAUDE.md").read_text(), "Rule one.\n")
        self.laptop.sync()
        self.assertState(self.laptop, "rules", "ok")
        self.world.commit("config", {"files/AGENTS.md": "Rule two.\n"})
        self.laptop.sync()
        self.assertState(self.laptop, "rules", "changed", "updated ~/.codex/AGENTS.md")
        self.assertEqual(self.laptop.path(".codex/AGENTS.md").read_text(), "Rule two.\n")

    def test_local_edit_is_kept(self):
        self.laptop.sync()
        self.laptop.path(".codex/AGENTS.md").write_text("My own edit.\n")
        self.world.commit("config", {"files/AGENTS.md": "Rule two.\n"})
        self.laptop.sync()
        item = self.assertState(self.laptop, "rules", "blocked", "was edited here since devcluster wrote it")
        self.assertEqual(self.laptop.path(".codex/AGENTS.md").read_text(), "My own edit.\n")
        self.assertIn("updated ~/.claude/CLAUDE.md", item["summary"])

    def test_existing_file_is_moved_aside_once(self):
        self.laptop.path(".codex").mkdir()
        self.laptop.path(".codex/AGENTS.md").write_text("What was here before.\n")
        self.laptop.sync()
        self.assertState(self.laptop, "rules", "changed", "moved ~/.codex/AGENTS.md aside")
        self.assertEqual(self.laptop.path(".codex/AGENTS.md.before-devcluster").read_text(), "What was here before.\n")
        self.assertEqual(self.laptop.path(".codex/AGENTS.md").read_text(), "Rule one.\n")

    def test_dry_run_writes_nothing(self):
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "rules", "would-change", "would write ~/.codex/AGENTS.md")
        self.assertFalse(self.laptop.path(".codex/AGENTS.md").exists())

    def test_memory_survives_between_runs(self):
        self.laptop.sync()
        memory = json.loads((self.laptop.dc / "state" / "items.json").read_text())
        self.assertIn("~/.codex/AGENTS.md", memory["file:rules"]["written"])
