"""Settings files and CLI versions copied from the machine the others follow."""

import json
import os
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from devcluster import config as configs
from devcluster.items import settings
from tests.fakes import ClusterTest

CODEX = """\
model = "big"
notify = ["HOME/bin/notify"]

[mcp_servers.db]
command = "HOME/bin/db"

[projects."HOME/work/a"]
trust_level = "trusted"

[hooks.state."HOME/.codex/config.toml:start:0:0"]
trusted_hash = "sha256:source"
"""

ITEMS = """\
[devcluster]
follow_folder = "~/shared"

[[settings]]
name = "codex-config"
file = "~/.codex/config.toml"
from = "laptop"
own = ["projects.*", "hooks.state.*"]

[[settings]]
name = "claude-servers"
file = "~/.claude.json"
from = "laptop"
only = ["mcpServers"]

[[settings]]
name = "hook-script"
file = "~/.codex/hook.py"
from = "laptop"
"""


class Settings(ClusterTest):
    def start(self, extra="", server_mode="on", items=ITEMS):
        self.world.config(items + extra, {"laptop": 'mode = "on"', "server": f'mode = "{server_mode}"'})
        self.laptop = self.world.machine("laptop")
        self.server = self.world.machine("server")

    def write(self, machine, rel, text):
        path = machine.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.replace("HOME", str(machine.home)))
        return path

    def carry(self):
        """What the secrets repo does between runs: the laptop's copies reach the server."""
        shutil.rmtree(self.server.path("shared"), ignore_errors=True)
        shutil.copytree(self.laptop.path("shared"), self.server.path("shared"))

    def source_files(self):
        self.write(self.laptop, ".codex/config.toml", CODEX)
        self.write(self.laptop, ".claude.json", json.dumps({"mcpServers": {"db": {"command": "HOME/bin/db"}}, "numStartups": 9}))
        self.write(self.laptop, ".codex/hook.py", "print('HOME')\n")

    def test_the_server_gets_the_laptops_settings_and_keeps_its_own_entries(self):
        self.start()
        self.source_files()
        self.write(self.server, ".codex/config.toml", 'model = "small"\n\n[mcp_servers.old]\ncommand = "x"\n\n[projects."HOME/work/b"]\ntrust_level = "trusted"\n\n[hooks.state."HOME/.codex/config.toml:start:0:0"]\ntrusted_hash = "sha256:server"\n')
        self.write(self.server, ".claude.json", json.dumps({"mcpServers": {"old": {}}, "oauthAccount": {"id": 1}}))
        self.laptop.sync()
        self.assertState(self.laptop, "codex-config", "changed", "saved this copy for the other machines")
        self.assertNotIn(str(self.laptop.home), self.laptop.path("shared/codex-config").read_text())
        self.assertEqual(json.loads(self.laptop.path("shared/claude-servers").read_text()), {"mcpServers": {"db": {"command": "{{home}}/bin/db"}}})
        self.laptop.sync()
        self.assertState(self.laptop, "codex-config", "ok", "the other machines follow this copy")

        self.server.sync()
        self.assertState(self.server, "codex-config", "busy", "waiting for laptop's copy")
        self.carry()
        self.server.sync()
        item = self.assertState(self.server, "codex-config", "changed", "replaced it with laptop's copy")
        self.assertIn("state/settings/codex-config.before", item["summary"])
        codex = tomllib.loads(self.server.path(".codex/config.toml").read_text())
        home = str(self.server.home)
        self.assertEqual(codex["model"], "big")
        self.assertEqual(codex["notify"], [f"{home}/bin/notify"])
        self.assertEqual(set(codex["mcp_servers"]), {"db"})
        self.assertEqual(set(codex["projects"]), {f"{home}/work/a", f"{home}/work/b"})
        self.assertEqual(codex["hooks"]["state"][f"{home}/.codex/config.toml:start:0:0"]["trusted_hash"], "sha256:server")
        claude = json.loads(self.server.path(".claude.json").read_text())
        self.assertEqual(claude, {"mcpServers": {"db": {"command": f"{home}/bin/db"}}, "oauthAccount": {"id": 1}})
        self.assertEqual(self.server.path(".codex/hook.py").read_text(), f"print('{home}')\n")
        self.assertState(self.server, "hook-script", "changed", "wrote laptop's copy")
        self.server.sync()
        for name in ("codex-config", "claude-servers", "hook-script"):
            self.assertState(self.server, name, "ok", "matches laptop's copy")

    def test_an_edit_on_the_server_is_replaced_and_the_first_copy_is_kept(self):
        self.start()
        self.source_files()
        original = self.write(self.server, ".codex/hook.py", "original\n")
        self.laptop.sync()
        self.carry()
        self.server.sync()
        original.write_text("edited\n")
        self.server.sync()
        self.assertState(self.server, "hook-script", "changed", "replaced it with laptop's copy")
        self.assertEqual(original.read_text(), f"print('{self.server.home}')\n")
        self.assertEqual(self.server.path(".devcluster/state/settings/hook-script.before").read_text(), "original\n")

    def test_a_change_on_the_laptop_reaches_the_server(self):
        self.start()
        self.source_files()
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.write(self.laptop, ".codex/config.toml", CODEX.replace('"big"', '"bigger"'))
        self.laptop.sync()
        self.assertState(self.laptop, "codex-config", "changed", "saved this copy")
        self.carry()
        self.server.sync()
        self.assertState(self.server, "codex-config", "changed", "replaced it with laptop's copy")
        self.assertEqual(tomllib.loads(self.server.path(".codex/config.toml").read_text())["model"], "bigger")

    def test_a_linked_file_is_written_through_its_link(self):
        self.start()
        self.source_files()
        real = self.write(self.server, "launcher/config.toml", 'model = "small"\n')
        os.chmod(real, 0o600)
        self.server.path(".codex").mkdir()
        self.server.path(".codex/config.toml").symlink_to(real)
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertState(self.server, "codex-config", "changed")
        self.assertTrue(self.server.path(".codex/config.toml").is_symlink())
        self.assertEqual(tomllib.loads(real.read_text())["model"], "big")
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)

    def test_a_new_script_can_run(self):
        self.start()
        self.source_files()
        self.write(self.laptop, ".codex/hook.py", "#!/bin/sh\necho hi\n")
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertEqual(self.server.path(".codex/hook.py").stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.server.path(".codex/config.toml").stat().st_mode & 0o777, 0o600)

    def test_dry_run_changes_nothing(self):
        self.start(server_mode="dry-run")
        self.source_files()
        self.write(self.server, ".codex/hook.py", "mine\n")
        self.laptop.sync("--dry-run")
        self.assertState(self.laptop, "codex-config", "would-change", "would save this copy")
        self.assertFalse(self.laptop.path("shared").exists())
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertState(self.server, "hook-script", "would-change", "would replace it with laptop's copy")
        self.assertEqual(self.server.path(".codex/hook.py").read_text(), "mine\n")

    def test_a_broken_file_on_the_laptop_is_not_shared(self):
        self.start()
        self.source_files()
        self.laptop.sync()
        before = self.laptop.path("shared/codex-config").read_text()
        self.write(self.laptop, ".codex/config.toml", "model = \n")
        self.laptop.sync()
        self.assertState(self.laptop, "codex-config", "failed", "isn't valid TOML, so the other machines keep the last copy")
        self.assertEqual(self.laptop.path("shared/codex-config").read_text(), before)

    def test_a_server_file_that_does_not_read_is_replaced_whole(self):
        self.start()
        self.source_files()
        self.write(self.server, ".codex/config.toml", "[broken\n")
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertState(self.server, "codex-config", "changed")
        self.assertEqual(set(tomllib.loads(self.server.path(".codex/config.toml").read_text())["projects"]), {f"{self.server.home}/work/a"})

    def test_a_json_file_that_does_not_read_is_left_alone(self):
        self.start()
        self.source_files()
        self.write(self.server, ".claude.json", "{not json")
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertState(self.server, "claude-servers", "failed", "isn't valid JSON, so it was left alone")
        self.assertEqual(self.server.path(".claude.json").read_text(), "{not json")

    def test_a_json_entry_the_machine_keeps(self):
        self.start(items=ITEMS.replace('only = ["mcpServers"]\n', 'only = ["mcpServers"]\nown = ["mcpServers.neo"]\n'))
        self.write(self.laptop, ".claude.json", json.dumps({"mcpServers": {"db": {}}}))
        self.write(self.server, ".claude.json", json.dumps({"mcpServers": {"old": {}, "neo": {"url": "9010"}}, "theme": "dark"}))
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertEqual(json.loads(self.server.path(".claude.json").read_text()), {"mcpServers": {"db": {}, "neo": {"url": "9010"}}, "theme": "dark"})
        self.server.sync()
        self.assertState(self.server, "claude-servers", "ok", "matches laptop's copy")

    def test_a_key_the_laptop_drops_is_dropped(self):
        self.start()
        self.write(self.laptop, ".claude.json", json.dumps({"numStartups": 1}))
        self.write(self.server, ".claude.json", json.dumps({"mcpServers": {"old": {}}, "theme": "dark"}))
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertEqual(json.loads(self.server.path(".claude.json").read_text()), {"theme": "dark"})


