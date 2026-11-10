"""Tools, CLIs, secrets and files."""

import base64
import json
import subprocess

from devcluster import config as configs
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

    def test_identical_copy_in_a_tool_is_cleared(self):
        self.start()
        self.world.commit("skills", {"extra.txt": "x\n"})
        self.laptop.path("tools/skills/extra.txt").write_text("x\n")
        self.laptop.sync()
        self.assertState(self.laptop, "skills", "changed", "after clearing 1 file here that was identical to the incoming one")
        self.assertEqual(self.laptop.git("tools/skills", "status", "--porcelain"), "")

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


LOGIN_CHECK = """#!/bin/sh
if [ -s "$HOME/.missing" ]; then echo "no login here for: $(tr '\\n' ' ' < "$HOME/.missing")"; exit 1; fi
echo "every login works"
"""
# Logs in the first two missing identities, or refuses when .refuse exists.
LOGIN = """#!/bin/sh
echo run >> "$HOME/.login-count"
if [ -e "$HOME/.refuse" ]; then echo "password=hunter2 refused" >&2; exit 4; fi
tail -n +3 "$HOME/.missing" > "$HOME/.missing.new"; mv "$HOME/.missing.new" "$HOME/.missing"
"""


class Setups(ClusterTest):
    def start(self, *missing, fail=False):
        self.world.config(
            '[[setup]]\nname = "toolchain"\ncheck = "sh .setup-check"\nsetup = "sh .setup"\n',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        self.laptop.path(".setup-check").write_text(
            'if [ -s "$HOME/.missing" ]; then echo "not set up here: $(tr \'\\n\' \' \' < "$HOME/.missing")"; exit 1; fi\necho "everything is in place"\n')
        self.laptop.path(".setup").write_text(
            ("exit 3\n" if fail else "") + 'echo run >> "$HOME/.setup-count"\nsed 1d "$HOME/.missing" > "$HOME/.missing.next"; mv "$HOME/.missing.next" "$HOME/.missing"\n')
        self.laptop.path(".missing").write_text("".join(f"{name}\n" for name in missing))
        return self.laptop.sync()

    def runs(self):
        path = self.laptop.path(".setup-count")
        return len(path.read_text().split()) if path.exists() else 0

    def test_something_in_place_is_left_alone(self):
        self.start()
        self.assertState(self.laptop, "toolchain", "ok", "everything is in place")
        self.assertEqual(self.runs(), 0)

    def test_what_is_missing_is_set_up_a_part_at_a_time(self):
        self.start("sdk", "simulator")
        self.assertState(self.laptop, "toolchain", "changed", "set up part; not set up here: simulator")
        self.laptop.sync()
        self.assertState(self.laptop, "toolchain", "changed", "set up; everything is in place")
        self.laptop.sync()
        self.assertState(self.laptop, "toolchain", "ok")
        self.assertEqual(self.runs(), 2)

    def test_a_failed_setup_waits_before_trying_again(self):
        self.start("sdk", fail=True)
        self.assertState(self.laptop, "toolchain", "failed", "not set up here: sdk; setting up exited 3")
        self.laptop.sync()
        self.assertState(self.laptop, "toolchain", "failed", "setting up failed at")

    def test_dry_run_only_reports(self):
        self.world.config(
            '[[setup]]\nname = "toolchain"\ncheck = "false"\nsetup = "touch $HOME/.ran"\n',
            {"laptop": 'mode = "dry-run"'},
        )
        self.laptop = self.world.machine("laptop")
        self.laptop.sync()
        self.assertState(self.laptop, "toolchain", "would-change", "would set up")
        self.assertFalse(self.laptop.path(".ran").exists())


class Logins(ClusterTest):
    def start(self, *missing, machine='mode = "on"'):
        self.world.config(
            '[[login]]\nname = "chat"\ncheck = "sh .login-check"\nlogin = "sh .login"\n',
            {"laptop": machine},
        )
        self.laptop = self.world.machine("laptop")
        self.laptop.path(".login-check").write_text(LOGIN_CHECK)
        self.laptop.path(".login").write_text(LOGIN)
        self.laptop.path(".missing").write_text("".join(f"{name}\n" for name in missing))
        return self.laptop.sync()

    def logins(self):
        path = self.laptop.path(".login-count")
        return len(path.read_text().split()) if path.exists() else 0

    def test_a_working_check_is_left_alone(self):
        self.start()
        self.assertState(self.laptop, "chat", "ok", "every login works")
        self.assertEqual(self.logins(), 0)

    def test_a_missing_login_is_made(self):
        self.start("ada")
        self.assertState(self.laptop, "chat", "changed", "logged in; every login works")
        self.laptop.sync()
        self.assertState(self.laptop, "chat", "ok")
        self.assertEqual(self.logins(), 1)

    def test_logins_made_a_few_at_a_time_carry_on_each_run(self):
        self.start("ada", "bo", "cy", "di", "ed")
        self.assertState(self.laptop, "chat", "changed", "logged in some; no login here for: cy di ed")
        self.laptop.sync()
        self.assertState(self.laptop, "chat", "changed", "logged in some; no login here for: ed")
        self.laptop.sync()
        self.assertState(self.laptop, "chat", "changed", "logged in; every login works")
        self.assertEqual(self.logins(), 3)

    def test_a_refused_login_waits_before_trying_again(self):
        self.start_refusing("ada")
        item = self.assertState(self.laptop, "chat", "failed", "no login here for: ada; logging in exited 4")
        self.assertIn("password=***", item["detail"])
        self.assertNotIn("hunter2", json.dumps(self.laptop.report()))
        self.laptop.sync()
        self.assertState(self.laptop, "chat", "failed", "is tried again after")
        self.assertEqual(self.logins(), 1)
        self.laptop.sync(now=self.laptop.report()["finished"] + 4000)
        self.assertState(self.laptop, "chat", "failed", "logging in exited 4")
        self.assertEqual(self.logins(), 2)

    def start_refusing(self, *missing):
        self.world.config(
            '[[login]]\nname = "chat"\ncheck = "sh .login-check"\nlogin = "sh .login"\n',
            {"laptop": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        self.laptop.path(".login-check").write_text(LOGIN_CHECK)
        self.laptop.path(".login").write_text(LOGIN)
        self.laptop.path(".refuse").write_text("")
        self.laptop.path(".missing").write_text("".join(f"{name}\n" for name in missing))
        return self.laptop.sync()

    def test_a_new_login_to_make_is_tried_at_once(self):
        self.start_refusing("ada")
        self.laptop.path(".refuse").unlink()
        self.laptop.path(".missing").write_text("ada\nbo\n")
        self.laptop.sync()
        self.assertState(self.laptop, "chat", "changed", "logged in; every login works")

    def test_a_login_that_changes_nothing_fails(self):
        self.start("ada")
        self.laptop.path(".login").write_text('#!/bin/sh\necho run >> "$HOME/.login-count"\n')
        self.laptop.path(".missing").write_text("bo\n")
        self.laptop.sync()
        self.assertState(self.laptop, "chat", "failed", "no login here for: bo; logging in changed nothing")

    def test_dry_run_only_reports(self):
        self.start("ada", machine='mode = "dry-run"')
        self.assertState(self.laptop, "chat", "would-change", "no login here for: ada; would log in")
        self.assertEqual(self.logins(), 0)

    def test_the_config_needs_both_commands(self):
        root = self.world.root / "bad-config"
        (root / "machines").mkdir(parents=True)
        (root / "cluster.toml").write_text('[[login]]\nname = "chat"\ncheck = "true"\nlogin_timeout = 0\n')
        (root / "machines" / "laptop.toml").write_text("")
        with self.assertRaises(configs.ConfigError) as caught:
            configs.load(root)
        self.assertIn("cluster.toml: login chat: missing 'login'", caught.exception.errors)
        self.assertIn("cluster.toml: login chat: login_timeout must be a whole number of seconds", caught.exception.errors)


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

    def test_a_file_this_machine_keeps_for_itself_doesnt_stop_the_retry(self):
        # Something on this machine keeps writing its own ledger in the checkout; no decrypt writes it.
        self.world.commit("config", {"cluster.toml": (
            '[secrets]\npath = "~/secrets"\nurl = "' + str(self.world.github / "secrets.git") + '"\n'
            'check = "./bin/secrets check"\ndecrypt = "./bin/secrets decrypt"\nown = ["local/ledger-{machine}.json"]\n'
        )})
        self.laptop.path(".fail-decrypt").write_text("")
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.path("secrets/local/ledger-laptop.json").write_text("{}\n")
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "failed", "decrypting failed")
        self.laptop.path("secrets/local/ledger-laptop.json").write_text('{"logins": 1}\n')
        self.laptop.path(".fail-decrypt").unlink()
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "changed", "retried decrypting")
        self.assertEqual(self.laptop.path("secrets/local/ledger-laptop.json").read_text(), '{"logins": 1}\n')

        # Another machine's ledger is not this machine's own.
        self.laptop.path(".fail-decrypt").write_text("")
        self.world.commit("secrets", {"encrypted/other.env.enc": base64.b64encode(b"OTHER=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "failed", "decrypting failed")
        self.laptop.path("secrets/local/ledger-server.json").write_text("{}\n")
        self.laptop.path(".fail-decrypt").unlink()
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "blocked", "has changed since")

    def test_a_failed_decrypt_someone_finished_by_hand_is_behind_us(self):
        self.laptop.path(".fail-decrypt").write_text("")
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "failed", "decrypting failed")
        self.laptop.path(".fail-decrypt").unlink()
        subprocess.run(["./bin/secrets", "decrypt"], cwd=self.laptop.path("secrets"), env=self.laptop.env(), check=True, capture_output=True)
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "ok", "up to date, and its check passes")
        self.assertEqual(self.laptop.path("secrets/local/new.env").read_text(), "NEW=1\n")
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "ok")
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


