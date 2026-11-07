"""One run of `devcluster sync`: bring this machine in line with the list, then report."""

import fcntl
import json
import os
import platform
import socket
import sys
import traceback
from dataclasses import replace

from . import __version__, config as configs, fixer as fixers, status
from .context import Context
from .engine import engine_commit, pull_config, update_engine
from .git import Git, version_text
from .items import HANDLERS
from .paths import Layout, clock, now
from .proc import tail
from .results import BLOCKED, FAILED, ItemResult

CARRY = "DEVCLUSTER_CARRY"  # results handed to the new engine after it updates itself


def lock(path):
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(temporary, path)


def run(dry_run=False, only=None, publish=True, quiet=False, argv=None):
    layout = Layout()
    machine = layout.machine()
    if not machine:
        print(f"devcluster isn't set up on this machine ({layout.machine_file} is missing); run devcluster install.", file=sys.stderr)
        return 2
    layout.state.mkdir(parents=True, exist_ok=True)
    held = lock(layout.state / "sync.lock")
    if held is None:
        if not quiet:
            print("Another devcluster sync is running on this machine, so this one stopped.")
        return 0

    report = {
        "format": 1,
        "machine": machine,
        "hostname": socket.gethostname(),
        "started": now(),
        "devcluster": __version__,
        "git": version_text(),
        "python": platform.python_version(),
        "errors": [],
    }
    results = []
    carried = os.environ.pop(CARRY, None)
    if carried:
        results = [ItemResult.from_dict(entry) for entry in json.loads(carried)]
    else:
        results.append(pull_config(layout))

    config = None
    try:
        config = configs.load(layout.config)
    except configs.ConfigError as error:
        report["errors"] = error.errors
    if config is not None and machine not in config.machines:
        report["errors"] = [f"this machine is {machine!r}, and the config has no machines/{machine}.toml"]
        config = None

    if config is not None and not carried:
        outcome, restart = update_engine(layout, config, machine)
        results.append(outcome)
        if restart:
            held.close()
            env = dict(os.environ, **{CARRY: json.dumps([r.to_dict() for r in results])})
            program = str(layout.engine / "bin" / "devcluster")
            os.execve(program, [program, *(argv if argv is not None else sys.argv[1:])], env)

    dry = True
    memory = _load_json(layout.state / "items.json", {})
    if config is not None:
        dry = dry_run or config.machines[machine].mode != "on"
        context = Context(machine, dry, config, now(), memory, layout.root)
        trial = replace(context, dry_run=True)  # for an item tried in dry run on a machine that's on
        fixer, fixing = _fixer(layout, context, config, machine, results, preview=dry_run)
        plan = config.plan(machine)
        for item in plan:
            if only and item.name not in only:
                continue
            if f"{item.kind}:{item.name}" in fixing:
                results.append(fixing[f"{item.kind}:{item.name}"])  # an agent is working on it
                continue
            try:
                outcome = HANDLERS[item.kind](item, trial if item.get("mode") == "dry-run" else context)
            except Exception:
                outcome = ItemResult(item.kind, item.name, FAILED, "devcluster hit an internal error on this item", tail(traceback.format_exc()))
            results.append(outcome)
        if fixer is not None and not dry and not only:
            try:
                results += fixer.after(plan, results)
            except Exception:
                results.append(ItemResult("fixer", machine, FAILED, "devcluster hit an internal error in its fixer", tail(traceback.format_exc())))
        _save_json(layout.state / "items.json", memory)

    report["mode"] = "dry-run" if dry else "on"
    report["engine"] = engine_commit()
    report["config"] = Git(layout.config).rev("HEAD")
    report["items"] = [r.to_dict() for r in results]
    report["finished"] = now()
    _save_json(layout.state / "last.json", report)
    _append_log(layout, status.log_line(report))

    problem = None
    if publish:
        heartbeat = config.setting("heartbeat", 300) if config is not None else 0
        problem = status.publish(layout, report, heartbeat)
        if problem:
            _append_log(layout, f"  {problem}\n")

    if not quiet:
        mode = " (dry run)" if dry else ""
        print(f"devcluster sync on {machine}{mode} at {clock(report['finished'])}")
        for line in status.summary_lines(report):
            print(line)
        if problem:
            print(f"  Status not published: {problem}")
    if report["errors"]:
        return 2
    return 1 if any(r.state in (FAILED, BLOCKED) for r in results) else 0


def _fixer(layout, context, config, machine, results, preview=False):
    """The fixer for this run, and the items an agent is fixing. With fixer.toml naming no
    command it starts nothing, but an agent already running is still seen through."""
    chosen = fixers.settings(config, machine)
    if chosen is None:
        if not (context.memory.get("fixer") or {}).get("running"):
            return None, {}
        chosen = dict(fixers.DEFAULTS)
    fixer = fixers.Fixer(layout, context, config, machine, chosen, persist=lambda: _save_json(layout.state / "items.json", context.memory))
    try:
        return fixer, fixer.check(dry_run=preview)
    except Exception:
        results.append(ItemResult("fixer", machine, FAILED, "devcluster hit an internal error in its fixer", tail(traceback.format_exc())))
        return None, {}


def _append_log(layout, line):
    path = layout.state / "sync.log"
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, path.with_name("sync.log.1"))
        with open(path, "a") as handle:
            handle.write(line)
    except OSError:
        pass
