"""Git checkouts: repos worked on directly on a branch, and tools that follow a ref.

A repo syncs both ways: origin's commits come in, this machine's commits go out.
A tool only follows: it moves to the ref the list names and never pushes.

Nothing here force-pushes, rebases, or discards a change. Git's own refusal to
overwrite uncommitted work is passed on as "Needs you", with the file names, unless every
file in the way already holds exactly what the update would write there.
"""

import os
import re
import shutil
import time

from ..context import result
from ..git import Git, clone, git_version, same_remote, short, version_text
from ..paths import clock, expand
from ..proc import shell, tail
from ..results import BLOCKED, BUSY, CHANGED, FAILED, OK, WOULD_CHANGE, plural

FETCH_TIMEOUT = 600
MERGE_TIMEOUT = 900
CLONE_TIMEOUT = 3600
INSTALL_TIMEOUT = 1800
ROUNDS = 3  # tries at pushing when origin keeps moving

# A fast-forward that keeps what Git would otherwise replace without asking: an ignored
# file in the way, and changes a configured autostash would set aside and reapply.
FAST_FORWARD = ("merge", "--ff-only", "--quiet", "--no-overwrite-ignore", "--no-autostash")


def sync_repo(item, ctx):
    path = expand(item.get("path"))
    branch = item.get("branch", "main")
    timeout = item.get("timeout", FETCH_TIMEOUT)

    if not path.exists():
        if ctx.dry_run:
            return result(item, WOULD_CHANGE, f"would clone {item.get('url')} into {item.get('path')}" + _and_install(item))
        cloned = clone(item.get("url"), path, branch, CLONE_TIMEOUT)
        if not cloned.ok:
            return result(item, FAILED, f"couldn't clone it: {cloned.first_line()}", tail(cloned.err))
        return _after_update(item, ctx, Git(path), ["cloned it"])

    git = Git(path, ctx.identity())
    problem = _preflight(item, path, git)
    if problem:
        return problem
    current = git.branch()
    if current != branch:
        return result(item, BLOCKED, f"is checked out on {current or 'a detached commit'}, not {branch}; devcluster syncs {branch} only while it's checked out")

    done = []
    for _ in range(ROUNDS):
        fetched = git.run("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=timeout)
        if not fetched.ok:
            return result(item, FAILED, _prefix(done, f"couldn't fetch from origin: {fetched.first_line()}"), tail(fetched.err))
        local = git.rev(f"refs/heads/{branch}")
        remote = git.rev(f"refs/remotes/origin/{branch}")
        if local is None:
            return result(item, BLOCKED, f"has no commits on {branch} yet")
        if local == remote:
            break
        behind = git.count(f"{local}..{remote}") if remote else 0
        ahead = git.count(f"{remote}..{local}") if remote else git.count(local)

        if ahead == 0:
            if ctx.dry_run:
                return result(item, WOULD_CHANGE, f"would pull {plural(behind, 'commit')} from origin" + _and_install(item))
            switched = _switched(item, git, branch, done)
            if switched:
                return switched
            moved, cleared = update(git, FAST_FORWARD, remote)
            if not moved.ok:
                if _raced(moved):
                    continue
                return _update_failed(item, moved, f"pulling {plural(behind, 'commit')} from origin", done, git, remote)
            switched = _switched(item, git, branch, done, after=True)
            if switched:
                return switched
            done.append(f"pulled {plural(behind, 'commit')}" + _cleared(cleared))
            break

        if behind == 0:
            if ctx.dry_run:
                return result(item, WOULD_CHANGE, f"would push {plural(ahead, 'commit')} to origin")
            pushed = git.run("push", "--porcelain", "origin", f"refs/heads/{branch}:refs/heads/{branch}", timeout=timeout)
            if pushed.ok:
                done.append(f"pushed {plural(ahead, 'commit')}")
                break
            if _rejected(pushed):
                continue  # someone pushed first: fetch, merge and try again
            return result(item, FAILED, _prefix(done, f"couldn't push: {pushed.first_line()}"), tail(pushed.err + pushed.out))

        # Both sides have commits. Test the merge without touching the working tree.
        if git_version() is None or git_version() < (2, 38):
            return result(item, FAILED, f"has split from origin, and merging it needs Git 2.38 or newer; this machine has {version_text()}")
        tested = git.run("merge-tree", "--write-tree", "--name-only", "--no-messages", local, remote, timeout=MERGE_TIMEOUT)
        split = f"{plural(ahead, 'commit')} here and {plural(behind, 'commit')} on origin"
        if tested.code == 1:
            files = _conflicted(tested.out)
            return result(
                item,
                BLOCKED,
                _prefix(done, f"has {split}, and merging them conflicts in {_files(files)}; nothing was changed"),
                "\n".join(files),
            )
        if not tested.ok:
            return result(item, FAILED, _prefix(done, f"couldn't test the merge: {tested.first_line()}"), tail(tested.err))
        if ctx.dry_run:
            return result(item, WOULD_CHANGE, f"has {split}; would merge them (the merge is clean) and push" + _and_install(item))
        tree = tested.out.split()[0]
        message = f"Merge origin/{branch} into {branch} on {ctx.machine}\n\nMade by devcluster sync."
        made = git.run("commit-tree", tree, "-p", local, "-p", remote, "-m", message, timeout=120)
        if not made.ok:
            return result(item, FAILED, _prefix(done, f"couldn't make the merge commit: {made.first_line()}"), tail(made.err))
        switched = _switched(item, git, branch, done)
        if switched:
            return switched
        merge = made.out.strip()
        moved, cleared = update(git, FAST_FORWARD, merge)
        if not moved.ok:
            if _raced(moved):
                continue
            return _update_failed(item, moved, "merging origin's commits", done, git, merge)
        switched = _switched(item, git, branch, done, after=True)
        if switched:
            return switched
        done.append(f"merged {plural(behind, 'commit')} from origin" + _cleared(cleared))
        # The next round pushes the merge.
    else:
        return result(item, FAILED, _prefix(done, "origin kept moving while devcluster pushed; the next run tries again"))

    return _after_update(item, ctx, git, done)


def sync_tool(item, ctx):
    path = expand(item.get("path"))
    ref = item.get("ref", "main")
    timeout = item.get("timeout", FETCH_TIMEOUT)
    done = []

    if not path.exists():
        if ctx.dry_run:
            return result(item, WOULD_CHANGE, f"would clone {item.get('url')} into {item.get('path')} at {ref}" + _and_install(item))
        pinned = re.fullmatch(r"[0-9a-fA-F]{7,40}", ref)
        cloned = clone(item.get("url"), path, None if pinned else ref, CLONE_TIMEOUT)
        if not cloned.ok:
            return result(item, FAILED, f"couldn't clone it: {cloned.first_line()}", tail(cloned.err))
        done.append("cloned it")

    git = Git(path)
    problem = _preflight(item, path, git)
    if problem:
        return problem
    fetched = git.run("fetch", "--quiet", "--tags", "origin", "+refs/heads/*:refs/remotes/origin/*", timeout=timeout)
    if not fetched.ok:
        return result(item, FAILED, f"couldn't fetch from origin: {fetched.first_line()}", tail(fetched.err))

    branch_target = git.rev(f"refs/remotes/origin/{ref}")
    target = branch_target or git.rev(f"refs/tags/{ref}")
    if target is None and re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        target = git.rev(ref)
    if target is None:
        return result(item, FAILED, f"origin has no branch, tag or commit called {ref}")

    head = git.rev("HEAD")
    current = git.branch()
    memory = ctx.mem(item)
    if branch_target and current not in (None, ref):
        return result(item, BLOCKED, f"is checked out on {current}, not {ref}")
    if head == target:
        memory["at"] = head
        return _after_update(item, ctx, git, done)

    # Commits origin doesn't have were made here, unless the checkout is still exactly
    # where devcluster last put it: then they came from origin before it was rewound.
    local = git.local_only("HEAD") if head != memory.get("at") else 0
    if local:
        return result(item, BLOCKED, f"has {plural(local, 'commit')} that origin doesn't have; devcluster won't move it off them. If this machine is where it's edited, list it as a [[repo]] here")

    forward = bool(branch_target) and current == ref and git.is_ancestor(head, target)
    verb = "update" if forward else "move"
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"would {verb} it from {short(head)} to {short(target)}" + _and_install(item))
    if forward:
        switched = _switched(item, git, ref, done)
        if switched:
            return switched
        moved, cleared = update(git, FAST_FORWARD, target)
    else:
        # Going back, or to a pinned version, detaches at that commit instead of moving a
        # branch, so no commit can be dropped, not even one an agent makes during the run.
        moved, cleared = update(git, ("checkout", "--quiet", "--no-overwrite-ignore", "--detach"), target)
    if not moved.ok:
        return _update_failed(item, moved, f"moving it to {short(target)}", done, git, target)
    if forward:
        switched = _switched(item, git, ref, done, after=True)
        if switched:
            return switched
    else:
        # A commit made on a detached checkout in the moment before the move is reachable
        # only from Git's log of where HEAD was. Keep it on a branch and say so.
        before = git.rev("HEAD@{1}")
        if before and before != head and not git.out("for-each-ref", "--contains", before, "refs/heads", timeout=60):
            kept = f"devcluster-kept-{short(before)}"
            git.run("branch", kept, before, timeout=60)
            return result(item, BLOCKED, f"a commit was made here while devcluster moved it to {short(target)}; that commit is kept on branch {kept}")
    done.append((f"{verb}d from {short(head)}" if forward else f"moved from {short(head)}") + _cleared(cleared))
    memory["at"] = target
    return _after_update(item, ctx, git, done)


