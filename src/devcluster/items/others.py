"""CLIs at a pinned version or at the version one machine has, logins each machine makes for
itself, the secrets repo, and files put in place from the config."""

import hashlib
import os
import re
import tempfile

from ..context import result
from ..git import Git, clone
from ..paths import clock, expand
from ..proc import redact, shell, tail
from ..results import BLOCKED, BUSY, CHANGED, FAILED, OK, WOULD_CHANGE, plural
from . import follow
from .checkouts import CLONE_TIMEOUT, FAST_FORWARD, FETCH_TIMEOUT, _cleared, _prefix, _preflight, _update_failed, exchange, update

CHECK_TIMEOUT = 60
CLI_INSTALL_TIMEOUT = 1200
LOGIN_TIMEOUT = 1800
SECRETS_TIMEOUT = 600


# CLIs


VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][\w.+-]*)?")


def has_version(output, version):
    """True when the version appears as a whole word: 0.163.0 doesn't match 0.163.0-alpha.5."""
    return re.search(rf"(?<![\w.+-]){re.escape(version)}(?![\w.+-])", output) is not None


def _found(output):
    match = VERSION.search(output)
    return match.group(0) if match else "no version"


def sync_cli(item, ctx):
    version = item.get("version")
    timeout = item.get("timeout", CHECK_TIMEOUT)
    source = item.get("from")
    if source and follow.is_source(ctx, item):
        return _share_version(item, ctx, timeout)
    if source:
        shared = follow.read_copy(ctx, item)
        if shared is None:
            return result(item, BUSY, f"waiting for {source}'s version to arrive in {follow.copy_path(ctx, item)}")
        version = shared.strip()
        if not VERSION.fullmatch(version):
            return result(item, FAILED, f"{source}'s copy holds {version[:40]!r}, which isn't a version")
    checked = shell(item.get("check"), cwd=os.path.expanduser("~"), timeout=timeout)
    output = checked.out + "\n" + checked.err
    if checked.ok and has_version(output, version):
        return result(item, OK, f"at {version}, the same as {source}" if source else f"at {version}")
    found = _found(output) if checked.ok else "not installed"
    memory = ctx.mem(item)
    if memory.get("failed") == version and ctx.now - memory.get("failed_at", 0) < ctx.retry_after:
        retry = clock(memory["failed_at"] + ctx.retry_after)
        return result(item, FAILED, f"has {found}; installing {version} failed at {clock(memory['failed_at'])}, and is tried again after {retry}", memory.get("failure", ""))
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"has {found}; would install {version}")
    command = item.get("install").replace("{version}", version)
    ran = shell(command, cwd=os.path.expanduser("~"), timeout=item.get("install_timeout", CLI_INSTALL_TIMEOUT))
    if not ran.ok:
        how = "timed out" if ran.timed_out else f"exited {ran.code}"
        memory.update(failed=version, failed_at=ctx.now, failure=tail(ran.err or ran.out))
        return result(item, FAILED, f"has {found}; installing {version} {how}", memory["failure"])
    again = shell(item.get("check"), cwd=os.path.expanduser("~"), timeout=timeout)
    if not (again.ok and has_version(again.out + "\n" + again.err, version)):
        now_found = _found(again.out + again.err) if again.ok else "not installed"
        memory.update(failed=version, failed_at=ctx.now, failure=tail(again.err or again.out))
        return result(item, FAILED, f"installed {version}, but its check still shows {now_found}")
    memory.clear()
    return result(item, CHANGED, f"installed {version} (had {found})")


def _share_version(item, ctx, timeout):
    """On the machine the others follow: save the version installed here for them."""
    checked = shell(item.get("check"), cwd=os.path.expanduser("~"), timeout=timeout)
    version = _found(checked.out + "\n" + checked.err) if checked.ok else None
    if not version or version == "no version":
        how = "timed out" if checked.timed_out else f"exited {checked.code}" if not checked.ok else "shows no version"
        return result(item, FAILED, f"its check {how}, so the other machines keep the last version", tail(checked.err or checked.out))
    if (follow.read_copy(ctx, item) or "").strip() == version:
        return result(item, OK, f"at {version}; the other machines follow it")
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"at {version}; would save it for the other machines")
    follow.save_copy(ctx, item, version + "\n")
    return result(item, CHANGED, f"at {version}; saved it for the other machines")


# Logins a tool keeps for itself on each machine. Some can't be copied from another machine:
# an OAuth login whose refresh token changes each time it's used signs both copies out. So
# each machine makes its own, with the item's commands, which never print a credential.


