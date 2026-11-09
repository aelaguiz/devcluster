"""When an item stays stuck, save everything local, then start an agent to fix it.

The engine stays plain code and stops whenever going on could lose work. This module acts only
on that path. When an item is still stuck on a later run, it saves the whole checkout to a
branch, writes a brief, and starts the agent command the config names, in the background.
Later runs leave the item to the agent, read what it did, give it one more try if the item is
stuck in a new way, and send the owner one message when a fix needs their hands. It also has
each machine watch the next one in the list, so a machine that stops reporting gets checked
over SSH.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import status
from .git import Git
from .paths import clock, engine_root, expand, now as clock_now
from .proc import run as run_command, tail
from .results import BLOCKED, BUSY, CHANGED, FAILED, OK, ItemResult

CHECKOUTS = ("repo", "tool", "secrets")
HANDED = ("repo", "tool", "secrets", "cli", "file", "settings")
OUTCOMES = ("fixed", "waiting", "needs-owner", "could-not-fix")
DEFAULTS = {"timeout": 1800, "wait": 1800, "failed_after": 1800, "quiet_after": 1200}
NOTE_FOR = 24 * 3600     # how long a fixed item's status line says an agent fixed it
MAX_WAITS = 6            # "someone is working there" this many times running, then the owner hears
MAX_STARTS = 3           # the agent command ending at once without a result, then the owner hears
QUICK = 120              # seconds; an agent that ends sooner without a result never got going
NOT_TRIES = ("waiting", "no-start")
KEEP_RUNS = 30 * 86400   # run folders older than this are removed
SKILL = engine_root() / "skills" / "devcluster-fixer" / "SKILL.md"


def settings(config, machine):
    """fixer.toml's settings for this machine, or None when it names no fixer command."""
    merged = dict(DEFAULTS)
    merged.update(config.fixer)
    merged.update({k: v for k, v in config.fixer_machines.get(machine, {}).items() if k in DEFAULTS or k in ("command", "notify")})
    return merged if merged.get("command") else None


def _machine_setting(config, machine, key, default):
    return config.fixer_machines.get(machine, {}).get(key, default)


def fingerprint(result):
    """How an item is stuck, without the numbers that change from run to run. The detail (the
    files in the way, or the command's own output) names the problem when there is one; the
    summary around it changes wording, such as an install that failed and then waits to retry."""
    def steady(text):
        return re.sub(r"\b[0-9a-f]{7,40}\b|\d+", "#", text or "")
    what = result.detail if (result.detail or "").strip() else result.summary
    return hashlib.sha256(f"{result.state}\n{steady(what)}".encode()).hexdigest()[:16]


def key_of(kind, name):
    return f"{kind}:{name}"


