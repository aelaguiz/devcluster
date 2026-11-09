"""The keep list: folders a machine's nightly cleanup leaves whole until a date.

Most of what fills a disk is left over from finished work, so the cleanup treats anything
without a sign of life as finished. Some folders are still needed and show no sign of it, such
as results someone is reading. Whoever needs one writes it here, with who, why and until when,
and every machine's cleanup sees it on its next sync. The list lives in the config repo as
keep.toml; `devcluster keep` edits it, checks it, commits it and pushes it.
"""

import datetime
import json
import os
import posixpath
import sys
import time

from . import config as configs
from .git import Git
from .items.checkouts import FAST_FORWARD
from .paths import Layout, now as clock_now
from .sync import lock

FILE = "keep.toml"
HEADER = """\
# Folders the nightly disk cleanup keeps whole until their date, and who needs them.
# Edit it with `devcluster keep add`, `keep list` and `keep remove`; each write checks the
# list, commits it and pushes it, so every machine has it on its next sync.
"""
TRIES = 4           # pushes before giving up when other machines keep pushing first
LOCK_WAIT = 600     # seconds to wait for this machine's sync to finish with the config checkout


def today():
    return datetime.date.fromtimestamp(clock_now())


def active(entries, machine, on=None):
    """This machine's entries whose date hasn't passed. An entry keeps its folder to the end of
    its until day."""
    on = on or today()
    return [e for e in entries if e["machine"] == machine and e["until"] >= on]


def kept_paths(entries, machine, on=None):
    """The folders this machine's entries keep, resolved through links."""
    return [os.path.realpath(e["path"]) for e in active(entries, machine, on)]


def render(entries, header=HEADER):
    """keep.toml's text: a comment, then one table per entry, sorted by machine and path."""
    out = [header.rstrip("\n"), ""]
    for entry in sorted(entries, key=lambda e: (e["machine"], e["path"])):
        out.append("[[keep]]")
        for key in configs.KEEP_FIELDS:
            value = entry[key]
            if isinstance(value, datetime.date):
                value = value.isoformat()
            out.append(f"{key} = {_string(value)}")
        out.append("")
    return "\n".join(out)


def _string(value):
    # A TOML basic string: JSON's escapes are valid TOML escapes.
    return json.dumps(str(value), ensure_ascii=False)


def _header(text):
    """The comment lines at the top of an existing keep.toml, kept when it is rewritten."""
    lines = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(line)
        elif line.strip() or lines:
            break
    return "\n".join(lines) + "\n" if lines else HEADER


# The command


def command(args):
    layout = Layout()
    try:
        config = configs.load(layout.config)
    except configs.ConfigError as error:
        return _fail(f"the config has problems, so the keep list can't be read: {error.errors[0]}")
    here = layout.machine()
    if args.keep_command == "list":
        return _list(config, here, args)
    machine = args.machine or here
    if not machine:
        return _fail("this machine has no devcluster name; say which machine with --machine")
    if machine not in config.machines:
        return _fail(f"there is no machine {machine!r} in the config")
    path = _path(args.path, machine == here, must_exist=args.keep_command == "add")
    if path is None:
        return _fail(f"{args.path} doesn't exist on this machine" if machine == here else f"{args.path} isn't a full path")
    if args.keep_command == "add":
        until = configs.parse_date(args.until)
        if until is None:
            return _fail(f"--until is the last day it's kept, written YYYY-MM-DD; {args.until!r} isn't one")
        if until < today():
            return _fail(f"{until} has passed; give the last day it's still needed")
        entry = {"machine": machine, "path": path, "who": args.who.strip(), "why": args.why.strip(), "until": until}
        if not entry["who"] or not entry["why"]:
            return _fail("say who relies on it (--who) and why (--why)")
        return write(layout, lambda entries: _add(entries, entry), f"Keep {path} on {machine} until {until}: {entry['why']}",
                     f"kept {path} on {machine} until {until}")
    return write(layout, lambda entries: _remove(entries, machine, path), f"Stop keeping {path} on {machine}",
                 f"{path} on {machine} is off the keep list")


def _path(text, here, must_exist=True):
    """The path an entry names: on this machine resolved through links, and for a new entry
    it must exist; for another machine, a full path as written."""
    if here:
        resolved = os.path.realpath(os.path.expanduser(text))
        return resolved if os.path.exists(resolved) or not must_exist else None
    return posixpath.normpath(text) if text.startswith("/") else None


def _add(entries, entry):
    rest = [e for e in entries if not (e["machine"] == entry["machine"] and e["path"] == entry["path"])]
    return rest + [entry], None


def _remove(entries, machine, path):
    rest = [e for e in entries if not (e["machine"] == machine and e["path"] == path)]
    if len(rest) == len(entries):
        return None, f"{path} on {machine} isn't on the keep list"
    return rest, None


def _list(config, here, args):
    machine = None if args.all else (args.machine or here)
    entries = [e for e in config.keep if machine is None or e["machine"] == machine]
    if not entries:
        print(f"Nothing is on the keep list{' for ' + machine if machine else ''}.")
        return 0
    on = today()
    for entry in sorted(entries, key=lambda e: (e["machine"], e["until"], e["path"])):
        state = f"until {entry['until']}" if entry["until"] >= on else f"expired {entry['until']}"
        print(f"{entry['machine']}  {entry['path']}  {state}\n    {entry['who']}: {entry['why']}")
    return 0