def _said(ran):
    """The last line a command printed, which is what status shows of a login item."""
    for line in reversed((ran.out + "\n" + ran.err).splitlines()):
        if line.strip():
            return redact(line.strip())[:300]
    return ""


def _check_line(ran):
    if ran.timed_out:
        return "its check timed out"
    return _said(ran) or ("its check passes" if ran.ok else f"its check exited {ran.code}")


LOGIN_WORDS = {"run": "login", "timeout": "login_timeout", "done": "logged in", "some": "logged in some",
               "doing": "logging in", "would": "would log in"}
SETUP_WORDS = {"run": "setup", "timeout": "setup_timeout", "done": "set up", "some": "set up part",
               "doing": "setting up", "would": "would set up"}


def sync_login(item, ctx):
    return _check_then_run(item, ctx, LOGIN_WORDS)


def sync_setup(item, ctx):
    """Something each machine needs set up once, such as a toolchain or a service: the check
    says whether it's in place, and the setup command puts it there. Same rules as a login."""
    return _check_then_run(item, ctx, SETUP_WORDS)


def _check_then_run(item, ctx, words):
    home = os.path.expanduser("~")
    timeout = item.get("timeout", CHECK_TIMEOUT)
    memory = ctx.mem(item)
    checked = shell(item.get("check"), cwd=home, timeout=timeout)
    missing = _check_line(checked)
    if checked.ok:
        memory.clear()
        return result(item, OK, missing)
    # Running again right after a run that changed nothing would ask the same server the
    # same thing every run. A change in what the check reports, such as a new login to make,
    # is worth trying at once.
    if memory.get("missing") == missing and ctx.now - memory.get("failed_at", 0) < ctx.retry_after:
        retry = clock(memory["failed_at"] + ctx.retry_after)
        return result(item, FAILED, f"{missing}; {words['doing']} failed at {clock(memory['failed_at'])}, and is tried again after {retry}", memory.get("failure", ""))
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"{missing}; {words['would']}")
    ran = shell(item.get(words["run"]), cwd=home, timeout=item.get(words["timeout"], LOGIN_TIMEOUT))
    failure = "" if ran.ok else tail(ran.err or ran.out)
    again = shell(item.get("check"), cwd=home, timeout=timeout)
    now_missing = _check_line(again)
    if again.ok:
        memory.clear()
        return result(item, CHANGED, f"{words['done']}; {now_missing}", failure)
    if now_missing != missing:
        # Some of it was done and the rest is still to come: the next run carries on.
        memory.clear()
        return result(item, CHANGED, f"{words['some']}; {now_missing}", failure)
    how = "timed out" if ran.timed_out else f"exited {ran.code}" if not ran.ok else "changed nothing"
    memory.update(missing=now_missing, failed_at=ctx.now, failure=failure)
    return result(item, FAILED, f"{now_missing}; {words['doing']} {how}", failure)


# Secrets. The repo's own commands do the decrypting; their output is never recorded,
# because it's the one place a secret could leak into a status report.


