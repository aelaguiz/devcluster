"""Set devcluster up on a machine: its folder, its command, and the timer that runs it."""

import os
import plistlib
import shlex
import sys
from pathlib import Path

from . import config as configs
from .config import NAME
from .git import Git, clone, same_remote
from .paths import Layout, engine_root
from .proc import run

LABEL = "io.github.aelaguiz.devcluster.sync"
UNIT = "devcluster-sync"


# Folders that belong to one agent session: an AI Manager session's own Claude or Codex build,
# and its private runtime folder. The timer outlives the session that ran install, so a PATH
# naming them would keep running that session's old tools months later.
_SESSION_PREFIXES = ("/opt/aimgr-claude-runtime/artifacts/", "/opt/aimgr-codex-runtime/artifacts/")
_SESSION_PART = "/.aim-private-runtime-"


def timer_path(path):
    """The installing shell's PATH without the folders that belong to one session."""
    kept = [entry for entry in path.split(":") if not entry.startswith(_SESSION_PREFIXES) and _SESSION_PART not in entry]
    return ":".join(kept) or "/usr/bin:/bin"


def launchd_plist(layout, interval, path):
    program = str(layout.engine / "bin" / "devcluster")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [program, "sync", "--quiet"],
        "StartInterval": interval,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "EnvironmentVariables": {"PATH": path, "DEVCLUSTER_HOME": str(layout.root)},
        "StandardOutPath": str(layout.state / "timer.log"),
        "StandardErrorPath": str(layout.state / "timer.log"),
    })


def systemd_units(layout, interval, path):
    program = shlex.quote(str(layout.engine / "bin" / "devcluster"))
    service = "\n".join([
        "[Unit]",
        "Description=devcluster sync",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={program} sync --quiet",
        f'Environment="PATH={path}"',
        f'Environment="DEVCLUSTER_HOME={layout.root}"',
        "Nice=10",
        "IOSchedulingClass=idle",
        "",
    ])
    timer = "\n".join([
        "[Unit]",
        "Description=Run devcluster sync on a timer",
        "",
        "[Timer]",
        "OnBootSec=60",
        f"OnUnitActiveSec={interval}",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return service, timer


def _launchd_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def _schedule(layout, interval, path):
    """Write and start the timer. Returns lines to show."""
    if sys.platform == "darwin":
        plist = _launchd_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(launchd_plist(layout, interval, path))
        # The login session's domain when someone is logged in at the screen; otherwise
        # the user's background domain, which exists over SSH on a Mac nobody is logged in to.
        uid = os.getuid()
        for domain in (f"gui/{uid}", f"user/{uid}"):
            run(["launchctl", "bootout", f"{domain}/{LABEL}"], timeout=30)
        for domain in (f"gui/{uid}", f"user/{uid}"):
            started = run(["launchctl", "bootstrap", domain, str(plist)], timeout=30)
            if started.ok:
                return [f"Started the launchd timer ({plist.name}) in {domain}, every {interval} seconds."]
        return [f"Wrote {plist}, but launchd wouldn't start it: {started.first_line()}"]
    if sys.platform.startswith("linux"):
        folder = _systemd_dir()
        folder.mkdir(parents=True, exist_ok=True)
        service, timer = systemd_units(layout, interval, path)
        (folder / f"{UNIT}.service").write_text(service)
        (folder / f"{UNIT}.timer").write_text(timer)
        lines = []
        for command in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"]):
            done = run(command, timeout=60)
            if not done.ok:
                return [f"Wrote the systemd units in {folder}, but `{' '.join(command)}` failed: {done.first_line()}"]
        lines.append(f"Started the systemd timer ({UNIT}.timer), every {interval} seconds.")
        linger = run(["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"], timeout=30)
        if linger.ok and "Linger=no" in linger.out:
            lines.append(f"It only runs while you're logged in. To keep it running: sudo loginctl enable-linger {os.environ.get('USER', '')}")
        return lines
    return ["No timer for this system; run `devcluster sync` from cron."]


def _unschedule():
    lines = []
    if sys.platform == "darwin":
        plist = _launchd_path()
        for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
            run(["launchctl", "bootout", f"{domain}/{LABEL}"], timeout=30)
        if plist.exists():
            plist.unlink()
            lines.append(f"Stopped and removed {plist}.")
    elif sys.platform.startswith("linux"):
        run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], timeout=60)
        for suffix in ("service", "timer"):
            unit = _systemd_dir() / f"{UNIT}.{suffix}"
            if unit.exists():
                unit.unlink()
                lines.append(f"Stopped and removed {unit}.")
        run(["systemctl", "--user", "daemon-reload"], timeout=60)
    return lines