class Fixer:
    def __init__(self, layout, ctx, config, machine, chosen, persist=None):
        self.layout = layout
        self.persist = persist or (lambda: None)  # writes items.json, so a crash can't lose a started agent
        self.ctx = ctx
        self.config = config
        self.machine = machine
        self.settings = chosen
        self.now = ctx.now
        self.root = layout.state / "fixer"
        self.state = ctx.memory.setdefault("fixer", {})
        for name in ("episodes", "fixed", "quiet"):
            self.state.setdefault(name, {})

    # Before the items run

    def check(self, dry_run=False):
        """Finish off a fixer that has stopped. Returns the results to report, by item key,
        for anything an agent is still working on, so the run leaves those items alone.
        A dry run only reports; it never stops an agent or records what one did."""
        running = self.state.get("running")
        if not dry_run:
            self._prune(running)
        if not running:
            return {}
        if dry_run:
            return {running["key"]: self._busy(running)}
        if _alive(running):
            if self.now <= running["deadline"]:
                return {running["key"]: self._busy(running)}
            if not _stop(running):
                return {running["key"]: self._busy(running, stopping=True)}
            minutes = round((running["deadline"] - running["started"]) / 60)
            outcome = {"outcome": "could-not-fix", "summary": f"the fixer ran out of time after {minutes} minutes", "details": _log_tail(running)}
        else:
            outcome = _read_result(Path(running["dir"]) / "result.json")
            if outcome is None and _ran_for(running) < QUICK:
                # A busy account, a rate limit or a network blip: the agent never got going,
                # so this isn't a try, and the next run starts it again.
                outcome = {"outcome": "no-start", "summary": "the agent command ended at once without a result", "details": _log_tail(running)}
            elif outcome is None:
                outcome = {"outcome": "could-not-fix", "summary": "the fixer stopped without writing a result", "details": _log_tail(running)}
        self._record(running, outcome)
        self.state["running"] = None
        return {}

    def _prune(self, running):
        if not self.root.is_dir():
            return
        keep = Path(running["dir"]) if running else None
        for folder in self.root.iterdir():
            try:
                # File times are the real clock's, so compare with the real clock.
                old = folder.is_dir() and folder.name != "messages" and time.time() - folder.stat().st_mtime > KEEP_RUNS
            except OSError:
                continue
            if old and folder != keep:
                shutil.rmtree(folder, ignore_errors=True)

    def _busy(self, running, stopping=False):
        kind, _, name = running["key"].partition(":")
        saved = running.get("saved") or {}
        where = f"; everything that was here is saved on branch {saved['branch']}" if saved.get("branch") else ""
        if stopping:
            return ItemResult(kind, name, BUSY, f"its agent ran out of time at {clock(running['deadline'])} and is still being stopped")
        if kind == "machine":
            return ItemResult("machine", name, BUSY, f"stopped reporting at {clock(running['quiet_since'])}; an agent has been checking it over SSH since {clock(running['started'])}")
        return ItemResult(kind, name, BUSY, f"an agent has been fixing it since {clock(running['started'])}{where}")

    def _record(self, running, outcome):
        key = running["key"]
        kind, _, name = key.partition(":")
        episode = self.state["quiet"].get(name) if kind == "machine" else self.state["episodes"].get(key)
        if kind != "machine":
            # The item's next run is the test of the fix, so it shouldn't wait out the hour
            # a failed install normally waits before it is tried again.
            for field in ("failed", "failed_at", "failed_step", "failure"):
                self.ctx.memory.get(key, {}).pop(field, None)
        if episode is None:
            return
        attempt = {
            "at": running["started"],
            "fp": running["fp"],
            "outcome": outcome["outcome"],
            "summary": outcome.get("summary", ""),
            "details": outcome.get("details", ""),
            "message": outcome.get("message", ""),
            "saved": running.get("saved"),
            "dir": running["dir"],
        }
        episode.setdefault("attempts", []).append(attempt)
        if outcome["outcome"] == "waiting":
            waits = sum(1 for a in episode["attempts"] if a["outcome"] == "waiting")
            episode["next_try"] = self.now + min(self.settings["wait"] * 2 ** (waits - 1), 4 * 3600)
        elif kind == "machine":
            # Give the machine's own timer time to report before deciding the fix didn't work.
            episode["next_try"] = self.now + self.settings["quiet_after"]

    # After the items run

    def after(self, plan, results):
        """Track stuck items, note recent fixes on their status lines, hand one stuck item to
        the fixer, and watch the neighbouring machine. Returns extra results to report."""
        if not self.settings.get("command"):
            return []  # turned off: an agent already running is still seen through by check()
        trial = {key_of(i.kind, i.name) for i in plan if i.get("mode") == "dry-run"}
        handled = [r for r in results if r.kind in HANDED and key_of(r.kind, r.name) not in trial]
        busy = {key_of(r.kind, r.name) for r in handled if r.state == BUSY}
        self._track(handled, busy, {key_of(i.kind, i.name) for i in plan})
        self._notes(handled)
        self._hand_off(plan, handled)
        return self._watch()

    def _track(self, results, busy, planned):
        episodes = self.state["episodes"]
        for result in results:
            key = key_of(result.kind, result.name)
            if key in busy:
                continue
            if result.state in (BLOCKED, FAILED):
                episode = episodes.setdefault(key, {"since": self.now, "runs": 0, "attempts": []})
                episode["runs"] += 1
                episode.update(state=result.state, fp=fingerprint(result), summary=result.summary, detail=result.detail)
            elif result.state in (OK, CHANGED):
                episode = episodes.pop(key, None)
                fixes = [a for a in (episode or {}).get("attempts", []) if a["outcome"] == "fixed"]
                if fixes:
                    self.state["fixed"][key] = {"at": fixes[-1]["at"], "summary": fixes[-1]["summary"], "saved": fixes[-1].get("saved")}
        running = (self.state.get("running") or {}).get("key")
        for key in [k for k in episodes if k not in planned and k != running]:
            del episodes[key]

    def _notes(self, results):
        fixed = self.state["fixed"]
        for key in list(fixed):
            if self.now - fixed[key]["at"] > NOTE_FOR:
                del fixed[key]
        for result in results:
            fix = fixed.get(key_of(result.kind, result.name))
            if fix and result.state in (OK, CHANGED):
                result.summary += f" (an agent fixed it at {clock(fix['at'])}: {fix['summary'].rstrip('.')}{_saved_clause(fix.get('saved'))})"

    def _hand_off(self, plan, results):
        items = {key_of(i.kind, i.name): i for i in plan}
        for result in results:
            key = key_of(result.kind, result.name)
            episode = self.state["episodes"].get(key)
            if not episode or key not in items or result.state not in (BLOCKED, FAILED):
                continue
            if self.now < episode.get("next_try", 0) or episode.get("notified"):
                continue  # the owner hears once, and the item stays theirs until it syncs
            if episode["state"] == BLOCKED:
                ready = episode["runs"] >= 2
            else:
                ready = self.now - episode["since"] >= self.settings["failed_after"]
            if not ready:
                continue
            tries = [a for a in episode["attempts"] if a["outcome"] not in NOT_TRIES]
            waits = [a for a in episode["attempts"] if a["outcome"] == "waiting"]
            owner = [a for a in tries if a["outcome"] == "needs-owner" and a["fp"] == episode["fp"]]
            if owner or len(tries) >= 2 or (tries and tries[-1]["fp"] == episode["fp"]) or len(waits) >= MAX_WAITS or _stalled(episode):
                if self._notify_item(items[key], episode, owner[-1] if owner else None):
                    episode["notified"] = self.now
                continue
            if self.state.get("running"):
                continue
            if self._start_item(items[key], episode):
                busy = self._busy(self.state["running"])
                result.state, result.summary = busy.state, busy.summary

    # Starting a fixer for an item

    def _start_item(self, item, episode):
        run_dir = self._run_dir(f"{item.kind}-{item.name}")
        path = expand(item.get("path")) if item.get("path") else None
        saved = None
        if item.kind in CHECKOUTS and path and path.is_dir() and Git(path).is_checkout():
            in_way = episode.get("detail", "").splitlines() if "would overwrite" in episode.get("summary", "") else []
            who = self.ctx.identity() or {
                "GIT_AUTHOR_NAME": "devcluster", "GIT_AUTHOR_EMAIL": "devcluster@localhost",
                "GIT_COMMITTER_NAME": "devcluster", "GIT_COMMITTER_EMAIL": "devcluster@localhost",
            }
            if item.kind == "secrets":
                here = "it holds credentials, so it stays on this machine"
            elif public_origin(item.get("url")):
                here = "its origin is public, so it stays on this machine"
            else:
                here = None
            saved = save(Git(path, who), self.machine, item, self.now, in_way, push=here is None, here=here)
            saved["public"] = here is not None and item.kind != "secrets"
            if item.kind == "secrets":
                # Decrypted credentials are files Git ignores, so the saved commit doesn't hold
                # them, and they must never reach a branch. A private copy keeps them instead.
                folder = run_dir / "ignored-files"
                saved["ignored"] = {"folder": str(folder), "count": copy_ignored(Git(path), folder)}
        brief = item_brief(self, item, episode, saved, run_dir, path)
        (run_dir / "brief.md").write_text(brief)
        cwd = path if path and path.is_dir() else Path.home()
        return self._launch(key_of(item.kind, item.name), run_dir, cwd, episode["fp"], saved=saved)

    def _run_dir(self, label):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now))
        run_dir = self.root / f"{stamp}-{re.sub(r'[^A-Za-z0-9._-]', '-', label)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _launch(self, key, run_dir, cwd, fp, **extra):
        at = clock_now()  # not the run's start: the items before this may have taken a while
        started, problem = launch(self.settings["command"], run_dir, cwd, key, at)
        running = {
            "key": key,
            "dir": str(run_dir),
            "started": at,
            "deadline": at + self.settings["timeout"],
            "fp": fp,
            **extra,
        }
        if started is None:
            self._record(running, {"outcome": "could-not-fix", "summary": f"the fixer command couldn't start: {problem}"})
            return False
        running.update(started)
        self.state["running"] = running
        self.persist()
        return True

    # Telling the owner

    def _notify_item(self, item, episode, owner_attempt):
        label = f"{item.kind} {item.name}"
        if owner_attempt:
            opening = f"The {label} on {self.machine} has been stuck since {_when(episode['since'])}. devcluster started an agent on it, and the fix needs you."
        elif _stalled(episode):
            opening = f"The {label} on {self.machine} has been stuck since {_when(episode['since'])}. devcluster tried {MAX_STARTS} times to start an agent on it, and the agent command ended at once each time."
        else:
            opening = f"The {label} on {self.machine} has been stuck since {_when(episode['since'])}. devcluster started an agent on it, and the agent couldn't fix it."
        lines = [opening, ""]
        lines += ["What devcluster says:", f"  {episode['summary']}", ""]
        lines += _attempt_lines(episode)
        saved = next((a["saved"] for a in reversed(episode["attempts"]) if a.get("saved")), None)
        if saved and saved.get("branch"):
            lines += [f"Everything that was in the checkout is saved on branch {saved['branch']}{' on origin' if saved.get('pushed') else ' on this machine only'}."]
        if owner_attempt:
            subject = f"devcluster on {self.machine}: {item.name} needs you"
        elif _stalled(episode):
            subject = f"devcluster on {self.machine}: the agent for {item.name} won't start"
        else:
            subject = f"devcluster on {self.machine}: {item.name} is stuck, and the agent couldn't fix it"
        return self._notify(subject, "\n".join(lines) + "\n", key_of(item.kind, item.name))

    def _notify(self, subject, body, label):
        command = self.settings.get("notify")
        if not command:
            return True  # no way to reach anyone; the status page still says it
        folder = self.root / "messages"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now))
        message = folder / f"{stamp}-{re.sub(r'[^A-Za-z0-9._-]', '-', label)}.txt"
        message.write_text(body)
        env = dict(os.environ, DEVCLUSTER_SUBJECT=subject, DEVCLUSTER_MESSAGE=str(message), DEVCLUSTER_MACHINE=self.machine)
        sent = run_command(["/bin/sh", "-c", command], timeout=180, env=env, input=body)
        with open(folder / "sent.log", "a") as log:
            log.write(f"{stamp} {'sent' if sent.ok else 'failed'} {subject}" + ("" if sent.ok else f": {sent.first_line()}") + "\n")
        return sent.ok

    # Watching the next machine

    def _watch(self):
        """When a machine stops reporting, the first machine after it in the list whose own
        report is fresh checks on it."""
        quiet_after = self.settings["quiet_after"]
        names = sorted(self.config.machines)
        if len(names) < 2:
            return []
        reports, problem = status.read_all(self.layout)
        if problem:
            return []  # stale reports would make every machine look quiet to every other
        last = {name: (reports.get(name) or {}).get("finished") for name in names}
        last[self.machine] = self.now
        results = []
        for name in names:
            if name == self.machine:
                continue
            seen = last.get(name)
            if not seen or self.now - seen <= quiet_after or self.config.machines[name].mode != "on":
                # Never reported (not set up yet), reporting, or not syncing for real.
                self.state["quiet"].pop(name, None)
                continue
            if not _machine_setting(self.config, name, "watched", True):
                continue
            later = names[names.index(name) + 1:] + names[:names.index(name)]
            watcher = next((n for n in later if self.config.machines[n].mode == "on" and last.get(n) and self.now - last[n] <= quiet_after), None)
            if watcher != self.machine:
                continue
            episode = self.state["quiet"].get(name)
            if not episode or episode["since"] != seen:
                episode = self.state["quiet"][name] = {"since": seen, "attempts": []}
            results.append(self._check_quiet(name, episode, reports.get(name) or {}))
        return results

    def _check_quiet(self, name, episode, report):
        since = clock(episode["since"])
        running = self.state.get("running")
        if running and running["key"] == key_of("machine", name):
            return self._busy(running)
        if episode.get("given_up"):
            return self._quiet_notice(name, episode, episode["given_up"], "gave-up")
        target = _machine_setting(self.config, name, "ssh", "")
        if not target:
            return self._quiet_notice(name, episode, "and fixer.toml names no ssh target to check it with", "no-ssh")
        if episode.get("unreachable") and self.now < episode.get("next_ssh", 0):
            return self._quiet_notice(name, episode, episode["unreachable"], "unreachable")
        reach = run_command([_ssh(), *SSH_OPTIONS, target, "true"], timeout=45)
        if not reach.ok:
            # Look again every ten minutes, so a machine that wakes up without reporting
            # still gets an agent.
            episode["unreachable"] = f"and can't be reached over SSH ({target}: {reach.first_line()}); it may be off, asleep or off the network"
            episode["next_ssh"] = self.now + 600
            return self._quiet_notice(name, episode, episode["unreachable"], "unreachable")
        episode.pop("unreachable", None)
        tries = [a for a in episode["attempts"] if a["outcome"] not in NOT_TRIES]
        if len(tries) >= 2 or any(a["outcome"] == "needs-owner" for a in tries) or _stalled(episode):
            episode["given_up"] = "and the fixer couldn't get it reporting again"
            return self._quiet_notice(name, episode, episode["given_up"], "gave-up")
        if running or self.now < episode.get("next_try", 0):
            return ItemResult("machine", name, BUSY, f"stopped reporting at {since}; an agent will check it over SSH")
        run_dir = self._run_dir(f"machine-{name}")
        (run_dir / "brief.md").write_text(machine_brief(self, name, target, episode, report, run_dir))
        if self._launch(key_of("machine", name), run_dir, Path.home(), f"quiet-{episode['since']}", quiet_since=episode["since"]):
            return self._busy(self.state["running"])
        return ItemResult("machine", name, FAILED, f"stopped reporting at {since}; the fixer command couldn't start")

    def _quiet_notice(self, name, episode, reason, kind):
        """The quiet machine's status line, and one message to the owner for each kind of problem."""
        since = clock(episode["since"])
        told = episode.setdefault("told", {})
        if kind not in told:
            lines = [f"{name} stopped reporting to devcluster at {_when(episode['since'])} {reason}.", ""]
            lines += _attempt_lines(episode)
            lines += [f"This was noticed by {self.machine}, the next machine in the list."]
            if self._notify(f"devcluster: {name} stopped reporting", "\n".join(lines) + "\n", f"machine-{name}"):
                told[kind] = self.now
        when = f"; the owner was told at {clock(told[kind])}" if kind in told else ""
        return ItemResult("machine", name, BLOCKED, f"stopped reporting at {since} {reason}{when}")


