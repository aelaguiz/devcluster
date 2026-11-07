"""Git checkouts: repos worked on directly on a branch, and tools that follow a ref.

A repo syncs both ways: origin's commits come in, this machine's commits go out.
A tool only follows: it moves to the ref the list names and never pushes.

Nothing here force-pushes, rebases, or discards a change. Git's own refusal to
overwrite uncommitted work is passed on as "Needs you", with the file names, unless every
file in the way already holds exactly what the update would write there.

When the commit changes, the item's install runs, then its check (see installs.py). If
either fails, the files the install changed go back as they were, and a tool also goes
back to the last commit that installed cleanly. A repo's checkout never goes back:
people work in it.
"""

import os
import re
import shutil
import time

from .. import installs
from ..context import result
from ..git import Git, clone, git_version, same_remote, short, version_text
from ..paths import clock, expand
from ..proc import run, tail
from ..results import BLOCKED, BUSY, CHANGED, FAILED, OK, WOULD_CHANGE, plural

FETCH_TIMEOUT = 600
MERGE_TIMEOUT = 900
CLONE_TIMEOUT = 3600
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
            return result(item, WOULD_CHANGE, f"would clone {item.get('url')} into {item.get('path')}" + _and_install(item, ctx))
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
                return result(item, WOULD_CHANGE, f"would pull {plural(behind, 'commit')} from origin" + _and_install(item, ctx))
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
            return result(item, WOULD_CHANGE, f"has {split}; would merge them (the merge is clean) and push" + _and_install(item, ctx))
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
            return result(item, WOULD_CHANGE, f"would clone {item.get('url')} into {item.get('path')} at {ref}" + _and_install(item, ctx))
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
    good = memory.get("good")
    if item.get("install") and memory.get("failed") == target and _waiting(ctx, memory) and good not in (None, target) and head in (target, good):
        return _while_failed(item, ctx, git, head, target, good)
    if head == target:
        memory["at"] = head
        return _after_update(item, ctx, git, done, tool=True)

    # Commits origin doesn't have were made here, unless the checkout is still exactly
    # where devcluster last put it: then they came from origin before it was rewound.
    local = git.local_only("HEAD") if head != memory.get("at") else 0
    if local:
        return result(item, BLOCKED, f"has {plural(local, 'commit')} that origin doesn't have; devcluster won't move it off them. If this machine is where it's edited, list it as a [[repo]] here")

    forward = bool(branch_target) and current == ref and git.is_ancestor(head, target)
    verb = "update" if forward else "move"
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"would {verb} it from {short(head)} to {short(target)}" + _and_install(item, ctx))
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
        kept = _keep_stray_commit(git, head)
        if kept:
            return result(item, BLOCKED, f"a commit was made here while devcluster moved it to {short(target)}; that commit is kept on branch {kept}")
    done.append((f"{verb}d from {short(head)}" if forward else f"moved from {short(head)}") + _cleared(cleared))
    memory["at"] = target
    return _after_update(item, ctx, git, done, tool=True)


def _keep_stray_commit(git, head):
    """After a detached move: a commit made on the detached checkout in the moment before the
    move is reachable only from Git's log of where HEAD was. Keep it on a branch."""
    before = git.rev("HEAD@{1}")
    if before and before != head and not git.out("for-each-ref", "--contains", before, "refs/heads", timeout=60):
        kept = f"devcluster-kept-{short(before)}"
        git.run("branch", kept, before, timeout=60)
        return kept
    return None


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


