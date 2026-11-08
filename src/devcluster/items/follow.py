"""Items that copy one machine. The machine an item names in `from` is its source: each run there
saves a copy into the follow folder, which something else carries to every machine (such as an
encrypted secrets repo), and every other machine is made to match that copy."""

import os
import re
import tempfile
from pathlib import Path

from ..paths import expand

HOME = "{{home}}"  # stands for the home folder in a saved copy, so each machine gets its own paths


def is_source(ctx, item):
    return ctx.machine == item.get("from")


def copy_path(ctx, item):
    return expand(ctx.config.setting("follow_folder", "")) / item.name


def read_copy(ctx, item):
    try:
        return copy_path(ctx, item).read_text()
    except FileNotFoundError:
        return None


def save_copy(ctx, item, text):
    path = copy_path(ctx, item)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write(path, text, 0o600)


def to_copy(text, home=None):
    """This machine's home folder replaced by the marker, wherever it's a whole path part."""
    home = home or os.path.expanduser("~")
    return re.sub(re.escape(home) + r"(?![\w.-])", lambda _: HOME, text)


def from_copy(text, home=None):
    return text.replace(HOME, home or os.path.expanduser("~"))


def write(path, text, mode):
    """Replace a file in one step. The caller passes the real path, so a link to it stays a link."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.devcluster-")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