def _fail(message):
    print(f"devcluster keep: {message}", file=sys.stderr)
    return 1


# Writing the list


def write(layout, change, message, done):
    """Apply `change` to the entries in the config checkout's keep.toml, check the whole config,
    commit only keep.toml and push. When another machine pushed first, the edit is made again on
    top of origin's newest commit, so nothing anyone else pushed is lost and no merge can
    conflict. A push that still fails undoes this machine's commit, so the config checkout never
    holds a commit origin doesn't have (that would stop its next sync)."""
    git = Git(layout.config)
    if not git.is_checkout():
        return _fail(f"there's no config checkout at {layout.config}; run devcluster install")
    held = _wait_for_sync(layout)
    if held is None:
        return _fail("this machine's sync kept the config checkout busy for 10 minutes; try again")
    try:
        problem = _ready(git)
        if problem:
            return _fail(problem)
        branch = git.branch()
        for attempt in range(TRIES):
            moved = _catch_up(git, branch)
            if moved:
                return _fail(moved)
            outcome = _commit(layout, git, change, message)
            if isinstance(outcome, str):
                return _fail(outcome)
            if outcome is False:
                print(f"{done}; it was already so.")
                return 0
            pushed = git.run("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", timeout=300)
            if pushed.ok:
                print(f"{done}.")
                return 0
            git.run("reset", "--quiet", "--keep", "HEAD~1", timeout=120)  # this run's own commit, never pushed
            if not _rejected(pushed.err):
                return _fail(f"couldn't push the keep list: {pushed.first_line()}; nothing changed")
            time.sleep(min(2 ** attempt, 8))
        return _fail(f"other machines kept pushing to the config first, {TRIES} times; nothing changed, try again")
    finally:
        held.close()


def _wait_for_sync(layout):
    layout.state.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + LOCK_WAIT
    told = False
    while True:
        held = lock(layout.state / "sync.lock")
        if held is not None or time.time() > deadline:
            return held
        if not told:
            print("Waiting for this machine's sync to finish with the config checkout...", file=sys.stderr)
            told = True
        time.sleep(2)


def _ready(git):
    busy = git.operation_in_progress()
    if busy:
        return f"the config checkout is in the middle of {busy}"
    if git.index_locked():
        return "the config checkout is busy (its index is locked)"
    if git.out("status", "--porcelain", "--", FILE, timeout=120):
        return f"{FILE} in the config checkout has changes nobody committed; commit or drop them first"
    if not git.branch():
        return "the config checkout isn't on a branch"
    return None


def _catch_up(git, branch):
    fetched = git.run("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=300)
    if not fetched.ok:
        return f"couldn't fetch the config: {fetched.first_line()}"
    remote = f"refs/remotes/origin/{branch}"
    if git.count(remote + "..HEAD"):
        return "the config checkout has commits origin doesn't have; push or drop them first"
    if git.rev("HEAD") != git.rev(remote):
        moved = git.run(*FAST_FORWARD, remote, timeout=300)
        if not moved.ok:
            return f"couldn't bring the config checkout up to date: {moved.first_line()}"
    return None


def _commit(layout, git, change, message):
    """Write the changed list and commit it. False when nothing changed; text when it can't."""
    path = layout.config / FILE
    text = path.read_text() if path.exists() else ""
    entries, problem = _entries(layout, text)
    if problem:
        return problem
    on = today()
    changed, problem = change([e for e in entries if e["until"] >= on])
    if problem:
        return problem
    new = render(changed, _header(text))
    if new == text:
        return False
    path.write_text(new)
    try:
        configs.load(layout.config)
    except configs.ConfigError as error:
        _restore(git, path, text)
        return f"the keep list wouldn't be valid: {error.errors[0]}"
    git.run("add", "--", FILE, timeout=60)
    # A path after -- commits only that file, whatever else someone has staged here.
    committed = git.run("commit", "--quiet", "-m", message, "--", FILE, timeout=120, env=_identity(layout))
    if not committed.ok:
        _restore(git, path, text)
        return f"couldn't commit the keep list: {committed.first_line()}"
    return True


def _entries(layout, text):
    if not text.strip():
        return [], None
    errors = []
    names = {p.stem for p in (layout.config / "machines").glob("*.toml")}
    entries = configs.read_keep(layout.config / FILE, names, errors)
    if errors:
        return None, f"{FILE} has problems: {errors[0]}"
    return entries, None


def _restore(git, path, text):
    if git.run("ls-files", "--error-unmatch", FILE, timeout=60).code == 0:
        git.run("checkout", "--quiet", "--", FILE, timeout=60)
    elif text:
        path.write_text(text)
    else:
        path.unlink(missing_ok=True)


def _identity(layout):
    try:
        settings = configs.load(layout.config).settings
    except configs.ConfigError:
        return None
    env = {}
    if settings.get("git_name"):
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = settings["git_name"]
    if settings.get("git_email"):
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = settings["git_email"]
    return env or None


def _rejected(err):
    text = err.lower()
    return any(word in text for word in ("rejected", "fetch first", "non-fast-forward", "failed to update ref"))
