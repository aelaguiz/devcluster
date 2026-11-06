"""Read and check the config repo: cluster.toml plus one file per machine."""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The order a run works through the kinds: credentials first, since installs may need them.
KINDS = ("secrets", "repo", "tool", "cli", "file")

_COMMON = {"name", "on"}
FIELDS = {
    "secrets": ({"path", "url", "check", "decrypt"}, {"branch", "timeout"}),
    "repo": ({"path", "url"}, {"branch", "install", "timeout", "install_timeout"}),
    "tool": ({"path", "url"}, {"ref", "install", "timeout", "install_timeout"}),
    "cli": ({"name", "version", "check", "install"}, {"timeout", "install_timeout"}),
    "file": ({"name", "source", "targets"}, set()),
}
SETTINGS = {
    "engine": str,
    "heartbeat": int,
    "stale_after": int,
    "retry_failed_after": int,
    "git_name": str,
    "git_email": str,
}
MODES = ("dry-run", "on")


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


@dataclass
class Config:
    root: Path
    settings: dict
    items: list
    machines: dict

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

    if errors:
        raise ConfigError(errors)
    return Config(root, settings, items, machines)


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
        if key in ("timeout", "install_timeout"):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(f"{where}: {key} must be a whole number of seconds")
                ok = False
        elif key in ("on", "targets"):
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
                errors.append(f"{where}: {key} must be a list of names")
                ok = False
        elif not isinstance(value, str) or not value.strip():
            errors.append(f"{where}: {key} must be text")
            ok = False
    if not ok:
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
    return Machine(name, mode, str(data.get("os", "")), skip, items, engine)