def _after_update(item, ctx, git, done, tool=False):
    """Run the item's install and check when its commit has moved, then report."""
    head = git.rev("HEAD")
    changed = bool(done)
    summary = "; ".join(done) + f"; now at {short(head)}" if done else f"up to date at {short(head)}"
    command = item.get("install")
    if not command:
        return _report(item, ctx, CHANGED if changed else OK, summary)
    memory = ctx.mem(item)
    if memory.get("installed") == head:
        return _report(item, ctx, CHANGED if changed else OK, summary)
    if memory.get("failed") == head and _waiting(ctx, memory):
        retry = clock(memory["failed_at"] + ctx.retry_after)
        step = memory.get("failed_step", "install")
        return _report(item, ctx, FAILED, f"{summary}; its {step} failed at {clock(memory['failed_at'])}, and is tried again after {retry}", memory.get("failure", ""))
    if ctx.dry_run:
        return _report(item, ctx, WOULD_CHANGE, f"{summary}; would run its install{_keeping(item, ctx)}")

    outcome = installs.run(item, ctx, git.path)
    if outcome.ok:
        for key in ("failed", "failed_at", "failed_step", "failure"):
            memory.pop(key, None)
        memory["installed"] = memory["good"] = head
        checked = ", and its check passed" if item.get("check") else ""
        return _report(item, ctx, CHANGED, f"{summary}; ran its install{checked}")

    memory.update(failed=head, failed_at=ctx.now, failed_step=outcome.step, failure=outcome.output)
    text = f"{summary}; its {outcome.step} {outcome.how}"
    if outcome.restored:
        text += "; the files it changed were put back"
    good = memory.get("good")
    if outcome.moved_aside:
        added = "the file it added was" if outcome.moved_aside == 1 else f"the {outcome.moved_aside} files it added were"
        text += f"; {added} moved to ~/.devcluster/state/removed/{item.name}"
    if tool and good and good != head:
        text += "; " + _go_back(item, ctx, git, good, head)
    return _report(item, ctx, FAILED, text, outcome.output)


def _go_back(item, ctx, git, good, failed):
    """Return a tool from the commit whose install failed to its last good commit, and
    install that again. Says what happened."""
    memory = ctx.mem(item)
    head = git.rev("HEAD")
    if head != failed:
        # Someone committed or moved the checkout while the install ran: leave their work be.
        return f"its checkout moved to {short(head)} while the install ran, so devcluster left it there"
    moved = git.run("checkout", "--quiet", "--no-overwrite-ignore", "--detach", good, timeout=MERGE_TIMEOUT)
    if not moved.ok:
        return f"couldn't go back to {short(good)}, its last good commit ({moved.first_line()}); the next run tries again"
    kept = _keep_stray_commit(git, head)
    memory["at"] = good
    memory.pop("installed", None)
    said = f"went back to {short(good)}, its last good commit"
    if kept:
        said += f" (a commit made here meanwhile is kept on branch {kept})"
    return said + _install_good(item, ctx, git, good)


def _install_good(item, ctx, git, good):
    again = installs.run(item, ctx, git.path)
    if again.ok:
        ctx.mem(item)["installed"] = good
        return ", and installed it again"
    return f", but its {again.step} {again.how} there too; the next run tries again"


def _while_failed(item, ctx, git, head, target, good):
    """A tool whose newest commit failed, until that commit may be tried again: get back to
    the last good commit and its install, or stay there."""
    memory = ctx.mem(item)
    step = memory.get("failed_step", "install")
    waiting = f"{short(target)} failed its {step} at {clock(memory['failed_at'])}, and is tried again after {clock(memory['failed_at'] + ctx.retry_after)}"
    failure = memory.get("failure", "")
    if head == target:  # going back failed on an earlier run
        if ctx.dry_run:
            return _report(item, ctx, WOULD_CHANGE, f"{waiting}; would go back to {short(good)}, its last good commit")
        return _report(item, ctx, FAILED, f"{waiting}; {_go_back(item, ctx, git, good, target)}", failure)
    if memory.get("installed") != good:  # installing the good commit again failed on an earlier run
        if ctx.dry_run:
            return _report(item, ctx, WOULD_CHANGE, f"{waiting}; would install {short(good)}, its last good commit, again")
        return _report(item, ctx, FAILED, f"{waiting}; stays on {short(good)}, its last good commit" + _install_good(item, ctx, git, good), failure)
    return _report(item, ctx, FAILED, f"stays on {short(good)}, its last good commit: {waiting}", failure)


def _waiting(ctx, memory):
    return ctx.now - memory.get("failed_at", 0) < ctx.retry_after


def _report(item, ctx, state, summary, detail=""):
    """A result, plus edited install files kept aside: those need a person until dealt with."""
    note = installs.kept_note(ctx, item)
    if note:
        summary = f"{summary}; {note}"
        if state in (OK, CHANGED):
            state = BLOCKED
    return result(item, state, summary, detail)


def _switched(item, git, branch, done, after=False):
    """Stop if someone switched the checkout to another branch during the run."""
    current = git.branch()
    if current == branch:
        return None
    if after:
        # A fast-forward only adds commits, so nothing was lost, but they went to the wrong branch.
        return result(item, BLOCKED, _prefix(done, f"was switched to {current or 'a detached commit'} while devcluster updated it, so that got origin's commits instead of {branch}"))
    return result(item, BLOCKED, _prefix(done, f"was switched to {current or 'a detached commit'} during the run, so devcluster stopped before changing it"))


