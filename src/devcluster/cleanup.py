"""The nightly disk cleanup: one run on this machine, done by the agent cleanup.toml names.

Almost everything that fills these disks is left over from tasks whose agents are gone, so the
agent looks for signs of life and clears the rest, following the disk-cleanup skill. This
module gives it what it needs and checks what it did: a brief with the machine's volumes,
floors, notes and keep list; a run folder for its record; free space measured before and after
by the engine itself; and `devcluster cleanup rm`, the one way a run deletes, which refuses a
kept folder, a mount, a home folder and anything outside the places task folders live.

`[[nightly]] run = "devcluster cleanup"` runs it each night. Its last line is what
`devcluster status` shows. `devcluster cleanup due`, as that item's `early` command, starts a run
during the day when a volume falls under its floor.
"""

import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config as configs
from .keep import kept_paths
from .paths import Layout, clock, engine_root, now as clock_now
from .proc import run as run_command

TIMEOUT = 5400        # seconds an agent may take, by default
KEEP_DAYS = 14        # days a run's folder stays, by default
STATUSES = ("ok", "warning", "failed")
SKILL = engine_root() / "skills" / "disk-cleanup" / "SKILL.md"  # the engine this run is part of
TASK_PLACES = ("/tmp", "/private/tmp", "/private/var/folders")
# A volume under its floor starts a run during the day, but not within this long of the last
# run's start. A run that left it under its floor found everything else there live; some hours
# later tasks have ended and there is more to clear, and a machine filling that fast can run
# out before the night. Asked sooner, a run would only find the same live work again.
EARLY_AFTER = 6 * 3600


# One run


def run(dry_run=False):
    layout = Layout()
    machine = layout.machine()
    if not machine:
        return _end(f"failed: devcluster isn't set up on this machine ({layout.machine_file} is missing)", 2)
    try:
        config = configs.load(layout.config)
    except configs.ConfigError as error:
        return _end(f"failed: the config has problems, so no cleanup ran: {error.errors[0]}", 1)
    if machine not in config.machines:
        return _end(f"failed: the config has no machines/{machine}.toml", 1)
    command = config.cleanup.get("command", "").strip()
    if not command:
        return _end("failed: cleanup.toml names no command, so there is no agent to run the cleanup", 1)

    folder = layout.state / "cleanup"
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    held = _hold(folder / "run.lock")
    if held is None:
        return _end(f"didn't start: another cleanup run is going on this machine ({_holder(folder / 'run.lock')})", 0)
    try:
        return _run(layout, config, machine, command, folder, dry_run)
    finally:
        held.close()


def due():
    """Exit 0, saying why, when this machine should be cleaned now rather than tonight: a volume
    is under its floor, and no run has started in the last EARLY_AFTER. Otherwise exit 1."""
    layout = Layout()
    machine = layout.machine()
    if not machine:
        return _end("devcluster isn't set up on this machine", 1)
    try:
        config = configs.load(layout.config)
    except configs.ConfigError as error:
        return _end(f"the config has problems: {error.errors[0]}", 1)
    if machine not in config.machines or not config.cleanup.get("command", "").strip():
        return _end("no cleanup is set up for this machine", 1)
    under = []
    for volume in measure(_volumes(config, machine)):
        volume["after_free"] = volume.get("before_free")
        if under_floor(volume) is not None:
            under.append(f"{volume['path']} is under its {volume['floor']} floor with {human(volume['after_free'])} free")
    if not under:
        return _end("every volume is above its floor", 1)
    last = _last_start(layout.state / "cleanup")
    if last is not None and clock_now() - last < EARLY_AFTER:
        return _end(f"{under[0]}, and a run started at {clock(last)}", 1)
    return _end("; ".join(under), 0)


