"""Read and check the config repo: cluster.toml plus one file per machine."""

import datetime
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The order a run works through the kinds: credentials first, since installs may need them;
# setups after the repos, tools and CLIs they build on; logins after the setups they need. Settings copied from one machine travel in the
# secrets repo, so they come after it.
KINDS = ("secrets", "repo", "tool", "cli", "setup", "login", "file", "settings", "nightly")

_COMMON = {"name", "on", "mode"}
_INSTALLED = {"install", "check", "installs_into", "install_timeout"}
FIELDS = {
    "secrets": ({"path", "url", "check", "decrypt"}, {"branch", "timeout", "publish"}),
    "repo": ({"path", "url"}, {"branch", "timeout"} | _INSTALLED),
    "tool": ({"path", "url"}, {"ref", "timeout"} | _INSTALLED),
    "cli": ({"name", "check", "install"}, {"version", "from", "timeout", "install_timeout"}),
    "setup": ({"name", "check", "setup"}, {"timeout", "setup_timeout"}),
    "login": ({"name", "check", "login"}, {"timeout", "login_timeout"}),
    "file": ({"name", "source", "targets"}, set()),
    "settings": ({"name", "file", "from"}, {"own", "only"}),
    "nightly": ({"name", "run"}, {"hours", "timeout"}),
}
HOURS = re.compile(r"^(\d{1,2})-(\d{1,2})$")
DOTTED = re.compile(r"^([A-Za-z0-9_-]+|\*)(\.([A-Za-z0-9_-]+|\*))*$")
SETTINGS = {
    "engine": str,
    "heartbeat": int,
    "stale_after": int,
    "retry_failed_after": int,
    "git_name": str,
    "git_email": str,
    "follow_folder": str,  # where the machine an item copies leaves its copy for the others
}
MODES = ("dry-run", "on")

# fixer.toml: the agent devcluster starts on a machine when an item stays stuck, how it reaches
# the cluster's owner, and how a neighbour reaches each machine. It is its own file so an
# engine from before the fixer ignores it instead of stopping on a section it doesn't know.
FIXER = {
    "command": str,       # run with the brief on standard input
    "notify": str,        # gets the message on standard input; $DEVCLUSTER_SUBJECT names it
    "timeout": int,       # seconds the fixer may run
    "wait": int,          # seconds before trying again when the fixer says someone is working there
    "failed_after": int,  # seconds an item may keep failing before the fixer looks at it
    "quiet_after": int,   # seconds without a report before a neighbour checks the machine
}
FIXER_MACHINE = {
    "ssh": str,       # how a neighbour reaches it when it stops reporting, such as user@host
    "watched": bool,  # false for a machine that is often off or asleep, such as a laptop
}


# cleanup.toml: the agent that runs the nightly disk cleanup, how long it may take, and what
# each machine's run needs to know. Its own file, like fixer.toml, so an engine from before
# the cleanup ignores it.
CLEANUP = {
    "command": str,    # run with the brief on standard input, in the run's folder
    "timeout": int,    # seconds a run may take
    "keep_days": int,  # days a run's folder stays
    "notes": str,      # facts that hold on every machine, a file in the config repo
}
CLEANUP_MACHINE = {
    "notes": str,    # what a run needs to know about this machine, a file in the config repo
    "floors": dict,  # free space each volume should keep, such as {"/" = "150G", "/data" = "10%"}
}
FLOOR = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT](?:i?B)?|%)$", re.IGNORECASE)
# keep.toml: folders a machine's cleanup leaves whole until a date, and who needs them.
KEEP_FIELDS = ("machine", "path", "who", "why", "until")


class ConfigError(Exception):
    def __init__(self, errors):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class Item:
    kind: str
    name: str
    fields: dict
    source: str  # the file it came from, for messages
    order: int = 0

    def get(self, key, default=None):
        return self.fields.get(key, default)


