"""Where devcluster keeps its install on a machine, and the clock it reads."""

import os
import time
from pathlib import Path


def expand(path):
    return Path(os.path.expanduser(str(path)))


def now():
    """The current time. DEVCLUSTER_NOW overrides it so tests can move the clock."""
    value = os.environ.get("DEVCLUSTER_NOW")
    return float(value) if value else time.time()


def clock(timestamp):
    return time.strftime("%H:%M", time.localtime(timestamp))


class Layout:
    """~/.devcluster, or DEVCLUSTER_HOME when it is set."""

    def __init__(self, root=None):
        root = root or os.environ.get("DEVCLUSTER_HOME") or Path.home() / ".devcluster"
        self.root = Path(root)
        self.engine = self.root / "engine"
        self.config = self.root / "config"
        self.state = self.root / "state"
        self.machine_file = self.root / "machine"

    def machine(self):
        try:
            name = self.machine_file.read_text().strip()
        except FileNotFoundError:
            return None
        return name or None


def engine_root():
    """The checkout this code is running from."""
    return Path(__file__).resolve().parents[2]