def sync_secrets(item, ctx):
    path = expand(item.get("path"))
    branch = item.get("branch", "main")
    timeout = item.get("timeout", SECRETS_TIMEOUT)

    if not path.exists():
        if ctx.dry_run:
            return result(item, WOULD_CHANGE, f"would clone {item.get('url')} into {item.get('path')} and decrypt it")
        cloned = clone(item.get("url"), path, branch, CLONE_TIMEOUT)
        if not cloned.ok:
            return result(item, FAILED, f"couldn't clone it: {cloned.first_line()}")
        return _decrypt(item, ctx, Git(path), timeout, "cloned it")

    git = Git(path, ctx.identity())
    problem = _preflight(item, path, git)
    if problem:
        return problem
    if git.branch() != branch:
        return result(item, BLOCKED, f"is checked out on {git.branch() or 'a detached commit'}, not {branch}")
    # A decrypt that failed leaves files the check would mistake for a local edit. If none of
    # the decrypted files changed since it failed, retry instead of blocking for good.
    memory = ctx.mem(item)
    retrying = "decrypt_pending" in memory
    if retrying and _plaintext(git) != memory.get("plaintext"):
        # Something wrote here since: an edit nobody has published, or someone (often the fixer)
        # finishing what the decrypt couldn't. Once every plain file matches Git, a decrypt has
        # nothing left to overwrite, and the failed one is behind us.
        if not shell(item.get("check"), cwd=path, timeout=timeout).ok:
            return result(item, BLOCKED, "a decrypt failed on an earlier run, and a decrypted file here has changed since, so devcluster won't decrypt over it. Run the check by hand to see which file")
        if not ctx.dry_run:
            memory.pop("decrypt_pending", None)
            memory.pop("plaintext", None)
        retrying = False
    if item.get("publish"):
        return _both_ways(item, ctx, git, branch, timeout, retrying)
    if not retrying:
        checked = shell(item.get("check"), cwd=path, timeout=timeout)
        if not checked.ok:
            return result(item, BLOCKED, f"its check failed here (exit {checked.code}): this machine may have a credential edit nobody has published. Run the check by hand to see which file")
    fetched = git.run("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=FETCH_TIMEOUT)
    if not fetched.ok:
        return result(item, FAILED, f"couldn't fetch from origin: {fetched.first_line()}")
    head = git.rev("HEAD")
    remote = git.rev(f"refs/remotes/origin/{branch}")
    if head == remote:
        if retrying:
            if ctx.dry_run:
                return result(item, WOULD_CHANGE, "would retry decrypting, which failed on an earlier run")
            return _decrypt(item, ctx, git, timeout, "retried decrypting")
        return result(item, OK, "up to date, and its check passes")
    if not git.is_ancestor(head, remote):
        return result(item, BLOCKED, f"has {plural(git.local_only('HEAD'), 'commit')} here that origin doesn't have")
    behind = git.count(f"{head}..{remote}")
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"would pull {plural(behind, 'commit')} and decrypt")
    moved, cleared = update(git, FAST_FORWARD, remote)
    if not moved.ok:
        return _update_failed(item, moved, f"pulling {plural(behind, 'commit')}", [], git, remote)
    return _decrypt(item, ctx, git, timeout, f"pulled {plural(behind, 'commit')}" + _cleared(cleared))


def _both_ways(item, ctx, git, branch, timeout, retrying):
    """A secrets repo with a publish command also sends this machine's changes out: the command
    writes them into the files Git tracks, devcluster commits them, exchanges commits with origin
    the way it does for a repo, then decrypts whatever came in. After a failed decrypt nothing is
    published until a decrypt succeeds, since the files here may be a mix of old and new."""
    if ctx.dry_run:
        return _would_both_ways(item, git, branch, timeout, retrying)
    done = []
    if not retrying:
        published = shell(item.get("publish"), cwd=git.path, timeout=timeout)
        if not published.ok:
            how = "timed out" if published.timed_out else f"failed (exit {published.code})"
            return result(item, FAILED, f"its publish command {how}; nothing was committed")
        stopped = _commit(item, ctx, git, done)
        if stopped:
            return stopped
        # Pulling decrypts over the files here, so anything the publish missed has to stop the run.
        checked = shell(item.get("check"), cwd=git.path, timeout=timeout)
        if not checked.ok:
            return result(item, BLOCKED, _prefix(done, f"its check still fails after publishing (exit {checked.code}): this machine may have an edit the publish command didn't pick up, so devcluster stopped before pulling. Run the check by hand to see which file"))
    before = git.rev("HEAD")
    stopped = exchange(item, ctx, git, branch, FETCH_TIMEOUT, done)
    if stopped:
        return stopped
    if retrying or git.rev("HEAD") != before:
        return _decrypt(item, ctx, git, timeout, "; ".join(done) or "retried decrypting")
    if done:
        return result(item, CHANGED, "; ".join(done) + "; its check passes")
    return result(item, OK, "up to date, and its check passes")


def _commit(item, ctx, git, done):
    """Commit every change Git sees after the publish, within the repo's own ignore rules.
    Adds what it did to done. Returns a result when the run stops here, otherwise None."""
    staged = git.run("add", "--all", timeout=600)
    if staged.ok:
        listed = git.run("diff", "--cached", "--name-only", "-z", timeout=120)
        files = [name for name in listed.out.split("\0") if name] if listed.ok else []
        if not files:
            return None
        message = f"Publish what changed on {ctx.machine}\n\nMade by devcluster sync."
        staged = git.run("commit", "--quiet", "-m", message, timeout=300)
        if staged.ok:
            done.append(f"published {plural(len(files), 'changed file')}")
            return None
    if "index.lock" in staged.err + staged.out:
        return result(item, BUSY, "another Git command was using it; the next run tries again")
    return result(item, FAILED, f"couldn't commit what its publish command wrote: {staged.first_line()}", tail(staged.err + staged.out))


