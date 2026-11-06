"""What one item's sync found or did."""

from dataclasses import asdict, dataclass

OK = "ok"                      # already matches the list
CHANGED = "changed"            # devcluster changed it to match
WOULD_CHANGE = "would-change"  # dry run: devcluster would change it
BLOCKED = "blocked"            # it can't be changed safely; a person has to look
FAILED = "failed"              # a command failed; a later run retries it
BUSY = "busy"                  # something else was using it; the next run retries it

STATES = (OK, CHANGED, WOULD_CHANGE, BLOCKED, FAILED, BUSY)
LABELS = {
    OK: "OK",
    CHANGED: "Updated",
    WOULD_CHANGE: "Would change",
    BLOCKED: "Needs you",
    FAILED: "Failed",
    BUSY: "Busy",
}


@dataclass
class ItemResult:
    kind: str
    name: str
    state: str
    summary: str
    detail: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: data.get(k, "") for k in ("kind", "name", "state", "summary", "detail")})


def plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
