"""The Git operations devcluster needs, run without prompts and with timeouts."""

import os
import re
from functools import lru_cache
from pathlib import Path

from .proc import run

# Variables a Git hook may export that would point our commands at the wrong repo.
_REPO_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
)


def git_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k not in _REPO_VARIABLES}
    # Never wait on a prompt, and keep messages in English so they can be read.
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANG="C", GIT_MERGE_AUTOEDIT="no")
    if extra:
        env.update(extra)
    return env


@lru_cache(maxsize=1)
def git_version():
    result = run(["git", "--version"], timeout=30, env=git_env())
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", result.out)
    if not result.ok or not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def version_text():
    version = git_version()
    return ".".join(map(str, version)) if version else "not found"


def clone(url, path, branch=None, timeout=3600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone", "--quiet"]
    if branch:
        args += ["--branch", branch]
    return run(args + [url, str(path)], timeout=timeout, env=git_env())


class Git:
    def __init__(self, path, env=None):
        self.path = Path(path)
        self.env = git_env(env)

    def run(self, *args, timeout=300, input=None, env=None):
        merged = dict(self.env, **env) if env else self.env
        return run(["git", "-C", str(self.path), *args], timeout=timeout, env=merged, input=input)

    def out(self, *args, timeout=300):
        result = self.run(*args, timeout=timeout)
        return result.out.strip() if result.ok else None

    def is_checkout(self):
        """True when this folder is the top of a Git working tree, not just inside one."""
        top = self.out("rev-parse", "--show-toplevel", timeout=60)
        return top is not None and os.path.realpath(top) == os.path.realpath(self.path)

    def rev(self, ref):
        return self.out("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", timeout=60)

    def is_ancestor(self, older, newer):
        return self.run("merge-base", "--is-ancestor", older, newer, timeout=120).code == 0

    def count(self, *spec):
        value = self.out("rev-list", "--count", *spec, timeout=300)
        return int(value) if value and value.isdigit() else 0

    def local_only(self, rev="HEAD"):
        """Commits reachable from rev that no remote-tracking branch has."""
        return self.count(rev, "--not", "--remotes")

    def branch(self):
        return self.out("symbolic-ref", "--quiet", "--short", "HEAD", timeout=60)

    def origin_url(self):
        return self.out("remote", "get-url", "origin", timeout=60)

    def git_path(self, name):
        value = self.out("rev-parse", "--git-path", name, timeout=60)
        if value is None:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.path / path

    def operation_in_progress(self):
        for marker, label in (
            ("MERGE_HEAD", "a merge"),
            ("rebase-merge", "a rebase"),
            ("rebase-apply", "a rebase"),
            ("CHERRY_PICK_HEAD", "a cherry-pick"),
            ("REVERT_HEAD", "a revert"),
            ("BISECT_LOG", "a bisect"),
        ):
            path = self.git_path(marker)
            if path is not None and path.exists():
                return label
        return None

    def index_locked(self):
        path = self.git_path("index.lock")
        return path is not None and path.exists()

    def has_local_edits(self):
        """Tracked files changed but not committed."""
        status = self.out("status", "--porcelain", "--untracked-files=no", timeout=300)
        return bool(status)


def short(commit):
    return commit[:7] if commit else "nothing"


def same_remote(a, b):
    return _normal_remote(a) == _normal_remote(b)


def _normal_remote(url):
    url = (url or "").strip()
    if url.startswith("file://"):
        url = url[len("file://"):]
    if url.startswith(("/", "~", ".")):
        return ("local", os.path.realpath(os.path.expanduser(url)).rstrip("/").removesuffix(".git"))
    match = re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", url, re.I)
    if not match:
        match = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", url)
    if not match:
        return ("other", url)
    host, path = match.groups()
    return (host.lower(), path.strip("/").removesuffix(".git").lower())
