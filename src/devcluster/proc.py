"""Run commands with a timeout, and keep credentials out of anything recorded."""

import os
import re
import signal
import subprocess
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
