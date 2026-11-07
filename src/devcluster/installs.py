"""Running a repo's or tool's install: its check, what it wrote, edits made by hand since,
and putting its files back when it fails.

devcluster doesn't know how a tool lays out its files, and several tools install into the
same folders. So it watches the folders an item names in `installs_into`: a file that's new
or changed while the item's install runs was written by that install, and becomes one of
the item's own files. Before the next install, an own file whose contents changed since was
edited on this machine. It's copied to kept/ before the install goes ahead, so the edit is
never lost, and the item reports it until someone deals with it.
"""

import glob
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .proc import shell, tail

INSTALL_TIMEOUT = 1800
OUTSIDE_HOME = "_root"  # where kept/ and backup/ store a file from outside the home folder


@dataclass
class Outcome:
    ok: bool
    step: str = ""      # "install" or "check": the step that failed
    how: str = ""       # "exited 3", "timed out"
    output: str = ""
    kept: list = field(default_factory=list)  # edited files kept aside this time
    restored: bool = False


# Where things live


def _folder(ctx, part, item):
    return Path(ctx.root, *part.split("/"), item.name)


def _files_path(ctx, item):
    return Path(ctx.root, "state", "files", f"{item.name}.json")


def own_files(ctx, item):
    """The item's own files, each with the hash of what its last install wrote there."""
    try:
        return json.loads(_files_path(ctx, item).read_text())
    except (OSError, ValueError):
        return {}


def _save_own_files(ctx, item, files):
    path = _files_path(ctx, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(files, indent=0, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _inside(path):
    """Where kept/ and backup/ store a file: its path relative to the home folder."""
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):
        return path[len(home) + 1:]
    return os.path.join(OUTSIDE_HOME, path.lstrip(os.sep))


def _shown(stored):
    """A path stored under kept/, as a person would type it."""
    if stored.startswith(OUTSIDE_HOME + os.sep):
        return stored[len(OUTSIDE_HOME):]
    return "~/" + stored


def _is_file(path):
    return os.path.islink(path) or (os.path.exists(path) and not os.path.isdir(path))


# Watching the folders


def roots(item):
    found = []
    for pattern in item.get("installs_into") or []:
        path = os.path.normpath(os.path.expanduser(pattern))
        for match in (sorted(glob.glob(path)) if glob.has_magic(path) else [path]):
            if match not in found:
                found.append(match)
    # A folder inside another listed folder would be walked twice.
    return [r for r in found if not any(r != o and r.startswith(o.rstrip(os.sep) + os.sep) for o in found)]


def scan(item):
    """Every file and symlink under the item's folders, with what changes when one is rewritten."""
    files, folders = {}, set()
    for root in roots(item):
        for top, subdirs, names in os.walk(root):
            folders.add(top)
            for name in names + [d for d in subdirs if os.path.islink(os.path.join(top, d))]:
                path = os.path.join(top, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                files[path] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
    return files, folders


def digest(path):
    try:
        if os.path.islink(path):
            return hashlib.sha256(b"link:" + os.readlink(path).encode()).hexdigest()
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return None


def _copy(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    shutil.copy2(source, target, follow_symlinks=False)


# Edits made by hand


def edited(item, ctx):
    """The item's own files whose contents changed since its last install wrote them."""
    return [path for path, recorded in sorted(own_files(ctx, item).items()) if _is_file(path) and digest(path) != recorded]


def kept_files(ctx, item):
    """Edited copies kept for this item and not yet dealt with, as a person would type them."""
    folder = _folder(ctx, "kept", item)
    if not folder.is_dir():
        return []
    found = set()
    for stamp in folder.iterdir():
        if stamp.is_dir():
            for top, _, names in os.walk(stamp):
                for name in names:
                    found.add(_shown(os.path.relpath(os.path.join(top, name), stamp)))
    return sorted(found)


def kept_note(ctx, item):
    """What a report says while edited copies are kept, or "" when there are none."""
    files = kept_files(ctx, item)
    if not files:
        return ""
    shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
    folder = "~/" + os.path.relpath(_folder(ctx, "kept", item), os.path.expanduser("~"))
    if len(files) == 1:
        said = f"1 installed file was edited here after devcluster installed it ({shown}), and the edited copy is kept in {folder}"
    else:
        said = f"{len(files)} installed files were edited here after devcluster installed them ({shown}), and the edited copies are kept in {folder}"
    return f"{said}. Move the edits into the repo, then delete that folder"


def _keep(ctx, item, paths):
    folder = _folder(ctx, "kept", item) / time.strftime("%Y-%m-%d-%H%M%S", time.localtime(ctx.now))
    for path in paths:
        _copy(path, folder / _inside(path))
    return folder


def _drop_identical(folder, paths):
    """Drop kept copies that match the file in place now. Returns the paths still kept."""
    remaining = []
    for path in paths:
        copy = folder / _inside(path)
        if digest(str(copy)) == digest(path):
            copy.unlink()
        else:
            remaining.append(path)
    _prune(folder)
    _prune(folder.parent)
    return remaining


def _prune(folder):
    """Remove empty folders under and including this one."""
    if not folder.is_dir():
        return
    for top, _, _ in sorted(os.walk(folder), key=lambda entry: len(entry[0]), reverse=True):
        try:
            os.rmdir(top)
        except OSError:
            pass


# Running the install


def run(item, ctx, cwd):
    """Run the item's install and then its check, in the checkout, watching what the install writes."""
    watched = bool(item.get("installs_into"))
    if watched:
        owned = own_files(ctx, item)
        changed = edited(item, ctx)
        kept_folder = _keep(ctx, item, changed)
        backup = _folder(ctx, "state/backup", item)
        shutil.rmtree(backup, ignore_errors=True)
        for path in owned:
            if _is_file(path):
                _copy(path, backup / _inside(path))
        before, before_folders = scan(item)

    timeout = item.get("install_timeout", INSTALL_TIMEOUT)
    step, ran = "install", shell(item.get("install"), cwd=cwd, timeout=timeout)
    if ran.ok and item.get("check"):
        step, ran = "check", shell(item.get("check"), cwd=cwd, timeout=timeout)
    outcome = Outcome(ran.ok, step, "timed out" if ran.timed_out else f"exited {ran.code}", tail(ran.err or ran.out))
    if not watched:
        return outcome

    after, after_folders = scan(item)
    written = {p for p, signature in after.items() if before.get(p) != signature}
    removed = set(before) - set(after)

    if outcome.ok:
        files = {path: digest(path) for path in (set(owned) - removed) | written if _is_file(path)}
        _save_own_files(ctx, item, files)
        # A kept copy identical to what's installed now means someone ran the same install by hand.
        outcome.kept = _drop_identical(kept_folder, changed)
        shutil.rmtree(backup, ignore_errors=True)
        return outcome

    # Put back what the failed install changed: the item's own files from the backup, and
    # remove the files it added. A file the item never installed isn't the item's to restore.
    for path in sorted(written | removed):
        saved = backup / _inside(path)
        if path in owned and (saved.is_symlink() or saved.exists()):
            _copy(saved, path)
        elif path not in before and os.path.lexists(path) and not os.path.isdir(path):
            os.unlink(path)
    for folder in sorted(after_folders - before_folders, key=len, reverse=True):
        try:
            os.rmdir(folder)
        except OSError:
            pass
    shutil.rmtree(backup, ignore_errors=True)
    outcome.restored = True
    # The edits are back in place with the rest, so their kept copies aren't needed.
    _drop_identical(kept_folder, changed)
    return outcome
