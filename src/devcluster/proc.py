"""Run commands with a timeout, and keep credentials out of anything recorded."""

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass
class Result:
    code: int
    out: str
    err: str
    timed_out: bool = False

    @property
    def ok(self):
        return self.code == 0 and not self.timed_out

    def first_line(self):
        for line in (self.err + "\n" + self.out).splitlines():
            line = line.strip()
            if line and not line.startswith("hint:"):
                return redact(line)
        return f"exit {self.code}"


def run(args, cwd=None, timeout=300, env=None, input=None):
    """Run a command in its own process group. On timeout the whole group is stopped."""
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as error:
        return Result(127, "", f"{args[0]}: {error.strerror}")
    try:
        out, err = proc.communicate(input, timeout=timeout)
        return Result(proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        _stop(proc)
        out, err = proc.communicate()
        return Result(proc.returncode if proc.returncode is not None else -1, out, err, timed_out=True)


def _stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def shell(command, cwd=None, timeout=300, env=None):
    return run(["/bin/sh", "-c", command], cwd=cwd, timeout=timeout, env=env)


_SECRETS = [
    (re.compile(r"(\w+://)[^/\s@]+@"), r"\1***@"),
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{16,}"), r"\1***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"), "github_pat_***"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]{12,}"), r"\1***"),
    (re.compile(r"\b(xox[abposr]-)[A-Za-z0-9-]{8,}"), r"\1***"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2***"),
]


def redact(text):
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def tail(text, limit=2000):
    text = redact(text.strip())
    return text if len(text) <= limit else "..." + text[-limit:]


# Commands that outlive the run that starts them: the fixer's agent and nightly items.


def start(script, cwd, env, name, setenv=(), label=()):
    """Start a shell script in the background so it keeps running after this run ends. `name`
    is its systemd unit on Linux; `label` goes on its command line elsewhere. Returns how to
    find it again, or None and the reason it couldn't start."""
    if env.get("INVOCATION_ID") and shutil.which("systemd-run"):
        # A systemd service stops everything it started when its run ends, so this gets a unit
        # of its own.
        args = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={name}", f"--working-directory={cwd}", "--property=TimeoutStopSec=15"]
        for key in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "DEVCLUSTER_HOME", *setenv):
            if env.get(key):
                args.append(f"--setenv={key}={env[key]}")
        started = run(args + ["/bin/sh", "-c", script], timeout=60, env=env)
        if not started.ok:
            return None, started.first_line() or "systemd-run failed"
        return {"unit": name}, None
    try:
        # Its own session, so launchd doesn't stop it when this run ends.
        proc = subprocess.Popen(
            ["/bin/sh", "-c", script, *label],
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        return None, error.strerror
    return {"pid": proc.pid, "boot": boot_time()}, None


def alive(running):
    """Whether anything a started command began is still running: its systemd unit, or on macOS
    any process in its process group, which it can't leave without trying to."""
    if running.get("unit"):
        return run(["systemctl", "--user", "is-active", "--quiet", running["unit"]], timeout=30).ok
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


def stop(running):
    """Stop a started command and everything it began. True once none of it is left."""
    if running.get("unit"):
        run(["systemctl", "--user", "stop", running["unit"]], timeout=60)
        if alive(running):
            run(["systemctl", "--user", "kill", "--signal=SIGKILL", running["unit"]], timeout=30)
            time.sleep(1)
        return not alive(running)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(running["pid"], sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        for _ in range(20):
            if not alive(running):
                return True
            time.sleep(0.25)
    return not alive(running)


def boot_time():
    """When this machine last started, to tell a process from before a restart."""
    try:
        with open("/proc/stat") as stat:
            for line in stat:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    shown = run(["sysctl", "-n", "kern.boottime"], timeout=10)
    match = re.search(r"sec = (\d+)", shown.out)
    return int(match.group(1)) if match else None