def _and_install(item, ctx):
    if not item.get("install"):
        return ""
    return ", and run its install" + _keeping(item, ctx)


def _keeping(item, ctx):
    """What a dry run adds when installed files were edited here since the last install."""
    edits = installs.edited(item, ctx) if item.get("installs_into") else []
    return f", keeping a copy of {plural(len(edits), 'installed file')} edited here" if edits else ""


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
        differ = [name for name in files if not (git and _matching(git, target, name))] or list(files)
        kinds = {kind for name, kind in files.items() if name in differ}
        kind = " and ".join(k for k in ("uncommitted changes", "untracked files") if k in kinds)
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
    """The files Git named when it refused to overwrite them, each with what Git said it is:
    an uncommitted change or an untracked file."""
    text = moved.err + moved.out
    files, kind = {}, None
    if moved.ok or "would be overwritten" not in text:
        return files
    for line in text.splitlines():
        if line.startswith("\t"):
            if kind:
                files[line[1:]] = kind
        elif "would be overwritten" in line:
            kind = "untracked files" if "untracked working tree files" in line else "uncommitted changes"
        else:
            kind = None
    return files


def _matching(git, target, name):
    """When the file here holds exactly the bytes the update would write at this path, with the
    same executable bit, and no third version of it is staged: the target's blob, and the hash
    of those bytes. Otherwise None."""
    path = git.path / name
    if path.is_symlink() or not path.is_file():
        return None
    entry = _entry(git, target, name)
    if not entry or entry[0] not in ("100644", "100755"):
        return None
    mode, blob = entry
    if (mode == "100755") != bool(path.stat().st_mode & 0o100):
        return None
    # Compare raw bytes with what checkout would write, so a line-ending or filter
    # conversion Git treats as the same file still counts as a difference.
    written = run(["/bin/sh", "-c", 'git -C "$1" cat-file --filters "$2" | git hash-object --no-filters --stdin', "sh", str(git.path), f"{target}:{name}"], timeout=300, env=git.env)
    raw = written.out.strip() if written.ok else None
    if not raw or git.out("hash-object", "--no-filters", "--", name, timeout=300) != raw:
        return None
    staged = _staged(git, name)
    if staged and staged not in (entry, _entry(git, "HEAD", name)):
        return None
    return blob, raw


def _entry(git, tree, name):
    """The mode and blob tree has at exactly this path, or None."""
    listed = git.out("--literal-pathspecs", "ls-tree", "-z", "--full-tree", tree, "--", name, timeout=60)
    for line in (listed or "").split("\0"):
        info, _, path = line.partition("\t")
        parts = info.split()
        if path == name and len(parts) == 3 and parts[1] == "blob":
            return parts[0], parts[2]
    return None


def _staged(git, name):
    """The mode and blob staged at exactly this path, or None when nothing is. A conflicted
    path counts as a third version."""
    listed = git.out("--literal-pathspecs", "ls-files", "-z", "--stage", "--", name, timeout=60)
    found = []
    for line in (listed or "").split("\0"):
        info, _, path = line.partition("\t")
        parts = info.split()
        if path == name and len(parts) == 3:
            found.append((parts[0], parts[1]) if parts[2] == "0" else ("conflicted", parts[1]))
    return found[0] if len(found) == 1 else (("conflicted", "") if found else None)


def _clear(git, blobs, folder):
    """Clear matching files out of the way. A tracked file is staged as it is, which leaves it
    on disk. An untracked one is moved into the Git folder and checked again there, in case it
    changed in the meantime. Returns what was staged and what was moved, or None after undoing
    both."""
    staged, moved = [], []
    for name, (blob, raw) in blobs.items():
        here = _staged(git, name)
        if here:
            if here[1] != blob:
                if not git.run("--literal-pathspecs", "add", "--sparse", "--", name, timeout=60).ok:
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
        if git.out("hash-object", "--no-filters", "--", str(there), timeout=300) != raw:
            _undo(git, staged, moved, folder)
            return None
    return staged, moved


def _undo(git, staged, moved, folder):
    """Put cleared files back as they were. A moved file goes back only where nothing has taken
    its place; if any can't, the folder holding it stays."""
    if staged:
        git.run("--literal-pathspecs", "reset", "--quiet", "--", *staged, timeout=60)
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
