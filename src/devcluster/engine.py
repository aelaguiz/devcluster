"""Keep devcluster's own install current: its config checkout and its engine checkout."""

import re

from .git import Git, short
from .paths import engine_root
from .proc import tail
from .results import BLOCKED, BUSY, CHANGED, FAILED, OK, ItemResult, plural


def _result(name, state, summary, detail=""):
    return ItemResult("devcluster", name, state, summary, detail)


def pull_config(layout):
    """Fast-forward the config checkout. This happens in dry run too."""
    git = Git(layout.config)
    if not git.is_checkout():
        return _result("config", FAILED, f"there's no config checkout at {layout.config}; run devcluster install")
    if git.operation_in_progress() or git.index_locked():
        return _result("config", BUSY, "the config checkout is busy; using it as it is")
    branch = git.branch() or "main"
    fetched = git.run("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=300)
    if not fetched.ok:
        return _result("config", FAILED, f"couldn't fetch the config: {fetched.first_line()}; using the copy here", tail(fetched.err))
    head = git.rev("HEAD")
    remote = git.rev(f"refs/remotes/origin/{branch}")
    if head == remote:
        return _result("config", OK, f"up to date at {short(head)}")
    if not git.is_ancestor(head, remote):
        return _result("config", BLOCKED, f"the config checkout here has {plural(git.local_only('HEAD'), 'commit')} origin doesn't have; edit the config in its own checkout and push it")
    moved = git.run("merge", "--ff-only", "--quiet", remote, timeout=300)
    if not moved.ok:
        return _result("config", BLOCKED, f"couldn't update the config checkout: {moved.first_line()}; using it as it is", tail(moved.err))
    return _result("config", CHANGED, f"updated from {short(head)} to {short(remote)}")


def update_engine(layout, config, machine):
    """Move the engine checkout to the version the config names for this machine.

    Returns the result and whether this process should restart on the new version.
    """
    ref = config.machines[machine].engine or config.setting("engine", None)
    running = engine_root()
    git = Git(layout.engine)
    if running.resolve() != layout.engine.resolve() or not git.is_checkout():
        return _result("engine", OK, f"running from {running}, not the managed install, so devcluster isn't updating itself"), False
    head = git.rev("HEAD")
    if not ref:
        return _result("engine", OK, f"at {short(head)}; the config doesn't name a version"), False
    # A full commit ID never moves, so only that needs no fetch. A branch or tag might have.
    target = git.rev(ref) if re.fullmatch(r"[0-9a-fA-F]{40}", ref) else None
    if target is None:
        fetched = git.run("fetch", "--quiet", "--tags", "--force", "origin", "+refs/heads/*:refs/remotes/origin/*", timeout=300)
        if not fetched.ok:
            return _result("engine", FAILED, f"couldn't fetch devcluster version {ref}: {fetched.first_line()}"), False
        target = _resolve(git, ref)
    if target is None:
        return _result("engine", FAILED, f"the config names devcluster version {ref}, which origin doesn't have"), False
    if target == head:
        return _result("engine", OK, f"at {ref} ({short(head)})"), False
    if git.has_local_edits():
        return _result("engine", BLOCKED, f"the engine checkout at {layout.engine} has local edits, so it stays at {short(head)} instead of {ref}"), False
    moved = git.run("checkout", "--quiet", "--detach", target, timeout=300)
    if not moved.ok:
        return _result("engine", FAILED, f"couldn't move to {ref}: {moved.first_line()}"), False
    return _result("engine", CHANGED, f"updated from {short(head)} to {ref} ({short(target)})"), True


def _resolve(git, ref):
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        found = git.rev(ref)
        if found:
            return found
    return git.rev(f"refs/tags/{ref}") or git.rev(f"refs/remotes/origin/{ref}")


def engine_commit():
    return Git(engine_root()).rev("HEAD")
