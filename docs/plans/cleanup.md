# Plan: one nightly disk cleanup, one skill, one keep list

Disks on these machines fill from what finished tasks leave behind: checkouts, experiment output, QA runs, captures, builds. Before this, each machine ran its own cleanup, set up by hand, with its own launcher, brief, model and skill, and devcluster knew nothing about them. Some failed every night without anyone hearing, and the only keep list was a file on one machine. [Issue #28](https://github.com/aelaguiz/devcluster/issues/28) has the full case. This plan is how devcluster runs the cleanup instead. It is the build of [#9](https://github.com/aelaguiz/devcluster/issues/9).

## The judgment

Almost everything on these disks is left over from tasks whose agents are gone. The agent that made a folder isn't coming back for it and won't notice it's gone, and nobody else knows it exists. So the cleanup can't wait to be sure something is dead: nothing will ever make it sure, and a cleanup that waits keeps everything. Earlier cleanups failed exactly that way. After each loss, the next version added a test to pass before deleting (show no agent uses it, prove it disposable, report what no rule covers), and for nearly every folder the answer was "can't tell", so the folder stayed.

The cleanup looks for life, which can be seen, and treats the rest as finished: something runs from it, something points at it, someone worked in it in the last couple of days on an unfinished task, or someone wrote down that it's needed, with a date. Some things are needed and show no sign of it, such as last week's results an agent is still reading. The answer to that is the last sign: whoever needs the folder writes it down. It is never a new test the cleanup must pass, because that test is how a cleanup slides back to keeping everything.

The skill, [skills/disk-cleanup](../../skills/disk-cleanup/SKILL.md), carries this judgment and the commands, and names no machine.

## How it works

1. **The nightly item starts it.** `[[nightly]] run = "devcluster cleanup"` starts one run in the night's hours. The machine is named from devcluster's config, never from its hostname, which macOS changes.

2. **The engine prepares the run.** It makes a run folder under `~/.devcluster/state/cleanup/runs/`, measures free space on the machine's volumes itself, writes `keep.txt` with the folders this machine's keep list protects, and writes the brief: the machine and what it's for, its volumes and floors, the cluster's and the machine's notes from the config, its keep list entries, the skill's path, the previous run, the deadline, and what to write at the end.

3. **The agent does the cleanup.** `cleanup.toml` names it. It finds what's alive, adds that to `keep.txt`, salvages unsaved work, and removes the rest through `devcluster cleanup rm`. When it finds a folder someone needs beyond tonight that nothing shows, it puts it on the keep list for them. It writes `summary.json`: its status, one sentence, the live work it named, the largest things it kept and why, salvage branches and problems.

4. **Removal refuses what's kept, in code.** `devcluster cleanup rm` refuses kept folders, folders that hold or sit inside one, links, mounts and folders holding a mount, home folders and workspace roots, and anything outside the places task folders live. It records what it removed. In a dry run it removes nothing and records what it would have. These are facts, not judgments, so code holds them.

5. **The engine checks and reports.** It reads the summary (missing or invalid counts as failed), measures free space again, and prints one line, which `devcluster status` shows: what was freed on each volume, the agent's sentence, any volume still under its floor and any drive that wasn't mounted. A part the agent couldn't check is a warning, and the rest of the run still counts. One run goes at a time, and old run folders are removed after `keep_days`.

## The keep list

`keep.toml` in the config names a machine, a folder, who relies on it, why, and the last day it's needed. `devcluster keep add`, `list` and `remove` edit it from any machine: each write checks the whole config, commits only that file and pushes it, so every machine has it on its next sync. Every entry has a date, because an entry without one quietly becomes keep-forever. Renewing is one more `add`. Expired entries keep nothing and drop out on the next write. Facts that don't expire, such as which store a live service reads, go in the machine's notes instead, where the agent sees why a folder matters.

## Proving it works before it deletes

- A dry run on real disks, with a reader from another model family going through the largest things it would keep. Each needs a reason a person would accept. "Couldn't establish it was unused" counts as a failure of the skill.
- A replay of a real wrong deletion: with the lost folders on the keep list, the dry run keeps them, and everything else that night removed is still on its list.
- The engine's tests (`tests/test_cleanup.py`) cover the keep commands, including a push another machine beat; the brief; the summary checks; floors and missing drives; the time limit stopping the agent and everything it started; one run at a time; pruning; every removal guard; and the dry run.

## How it stays effective

Every run's record lists the largest things it kept and why, so a cleanup that starts hoarding shows in its own record. A change to the skill gets a dry run on a real disk before it merges. A wrong deletion is answered with a keep entry, and an easier way to make one, never with a new check before deleting.
