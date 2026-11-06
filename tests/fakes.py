"""A fake cluster for tests: bare Git repos on disk stand in for GitHub, and each
"machine" is its own home folder with devcluster installed from this checkout.

Nothing here touches the network, the real home folder, or a real timer.
"""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE_SECRETS = textwrap.dedent('''\
    #!/usr/bin/env python3
    """A stand-in for a SOPS secrets helper: encrypted/NAME.enc holds base64 of local/NAME."""
    import base64, pathlib, sys
    root = pathlib.Path(__file__).resolve().parent.parent
    encrypted, local = root / "encrypted", root / "local"
    action = sys.argv[1]
    if action == "decrypt" and (pathlib.Path.home() / ".fail-decrypt").exists():
        sys.exit("decrypt: key not readable")
    for path in sorted(encrypted.glob("*.enc")):
        plain = base64.b64decode(path.read_bytes())
        target = local / path.stem
        if action == "decrypt":
            local.mkdir(exist_ok=True)
            target.write_bytes(plain)
        elif action == "check" and (not target.exists() or target.read_bytes() != plain):
            print("Local and encrypted copies differ: " + path.stem + " " + target.read_text() if target.exists() else "missing", file=sys.stderr)
            sys.exit(1)
    print(action + ": OK; no secret values printed.")
''')

SECRET_VALUE = "hunter2-do-not-leak"


def git(cwd, *args, env=None, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, env=env or base_env(), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def base_env(home=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "DEVCLUSTER_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
    if home:
        env["HOME"] = str(home)
    else:
        env["GIT_CONFIG_GLOBAL"] = str(ROOT / "tests" / "gitconfig")
    return env


class World:
    def __init__(self, root):
        self.root = Path(root)
        self.github = self.root / "github"
        self.github.mkdir()
        self.work = self.root / "admin"  # where the "person" edits the remotes
        self.work.mkdir()
        self.machines = {}

    # Remotes

    def remote(self, name, files, branch="main"):
        bare = self.github / f"{name}.git"
        git(self.root, "init", "--quiet", "--bare", f"--initial-branch={branch}", str(bare))
        checkout = self.work / name
        git(self.root, "clone", "--quiet", str(bare), str(checkout))
        git(checkout, "checkout", "--quiet", "-B", branch)
        self.write(checkout, files)
        git(checkout, "add", "-A")
        git(checkout, "commit", "--quiet", "-m", f"Start {name}")
        git(checkout, "push", "--quiet", "origin", branch)
        return str(bare)

    def write(self, checkout, files):
        for rel, content in files.items():
            path = Path(checkout) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            if rel.startswith("bin/") or rel.endswith(".sh"):
                path.chmod(0o755)

    def commit(self, name, files, message="Change"):
        """Commit and push to a remote from the admin checkout."""
        checkout = self.work / name
        git(checkout, "pull", "--quiet", "--ff-only")
        self.write(checkout, files)
        git(checkout, "add", "-A")
        git(checkout, "commit", "--quiet", "-m", message)
        git(checkout, "push", "--quiet", "origin", "HEAD")
        return git(checkout, "rev-parse", "HEAD")

    def head(self, name, branch="main"):
        return git(self.root, "--git-dir", str(self.github / f"{name}.git"), "rev-parse", branch)

    def engine(self):
        """A bare repo holding this checkout's engine, so machines install it the real way."""
        bare = self.github / "engine.git"
        if bare.exists():
            return str(bare)
        source = self.work / "engine"
        source.mkdir()
        for part in ("bin", "src"):
            shutil.copytree(ROOT / part, source / part, ignore=shutil.ignore_patterns("__pycache__"))
        git(source, "init", "--quiet", "--initial-branch=main")
        git(source, "add", "-A")
        git(source, "commit", "--quiet", "-m", "devcluster")
        git(self.root, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
        git(source, "remote", "add", "origin", str(bare))
        git(source, "push", "--quiet", "origin", "main")
        return str(bare)

    def config(self, cluster, machines, files=None):
        content = {"cluster.toml": textwrap.dedent(cluster)}
        for name, text in machines.items():
            content[f"machines/{name}.toml"] = textwrap.dedent(text)
        content.update(files or {})
        return self.remote("config", content)

    # Machines

    def machine(self, name, install=True):
        machine = FakeMachine(self, name)
        self.machines[name] = machine
        if install:
            machine.install()
        return machine


class FakeMachine:
    def __init__(self, world, name):
        self.world = world
        self.name = name
        self.home = world.root / "machines" / name
        self.home.mkdir(parents=True)
        (self.home / ".gitconfig").write_text(textwrap.dedent(f"""\
            [user]
            \tname = Agent on {name}
            \temail = agent@{name}.invalid
            [init]
            \tdefaultBranch = main
            [advice]
            \tdetachedHead = false
            """))
        self.dc = self.home / ".devcluster"

    def env(self, **extra):
        env = base_env(self.home)
        env["DEVCLUSTER_HOME"] = str(self.dc)
        env.update(extra)
        return env

    def install(self):
        result = subprocess.run(
            [str(ROOT / "bin" / "devcluster"), "install", "--machine", self.name,
             "--config", str(self.world.github / "config.git"), "--engine-url", self.world.engine(),
             "--bin-dir", str(self.home / "bin"), "--no-schedule"],
            env=self.env(), capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"install failed on {self.name}:\n{result.stdout}{result.stderr}")
        return result

    def run(self, *args, now=None, **extra):
        env = self.env(**extra)
        if now is not None:
            env["DEVCLUSTER_NOW"] = str(now)
        return subprocess.run([str(self.home / "bin" / "devcluster"), *args], env=env, capture_output=True, text=True, cwd=self.home)

    def sync(self, *args, now=None, **extra):
        result = self.run("sync", *args, now=now, **extra)
        if result.returncode not in (0, 1):
            raise AssertionError(f"sync failed on {self.name} (exit {result.returncode}):\n{result.stdout}{result.stderr}")
        return self.report()

    def report(self):
        return json.loads((self.dc / "state" / "last.json").read_text())

    def item(self, name, report=None):
        report = report or self.report()
        found = [i for i in report["items"] if i["name"] == name]
        if not found:
            raise AssertionError(f"no item {name!r} in {[i['name'] for i in report['items']]}")
        return found[0]

    def path(self, rel):
        return self.home / rel

    def git(self, rel, *args, check=True):
        return git(self.path(rel), *args, env=base_env(self.home), check=check)

    def commit(self, rel, files, message="Work"):
        checkout = self.path(rel)
        for name, content in files.items():
            (checkout / name).parent.mkdir(parents=True, exist_ok=True)
            (checkout / name).write_text(content)
        self.git(rel, "add", "-A")
        self.git(rel, "commit", "--quiet", "-m", message)
        return self.git(rel, "rev-parse", "HEAD")


class ClusterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="devcluster-test-")
        self.world = World(os.path.realpath(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def assertState(self, machine, name, state, contains=None):
        item = machine.item(name)
        self.assertEqual(item["state"], state, f"{name} on {machine.name}: {item}")
        if contains:
            self.assertIn(contains, item["summary"])
        return item
