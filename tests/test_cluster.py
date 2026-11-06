"""A whole fake cluster: three machines working at once until they all agree."""

import base64

from tests.fakes import FAKE_SECRETS, ClusterTest, git
from tests.test_items import INSTALL


class WholeCluster(ClusterTest):
    def test_three_machines_converge(self):
        world = self.world
        notes = world.remote("notes", {"README.md": "notes\n"})
        work = world.remote("work", {"README.md": "work\n"})
        skills = world.remote("skills", {"skill.txt": "v1\n", "install.sh": f"#!/bin/sh\n{INSTALL}\n"})
        secrets = world.remote("secrets", {"bin/secrets": FAKE_SECRETS, "encrypted/a.env.enc": base64.b64encode(b"A=1\n").decode(), ".gitignore": "local/\n"})
        world.config(
            f"""\
            [secrets]
            path = "~/secrets"
            url = "{secrets}"
            check = "./bin/secrets check"
            decrypt = "./bin/secrets decrypt"

            [[repo]]
            path = "~/workspace/notes"
            url = "{notes}"

            [[repo]]
            path = "~/workspace/work"
            url = "{work}"
            on = ["laptop", "server"]

            [[tool]]
            name = "skills"
            path = "~/workspace/skills"
            url = "{skills}"
            install = "sh install.sh"

            [[cli]]
            name = "fakecli"
            version = "3.1.0"
            check = "cat $HOME/.fake-cli"
            install = 'echo "{{version}}" > "$HOME/.fake-cli"'

            [[file]]
            name = "rules"
            source = "files/AGENTS.md"
            targets = ["~/.codex/AGENTS.md"]
            """,
            {"laptop": 'mode = "on"', "server": 'mode = "on"', "studio": 'mode = "on"'},
            {"files/AGENTS.md": "Put heavy work on the server.\n"},
        )
        machines = [world.machine(name) for name in ("laptop", "server", "studio")]
        for machine in machines:
            report = machine.sync()
            states = {i["name"]: i["state"] for i in report["items"]}
            self.assertNotIn("failed", states.values(), report)
            self.assertNotIn("blocked", states.values(), report)

        laptop, server, studio = machines
        self.assertFalse(studio.path("workspace/work").exists(), "work is only on the laptop and server")

        # Everyone commits to notes at once; the laptop and server also commit to work.
        made = [m.commit("workspace/notes", {f"{m.name}.txt": m.name + "\n"}) for m in machines]
        made += [m.commit("workspace/work", {f"{m.name}.txt": m.name + "\n"}) for m in (laptop, server)]
        world.commit("skills", {"skill.txt": "v2\n"})
        world.commit("config", {"files/AGENTS.md": "Put heavy work on the server or the studio.\n"})

        for _ in range(2):
            for machine in machines:
                machine.sync()

        notes_heads = {m.git("workspace/notes", "rev-parse", "HEAD") for m in machines} | {world.head("notes")}
        work_heads = {m.git("workspace/work", "rev-parse", "HEAD") for m in (laptop, server)} | {world.head("work")}
        self.assertEqual(len(notes_heads), 1, "every machine has the same notes")
        self.assertEqual(len(work_heads), 1, "the laptop and server have the same work")
        for commit in made:
            git(world.root, "--git-dir", str(world.github / ("notes.git" if commit in made[:3] else "work.git")), "merge-base", "--is-ancestor", commit, "main")
        for machine in machines:
            self.assertEqual(machine.path(".skills/skill.txt").read_text(), "v2\n")
            self.assertEqual(machine.path(".fake-cli").read_text().strip(), "3.1.0")
            self.assertEqual(machine.path(".codex/AGENTS.md").read_text(), "Put heavy work on the server or the studio.\n")
            self.assertEqual(machine.path("secrets/local/a.env").read_text(), "A=1\n")
            states = {i["state"] for i in machine.report()["items"]}
            self.assertLessEqual(states, {"ok", "changed"}, machine.report())

        shown = laptop.run("status").stdout
        for machine in machines:
            self.assertIn(machine.name, shown)
        self.assertNotIn("stale", shown)
        self.assertNotIn("Needs you", shown)