def _last_start(folder):
    """When the newest run started, from its record, or None."""
    previous = _previous(folder)
    try:
        return float(json.loads((previous / "result.json").read_text())["started"]) if previous else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _run(layout, config, machine, command, folder, dry_run):
    timeout = config.cleanup.get("timeout", TIMEOUT)
    started = clock_now()
    previous = _previous(folder)
    run_dir = _new_run(folder, started)
    volumes = measure(_volumes(config, machine))
    entries = [e for e in config.keep if e["machine"] == machine and e["until"] >= _today()]
    (run_dir / "keep.txt").write_text("".join(f"{p}\n" for p in kept_paths(config.keep, machine, _today())))
    (run_dir / "brief.md").write_text(brief(config, machine, volumes, entries, run_dir, previous, started, timeout, dry_run))
    record = {"machine": machine, "started": started, "dry_run": dry_run, "timeout": timeout, "volumes": volumes}
    _save(run_dir / "result.json", record)

    code, timed_out = _agent(command, run_dir, machine, timeout, dry_run)
    summary, problem = read_summary(run_dir / "summary.json")
    after = measure(_volumes(config, machine))
    for volume, later in zip(volumes, after):
        volume["after_free"] = later.get("before_free")
    if timed_out:
        problem = f"stopped after {_span(timeout)}" + ("" if summary else ", before it wrote its summary")
    elif summary is None:
        problem = f"{problem} (the agent exited {code})"
    elif code != 0 and summary.get("status") == "ok":
        summary = {**summary, "status": "warning", "said": f"the agent exited {code} after writing its summary; {summary.get('said', '')}".rstrip("; ")}
    line, status = report(volumes, summary, problem, dry_run, _would(run_dir))
    record.update(ended=clock_now(), code=code, timed_out=timed_out, status=status, said=line, summary=summary)
    _save(run_dir / "result.json", record)
    _prune(folder / "runs", config.cleanup.get("keep_days", KEEP_DAYS), run_dir)
    return _end(line, 1 if status == "failed" else 0)


def _end(line, code):
    print(line)
    return code


def _today():
    return datetime.date.fromtimestamp(clock_now())


def _hold(path):
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def _holder(path):
    try:
        pid = path.read_text().strip()
    except OSError:
        pid = ""
    return f"process {pid}" if pid else "its process is unknown"


def _previous(folder):
    latest = folder / "latest"
    return Path(os.path.realpath(latest)) if latest.is_symlink() and latest.exists() else None


def _new_run(folder, started):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started))
    run_dir = folder / "runs" / stamp
    number = 1
    while run_dir.exists():
        number += 1
        run_dir = folder / "runs" / f"{stamp}-{number}"
    run_dir.mkdir(parents=True)
    link = folder / "latest.new"
    link.unlink(missing_ok=True)
    os.symlink(run_dir, link)
    os.replace(link, folder / "latest")
    return run_dir