class FollowedVersion(ClusterTest):
    def start(self):
        cli = 'check = "cat $HOME/.fake-cli"\ninstall = \'echo "fakecli {version}" > "$HOME/.fake-cli"\'\n'
        self.world.config(
            f'[devcluster]\nfollow_folder = "~/shared"\n\n[[cli]]\nname = "fakecli"\nfrom = "laptop"\n{cli}',
            {"laptop": 'mode = "on"', "server": 'mode = "on"'},
        )
        self.laptop = self.world.machine("laptop")
        self.server = self.world.machine("server")

    def carry(self):
        shutil.rmtree(self.server.path("shared"), ignore_errors=True)
        shutil.copytree(self.laptop.path("shared"), self.server.path("shared"))

    def test_the_server_installs_the_laptops_version(self):
        self.start()
        self.laptop.path(".fake-cli").write_text("fakecli 0.163.0-alpha.5\n")
        self.server.path(".fake-cli").write_text("fakecli 0.167.0\n")
        self.laptop.sync()
        self.assertState(self.laptop, "fakecli", "changed", "at 0.163.0-alpha.5; saved it for the other machines")
        self.assertEqual(self.laptop.path(".fake-cli").read_text(), "fakecli 0.163.0-alpha.5\n")
        self.server.sync()
        self.assertState(self.server, "fakecli", "busy", "waiting for laptop's version")
        self.carry()
        self.server.sync()
        self.assertState(self.server, "fakecli", "changed", "installed 0.163.0-alpha.5 (had 0.167.0)")
        self.server.sync()
        self.assertState(self.server, "fakecli", "ok", "at 0.163.0-alpha.5, the same as laptop")
        self.laptop.path(".fake-cli").write_text("fakecli 0.170.0\n")
        self.laptop.sync()
        self.carry()
        self.server.sync()
        self.assertState(self.server, "fakecli", "changed", "installed 0.170.0")

    def test_a_copy_that_is_not_a_version_is_not_installed(self):
        self.start()
        self.server.path("shared").mkdir()
        self.server.path("shared/fakecli").write_text("1.0; rm -rf ~\n")
        self.server.sync()
        self.assertState(self.server, "fakecli", "failed", "which isn't a version")
        self.assertFalse(self.server.path(".fake-cli").exists())