# No prompts, a short wait, and a neighbour's host key accepted the first time (a changed one is
# still refused). The system's ssh, because a wrapper on PATH may expect a terminal.
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new")


def _ssh():
    return os.environ.get("DEVCLUSTER_SSH") or ("/usr/bin/ssh" if Path("/usr/bin/ssh").exists() else "ssh")


# Saving a checkout


def save(git, machine, item, now, in_way=(), push=True, here=None):
    """One commit holding everything local in the checkout, on a devcluster/saved/ branch.
    It is built from a copy of the index, so the working tree, the real index and the branch
    are untouched; the copy keeps a sparse checkout's skip flags, so files outside the sparse
    folders aren't recorded as deleted. Local commits are its ancestors."""
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(now))
    branch = f"devcluster/saved/{machine}/{item.name}/{stamp}"
    index = git.git_path("index")
    copy = git.git_path("devcluster-save-index")
    if index is None or copy is None:
        return {"error": "couldn't find the checkout's Git folder"}
    head = git.rev("HEAD")
    env = {"GIT_INDEX_FILE": str(copy)}
    try:
        if index.exists():
            shutil.copyfile(index, copy)
        elif head:
            git.run("read-tree", head, env=env, timeout=600)
        # What was staged, before the working tree is added over it. This fails when the index
        # holds a conflict; then the working tree's version is what there is.
        staged_tree = git.run("write-tree", env=env, timeout=600)
        added = git.run("add", "-A", "--sparse", env=env, timeout=3600)
        if not added.ok:
            return {"error": f"couldn't record the local files: {added.first_line()}"}
        named = [name for name in in_way if name and (git.path / name).exists()]
        if named:
            git.run("--literal-pathspecs", "add", "-f", "--sparse", "--", *named, env=env, timeout=600)
        tree = git.run("write-tree", env=env, timeout=600)
        if not tree.ok:
            return {"error": f"couldn't record the local files: {tree.first_line()}"}
    finally:
        try:
            copy.unlink()
        except OSError:
            pass
    message = (
        f"devcluster saved {item.kind} {item.name} on {machine} before handing it to the fixer\n\n"
        f"Everything local in {item.get('path')} at {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}: "
        "its commits, uncommitted edits and untracked files. The engine changed nothing here.\n"
    )
    parents = ["-p", head] if head else []
    head_tree = git.out("rev-parse", f"{head}^{{tree}}", timeout=60) if head else None
    if staged_tree.ok and staged_tree.out.strip() not in (head_tree, tree.out.strip()):
        # Staged, then changed again: the staged version is the second parent, as git stash keeps it.
        staged = git.run("commit-tree", staged_tree.out.strip(), *parents, "-m", "What was staged", timeout=120)
        if staged.ok:
            parents += ["-p", staged.out.strip()]
    commit = git.run("commit-tree", tree.out.strip(), *parents, "-m", message, timeout=120)
    if not commit.ok:
        return {"error": f"couldn't make the saved commit: {commit.first_line()}"}
    sha = commit.out.strip()
    named = git.run("update-ref", f"refs/heads/{branch}", sha, timeout=60)
    if not named.ok:
        return {"error": f"made the saved commit {sha[:10]} but couldn't name a branch for it: {named.first_line()}", "commit": sha}
    saved = {"branch": branch, "commit": sha, "pushed": False}
    if push:
        pushed = git.run("push", "--quiet", "origin", f"{sha}:refs/heads/{branch}", timeout=int(item.get("timeout", 600)))
        saved["pushed"] = pushed.ok
        if not pushed.ok:
            saved["error"] = pushed.first_line() or "the push failed"
    else:
        saved["error"] = here or "it stays on this machine"
    return saved


