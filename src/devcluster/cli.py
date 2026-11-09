"""The devcluster command."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__, cleanup, config as configs, install, keep, status, sync
from .paths import Layout


def _status(args):
    layout = Layout()
    machines = []
    stale_after = 900
    try:
        loaded = configs.load(layout.config)
        machines = list(loaded.machines)
        stale_after = loaded.setting("stale_after", 900)
    except configs.ConfigError:
        pass
    if not (layout.config / ".git").exists():
        print(f"devcluster isn't set up here ({layout.config} is missing); run devcluster install.", file=sys.stderr)
        return 2
    reports, note = status.read_all(layout)
    if args.json:
        print(json.dumps(reports, indent=1, sort_keys=True))
    else:
        print(status.render(reports, machines, stale_after, note, show_all=args.all))
    return 0


def _check_config(args):
    root = Path(args.dir) if args.dir else Layout().config
    try:
        loaded = configs.load(root)
    except configs.ConfigError as error:
        print(f"{root} has {len(error.errors)} problem{'s' if len(error.errors) != 1 else ''}:")
        for line in error.errors:
            print(f"  {line}")
        return 2
    print(f"{root} is a valid config.")
    for name, machine in sorted(loaded.machines.items()):
        if args.machine and name not in args.machine:
            continue
        mode = "syncs for real" if machine.mode == "on" else "dry run"
        print(f"\n{name} ({mode}):")
        for item in loaded.plan(name):
            where = item.get("path") or item.get("file") or ", ".join(item.get("targets", [])) or item.get("version", "")
            if item.get("from"):
                where = f"{where}, copied from {item.get('from')}" if where else f"the version {item.get('from')} has"
            trial = " (tried in dry run)" if item.get("mode") == "dry-run" and machine.mode == "on" else ""
            print(f"  {item.kind} {item.name}: {where}{trial}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="devcluster", description="Keep a cluster of development machines in sync.")
    parser.add_argument("--version", action="version", version=f"devcluster {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("sync", help="bring this machine in line with the config, then report")
    run.add_argument("--dry-run", action="store_true", help="report what would change without changing anything")
    run.add_argument("--only", nargs="+", metavar="NAME", help="run only these items")
    run.add_argument("--no-publish", action="store_true", help="don't push this run's status")
    run.add_argument("--quiet", action="store_true", help="print nothing; the timer uses this")

    show = commands.add_parser("status", help="every machine's last run")
    show.add_argument("--json", action="store_true", help="print the raw reports")
    show.add_argument("--all", action="store_true", help="list every item, not just the ones that need a look")

    check = commands.add_parser("check-config", help="check a config checkout and show what each machine gets")
    check.add_argument("dir", nargs="?", help="the config checkout (default: this machine's)")
    check.add_argument("--machine", nargs="+", help="show only these machines")

    setup = commands.add_parser("install", help="set devcluster up on this machine")
    setup.add_argument("--machine", required=True, help="this machine's name in the config")
    setup.add_argument("--config", help="the config repo's URL")
    setup.add_argument("--engine-url", help="where to get devcluster from (default: where this copy came from)")
    setup.add_argument("--interval", type=int, default=120, help="seconds between runs (default 120)")
    setup.add_argument("--bin-dir", help="where to put the devcluster command (default ~/.local/bin)")
    setup.add_argument("--path", help="PATH for the timer's runs (default: this shell's PATH)")
    setup.add_argument("--no-schedule", action="store_true", help="don't start the timer")

    remove = commands.add_parser("uninstall", help="stop the timer and remove the command")
    remove.add_argument("--bin-dir", help="where the devcluster command was put")

    clean = commands.add_parser("cleanup", help="run this machine's disk cleanup now, with the agent cleanup.toml names")
    clean.add_argument("--dry-run", action="store_true", help="decide everything and remove nothing; record what would go")
    clean_commands = clean.add_subparsers(dest="cleanup_command")
    rm = clean_commands.add_parser("rm", help="remove folders, refusing anything kept, a mount, a home folder or a link")
    rm.add_argument("paths", nargs="+", metavar="PATH")
    rm.add_argument("--sudo", action="store_true", help="remove with sudo -n, for files this user can't remove")

    kept = commands.add_parser("keep", help="the folders each machine's cleanup keeps until a date")
    keep_commands = kept.add_subparsers(dest="keep_command", required=True)
    add = keep_commands.add_parser("add", help="keep a folder until a date, or renew it")
    add.add_argument("path", metavar="PATH")
    add.add_argument("--until", required=True, metavar="YYYY-MM-DD", help="the last day it's needed")
    add.add_argument("--who", required=True, help="who relies on it")
    add.add_argument("--why", required=True, help="what they need it for")
    add.add_argument("--machine", help="the machine it's on (default: this one)")
    listing = keep_commands.add_parser("list", help="show the keep list")
    listing.add_argument("--machine", help="one machine's folders (default: this one)")
    listing.add_argument("--all", action="store_true", help="every machine's folders")
    drop = keep_commands.add_parser("remove", help="take a folder off the keep list")
    drop.add_argument("path", metavar="PATH")
    drop.add_argument("--machine", help="the machine it's on (default: this one)")

    args = parser.parse_args(argv)
    if args.command == "cleanup":
        if args.cleanup_command == "rm":
            return cleanup.remove(args.paths, args.sudo)
        return cleanup.run(args.dry_run)
    if args.command == "keep":
        return keep.command(args)
    if args.command == "sync":
        return sync.run(args.dry_run, args.only, not args.no_publish, args.quiet, argv)
    if args.command == "status":
        return _status(args)
    if args.command == "check-config":
        return _check_config(args)
    if args.command == "install":
        if args.interval < 30:
            parser.error("--interval must be at least 30 seconds")
        return install.install(args.machine, args.config, args.engine_url, args.interval, args.bin_dir, not args.no_schedule, args.path)
    if args.command == "uninstall":
        return install.uninstall(args.bin_dir)
    return 2