@dataclass
class Machine:
    name: str
    mode: str = "dry-run"
    os: str = ""
    skip: list = field(default_factory=list)
    items: list = field(default_factory=list)
    engine: str = ""  # a devcluster version for this machine alone, to try one before the rest
    description: str = ""


@dataclass
class Config:
    root: Path
    settings: dict
    items: list
    machines: dict
    fixer: dict = field(default_factory=dict)          # fixer.toml's settings
    fixer_machines: dict = field(default_factory=dict)  # fixer.toml's [machines.NAME] tables
    cleanup: dict = field(default_factory=dict)           # cleanup.toml's settings
    cleanup_machines: dict = field(default_factory=dict)  # cleanup.toml's [machines.NAME] tables
    keep: list = field(default_factory=list)              # keep.toml's entries

    def setting(self, key, default):
        return self.settings.get(key, default)

    def plan(self, machine):
        """The items that apply to one machine, in the order a run works through them."""
        chosen = {}
        for item in self.items:
            on = item.get("on")
            if on is None or machine in on:
                chosen[item.name] = item
        own = self.machines[machine]
        for item in own.items:
            chosen[item.name] = item
        for name in own.skip:
            chosen.pop(name, None)
        return sorted(chosen.values(), key=lambda i: (KINDS.index(i.kind), i.order))


def load(root):
    root = Path(root)
    errors = []
    cluster = _read(root / "cluster.toml", errors, required=True)
    machines_dir = root / "machines"
    machine_files = sorted(machines_dir.glob("*.toml")) if machines_dir.is_dir() else []
    machine_names = {path.stem for path in machine_files}
    if cluster is not None and not machine_files:
        errors.append("machines/: no machine files; add machines/NAME.toml for each machine")
    for name in sorted(machine_names):
        if not NAME.match(name):
            errors.append(f"machines/{name}.toml: machine names use letters, digits, '.', '_' and '-'")

    settings = {}
    items = []
    if cluster is not None:
        unknown = set(cluster) - set(KINDS) - {"devcluster"}
        for key in sorted(unknown):
            errors.append(f"cluster.toml: unknown section [{key}]")
        settings = _settings(cluster.get("devcluster", {}), errors)
        items = _items(cluster, "cluster.toml", root, machine_names, errors, shared=True)

    shared_names = {item.name for item in items}
    machines = {}
    for path in machine_files:
        data = _read(path, errors)
        if data is None:
            continue
        machines[path.stem] = _machine(path.stem, data, root, shared_names, machine_names, errors)

    fixer, fixer_machines = _fixer(root / "fixer.toml", machine_names, errors)
    cleanup, cleanup_machines = _cleanup(root / "cleanup.toml", root, machine_names, errors)
    keep = read_keep(root / "keep.toml", machine_names, errors)

    followers = [item for item in items + [i for m in machines.values() for i in m.items] if item.get("from")]
    if followers and not settings.get("follow_folder"):
        errors.append(f"cluster.toml: {followers[0].kind} {followers[0].name} copies {followers[0].get('from')}, so [devcluster] needs follow_folder: a folder every machine shares, such as one in the secrets repo")

    if errors:
        raise ConfigError(errors)
    return Config(root, settings, items, machines, fixer, fixer_machines, cleanup, cleanup_machines, keep)


def _read(path, errors, required=False):
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        if required:
            errors.append(f"{path.name}: missing")
    except tomllib.TOMLDecodeError as error:
        errors.append(f"{_label(path)}: {error}")
    return None


def _label(path):
    return f"machines/{path.name}" if path.parent.name == "machines" else path.name


def _settings(data, errors):
    if not isinstance(data, dict):
        errors.append("cluster.toml: [devcluster] must be a table")
        return {}
    for key, value in data.items():
        expected = SETTINGS.get(key)
        if expected is None:
            errors.append(f"cluster.toml: unknown setting devcluster.{key}")
        elif not isinstance(value, expected) or isinstance(value, bool):
            errors.append(f"cluster.toml: devcluster.{key} must be a {expected.__name__}")
    return dict(data)