def _would_both_ways(item, git, branch, timeout, retrying):
    """What a run would do, without publishing, committing or decrypting anything."""
    plans = []
    if retrying:
        plans.append("retry decrypting, which failed on an earlier run")
    else:
        checked = shell(item.get("check"), cwd=git.path, timeout=timeout)
        if not checked.ok or git.out("status", "--porcelain", timeout=120):
            plans.append("publish what changed on this machine")
    fetched = git.run("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=FETCH_TIMEOUT)
    if not fetched.ok:
        return result(item, FAILED, f"couldn't fetch from origin: {fetched.first_line()}")
    head = git.rev("HEAD")
    remote = git.rev(f"refs/remotes/origin/{branch}")
    ahead = git.count(f"{remote}..{head}") if remote else git.count(head)
    behind = git.count(f"{head}..{remote}") if remote else 0
    if ahead:
        plans.append(f"push {plural(ahead, 'commit')}")
    if behind:
        plans.append(f"pull {plural(behind, 'commit')} and decrypt")
    if not plans:
        return result(item, OK, "up to date, and its check passes")
    return result(item, WOULD_CHANGE, "would " + ", then ".join(plans))


def _plaintext(git):
    """The size and modification time of each file Git doesn't track, which is where the
    decrypted secrets live. Nothing derived from their contents is kept."""
    listing = git.run("ls-files", "-z", "--others", timeout=120)
    prints = {}
    for name in sorted(filter(None, listing.out.split("\0"))):
        try:
            stat = (git.path / name).stat()
            prints[name] = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            prints[name] = None
    return prints


def _decrypt(item, ctx, git, timeout, done):
    path = git.path
    memory = ctx.mem(item)
    decrypted = shell(item.get("decrypt"), cwd=path, timeout=timeout)
    if not decrypted.ok:
        memory.update(decrypt_pending=git.rev("HEAD"), plaintext=_plaintext(git))
        return result(item, FAILED, f"{done}, but decrypting failed (exit {decrypted.code}); check that this machine's key can read it")
    checked = shell(item.get("check"), cwd=path, timeout=timeout)
    if not checked.ok:
        memory.update(decrypt_pending=git.rev("HEAD"), plaintext=_plaintext(git))
        return result(item, FAILED, f"{done} and decrypted, but the check afterwards failed (exit {checked.code})")
    memory.pop("decrypt_pending", None)
    memory.pop("plaintext", None)
    return result(item, CHANGED, f"{done} and decrypted; its check passes")


# Files. devcluster copies the config's file into place and remembers what it wrote,
# so an edit made on this machine is reported instead of overwritten.


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.devcluster-")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _set_aside(path, now):
    backup = path.with_name(path.name + ".before-devcluster")
    if backup.exists() or backup.is_symlink():
        backup = path.with_name(f"{path.name}.before-devcluster-{int(now)}")
    os.rename(path, backup)
    return backup


def sync_file(item, ctx):
    source = ctx.config.root / item.get("source")
    try:
        content = source.read_bytes()
    except OSError:
        return result(item, FAILED, f"{item.get('source')} isn't in the config")
    digest = _digest(content)
    written = ctx.mem(item).setdefault("written", {})
    notes, blocked, changed = [], [], False

    for label in item.get("targets"):
        target = expand(label)
        if target.is_dir():
            blocked.append(f"{label} is a folder")
            continue
        current = target.read_bytes() if target.is_file() else None
        if current == content:
            written[label] = digest
            continue
        if current is not None and label in written and written[label] != _digest(current):
            blocked.append(f"{label} was edited here since devcluster wrote it, so it was left alone")
            continue
        replacing = current is not None and label not in written
        if ctx.dry_run:
            if current is None:
                notes.append(f"would write {label}")
            elif replacing:
                notes.append(f"would move {label} aside and write the config's copy")
            else:
                notes.append(f"would update {label}")
            continue
        if replacing:
            backup = _set_aside(target, ctx.now)
            notes.append(f"moved {label} aside to {backup.name} and wrote the config's copy")
        else:
            notes.append(f"updated {label}" if current is not None else f"wrote {label}")
        _write(target, content)
        written[label] = digest
        changed = True

    if blocked:
        return result(item, BLOCKED, "; ".join(blocked + notes))
    if notes and ctx.dry_run:
        return result(item, WOULD_CHANGE, "; ".join(notes))
    if changed:
        return result(item, CHANGED, "; ".join(notes))
    count = len(item.get("targets"))
    return result(item, OK, "matches the config" if count == 1 else f"all {count} targets match the config")