class Merge(ClusterTest):
    def test_brackets_inside_strings_and_arrays_are_not_tables(self):
        source = 'a = """\n[not.a.table]\n"""\nb = [\n[1, 2],\n]\n\n[projects."x"]\nt = 1\n\n[c]\nd = \'[e]\'\n'
        local = '[projects."y"]\nt = 2\n'
        merged = settings.merge_toml(source, local, ["projects.*"])
        data = tomllib.loads(merged)
        self.assertEqual(data["a"], "[not.a.table]\n")
        self.assertEqual(data["b"], [[1, 2]])
        self.assertEqual(data["projects"], {"x": {"t": 1}, "y": {"t": 2}})
        self.assertEqual(data["c"], {"d": "[e]"})

    def test_extra_quotes_closing_a_string_and_quotes_in_a_comment(self):
        local = 'a = """a"""" # " """\nb = """\n[unterminated\n"""\n[projects.local]\ntrust_level = "trusted"\n'
        merged = settings.merge_toml('[projects.source]\ntrust_level = "trusted"\n', local, ["projects.*"])
        self.assertEqual(set(tomllib.loads(merged)["projects"]), {"source", "local"})

    def test_a_file_that_reads_but_cannot_be_split_is_left_alone(self):
        original = settings.blocks

        def failing(text):
            if "local" in text:
                raise settings.Unusable("can't split")
            return original(text)

        settings.blocks = failing
        try:
            with self.assertRaises(settings.Unusable):
                settings.merge_toml("[projects.source]\nt = 1\n", "[projects.local]\nt = 2\n", ["projects.*"])
        finally:
            settings.blocks = original

    def test_one_named_entry_is_kept_and_the_rest_is_the_sources(self):
        source = '[mcp_servers.db]\nurl = "db"\n\n[mcp_servers.browser]\nurl = "9000"\n'
        local = '[mcp_servers.old]\nurl = "old"\n\n[mcp_servers.neo]\nurl = "9010"\n\n[mcp_servers.neo.env]\nA = "1"\n'
        data = tomllib.loads(settings.merge_toml(source, local, ["mcp_servers.neo"]))
        self.assertEqual(data["mcp_servers"], {"db": {"url": "db"}, "browser": {"url": "9000"}, "neo": {"url": "9010", "env": {"A": "1"}}})

    def test_a_machines_own_entry_replaces_the_sources_whole(self):
        source = '[projects."x"]\nt = 1\n\n[projects."x".deep]\nu = 1\n\n[other]\nv = 1\n'
        local = '[projects."x"]\nt = 9\n'
        data = tomllib.loads(settings.merge_toml(source, local, ["projects.*"]))
        self.assertEqual(data["projects"], {"x": {"t": 9}})
        self.assertEqual(data["other"], {"v": 1})