def _save(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
    os.replace(temporary, path)


# Volumes and floors


def _volumes(config, machine):
    floors = config.cleanup_machines.get(machine, {}).get("floors") or {"/": None}
    return [{"path": path, "floor": floor} for path, floor in floors.items()]


def measure(volumes):
    """Each volume with its free and total bytes as the engine sees them now. A volume that
    isn't a mount (a drive that is missing) is marked so instead of measuring what is under it."""
    measured = []
    for volume in volumes:
        entry = dict(volume)
        path = entry["path"]
        if path != "/" and not os.path.ismount(path):
            entry["missing"] = True
        else:
            try:
                stats = os.statvfs(path)
                entry["before_free"] = stats.f_bavail * stats.f_frsize
                entry["total"] = stats.f_blocks * stats.f_frsize
            except OSError:
                entry["missing"] = True
        measured.append(entry)
    return measured


def under_floor(volume):
    """The floor in bytes when the volume ends below it, else None."""
    if volume.get("missing") or not volume.get("floor") or volume.get("after_free") is None:
        return None
    floor = configs.floor_bytes(volume["floor"], volume.get("total", 0))
    return floor if floor is not None and volume["after_free"] < floor else None


# The brief


def brief(config, machine, volumes, entries, run_dir, previous, started, timeout, dry_run):
    own = config.cleanup_machines.get(machine, {})
    description = config.machines[machine].description
    reserve = min(600, timeout // 6)
    deadline = started + timeout - reserve
    lines = [
        f"# Disk cleanup on {machine}",
        "",
        f"You are running tonight's disk cleanup on the machine devcluster calls {machine}"
        + (f": {description.rstrip('.')}" if description else "") + ".",
        "",
        f"Read the skill first: {SKILL}. It holds the judgment and the commands.",
        "",
        "## This run",
        "",
    ]
    if dry_run:
        lines += [
            "**This is a dry run. Change nothing.** Make every decision you would make tonight and record it: "
            "send each removal through `devcluster cleanup rm`, which in a dry run only writes it down in "
            "`would-remove.jsonl`. Don't push salvage branches, stop processes, prune containers, delete devices "
            "or snapshots, or remove anything any other way; write what you would do in summary.json instead.",
            "",
        ]
    lines += [
        f"- Run folder: {run_dir}. Write everything you produce here.",
        f"- Previous run: {previous}" if previous else "- Previous run: none on this machine yet.",
        f"- Started {clock(started)}. Stop new work by {clock(deadline)} ({_span(timeout - reserve)} from the start) "
        f"and leave the last {_span(reserve)} for checking what you did and writing the summary. "
        f"The run is stopped at {clock(started + timeout)}.",
        "",
        "## Volumes",
        "",
    ]
    for volume in volumes:
        if volume.get("missing"):
            lines.append(f"- {volume['path']}: not mounted. Skip it and say so; never clean the folder underneath a missing mount.")
            continue
        floor = f", floor {volume['floor']}" if volume.get("floor") else ""
        lines.append(f"- {volume['path']}: {human(volume['before_free'])} free of {human(volume['total'])}{floor}")
    lines += ["", "A floor is the least free space the volume should have. It is a floor, not a finish line: keep clearing dead things after it is met.", ""]
    notes = [n for n in (config.cleanup.get("notes"), own.get("notes")) if n]
    lines += ["## Notes", ""]
    if notes:
        lines.append("Read these before deciding anything. They say what is special about this cluster and this machine:")
        lines += [f"- {config.root / n}" for n in notes]
    else:
        lines.append("There are no notes for this machine.")
    lines += ["", "## The keep list", ""]
    if entries:
        lines.append("Each folder here stays whole, with everything under it, until the end of its date. Name each in what you kept.")
        lines.append("")
        for entry in sorted(entries, key=lambda e: e["path"]):
            lines.append(f"- {entry['path']} until {entry['until']}: {entry['who']}; {entry['why']}")
    else:
        lines.append("Nothing on this machine is on the keep list.")
    lines += [
        "",
        f"`{run_dir / 'keep.txt'}` holds those paths. Add the live work you find, one path per line, "
        "before you remove anything: `devcluster cleanup rm` refuses every path in it and anything that holds one.",
        "",
        "When someone needs a folder beyond tonight and nothing shows it (a finished run they are still reading), "
        "put it on the keep list for them with `devcluster keep add PATH --until YYYY-MM-DD --who WHO --why WHY`.",
        "",
        "## Removing",
        "",
        "Every deletion goes through `devcluster cleanup rm PATH...`. It refuses a kept path, anything holding one, "
        "a link, a mount, a home folder and anything outside the places task folders live, says why, and records "
        "what it removed in `removed.jsonl`. Add `--sudo` for files your user can't remove.",
        "",
        "## What to write",
        "",
        f"Before you finish, write `{run_dir / 'summary.json'}`:",
        "",
        "```json",
        '{"status": "ok", "said": "one sentence a person can act on", "live": ["the live work you named"],',
        ' "kept": [{"path": "/a/large/folder", "bytes": 0, "why": "a reason a person would accept"}],',
        ' "salvage": ["branches or refs holding unsaved work"], "problems": ["what failed or wasn\'t checked"]}',
        "```",
        "",
        "`status` is `ok`, `warning` (something wasn't covered, or pressure remains) or `failed` (the cleanup "
        "couldn't run). List the largest things you kept, each with its reason. "
        "\"Couldn't establish it was unused\" is not a reason. "
        "devcluster measures free space itself, before and after.",
        "",
    ]
    return "\n".join(lines)


# The agent


def _agent(command, run_dir, machine, timeout, dry_run):
    """Run the agent with the brief on standard input. Returns its exit code and whether it was
    stopped at its time limit."""
    env = dict(os.environ)
    env.update(DEVCLUSTER_CLEANUP_RUN=str(run_dir), DEVCLUSTER_MACHINE=machine)
    env.pop("DEVCLUSTER_CLEANUP_DRY_RUN", None)
    if dry_run:
        env["DEVCLUSTER_CLEANUP_DRY_RUN"] = "1"
    # The agent calls `devcluster cleanup rm` and `devcluster keep`: the same engine as this run.
    env["PATH"] = f"{engine_root() / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    with open(run_dir / "brief.md") as stdin, open(run_dir / "agent.log", "w") as out:
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=run_dir, env=env, stdin=stdin, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            out.write(f"couldn't start the agent: {error.strerror}\n")
            return 127, False
        # When this run is stopped, by its nightly time limit or a person, the agent stops too.
        previous = signal.signal(signal.SIGTERM, lambda *_: (_stop_group(proc), sys.exit(143)))
        try:
            return proc.wait(timeout), False
        except subprocess.TimeoutExpired:
            _stop_group(proc)
            return proc.returncode, True
        except KeyboardInterrupt:
            _stop_group(proc)
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)


def _stop_group(proc):
    for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(wait)
            # The shell may be gone while something it started is still in the group.
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            pass


def read_summary(path):
    """The agent's summary.json, or None and why it can't be used."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None, "the agent wrote no summary.json"
    except (OSError, ValueError) as error:
        return None, f"summary.json can't be read: {error}"
    if not isinstance(data, dict):
        return None, "summary.json isn't an object"
    if data.get("status") not in STATUSES:
        return None, f"summary.json's status is {data.get('status')!r}, not ok, warning or failed"
    return data, None


# The report


def report(volumes, summary, problem, dry_run, would):
    """The run's one-line result, and its status."""
    said = str((summary or {}).get("said") or "").strip()
    status = summary["status"] if summary else "failed"
    if problem:
        status = "failed"
    if dry_run:
        count, size = would
        head = f"dry run: would remove {count} path{'' if count == 1 else 's'}, {human(size)}"
    else:
        head = _freed(volumes)
    notes = []
    for volume in volumes:
        if volume.get("missing"):
            notes.append(f"{volume['path']} isn't mounted, so it wasn't cleaned")
        elif not dry_run:
            floor = under_floor(volume)
            if floor is not None:
                notes.append(f"{volume['path']} is still under its {volume['floor']} floor with {human(volume['after_free'])} free")
    if notes and status == "ok":
        status = "warning"
    if status == "failed":
        parts = [f"failed: {problem or said or 'the agent said the cleanup failed'}", head]
        if problem and said:
            parts.append(said)
    elif status == "warning":
        parts = [head, f"warning: {said}" if said else "warning"]
    else:
        parts = [f"{head}: {said}" if said else head]
    return "; ".join(parts + notes), status


def _freed(volumes):
    measured = [v for v in volumes if not v.get("missing") and v.get("after_free") is not None]
    if not measured:
        return "freed nothing it could measure"
    total = sum(v["after_free"] - v["before_free"] for v in measured)
    if len(measured) == 1:
        return f"freed {human(total)} on {measured[0]['path']}"
    each = ", ".join(f"{v['path']} {human(v['after_free'] - v['before_free'])}" for v in measured)
    return f"freed {human(total)} ({each})"


def _would(run_dir):
    count = size = 0
    try:
        lines = (run_dir / "would-remove.jsonl").read_text().splitlines()
    except OSError:
        return 0, 0
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        count += 1
        size += int(entry.get("bytes") or 0)
    return count, size


def human(size):
    size = int(size or 0)
    sign = "-" if size < 0 else ""
    size = abs(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{sign}{size} B"
            return f"{sign}{size:.1f} {unit}" if size < 10 else f"{sign}{size:.0f} {unit}"
        size /= 1024


def _span(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds % 3600 >= 60 else f"{seconds // 3600}h"


def _prune(runs, days, current):
    cutoff = clock_now() - days * 86400
    for path in runs.iterdir():
        if path == current or not path.is_dir() or path.is_symlink():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            pass


# Removing, with guards


def remove(paths, sudo=False, dry_run=False):
    """`devcluster cleanup rm`: remove each path that passes every guard, record it, and refuse
    the rest with a reason. Returns 1 if any path was refused or couldn't be removed."""
    layout = Layout()
    machine = os.environ.get("DEVCLUSTER_MACHINE") or layout.machine()
    run_dir = os.environ.get("DEVCLUSTER_CLEANUP_RUN")
    dry_run = dry_run or os.environ.get("DEVCLUSTER_CLEANUP_DRY_RUN") == "1"
    guards, problem = Guards.load(layout, machine, run_dir)
    failed = False
    for path in paths:
        reason = problem or guards.refusal(path)
        if reason:
            print(f"refused, {reason}: {path}")
            failed = True
            continue
        target = canonical(path)
        size = _size(target, sudo)
        if dry_run:
            _append(run_dir, "would-remove.jsonl", target, size)
            print(f"would remove {target} ({human(size)})")
            continue
        error = _delete(target, sudo)
        if error:
            print(f"couldn't remove {target}: {error}")
            failed = True
            continue
        _append(run_dir, "removed.jsonl", target, size)
        print(f"removed {target} ({human(size)})")
    return 1 if failed else 0


class Guards:
    """What `cleanup rm` refuses on this machine."""

    def __init__(self, home, places, kept, mounts):
        self.home = home
        self.places = places
        self.kept = kept
        self.mounts = mounts
        self.never = {"/", home, os.path.join(home, "workspace"), os.path.join(home, "worktrees"), *places}

    @classmethod
    def load(cls, layout, machine, run_dir):
        home = canonical(os.path.expanduser("~"))
        try:
            config = configs.load(layout.config)
        except configs.ConfigError as error:
            return cls(home, [], [], set()), f"the config has problems, so the keep list can't be read ({error.errors[0]})"
        floors = config.cleanup_machines.get(machine, {}).get("floors") or {}
        places = [home] + [canonical(v) for v in floors if v != "/"]
        places += [canonical(p) for p in TASK_PLACES if os.path.isdir(p)]
        kept = [canonical(p) for p in kept_paths(config.keep, machine, _today())] if machine else []
        if run_dir:
            try:
                lines = Path(run_dir, "keep.txt").read_text().splitlines()
            except FileNotFoundError:
                lines = []
            except OSError as error:
                return cls(home, places, kept, set()), f"this run's keep.txt can't be read ({error.strerror})"
            kept += [canonical(os.path.expanduser(line.strip())) for line in lines if line.strip().startswith(("/", "~"))]
        return cls(home, list(dict.fromkeys(places)), list(dict.fromkeys(kept)), mount_points()), None

    def refusal(self, path):
        if not os.path.lexists(path):
            return "it doesn't exist"
        if ".." in path.split("/"):
            return "it has '..' in it; name the folder by its own full path"
        if os.path.islink(path.rstrip("/") or "/"):
            return "it's a link; remove a link itself with plain rm, never what it points to"
        through = self._link_on_the_way(path)
        if through:
            return f"it goes through the link {through}; name the folder itself, never a path through a link"
        target = canonical(path)
        if target in self.never:
            return "it's a home folder, a workspace root or a place task folders live, not a task folder"
        if target in self.mounts or os.path.ismount(target):
            return "it's a mount"
        if any(_inside(m, target) for m in self.mounts):
            return "it holds a mount"
        if not any(_inside(target, place) for place in self.places):
            return "it's outside the places task folders live"
        for kept in self.kept:
            if target == kept:
                return f"it's kept ({kept})"
            if _inside(kept, target):
                return f"it holds something kept ({kept})"
            if _inside(target, kept):
                return f"it's inside something kept ({kept})"
        return None


    def _link_on_the_way(self, path):
        """A link on the way to `path` (`link/.` and `link/child` both go through one), other than
        one that only stands for a place task folders live or a folder holding one (macOS's /tmp
        is a link to /private/tmp)."""
        lexical = os.path.abspath(os.path.expanduser(path))
        above = "/"
        for part in lexical.strip("/").split("/"):
            above = os.path.join(above, part)
            if os.path.islink(above):
                real = canonical(above)
                if not any(real == place or _inside(place, real) for place in self.places):
                    return above
        return None


def canonical(path):
    """The path as the disk spells it, links resolved. macOS's disks ignore case, so a path typed
    with other capitals names the same folder, and every comparison here has to see that."""
    real = os.path.realpath(path)
    if sys.platform != "darwin" or not hasattr(fcntl, "F_GETPATH"):
        return real
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        parent, name = os.path.split(real)
        if not name:
            return real
        parent = canonical(parent)
        try:
            names = os.listdir(parent)
        except OSError:
            return os.path.join(parent, name)
        same = [n for n in names if n == name] or [n for n in names if n.casefold() == name.casefold()]
        return os.path.join(parent, same[0] if same else name)
    try:
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
    except OSError:
        return real
    finally:
        os.close(fd)


def _inside(path, folder):
    return path.startswith(folder.rstrip("/") + "/")


def mount_points():
    """Every mount point on this machine."""
    points = set()
    try:
        with open("/proc/self/mounts") as mounts:
            for line in mounts:
                parts = line.split()
                if len(parts) > 1:  # spaces and tabs in a mount point are written as \040 and \011
                    points.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1]))
        return points
    except OSError:
        pass
    shown = run_command(["/sbin/mount"], timeout=60)
    for line in shown.out.splitlines():
        if " on " in line and " (" in line:
            points.add(line.split(" on ", 1)[1].rsplit(" (", 1)[0])
    return points


def _call(args, timeout):
    """Run a command in this process's own group, so stopping the cleanup stops it too."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, "", str(error)
    return done.returncode, done.stdout, done.stderr


def _size(target, sudo):
    code, out, _ = _call((["sudo", "-n"] if sudo else []) + ["du", "-skx", target], 3600)
    try:
        return int(out.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def _delete(target, sudo):
    args = (["sudo", "-n"] if sudo else []) + ["rm", "-rf"]
    if sys.platform.startswith("linux"):
        args.append("--one-file-system")  # never cross into a drive mounted inside it
    code, _, err = _call(args + ["--", target], 6 * 3600)
    if os.path.lexists(target):
        return (err.strip().splitlines() or ["it is still there"])[0] if code != 0 else "it is still there"
    return None


def _append(run_dir, name, target, size):
    if not run_dir:
        return
    entry = {"path": target, "bytes": size, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(Path(run_dir, name), "a") as out:
        out.write(json.dumps(entry) + "\n")
