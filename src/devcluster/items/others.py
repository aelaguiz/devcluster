"""CLIs at a pinned version, the secrets repo, and files put in place from the config."""

import hashlib
import os
import re
import tempfile

from ..context import result
from ..git import Git, clone
from ..paths import clock, expand
from ..proc import shell, tail
from ..results import BLOCKED, CHANGED, FAILED, OK, WOULD_CHANGE, plural
from .checkouts import CLONE_TIMEOUT, FETCH_TIMEOUT, MERGE_TIMEOUT, _preflight, _update_failed

CHECK_TIMEOUT = 60
CLI_INSTALL_TIMEOUT = 1200
SECRETS_TIMEOUT = 600


# CLIs


def has_version(output, version):
    """True when the version appears as a whole word: 0.163.0 doesn't match 0.163.0-alpha.5."""
    return re.search(rf"(?<![\w.+-]){re.escape(version)}(?![\w.+-])", output) is not None


def _found(output):
    match = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-+][\w.+-]*)?", output)
    return match.group(0) if match else "no version"


def sync_cli(item, ctx):
    version = item.get("version")
    timeout = item.get("timeout", CHECK_TIMEOUT)
    checked = shell(item.get("check"), cwd=os.path.expanduser("~"), timeout=timeout)
    output = checked.out + "\n" + checked.err
    if checked.ok and has_version(output, version):
        return result(item, OK, f"at {version}")
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
        return _decrypt(item, path, timeout, "cloned it")

    git = Git(path)
    problem = _preflight(item, path, git)
    if problem:
        return problem
    if git.branch() != branch:
        return result(item, BLOCKED, f"is checked out on {git.branch() or 'a detached commit'}, not {branch}")
    checked = shell(item.get("check"), cwd=path, timeout=timeout)
    if not checked.ok:
        return result(item, BLOCKED, f"its check failed here (exit {checked.code}): this machine may have a credential edit nobody has published. Run the check by hand to see which file")
    fetched = git.run("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=FETCH_TIMEOUT)
    if not fetched.ok:
        return result(item, FAILED, f"couldn't fetch from origin: {fetched.first_line()}")
    head = git.rev("HEAD")
    remote = git.rev(f"refs/remotes/origin/{branch}")
    if head == remote:
        return result(item, OK, "up to date, and its check passes")
    if not git.is_ancestor(head, remote):
        return result(item, BLOCKED, f"has {plural(git.local_only('HEAD'), 'commit')} here that origin doesn't have")
    behind = git.count(f"{head}..{remote}")
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"would pull {plural(behind, 'commit')} and decrypt")
    moved = git.run("merge", "--ff-only", "--quiet", remote, timeout=MERGE_TIMEOUT)
    if not moved.ok:
        return _update_failed(item, moved, f"pulling {plural(behind, 'commit')}", [])
    return _decrypt(item, path, timeout, f"pulled {plural(behind, 'commit')}")


def _decrypt(item, path, timeout, done):
    decrypted = shell(item.get("decrypt"), cwd=path, timeout=timeout)
    if not decrypted.ok:
        return result(item, FAILED, f"{done}, but decrypting failed (exit {decrypted.code}); check that this machine's key can read it")
    checked = shell(item.get("check"), cwd=path, timeout=timeout)
    if not checked.ok:
        return result(item, FAILED, f"{done} and decrypted, but the check afterwards failed (exit {checked.code})")
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
    return result(item, OK, f"{plural(len(item.get('targets')), 'target')} match the config")