class Validation(ClusterTest):
    def check(self, cluster):
        root = self.world.root / f"config-{len(list(self.world.root.glob('config-*')))}"
        (root / "machines").mkdir(parents=True)
        (root / "cluster.toml").write_text(cluster)
        for name in ("laptop", "server"):
            (root / "machines" / f"{name}.toml").write_text("")
        with self.assertRaises(configs.ConfigError) as caught:
            configs.load(root)
        return "\n".join(caught.exception.errors)

    def test_cli_needs_a_version_or_from_but_not_both(self):
        both = '[devcluster]\nfollow_folder = "~/s"\n[[cli]]\nname = "c"\nversion = "1.0"\nfrom = "laptop"\ncheck = "c"\ninstall = "i"\n'
        self.assertIn("give either a version or from", self.check(both))
        neither = '[[cli]]\nname = "c"\ncheck = "c"\ninstall = "i"\n'
        self.assertIn("give either a version or from", self.check(neither))

    def test_settings_fields_fit_the_file(self):
        self.assertIn("own names entries in a TOML or JSON file", self.check('[devcluster]\nfollow_folder = "~/s"\n[[settings]]\nname = "s"\nfile = "~/a.py"\nfrom = "laptop"\nown = ["projects.*"]\n'))
        self.assertIn("with * for any one key", self.check('[devcluster]\nfollow_folder = "~/s"\n[[settings]]\nname = "s"\nfile = "~/a.toml"\nfrom = "laptop"\nown = ["projects.a b"]\n'))
        self.assertIn("only names the keys", self.check('[devcluster]\nfollow_folder = "~/s"\n[[settings]]\nname = "s"\nfile = "~/a.toml"\nfrom = "laptop"\nonly = ["k"]\n'))
        self.assertIn("which has no machines/desk.toml", self.check('[devcluster]\nfollow_folder = "~/s"\n[[settings]]\nname = "s"\nfile = "~/a.toml"\nfrom = "desk"\n'))
        self.assertIn("needs follow_folder", self.check('[[settings]]\nname = "s"\nfile = "~/a.toml"\nfrom = "laptop"\n'))


class ChangedMeanwhile(unittest.TestCase):
    def test_a_save_made_while_updating_is_not_undone(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "shared").mkdir()
            (folder / "shared" / "claude-servers").write_text('{"mcpServers": {"db": {}}}')
            target = folder / "claude.json"
            target.write_text('{"numStartups": 1}')
            ctx = SimpleNamespace(machine="server", dry_run=False, root=folder / "dc",
                                  config=SimpleNamespace(setting=lambda key, default=None: str(folder / "shared") if key == "follow_folder" else default))
            item = configs.Item("settings", "claude-servers", {"file": str(target), "from": "laptop", "only": ["mcpServers"]}, "test")
            original = settings._wanted

            def meanwhile(*args):
                target.write_text('{"numStartups": 2}')  # Claude Code saves while devcluster works
                return original(*args)

            settings._wanted = meanwhile
            try:
                outcome = settings.sync_settings(item, ctx)
            finally:
                settings._wanted = original
            self.assertEqual(outcome.state, "busy")
            self.assertEqual(json.loads(target.read_text()), {"numStartups": 2})
            self.assertEqual(settings.sync_settings(item, ctx).state, "changed")
            self.assertEqual(json.loads(target.read_text()), {"numStartups": 2, "mcpServers": {"db": {}}})