class PublishedSecrets(ClusterTest):
    """A secrets repo with a publish command: this machine's edits go out, origin's come in."""

    EDITED = f"TOKEN={SECRET_VALUE}-edited\n"

    def setUp(self):
        super().setUp()
        encoded = base64.b64encode(f"TOKEN={SECRET_VALUE}\n".encode()).decode()
        self.world.remote("secrets", {"bin/secrets": FAKE_SECRETS, "encrypted/app.env.enc": encoded, ".gitignore": "local/\n"})
        self.world.config(self.cluster("./bin/secrets publish"), {"laptop": 'mode = "on"'})
        self.laptop = self.world.machine("laptop")
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "changed", "cloned it and decrypted")

    def cluster(self, publish):
        return (
            '[secrets]\npath = "~/secrets"\nurl = "' + str(self.world.github / "secrets.git") + '"\n'
            f'check = "./bin/secrets check"\ndecrypt = "./bin/secrets decrypt"\npublish = "{publish}"\n'
        )

    def configure(self, publish):
        self.world.commit("config", {"cluster.toml": self.cluster(publish)})

    def origin_has(self, name, content):
        shown = git(self.world.root, "--git-dir", str(self.world.github / "secrets.git"), "show", f"main:encrypted/{name}.enc")
        return base64.b64decode(shown).decode() == content

    def test_up_to_date_is_ok(self):
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "ok", "up to date, and its check passes")

    def test_publishes_a_local_edit(self):
        self.laptop.path("secrets/local/app.env").write_text(self.EDITED)
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "changed", "published 1 changed file; pushed 1 commit; its check passes")
        self.assertTrue(self.origin_has("app.env", self.EDITED))
        self.assertIn("Publish what changed on laptop", git(self.world.root, "--git-dir", str(self.world.github / "secrets.git"), "log", "-1", "--format=%B"))
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "ok")
        self.assertNoLeak()

    def test_pulls_and_decrypts_what_origin_has(self):
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "changed", "pulled 1 commit and decrypted; its check passes")
        self.assertEqual(self.laptop.path("secrets/local/new.env").read_text(), "NEW=1\n")

    def test_publishes_and_pulls_in_one_run(self):
        self.laptop.path("secrets/local/app.env").write_text(self.EDITED)
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "changed", "published 1 changed file; merged 1 commit from origin; pushed 2 commits and decrypted")
        self.assertTrue(self.origin_has("app.env", self.EDITED))
        self.assertTrue(self.origin_has("new.env", "NEW=1\n"))
        self.assertEqual(self.laptop.path("secrets/local/new.env").read_text(), "NEW=1\n")
        self.assertEqual(self.laptop.path("secrets/local/app.env").read_text(), self.EDITED)

    def test_the_same_file_changed_on_both_sides_blocks(self):
        self.laptop.path("secrets/local/app.env").write_text(self.EDITED)
        theirs = f"TOKEN={SECRET_VALUE}-rotated\n"
        before = self.world.commit("secrets", {"encrypted/app.env.enc": base64.b64encode(theirs.encode()).decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "blocked", "merging them conflicts in encrypted/app.env.enc; nothing was changed")
        self.assertEqual(self.laptop.item("secrets")["detail"], "encrypted/app.env.enc")
        self.assertEqual(self.world.head("secrets"), before)
        self.assertEqual(self.laptop.path("secrets/local/app.env").read_text(), self.EDITED)
        self.assertNoLeak()
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "blocked", "conflicts in encrypted/app.env.enc")

    def test_a_failed_publish_commits_nothing(self):
        before = self.laptop.git("secrets", "rev-parse", "HEAD")
        self.laptop.path("secrets/local/app.env").write_text(self.EDITED)
        self.laptop.path(".fail-publish").write_text("")
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "failed", "its publish command failed (exit 1); nothing was committed")
        self.assertEqual(self.laptop.git("secrets", "rev-parse", "HEAD"), before)
        self.assertEqual(self.world.head("secrets"), before)
        self.assertNoLeak()

    def test_a_check_that_still_fails_after_publishing_stops_before_pulling(self):
        self.configure("true")
        before = self.laptop.git("secrets", "rev-parse", "HEAD")
        self.laptop.path("secrets/local/app.env").write_text(self.EDITED)
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "blocked", "its check still fails after publishing")
        self.assertEqual(self.laptop.git("secrets", "rev-parse", "HEAD"), before)
        self.assertFalse(self.laptop.path("secrets/local/new.env").exists())
        self.assertEqual(self.laptop.path("secrets/local/app.env").read_text(), self.EDITED)
        self.assertNoLeak()

    def test_dry_run_changes_nothing(self):
        before = self.laptop.git("secrets", "rev-parse", "HEAD")
        self.laptop.path("secrets/local/app.env").write_text(self.EDITED)
        origin = self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "secrets", "would-change", "would publish what changed on this machine, then pull 1 commit and decrypt")
        self.assertEqual(self.laptop.git("secrets", "rev-parse", "HEAD"), before)
        self.assertEqual(self.world.head("secrets"), origin)
        self.assertEqual(self.laptop.git("secrets", "status", "--porcelain"), "")
        self.assertFalse(self.laptop.path("secrets/local/new.env").exists())
        self.assertEqual(self.laptop.path("secrets/local/app.env").read_text(), self.EDITED)

    def test_a_failed_decrypt_whose_edit_was_published_by_hand_is_behind_us(self):
        # Home on November 9: the decrypt wouldn't overwrite a file this machine had changed,
        # then the fixer published that file and pushed. The next run is simply up to date.
        self.laptop.path(".fail-decrypt").write_text("")
        self.world.commit("secrets", {"encrypted/new.env.enc": base64.b64encode(b"NEW=1\n").decode()})
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "failed", "decrypting failed")
        self.laptop.path(".fail-decrypt").unlink()
        secrets, env = self.laptop.path("secrets"), self.laptop.env()
        subprocess.run(["./bin/secrets", "decrypt"], cwd=secrets, env=env, check=True, capture_output=True)
        self.laptop.path("secrets/local/app.env").write_text(self.EDITED)
        subprocess.run(["./bin/secrets", "publish"], cwd=secrets, env=env, check=True, capture_output=True)
        self.laptop.git("secrets", "commit", "--quiet", "-am", "Publish what changed on laptop")
        self.laptop.git("secrets", "push", "--quiet", "origin", "main")
        self.laptop.sync()
        self.assertState(self.laptop, "secrets", "ok", "up to date, and its check passes")
        self.assertTrue(self.origin_has("app.env", self.EDITED))
        self.assertNoLeak()

    def test_a_failed_decrypt_is_retried_before_anything_is_published(self):
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

    assertNoLeak = Secrets.assertNoLeak


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