def _fixer(path, machine_names, errors):
    data = _read(path, errors) if path.exists() else None
    if data is None:
        return {}, {}
    machines = data.pop("machines", {})
    settings = _fixer_settings(data, "fixer.toml", FIXER, errors)
    if not isinstance(machines, dict):
        errors.append("fixer.toml: write [machines.NAME] for each machine")
        return settings, {}
    chosen = {}
    for name, entry in machines.items():
        where = f"fixer.toml: machines.{name}"
        if name not in machine_names:
            errors.append(f"{where}: there is no machines/{name}.toml")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{where} must be a table")
            continue
        chosen[name] = _fixer_settings(entry, where, {**FIXER, **FIXER_MACHINE}, errors, machine=True)
    return settings, chosen


def _fixer_settings(data, where, allowed, errors, machine=False):
    # A machine's own table may set command to "": no agent runs on that machine, such as a
    # laptop that is its owner's alone. Its stuck items wait in the status.
    for key, value in data.items():
        expected = allowed.get(key)
        if expected is None:
            errors.append(f"{where}: unknown setting {key!r}")
        elif expected is bool:
            if not isinstance(value, bool):
                errors.append(f"{where}: {key} must be true or false")
        elif expected is str:
            if not isinstance(value, str) or not (value.strip() or (machine and key == "command" and value == "")):
                errors.append(f"{where}: {key} must be text")
        elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"{where}: {key} must be a whole number of seconds")
    return dict(data)


def _cleanup(path, root, machine_names, errors):
    data = _read(path, errors) if path.exists() else None
    if data is None:
        return {}, {}
    machines = data.pop("machines", {})
    settings = _cleanup_settings(data, "cleanup.toml", CLEANUP, root, errors)
    if not isinstance(machines, dict):
        errors.append("cleanup.toml: write [machines.NAME] for each machine")
        return settings, {}
    chosen = {}
    for name, entry in machines.items():
        where = f"cleanup.toml: machines.{name}"
        if name not in machine_names:
            errors.append(f"{where}: there is no machines/{name}.toml")
        elif not isinstance(entry, dict):
            errors.append(f"{where} must be a table")
        else:
            chosen[name] = _cleanup_settings(entry, where, CLEANUP_MACHINE, root, errors)
    return settings, chosen


def _cleanup_settings(data, where, allowed, root, errors):
    for key, value in data.items():
        expected = allowed.get(key)
        if expected is None:
            errors.append(f"{where}: unknown setting {key!r}")
        elif key == "notes":
            _check_notes(value, where, root, errors)
        elif key == "floors":
            _check_floors(value, where, errors)
        elif expected is str:
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}: {key} must be text")
        elif not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"{where}: {key} must be a whole number")
    return dict(data)


def _check_notes(value, where, root, errors):
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: notes must be a file in the config repo")
        return
    notes = PurePosixPath(value)
    if notes.is_absolute() or ".." in notes.parts:
        errors.append(f"{where}: notes must be a path inside the config repo")
    elif not (root / notes).is_file():
        errors.append(f"{where}: notes {value} isn't in the config repo")


def _check_floors(value, where, errors):
    if not isinstance(value, dict) or not value:
        errors.append(f'{where}: floors names each volume and the free space it keeps, such as {{"/" = "150G"}}')
        return
    for volume, floor in value.items():
        if not volume.startswith("/"):
            errors.append(f"{where}: floors names volumes by their full path; {volume!r} isn't one")
        elif not isinstance(floor, str) or not FLOOR.match(floor.strip()):
            errors.append(f'{where}: the floor for {volume} is a size such as "150G" or a share such as "10%"; {floor!r} isn\'t one')


def floor_bytes(text, total):
    """The free bytes a floor such as "150G" or "10%" asks for on a volume of `total` bytes."""
    match = FLOOR.match(str(text).strip())
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2).upper()
    if unit == "%":
        return int(total * amount / 100)
    return int(amount * 1024 ** ("KMGT".index(unit[0]) + 1))


