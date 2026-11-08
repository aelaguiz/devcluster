"""Settings files copied from one machine, such as an agent CLI's config. The source machine saves
its file; every other machine's file is replaced with that copy, apart from the entries `own`
names, which each machine keeps as it has them, and, in a JSON file, the keys outside `only`."""

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..context import result
from ..paths import expand
from ..results import BLOCKED, BUSY, CHANGED, FAILED, OK, WOULD_CHANGE
from . import follow


class Unusable(Exception):
    """A file that can't be read or merged; the message says which and why."""


def sync_settings(item, ctx):
    label = item.get("file")
    path = expand(label)
    if follow.is_source(ctx, item):
        return _share(item, ctx, path, label)
    return _follow(item, ctx, path, label)


def _format(item):
    suffix = Path(item.get("file")).suffix
    return {".toml": "toml", ".json": "json"}.get(suffix, "text")


# On the source machine


def _share(item, ctx, path, label):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return result(item, FAILED, f"{label} isn't on this machine, so there's nothing for the others to copy")
    except (OSError, UnicodeDecodeError) as error:
        return result(item, FAILED, f"couldn't read {label}: {error}")
    try:
        copy = follow.to_copy(_shared(item, text))
    except Unusable as error:
        return result(item, FAILED, f"{label} {error}, so the other machines keep the last copy")
    if follow.read_copy(ctx, item) == copy:
        return result(item, OK, "the other machines follow this copy")
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, "would save this copy for the other machines")
    follow.save_copy(ctx, item, copy)
    return result(item, CHANGED, "saved this copy for the other machines")


def _shared(item, text):
    kind = _format(item)
    if kind == "toml":
        _toml(text, "isn't valid TOML")
    elif kind == "json":
        data = _json(text, "isn't valid JSON")
        if item.get("only"):
            if not isinstance(data, dict):
                raise Unusable("isn't a JSON object")
            chosen = {key: data[key] for key in item.get("only") if key in data}
            return json.dumps(chosen, indent=2, ensure_ascii=False) + "\n"
    return text


# On every other machine


def _follow(item, ctx, path, label):
    source = item.get("from")
    copy = follow.read_copy(ctx, item)
    if copy is None:
        return result(item, BUSY, f"waiting for {source}'s copy to arrive in {follow.copy_path(ctx, item)}")
    real = Path(os.path.realpath(path))
    if real.is_dir():
        return result(item, BLOCKED, f"{label} is a folder")
    try:
        current = real.read_text() if real.exists() else None
    except (OSError, UnicodeDecodeError) as error:
        return result(item, FAILED, f"couldn't read {label}: {error}")
    try:
        wanted = _wanted(item, follow.from_copy(copy), current, source)
    except Unusable as error:
        return result(item, FAILED, str(error))
    if wanted == current:
        return result(item, OK, f"matches {source}'s copy")
    if ctx.dry_run:
        return result(item, WOULD_CHANGE, f"would replace it with {source}'s copy" if current is not None else f"would write {source}'s copy")
    # A program that writes this file too, such as Claude Code, may have saved it since it was
    # read. Writing now would undo that save, so the next run starts again from it.
    if _read(real) != current:
        return result(item, BUSY, f"{label} changed while devcluster was updating it; the next run tries again")
    kept = _keep_first(ctx, item, current)
    mode = real.stat().st_mode & 0o777 if current is not None else (0o700 if wanted.startswith("#!") else 0o600)
    follow.write(real, wanted, mode)
    if real.read_text() != wanted:
        return result(item, FAILED, f"wrote {source}'s copy, but {label} doesn't match it")
    done = f"replaced it with {source}'s copy" if current is not None else f"wrote {source}'s copy"
    if kept:
        done += f"; what was here before is in {kept}"
    return result(item, CHANGED, done)


def _wanted(item, shared, current, source):
    kind = _format(item)
    if kind == "toml":
        if item.get("own"):
            return merge_toml(shared, current, item.get("own"), source)
        _toml(shared, f"{source}'s copy isn't valid TOML")
        return shared
    if kind == "json" and (item.get("only") or item.get("own")):
        chosen = _json(shared, f"{source}'s copy isn't valid JSON")
        data = _json(current, "this machine's copy isn't valid JSON, so it was left alone") if current else {}
        if not isinstance(data, dict) or not isinstance(chosen, dict):
            raise Unusable("this machine's copy isn't a JSON object, so it was left alone")
        if item.get("only"):
            wanted = _copy(data)
            for key in item.get("only"):
                if key in chosen:
                    wanted[key] = _copy(chosen[key])
                else:
                    wanted.pop(key, None)
        else:
            wanted = _copy(chosen)
        for path in _own_paths([data], _patterns(item.get("own", []))):
            _put(wanted, path, _copy(_at(data, path)[1]))
        return current if wanted == data else json.dumps(wanted, indent=2, ensure_ascii=False)
    if kind == "json":
        _json(shared, f"{source}'s copy isn't valid JSON")
    return shared


def _read(path):
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _keep_first(ctx, item, current):
    """The file as it was before devcluster first replaced it, kept once."""
    if current is None or ctx.root is None:
        return None
    kept = Path(ctx.root) / "state" / "settings" / f"{item.name}.before"
    if kept.exists():
        return None
    kept.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    follow.write(kept, current, 0o600)
    return str(kept).replace(os.path.expanduser("~"), "~", 1)


def _toml(text, problem):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise Unusable(problem) from None