def _preflight(item, path, git):
    """A result when the checkout can't be worked on right now, otherwise None."""
    if not git.is_checkout():
        return result(item, BLOCKED, f"{item.get('path')} exists but isn't a Git checkout")
    url = git.origin_url()
    if url is None:
        return result(item, BLOCKED, "has no origin remote")
    listed = item.get("url")
    # Git may rewrite URLs (url.<base>.insteadOf), so compare the origin as written and as
    # rewritten against the listed URL as written and as rewritten.
    written = git.out("config", "--get", "remote.origin.url", timeout=60) or url
    rewritten = git.out("ls-remote", "--get-url", listed, timeout=60) or listed
    if not (same_remote(written, listed) or same_remote(url, rewritten)):
        return result(item, BLOCKED, f"its origin is {written}, but the list says {listed}")
    operation = git.operation_in_progress()
    if operation:
        return result(item, BLOCKED, f"is in the middle of {operation}; devcluster won't touch it until that's finished")
    if git.index_locked():
        return result(item, BUSY, "another Git command is using it; the next run tries again")
    return None


def _after_update(item, ctx, git, done):
    """Run the item's install when its commit has moved, then report."""
    head = git.rev("HEAD")
    changed = bool(done)
    summary = "; ".join(done) + f"; now at {short(head)}" if done else f"up to date at {short(head)}"
    command = item.get("install")
    if not command:
        return result(item, CHANGED if changed else OK, summary)
    memory = ctx.mem(item)
    if memory.get("installed") == head:
        return result(item, CHANGED if changed else OK, summary)
    if memory.get("failed") == head and ctx.now - memory.get("failed_at", 0) < ctx.retry_after:
        retry = clock(memory["failed_at"] + ctx.retry_after)
        return result(item, FAILED, f"{summary}; its install failed at {clock(memory['failed_at'])}, and is tried again after {retry}", memory.get("failure", ""))
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"{summary}; would run its install")
    ran = shell(command, cwd=git.path, timeout=item.get("install_timeout", INSTALL_TIMEOUT))
    if not ran.ok:
        how = "timed out" if ran.timed_out else f"exited {ran.code}"
        memory.update(failed=head, failed_at=ctx.now, failure=tail(ran.err or ran.out))
        return result(item, FAILED, f"{summary}; its install {how}", memory["failure"])
    for key in ("failed", "failed_at", "failure"):
        memory.pop(key, None)
    memory["installed"] = head
    return result(item, CHANGED, f"{summary}; ran its install")