def read_keep(path, machine_names, errors):
    """keep.toml's entries, each a dict whose until is a date. Problems go into `errors`."""
    data = _read(path, errors) if path.exists() else None
    if data is None:
        return []
    for key in sorted(set(data) - {"keep"}):
        errors.append(f"keep.toml: unknown section {key!r}; write [[keep]] for each folder")
    entries = data.get("keep", [])
    if not isinstance(entries, list):
        errors.append("keep.toml: write [[keep]] for each folder")
        return []
    kept = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            errors.append(f"keep.toml: entry {number} must be a table")
            continue
        entry = _keep_entry(entry, number, machine_names, errors)
        if entry:
            kept.append(entry)
    return kept


def _keep_entry(entry, number, machine_names, errors):
    where = f"keep.toml: {entry['path']}" if isinstance(entry.get("path"), str) else f"keep.toml: entry {number}"
    ok = True
    for key in sorted(set(entry) - set(KEEP_FIELDS)):
        errors.append(f"{where}: unknown field {key!r}")
        ok = False
    for key in KEEP_FIELDS:
        if key not in entry:
            errors.append(f"{where}: missing {key!r}; each folder names its {', '.join(KEEP_FIELDS)}")
            ok = False
        elif key != "until" and (not isinstance(entry[key], str) or not entry[key].strip()):
            errors.append(f"{where}: {key} must be text")
            ok = False
    if not ok:
        return None
    if entry["machine"] not in machine_names:
        errors.append(f"{where}: machine {entry['machine']!r} has no machines/{entry['machine']}.toml")
        ok = False
    if not entry["path"].startswith("/"):
        errors.append(f"{where}: path must be a full path, starting with /")
        ok = False
    until = parse_date(entry["until"])
    if until is None:
        errors.append(f"{where}: until is the last day it's kept, written YYYY-MM-DD; {entry['until']!r} isn't one")
        ok = False
    return {**entry, "until": until} if ok else None


def parse_date(value):
    """A date from "YYYY-MM-DD" text or a TOML date, or None."""
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        try:
            return datetime.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _items(data, source, root, machine_names, errors, shared):
    items = []
    seen = {}
    order = 0
    for kind in KINDS:
        if kind not in data:
            continue
        entries = data[kind]
        if kind == "secrets":
            if not isinstance(entries, dict):
                errors.append(f"{source}: [secrets] is a single table, not [[secrets]]")
                continue
            entries = [entries]
        elif not isinstance(entries, list):
            errors.append(f"{source}: write [[{kind}]] for each {kind}")
            continue
        for entry in entries:
            order += 1
            item = _item(kind, entry, source, root, machine_names, errors, shared)
            if item is None:
                continue
            item.order = order
            if item.name in seen:
                errors.append(f"{source}: two items are named {item.name!r} ({seen[item.name]} and {kind}); give one a name")
                continue
            seen[item.name] = kind
            items.append(item)
    return items