def public_origin(url, timeout=30):
    """Whether anyone can read the origin without signing in, as with a public GitHub repo. A
    saved branch pushed there would publish this machine's uncommitted work: notes, plans and
    untracked files nobody meant to share. So the branch stays here instead.

    Asked anonymously over HTTPS, with no credentials and none of this user's Git settings, so a
    rewrite to SSH can't answer with this machine's key. A local origin is nobody else's, so it
    isn't public. When the answer is unclear (offline, a host that won't say), it counts as
    public: a branch kept here is still kept, and one pushed to a public repo can't be taken back."""
    https = anonymous_url(url)
    if not https:
        return False
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_ASKPASS", "SSH_ASKPASS")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    probe = run_command(["git", "-c", "credential.helper=", "-c", "core.askPass=", "ls-remote", "--heads", https],
                        timeout=timeout, env=env)
    if probe.ok:
        return True
    text = (probe.err + probe.out).lower()
    return not any(sign in text for sign in (
        "could not read username", "authentication failed", "repository not found",
        "terminal prompts disabled", "401", "403",
    ))


def anonymous_url(url):
    """The HTTPS address of a network origin, without any user name or token in it. None for a
    local path or file:// origin."""
    url = (url or "").strip()
    scp = re.match(r"^[^@/:]+@([^:/]+):(?!/)(.+)$", url)
    if scp:
        return f"https://{scp.group(1)}/{scp.group(2)}"
    ssh = re.match(r"^ssh://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$", url)
    if ssh:
        return f"https://{ssh.group(1)}/{ssh.group(2)}"
    web = re.match(r"^(https?)://(?:[^@/]+@)?(.+)$", url)
    if web:
        return f"{web.group(1)}://{web.group(2)}"
    return None


