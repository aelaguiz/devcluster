"""Commands that run once a night on each machine, such as Git's maintenance on its repos.

A nightly item starts in the background during its hours, so a long run never holds up the
sync, and later runs report how it went. It runs once a night. A machine asleep through its
hours skips that night rather than catching up the next morning, while someone may be using it.
"""

import os
import re
import time
from pathlib import Path

from ..context import result
from ..paths import clock, expand
from ..proc import alive, redact, start, stop, tail
from ..results import CHANGED, FAILED, OK, WOULD_CHANGE

HOURS = "1-5"
TIMEOUT = 3600
CHECKOUTS = ("secrets", "repo", "tool")
_HOURS = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def parse_hours(text):
    """(first, last) from text such as "1-5" or "23-4", or None when it isn't two different
    hours of the day."""
    match = _HOURS.match(text) if isinstance(text, str) else None
    if not match:
        return None
    first, last = int(match.group(1)), int(match.group(2))
    if first > 23 or last > 23 or first == last:
        return None
    return first, last


def window(now, hours):
    """When tonight's hours began and end, if `now` is inside them; otherwise None."""
    first, last = parse_hours(hours)
    day = time.localtime(now)
    for back in (0, 1):  # hours that run past midnight began the day before
        begin = time.mktime((day.tm_year, day.tm_mon, day.tm_mday - back, first, 0, 0, 0, 0, -1))
        end = time.mktime((day.tm_year, day.tm_mon, day.tm_mday - back + (last < first), last, 0, 0, 0, 0, -1))
        if begin <= now < end:
            return begin, end
    return None


def sync_nightly(item, ctx):
    memory = ctx.mem(item)
    hours = item.get("hours", HOURS)
    timeout = item.get("timeout", TIMEOUT)
    folder = Path(ctx.root or expand("~/.devcluster"), "state", "nightly", item.name)
    running = memory.get("running")
    if running:
        if alive(running):
            if ctx.now < running["started"] + timeout:
                return result(item, OK, f"running since {clock(running['started'])}")
            if ctx.dry_run:
                return result(item, OK, f"running since {clock(running['started'])}, past its {_span(timeout)}")
            stop(running)
        memory["last"] = _finished(running, folder, ctx.now, timeout)
        memory.pop("running")
    tonight = window(ctx.now, hours)
    last = memory.get("last")
    if tonight and not (last and last["started"] >= tonight[0]):
        if ctx.dry_run:
            return result(item, WOULD_CHANGE, "would start now")
        started, problem = _launch(item, ctx, folder)
        if started is None:
            memory["last"] = {"started": ctx.now, "ended": ctx.now, "code": None, "said": f"couldn't start: {problem}", "log": ""}
        else:
            memory["running"] = {**started, "started": ctx.now}
            return result(item, CHANGED, f"started at {clock(ctx.now)}")
    return _report(item, memory.get("last"), hours, ctx.now)


def _launch(item, ctx, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("log", "exit"):
        (folder / name).unlink(missing_ok=True)
    env = dict(os.environ)
    env.pop("DEVCLUSTER_CARRY", None)
    env["DEVCLUSTER_NIGHTLY_DIR"] = str(folder)
    # The checkouts this machine syncs, one path to a line, for a command that works on each.
    env["DEVCLUSTER_REPOS"] = "\n".join(
        str(expand(i.get("path"))) for i in ctx.config.plan(ctx.machine)
        if i.kind in CHECKOUTS and i.get("path") and expand(i.get("path")).is_dir()
    )
    script = f'(\n{item.get("run")}\n) > "$DEVCLUSTER_NIGHTLY_DIR/log" 2>&1; echo $? > "$DEVCLUSTER_NIGHTLY_DIR/exit"'
    unit = f"devcluster-nightly-{item.name}-{int(ctx.now)}"
    return start(script, Path.home(), env, unit, setenv=("DEVCLUSTER_NIGHTLY_DIR", "DEVCLUSTER_REPOS"), label=("devcluster-nightly", item.name))


def _finished(running, folder, now, timeout):
    try:
        code = int((folder / "exit").read_text().strip())
        ended = (folder / "exit").stat().st_mtime
    except (OSError, ValueError):
        code, ended = None, now  # stopped at its time limit, or the machine restarted under it
    try:
        log = (folder / "log").read_text(errors="replace")
    except OSError:
        log = ""
    said = next((redact(line.strip())[:300] for line in reversed(log.splitlines()) if line.strip()), "")
    if code is None:
        said = f"stopped after {_span(timeout)}" if now >= running["started"] + timeout else "ended without finishing, such as in a restart"
    return {"started": running["started"], "ended": ended, "code": code, "said": said, "log": tail(log)}


def _report(item, last, hours, now):
    first, end = parse_hours(hours)
    if not last:
        return result(item, OK, f"first runs between {first:02}:00 and {end:02}:00")
    when = _when(last["started"], now)
    took = _span(max(0, last["ended"] - last["started"]))
    if last["code"] == 0:
        return result(item, OK, f"ran {when} for {took}" + (f": {last['said']}" if last["said"] else ""))
    how = f"exited {last['code']}" if last["code"] is not None else last["said"]
    said = f": {last['said']}" if last["code"] is not None and last["said"] else ""
    return result(item, FAILED, f"its run {when} {how}{said}; it runs again at {first:02}:00", last.get("log", ""))


def _when(stamp, now):
    if now - stamp < 20 * 3600:
        return f"at {clock(stamp)}"
    return time.strftime("on %b %-d at %H:%M", time.localtime(stamp))


def _span(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"
