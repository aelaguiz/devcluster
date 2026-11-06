"""Each machine's last run: written locally, pushed to status/<machine>, and read back for everyone."""

import hashlib
import json
import time

from .git import Git
from .paths import now
from .results import BLOCKED, BUSY, CHANGED, FAILED, LABELS, OK, STATES, WOULD_CHANGE

PREFIX = "status/"
FILE = "status.json"
_TIMING = ("started", "finished")


def _key(report):
    """What the report says, ignoring when it was made, so an unchanged run can skip a push."""
    same = {k: v for k, v in report.items() if k not in _TIMING}
    return hashlib.sha256(json.dumps(same, sort_keys=True).encode()).hexdigest()


def publish(layout, report, heartbeat):
    """Push the report to this machine's status branch. Returns an error message, or None."""
    record_path = layout.state / "published.json"
    try:
        last = json.loads(record_path.read_text())
    except (OSError, ValueError):
        last = {}
    key = _key(report)
    if last.get("key") == key and report["finished"] - last.get("at", 0) < heartbeat:
        return None
    git = Git(layout.config, {
        "GIT_AUTHOR_NAME": "devcluster",
        "GIT_AUTHOR_EMAIL": "devcluster@localhost",
        "GIT_COMMITTER_NAME": "devcluster",
        "GIT_COMMITTER_EMAIL": "devcluster@localhost",
    })
    body = json.dumps(report, indent=1, sort_keys=True) + "\n"
    blob = git.run("hash-object", "-w", "--stdin", input=body)
    if not blob.ok:
        return f"couldn't record the status: {blob.first_line()}"
    tree = git.run("mktree", input=f"100644 blob {blob.out.strip()}\t{FILE}\n")
    if not tree.ok:
        return f"couldn't record the status: {tree.first_line()}"
    commit = git.run("commit-tree", tree.out.strip(), "-m", f"Status of {report['machine']}")
    if not commit.ok:
        return f"couldn't record the status: {commit.first_line()}"
    # Each machine is the only writer of its own status branch, and only the latest
    # report matters, so replacing it is safe. This is the only force-push devcluster makes.
    branch = f"refs/heads/{PREFIX}{report['machine']}"
    pushed = git.run("push", "--quiet", "--force", "origin", f"{commit.out.strip()}:{branch}", timeout=120)
    if not pushed.ok:
        return f"couldn't push the status: {pushed.first_line()}"
    record_path.write_text(json.dumps({"key": key, "at": report["finished"]}))
    return None


def read_all(layout):
    """Every machine's latest report, and a note if GitHub couldn't be reached."""
    git = Git(layout.config)
    note = None
    fetched = git.run("fetch", "--quiet", "--prune", "origin", f"+refs/heads/{PREFIX}*:refs/remotes/origin/{PREFIX}*", timeout=120)
    if not fetched.ok:
        note = f"Couldn't reach origin ({fetched.first_line()}); showing what was fetched last."
    reports = {}
    listing = git.out("for-each-ref", "--format=%(refname)", f"refs/remotes/origin/{PREFIX}") or ""
    for ref in listing.splitlines():
        machine = ref[len(f"refs/remotes/origin/{PREFIX}"):]
        shown = git.out("show", f"{ref}:{FILE}")
        try:
            reports[machine] = json.loads(shown) if shown else None
        except ValueError:
            reports[machine] = None
    try:
        own = json.loads((layout.state / "last.json").read_text())
        if own.get("machine") and own.get("finished", 0) >= (reports.get(own["machine"]) or {}).get("finished", 0):
            reports[own["machine"]] = own
    except (OSError, ValueError):
        pass
    return reports, note


def counts(report):
    tally = {state: 0 for state in STATES}
    for item in report.get("items", []):
        tally[item.get("state")] = tally.get(item.get("state"), 0) + 1
    return tally


def _ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def _tally_text(tally):
    order = (BLOCKED, FAILED, BUSY, WOULD_CHANGE, CHANGED, OK)
    words = {OK: "OK", CHANGED: "updated", WOULD_CHANGE: "would change", BLOCKED: "need you", FAILED: "failed", BUSY: "busy"}
    if tally.get(BLOCKED) == 1:
        words[BLOCKED] = "needs you"
    parts = [f"{tally[state]} {words[state]}" for state in order if tally.get(state)]
    return ", ".join(parts) or "nothing listed"


def render(reports, machines, stale_after, note=None, show_all=False, clock=None):
    """The text `devcluster status` prints."""
    current = clock if clock is not None else now()
    names = sorted(set(machines) | set(reports))
    rows = [("Machine", "Last sync", "Mode", "Items")]
    for name in names:
        report = reports.get(name)
        if not report:
            rows.append((name, "never reported", "", ""))
            continue
        age = current - report.get("finished", 0)
        when = _ago(age) + (" (stale)" if age > stale_after else "")
        mode = "dry run" if report.get("mode") == "dry-run" else report.get("mode", "")
        items = "config error" if report.get("errors") else _tally_text(counts(report))
        if name not in machines:
            items += " (not in the config)"
        rows.append((name, when, mode, items))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = ["  ".join(row[i].ljust(widths[i]) for i in range(3)) + "  " + row[3] for row in rows]
    lines[0:1] = [lines[0], "  ".join("-" * w for w in widths) + "  " + "-----"]

    sections = [(BLOCKED, "Needs you"), (FAILED, "Failed"), (BUSY, "Busy"), (WOULD_CHANGE, "Would change (dry run)")]
    if show_all:
        sections += [(CHANGED, "Updated"), (OK, "OK")]
    errors = [(name, error) for name in names for error in (reports.get(name) or {}).get("errors", [])]
    if errors:
        lines += ["", "Config errors:"] + [f"  {name}: {error}" for name, error in errors]
    for state, heading in sections:
        entries = [
            (name, item)
            for name in names
            for item in (reports.get(name) or {}).get("items", [])
            if item.get("state") == state
        ]
        if entries:
            lines += ["", f"{heading}:"]
            for name, item in entries:
                lines.append(f"  {name}  {item['kind']} {item['name']}: {item['summary']}")
    if note:
        lines += ["", note]
    return "\n".join(lines)


def log_line(report):
    tally = counts(report)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report["finished"]))
    errors = f"; config errors: {len(report['errors'])}" if report.get("errors") else ""
    return f"{stamp} {report['mode']} {_tally_text(tally)}{errors}\n"


def summary_lines(report):
    lines = []
    widths = max([len(LABELS[i["state"]]) for i in report.get("items", [])] or [0])
    for item in report.get("items", []):
        lines.append(f"  {LABELS[item['state']].ljust(widths)}  {item['kind']} {item['name']}: {item['summary']}")
    for error in report.get("errors", []):
        lines.append(f"  Config error: {error}")
    return lines