def _json(text, problem):
    try:
        return json.loads(text)
    except ValueError:
        raise Unusable(problem) from None


# Merging a TOML file: the source's text, with this machine's own entries in place of the
# source's. The text is spliced rather than rebuilt, so the result reads like the source's file,
# comments and all; then it's parsed and compared with what the merge should hold.


@dataclass
class Block:
    path: tuple  # the table's keys, or () for what comes before the first table
    lines: list = field(default_factory=list)


def merge_toml(source_text, local_text, own, source="the source"):
    patterns = _patterns(own)
    source_data = _toml(source_text, f"{source}'s copy isn't valid TOML")
    source_blocks = blocks(source_text)
    local_data, local_blocks = {}, []
    if local_text:
        try:
            local_data = tomllib.loads(local_text)
        except tomllib.TOMLDecodeError:
            local_data = {}  # it doesn't read, so nothing of it is kept
        else:
            try:
                local_blocks = blocks(local_text)
            except Unusable:
                raise Unusable("this machine's file is laid out in a way devcluster can't split into tables, so it was left alone") from None

    def entry(block):
        """The own entry a table belongs to: the shortest start of its keys that a name matches."""
        found = [block.path[: len(p)] for p in patterns if _matches(p, block.path)]
        return min(found, key=len) if found else None

    local_entries = {}
    for block in local_blocks:
        key = entry(block)
        if key is not None:
            local_entries.setdefault(key, []).append(block)
    chosen, used = [], set()
    for block in source_blocks:
        key = entry(block)
        if key in local_entries:
            if key not in used:
                chosen += local_entries[key]
                used.add(key)
            continue
        chosen.append(block)
    for key, entries in local_entries.items():
        if key not in used:
            chosen += entries

    text = _join(chosen)
    expected = _expected(source_data, local_data, patterns)
    try:
        merged = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise Unusable("keeping this machine's own entries made a file that isn't valid TOML, so it was left alone") from None
    if merged != expected:
        raise Unusable("keeping this machine's own entries didn't come out as it should, so it was left alone")
    return text


def blocks(text):
    """The file split at each table header, outside strings and multi-line values."""
    found = [Block(())]
    scan = _Scan()
    for line in text.splitlines(keepends=True):
        if scan.clean() and line.lstrip().startswith("["):
            found.append(Block(_header(line), [line]))
            continue
        found[-1].lines.append(line)
        scan.feed(line)
    return found


def _header(line):
    try:
        node = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        raise Unusable(f"has a table header devcluster can't read: {line.strip()[:80]}") from None
    path = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
        if isinstance(node, list):
            path.append("[]")
            break
    return tuple(path)


def _join(chosen):
    text = ""
    for block in chosen:
        part = "".join(block.lines)
        if text and not text.endswith("\n"):
            text += "\n"
        text += part
    return text


def _expected(source_data, local_data, patterns):
    """What the merge should hold: the source's data, with each own entry the machine has."""
    expected = _copy(source_data)
    for path in _own_paths([source_data, local_data], patterns):
        found, value = _at(local_data, path)
        if found:
            _put(expected, path, _copy(value))
    return expected


# Own entries are named by their keys joined with dots, where `*` stands for any one key:
# "projects.*" is each trusted folder, "mcp_servers.browser" one server.


def _patterns(own):
    return [tuple(name.split(".")) for name in own]


def _matches(pattern, path):
    return len(path) >= len(pattern) and all(want in ("*", key) for want, key in zip(pattern, path))


def _own_paths(datas, patterns):
    """Every entry the names match in any of the data, leaving out one inside another."""
    found = set()
    for data in datas:
        for pattern in patterns:
            found.update(_walk(data, pattern))
    return sorted(path for path in found if not any(path[: n] in found for n in range(1, len(path))))


def _walk(node, pattern, path=()):
    if not pattern:
        return [path]
    if not isinstance(node, dict):
        return []
    keys = list(node) if pattern[0] == "*" else [pattern[0]] if pattern[0] in node else []
    return [found for key in keys for found in _walk(node[key], pattern[1:], path + (key,))]


def _at(data, path):
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return False, None
        data = data[key]
    return True, data


def _put(data, path, value):
    for key in path[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[path[-1]] = value


def _copy(data):
    if isinstance(data, dict):
        return {key: _copy(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_copy(value) for value in data]
    return data


class _Scan:
    """Whether a line starts outside any string or bracketed value."""

    def __init__(self):
        self.string = None
        self.depth = 0

    def clean(self):
        return self.string is None and self.depth == 0

    def feed(self, line):
        i = 0
        while i < len(line):
            char = line[i]
            if self.string is not None:
                if self.string in ('"""', '"') and char == "\\":
                    i += 2
                    continue
                if self.string in ('"""', "'''") and line.startswith(self.string, i):
                    # Up to two more quotes right after the closing three belong to the string.
                    end = i + 3
                    while end < min(len(line), i + 5) and line[end] == self.string[0]:
                        end += 1
                    self.string = None
                    i = end
                    continue
                if self.string == char:
                    self.string = None
                i += 1
                continue
            if char == "#":
                break
            if line.startswith('"""', i) or line.startswith("'''", i):
                self.string = line[i : i + 3]
                i += 3
                continue
            if char in "\"'":
                self.string = char
            elif char in "[{":
                self.depth += 1
            elif char in "]}":
                self.depth = max(0, self.depth - 1)
            i += 1
        if self.string in ('"', "'"):
            self.string = None  # a one-line string ends with its line
