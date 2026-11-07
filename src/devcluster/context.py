"""What every item handler needs to know about the run it's part of."""

from dataclasses import dataclass, field
from pathlib import Path

from .results import ItemResult


@dataclass
class Context:
    machine: str
    dry_run: bool
    config: object
    now: float
    memory: dict = field(default_factory=dict)
    root: Path = None  # this machine's devcluster folder: kept/ and state/ are inside it

    def mem(self, item):
        """What this machine remembers about one item between runs."""
        return self.memory.setdefault(f"{item.kind}:{item.name}", {})

    @property
    def retry_after(self):
        return self.config.setting("retry_failed_after", 3600)

    def identity(self):
        """Who merge commits are made as, when the config names someone."""
        env = {}
        name = self.config.setting("git_name", None)
        email = self.config.setting("git_email", None)
        if name:
            env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = name
        if email:
            env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = email
        return env


def result(item, state, summary, detail=""):
    return ItemResult(item.kind, item.name, state, summary, detail)