def _item(kind, entry, source, root, machine_names, errors, shared):
    if not isinstance(entry, dict):
        errors.append(f"{source}: each [[{kind}]] must be a table")
        return None
    required, optional = FIELDS[kind]
    allowed = required | optional | _COMMON
    where = f"{source}: {kind} {entry.get('name') or entry.get('path') or '?'}"
    ok = True
    for key in sorted(set(entry) - allowed):
        errors.append(f"{where}: unknown field {key!r}")
        ok = False
    for key in sorted(required - set(entry)):
        errors.append(f"{where}: missing {key!r}")
        ok = False
    for key, value in entry.items():
        if key in ("timeout", "install_timeout", "login_timeout", "setup_timeout"):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(f"{where}: {key} must be a whole number of seconds")
                ok = False
        elif key in ("on", "targets", "installs_into", "own", "only"):
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
                errors.append(f"{where}: {key} must be a list of names")
                ok = False
        elif not isinstance(value, str) or not value.strip():
            errors.append(f"{where}: {key} must be text")
            ok = False
    if "mode" in entry and entry["mode"] not in MODES:
        errors.append(f"{where}: mode must be \"dry-run\" or \"on\"")
        ok = False
    if entry.get("check") and not entry.get("install") and kind in ("repo", "tool"):
        errors.append(f"{where}: check runs after the install, so it needs an install")
        ok = False
    if entry.get("installs_into") and not entry.get("install"):
        errors.append(f"{where}: installs_into says where the install writes, so it needs an install")
        ok = False
    if kind == "cli" and ("version" in entry) == ("from" in entry):
        errors.append(f"{where}: give either a version or from, the machine whose version the others install")
        ok = False
    if kind == "nightly" and isinstance(entry.get("hours"), str):
        hours = HOURS.match(entry["hours"])
        if not hours or int(hours.group(1)) > 23 or int(hours.group(2)) > 23 or hours.group(1) == hours.group(2):
            errors.append(f"{where}: hours says when it may start, from one hour of the day to another, such as \"1-5\" or \"23-4\"")
            ok = False
    if kind == "settings" and ok:
        suffix = PurePosixPath(entry["file"]).suffix
        if "own" in entry and suffix not in (".toml", ".json"):
            errors.append(f"{where}: own names entries in a TOML or JSON file; {entry['file']} isn't one")
            ok = False
        if "only" in entry and suffix != ".json":
            errors.append(f"{where}: only names the keys copied in a JSON file; {entry['file']} isn't one")
            ok = False
        for name in entry.get("own", []):
            if not DOTTED.match(name):
                errors.append(f"{where}: own names entries by their keys joined with dots, with * for any one key, such as \"projects.*\"; {name!r} isn't one")
                ok = False
    if not ok:
        return None
    if entry.get("from") and entry["from"] not in machine_names:
        errors.append(f"{where}: from names {entry['from']!r}, which has no machines/{entry['from']}.toml")
        return None
    if "on" in entry:
        if not shared:
            errors.append(f"{where}: 'on' only belongs in cluster.toml; a machine file applies to its own machine")
            return None
        for name in entry["on"]:
            if name not in machine_names:
                errors.append(f"{where}: on names {name!r}, which has no machines/{name}.toml")
                ok = False
    if kind == "file":
        source_path = PurePosixPath(entry["source"])
        if source_path.is_absolute() or ".." in source_path.parts:
            errors.append(f"{where}: source must be a path inside the config repo")
            ok = False
        elif not (root / source_path).is_file():
            errors.append(f"{where}: source {entry['source']} isn't in the config repo")
            ok = False
    name = entry.get("name") or ("secrets" if kind == "secrets" else PurePosixPath(entry["path"].rstrip("/")).name)
    if not NAME.match(name):
        errors.append(f"{where}: name {name!r} uses letters, digits, '.', '_' and '-'")
        ok = False
    return Item(kind, name, dict(entry), source) if ok else None


def _machine(name, data, root, shared_names, machine_names, errors):
    source = f"machines/{name}.toml"
    allowed = set(KINDS) | {"mode", "os", "skip", "description", "engine"}
    for key in sorted(set(data) - allowed):
        errors.append(f"{source}: unknown key {key!r}")
    mode = data.get("mode", "dry-run")
    if mode not in MODES:
        errors.append(f"{source}: mode must be \"dry-run\" or \"on\"")
    skip = data.get("skip", [])
    if not isinstance(skip, list) or not all(isinstance(s, str) for s in skip):
        errors.append(f"{source}: skip must be a list of item names")
        skip = []
    for item_name in skip:
        if item_name not in shared_names:
            errors.append(f"{source}: skip names {item_name!r}, which isn't an item in cluster.toml")
    engine = data.get("engine", "")
    if not isinstance(engine, str):
        errors.append(f"{source}: engine must be a tag or commit ID")
        engine = ""
    items = _items(data, source, root, machine_names, errors, shared=False)
    return Machine(name, mode, str(data.get("os", "")), skip, items, engine, str(data.get("description", "")))