def copy_ignored(git, folder):
    """Copy the files Git ignores in a checkout to a folder only this user can read. Returns how many."""
    listed = git.run("ls-files", "-z", "--others", "--ignored", "--exclude-standard", timeout=600)
    names = [name for name in listed.out.split("\0") if name] if listed.ok else []
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    count = 0
    for name in names[:5000]:
        source = git.path / name
        try:
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 50_000_000:
                continue
            target = folder / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source, target)
            count += 1
        except OSError:
            continue
    return count


# Starting the agent


def launch(command, run_dir, cwd, key, now):
    """Start the fixer command in the background with the brief on standard input. Returns
    how to find it again, or None and the reason it couldn't start."""
    brief = run_dir / "brief.md"
    env = dict(os.environ)
    env.pop("DEVCLUSTER_CARRY", None)
    env.update(DEVCLUSTER_FIXER_BRIEF=str(brief), DEVCLUSTER_FIXER_RESULT=str(run_dir / "result.json"), DEVCLUSTER_FIXER_DIR=str(run_dir))
    # The second command keeps the shell from replacing itself with the agent, so the shell
    # (whose command line names the run folder) lives exactly as long as the agent does.
    script = f'{command} < "$DEVCLUSTER_FIXER_BRIEF" > "$DEVCLUSTER_FIXER_DIR/fixer.log" 2>&1; echo $? > "$DEVCLUSTER_FIXER_DIR/exit"'
    if env.get("INVOCATION_ID") and shutil.which("systemd-run"):
        # A systemd service stops everything it started when its run ends, so the fixer gets
        # a unit of its own.
        unit = f"devcluster-fixer-{re.sub(r'[^A-Za-z0-9]', '-', key)}-{int(now)}"
        args = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}", f"--working-directory={cwd}", "--property=TimeoutStopSec=15"]
        for name in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "DEVCLUSTER_HOME", "DEVCLUSTER_FIXER_BRIEF", "DEVCLUSTER_FIXER_RESULT", "DEVCLUSTER_FIXER_DIR"):
            if env.get(name):
                args.append(f"--setenv={name}={env[name]}")
        started = run_command(args + ["/bin/sh", "-c", script], timeout=60, env=env)
        if not started.ok:
            return None, started.first_line() or "systemd-run failed"
        return {"unit": unit}, None
    try:
        # Its own session, so launchd doesn't stop it when this run ends.
        proc = subprocess.Popen(
            ["/bin/sh", "-c", script, "devcluster-fixer", str(run_dir)],
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        return None, error.strerror
    return {"pid": proc.pid, "boot": boot_time()}, None


def _alive(running):
    """Whether anything the agent started is still running: its systemd unit, or on macOS any
    process in its process group, which it can't leave without trying to."""
    if running.get("unit"):
        return run_command(["systemctl", "--user", "is-active", "--quiet", running["unit"]], timeout=30).ok
    group = running.get("pid")
    if not group or (running.get("boot") and running["boot"] != boot_time()):
        return False  # after a restart the number may belong to someone else
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _stop(running):
    """Stop the agent and everything it started. True once none of it is left."""
    if running.get("unit"):
        run_command(["systemctl", "--user", "stop", running["unit"]], timeout=60)
        if _alive(running):
            run_command(["systemctl", "--user", "kill", "--signal=SIGKILL", running["unit"]], timeout=30)
            time.sleep(1)
        return not _alive(running)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(running["pid"], sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        for _ in range(20):
            if not _alive(running):
                return True
            time.sleep(0.25)
    return not _alive(running)


def boot_time():
    """When this machine last started, to tell a process from before a restart."""
    try:
        with open("/proc/stat") as stat:
            for line in stat:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    shown = run_command(["sysctl", "-n", "kern.boottime"], timeout=10)
    match = re.search(r"sec = (\d+)", shown.out)
    return int(match.group(1)) if match else None


def _stalled(episode):
    """True when the last few starts all ended at once without a result."""
    last = episode.get("attempts", [])[-MAX_STARTS:]
    return len(last) == MAX_STARTS and all(a["outcome"] == "no-start" for a in last)


def _ran_for(running):
    """How long the agent command ran, by the clock, from its brief to its exit file."""
    folder = Path(running["dir"])
    try:
        return (folder / "exit").stat().st_mtime - (folder / "brief.md").stat().st_mtime
    except OSError:
        return QUICK  # stopped without the shell finishing: count it as a try


def _read_result(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("outcome") not in OUTCOMES:
        return None
    return {k: str(data.get(k, "")) for k in ("outcome", "summary", "details", "message")}


def _log_tail(running):
    try:
        return tail((Path(running["dir"]) / "fixer.log").read_text(errors="replace"))
    except OSError:
        return ""


# Briefs


def item_brief(fixer, item, episode, saved, run_dir, path):
    kind = item.kind
    lines = [
        f'You are devcluster\'s fixer on the machine "{fixer.machine}". A sync item here has been stuck since {_when(episode["since"])}. Read the fixer skill first: {SKILL}',
        "",
        "## The stuck item",
        "",
        f"- Item: {kind} \"{item.name}\", {_how_worked(item)}",
    ]
    if path:
        lines.append(f"- Path: {path}")
    fields = dict(item.fields)
    if kind in ("repo", "secrets"):
        fields.setdefault("branch", "main")
    for field in ("url", "branch", "ref", "version", "check", "install", "source", "targets", "decrypt"):
        if fields.get(field):
            value = ", ".join(fields[field]) if isinstance(fields[field], list) else fields[field]
            lines.append(f"- {field.capitalize()}: {value}")
    lines += [
        f"- State: {episode['state']}, on every run since it got stuck",
        f"- Status line: {episode['summary']}",
    ]
    if episode.get("detail"):
        lines += ["- Detail:", "", "```", episode["detail"].rstrip(), "```"]
    lines += ["", "## What the engine has done", ""]
    if saved and saved.get("branch"):
        where = "and it reached origin" if saved.get("pushed") else f"on this machine only ({saved.get('error', 'the push failed')})"
        lines.append(f"- Saved everything local in this checkout to branch {saved['branch']} (commit {saved['commit'][:10]}), {where}.")
        if saved.get("public"):
            lines.append("- Anyone can read this repo's origin. Whatever you commit and push there is published, so local notes, plans and other private files stay on this machine.")
    elif saved:
        lines.append(f"- Tried to save this checkout to a branch and couldn't: {saved.get('error')}. Nothing here is saved, so don't remove anything you can't put back.")
    else:
        lines.append("- Nothing was saved: this item isn't a Git checkout devcluster can save.")
    if saved and saved.get("ignored"):
        lines.append(f"- Copied the {saved['ignored']['count']} files Git ignores here, such as the decrypted credentials, to {saved['ignored']['folder']}, which only this user can read. They are not on the saved branch, and must never be.")
    elif saved and saved.get("branch"):
        lines.append("- Files Git ignores, such as build output or a local .env, are not on the saved branch unless Git named them as in the way. Leave them where they are.")
    lines.append("- It leaves this item alone until you exit, then runs again. Don't run the engine yourself.")
    earlier = [a for a in episode.get("attempts", [])]
    if earlier:
        lines += ["", "## Earlier tries on this item", ""]
        for attempt in earlier:
            lines.append(f"- {_when(attempt['at'])}: {attempt['outcome']}: {attempt['summary']}")
            if attempt.get("details"):
                lines += ["", "  " + attempt["details"].replace("\n", "\n  "), ""]
    if path and path.is_dir():
        processes, newest = activity(path)
        lines += ["", "## Activity in the checkout", ""]
        lines.append("- Processes whose working folder is inside it: " + ("; ".join(processes) if processes else "none"))
        if newest:
            lines.append(f"- Newest local change: {_when(newest[0])} ({newest[1]}), {_ago(fixer.now - newest[0])} before this brief")
        sparse = Git(path).out("config", "--bool", "core.sparseCheckout", timeout=30)
        if sparse == "true":
            lines.append("- The checkout is sparse; `git sparse-checkout list` shows its folders.")
    lines += ["", "## When you finish", "", f"Write your result as JSON to {run_dir / 'result.json'}. You have {round(fixer.settings['timeout'] / 60)} minutes."]
    return "\n".join(lines) + "\n"


def machine_brief(fixer, name, target, episode, report, run_dir):
    lines = [
        f'You are devcluster\'s fixer, started on "{fixer.machine}" because the machine "{name}" stopped reporting. Read the fixer skill first, especially its last section: {SKILL}',
        "",
        "## The quiet machine",
        "",
        f"- Machine: {name}, reached with `ssh {target}`",
        f"- Its last report: {_when(episode['since'])}, {_ago(fixer.now - episode['since'])} ago",
        f"- devcluster there lives in ~/.devcluster: state/sync.log and state/timer.log show its recent runs",
    ]
    stuck = [i for i in report.get("items", []) if i.get("state") in (BLOCKED, FAILED)]
    if stuck:
        lines.append("- Items its last report showed as stuck: " + "; ".join(f"{i['kind']} {i['name']}: {i['summary']}" for i in stuck))
    if report.get("errors"):
        lines.append("- Config errors in its last report: " + "; ".join(report["errors"]))
    earlier = episode.get("attempts", [])
    if earlier:
        lines += ["", "## Earlier tries", ""]
        lines += [f"- {_when(a['at'])}: {a['outcome']}: {a['summary']}" for a in earlier]
    lines += [
        "",
        "## When you finish",
        "",
        f"The machine counts as fixed once its timer is running and a sync there succeeds; it reports within about five minutes after that. Write your result as JSON to {run_dir / 'result.json'}. You have {round(fixer.settings['timeout'] / 60)} minutes.",
    ]
    return "\n".join(lines) + "\n"


def activity(path):
    """Processes whose working folder is inside the checkout, and its newest local change."""
    root = os.path.realpath(path)
    found = []
    if Path("/proc").is_dir():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cwd = os.readlink(entry / "cwd")
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                continue
            if cwd == root or cwd.startswith(root + "/"):
                found.append(f"{entry.name} {command[:100]}")
    else:
        listed = run_command(["lsof", "-a", "-d", "cwd", "-F", "pcn"], timeout=60)
        pid = command = None
        for line in listed.out.splitlines():
            if line.startswith("p"):
                pid, command = line[1:], ""
            elif line.startswith("c"):
                command = line[1:]
            elif line.startswith("n") and pid and int(pid) != os.getpid():
                cwd = os.path.realpath(line[1:])
                if cwd == root or cwd.startswith(root + "/"):
                    found.append(f"{pid} {command}")
    newest = None
    changed = Git(path).run("status", "--porcelain", "-z", "--untracked-files=all", timeout=600)
    entries = [e for e in changed.out.split("\0") if len(e) > 3][:5000] if changed.ok else []
    for entry in entries:
        name = entry[3:]
        try:
            stamp = (Path(path) / name).lstat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest[0]:
            newest = (stamp, name)
    return found, newest


def _how_worked(item):
    if item.kind == "repo":
        return f"worked on directly on {item.get('branch', 'main')}; devcluster pulls, merges and pushes it on every machine"
    if item.kind == "tool":
        return f"followed at {item.get('ref', 'its default branch')}; nobody edits it here, and devcluster updates it and runs its install"
    if item.kind == "secrets":
        return "the credentials checkout; never print a secret value"
    if item.kind == "cli" and item.get("from"):
        return f"a command-line tool devcluster installs at the version {item.get('from')} has"
    if item.kind == "cli":
        return "a command-line tool devcluster installs at a set version"
    if item.kind == "settings":
        return f"a settings file copied from {item.get('from')}, the machine the others follow; devcluster replaces this machine's file with that copy"
    return "a file the config puts in place on this machine"


def _attempt_lines(episode):
    lines = []
    for attempt in episode.get("attempts", []):
        lines.append(f"The agent at {_when(attempt['at'])}: {attempt['outcome']}: {attempt['summary']}")
        if attempt.get("message"):
            lines += ["", f"What it needs from you: {attempt['message']}"]
        if attempt.get("details"):
            lines += ["", attempt["details"], ""]
    return lines + ([""] if lines else [])


def _saved_clause(saved):
    if not saved or not saved.get("branch"):
        return ""
    return f"; what was here before is on branch {saved['branch']}"


def _when(stamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def _ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} minutes"
    if seconds < 48 * 3600:
        return f"{round(seconds / 3600)} hours"
    return f"{round(seconds / 86400)} days"