def _switched(item, git, branch, done, after=False):
    """Stop if someone switched the checkout to another branch during the run."""
    current = git.branch()
    if current == branch:
        return None
    if after:
        # A fast-forward only adds commits, so nothing was lost, but they went to the wrong branch.
        return result(item, BLOCKED, _prefix(done, f"was switched to {current or 'a detached commit'} while devcluster updated it, so that got origin's commits instead of {branch}"))
    return result(item, BLOCKED, _prefix(done, f"was switched to {current or 'a detached commit'} during the run, so devcluster stopped before changing it"))


def _and_install(item):
    return ", and run its install" if item.get("install") else ""


def _prefix(done, text):
    return "; ".join(done + [text])


def _raced(moved):
    """A fast-forward that lost a race with a commit made here a moment ago."""
    return "not possible to fast-forward" in (moved.err + moved.out).lower()


def _rejected(pushed):
    """A push refused because origin moved after the fetch: worth fetching and trying again."""
    text = pushed.err + pushed.out
    return any(sign in text for sign in (
        "[rejected]", "non-fast-forward", "fetch first",
        "failed to update ref", "cannot lock ref", "but expected",
    ))


def _update_failed(item, moved, what, done, git=None, target=None):
    text = moved.err + moved.out
    files = _in_the_way(moved)
    if files:
        kind = "untracked files" if "untracked working tree files" in text else "uncommitted changes"
        differ = [name for name in files if not (git and _matching(git, target, name))] or files
        same = len(files) - len(differ)
        note = f" ({plural(same, 'more file')} in the way {'is' if same == 1 else 'are'} identical to the incoming {'one' if same == 1 else 'ones'})" if same else ""
        return result(item, BLOCKED, _prefix(done, f"{what} would overwrite {kind} in {_files(differ)}{note}; nothing was changed"), "\n".join(differ))
    if "index.lock" in text:
        return result(item, BUSY, _prefix(done, "another Git command was using it; the next run tries again"))
    if moved.timed_out:
        return result(item, FAILED, _prefix(done, f"{what} timed out"))
    return result(item, FAILED, _prefix(done, f"{what} failed: {moved.first_line()}"), tail(text))