def _shim(layout, bin_dir):
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "devcluster"
    program = shlex.quote(str(layout.engine / "bin" / "devcluster"))
    home = shlex.quote(str(layout.root))
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ -z "${{DEVCLUSTER_HOME:-}}" ]; then DEVCLUSTER_HOME={home}; export DEVCLUSTER_HOME; fi\n'
        f'exec {program} "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def install(machine, config_url, engine_url=None, interval=120, bin_dir=None, schedule=True, path=None):
    layout = Layout()
    if not NAME.match(machine or ""):
        print("Give this machine a name made of letters, digits, '.', '_' and '-'.", file=sys.stderr)
        return 2
    layout.state.mkdir(parents=True, exist_ok=True)
    lines = []

    if not Git(layout.engine).is_checkout():
        here = engine_root()
        source = engine_url or Git(here).origin_url() or str(here)
        cloned = clone(source, layout.engine, None, 600)
        if not cloned.ok:
            print(f"Couldn't get devcluster from {source}: {cloned.first_line()}", file=sys.stderr)
            return 1
        lines.append(f"Put devcluster in {layout.engine}, from {source}.")

    config_git = Git(layout.config)
    if config_git.is_checkout():
        existing = config_git.origin_url()
        if config_url and not same_remote(existing or "", config_url):
            print(f"{layout.config} already follows {existing}; uninstall and remove it to use {config_url}.", file=sys.stderr)
            return 2
    elif not config_url:
        print("Say which config repo to follow with --config.", file=sys.stderr)
        return 2
    else:
        cloned = clone(config_url, layout.config, None, 600)
        if not cloned.ok:
            print(f"Couldn't clone the config from {config_url}: {cloned.first_line()}", file=sys.stderr)
            return 1
        lines.append(f"Put the config in {layout.config}, from {config_url}.")

    layout.machine_file.write_text(machine + "\n")
    lines.append(f"This machine is {machine!r}.")
    try:
        loaded = configs.load(layout.config)
        if machine not in loaded.machines:
            lines.append(f"The config has no machines/{machine}.toml yet; until it does, each run stops and reports that.")
        else:
            mode = loaded.machines[machine].mode
            lines.append("It syncs for real." if mode == "on" else "It's in dry run until machines/" + machine + ".toml says mode = \"on\".")
    except configs.ConfigError as error:
        lines.append(f"The config has errors, so runs will stop until they're fixed: {error.errors[0]}")

    shim = _shim(layout, Path(bin_dir) if bin_dir else Path.home() / ".local" / "bin")
    lines.append(f"Put the devcluster command at {shim}.")
    if str(shim.parent) not in os.environ.get("PATH", "").split(":"):
        lines.append(f"{shim.parent} isn't on your PATH.")
    if schedule:
        lines += _schedule(layout, interval, timer_path(path or os.environ.get("PATH", "/usr/bin:/bin")))
    for line in lines:
        print(line)
    return 0


def uninstall(bin_dir=None):
    layout = Layout()
    lines = _unschedule()
    shim = (Path(bin_dir) if bin_dir else Path.home() / ".local" / "bin") / "devcluster"
    if shim.exists():
        shim.unlink()
        lines.append(f"Removed {shim}.")
    lines.append(f"Left {layout.root} in place; remove it yourself if you're done with it.")
    for line in lines:
        print(line)
    return 0