def update(git, command, target):
    """Run a fast-forward or checkout to target. When Git refuses because files here are in
    the way, and every one of them already holds exactly what target has, clear them and run
    it again: the update writes the same contents back, so nothing is lost. Returns the last
    run, and how many files were cleared."""
    moved = git.run(*command, target, timeout=MERGE_TIMEOUT)
    files = _in_the_way(moved)
    if moved.ok or not files:
        return moved, 0
    blobs = {}
    for name in files:
        blobs[name] = _matching(git, target, name)
        if not blobs[name]:
            return moved, 0
    folder = git.git_path(f"devcluster/cleared-{time.time_ns()}")
    cleared = _clear(git, blobs, folder)
    if cleared is None:
        return moved, 0
    again = git.run(*command, target, timeout=MERGE_TIMEOUT)
    if again.ok:
        shutil.rmtree(folder, ignore_errors=True)
        return again, len(files)
    _undo(git, *cleared, folder)
    return again, 0


def _in_the_way(moved):
    """The files Git named when it refused to overwrite them."""
    text = moved.err + moved.out
    if moved.ok or "would be overwritten" not in text:
        return []
    return [line.strip() for line in text.splitlines() if line.startswith("\t")]


def _matching(git, target, name):
    """The blob target has at this path when the file here holds exactly that, with the same
    executable bit, and no third version of it is staged; otherwise None."""
    path = git.path / name
    if path.is_symlink() or not path.is_file():
        return None
    entry = git.out("ls-tree", target, "--", name, timeout=60)
    if not entry or "\t" not in entry:
        return None
    mode, kind, blob = entry.split("\t", 1)[0].split()
    if kind != "blob" or mode not in ("100644", "100755"):
        return None
    if (mode == "100755") != bool(path.stat().st_mode & 0o100):
        return None
    if git.out("hash-object", "--", name, timeout=60) != blob:
        return None
    staged = git.out("ls-files", "--stage", "--", name, timeout=60)
    if staged and staged.split()[1] not in (blob, _blob(git, f"HEAD:{name}")):
        return None
    return blob


def _blob(git, spec):
    return git.out("rev-parse", "--verify", "--quiet", spec, timeout=60)


def _clear(git, blobs, folder):
    """Clear matching files out of the way. A tracked file is staged as it is, which leaves it
    on disk. An untracked one is moved into the Git folder and checked again there, in case it
    changed in the meantime. Returns what was staged and what was moved, or None after undoing
    both."""
    staged, moved = [], []
    for name, blob in blobs.items():
        if git.out("ls-files", "--", name, timeout=60):
            if _blob(git, f":{name}") != blob:
                if not git.run("add", "--sparse", "--", name, timeout=60).ok:
                    _undo(git, staged, moved, folder)
                    return None
                staged.append(name)
            continue
        there = folder / name
        try:
            there.parent.mkdir(parents=True, exist_ok=True)
            os.rename(git.path / name, there)
        except OSError:
            _undo(git, staged, moved, folder)
            return None
        moved.append((git.path / name, there))
        if git.out("hash-object", f"--path={name}", "--", str(there), timeout=60) != blob:
            _undo(git, staged, moved, folder)
            return None
    return staged, moved


def _undo(git, staged, moved, folder):
    """Put cleared files back as they were. A moved file goes back only where nothing has taken
    its place; if any can't, the folder holding it stays."""
    if staged:
        git.run("reset", "--quiet", "--", *staged, timeout=60)
    kept = False
    for original, there in moved:
        try:
            if original.exists() or original.is_symlink():
                kept = True
                continue
            original.parent.mkdir(parents=True, exist_ok=True)
            os.rename(there, original)
        except OSError:
            kept = True
    if not kept:
        shutil.rmtree(folder, ignore_errors=True)


def _cleared(count):
    if not count:
        return ""
    return f", after clearing {plural(count, 'file')} here that {'was' if count == 1 else 'were'} identical to the incoming {'one' if count == 1 else 'ones'}"


def _conflicted(output):
    lines = output.splitlines()[1:]
    files = []
    for line in lines:
        if not line.strip():
            break
        if line not in files:
            files.append(line)
    return files


def _files(files, limit=5):
    if not files:
        return "some files"
    shown = ", ".join(files[:limit])
    return shown if len(files) <= limit else f"{shown} and {len(files) - limit} more"
