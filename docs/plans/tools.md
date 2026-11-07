# Plan: skills and agent tools in sync on every machine

This is the plan for [issue #3](https://github.com/aelaguiz/devcluster/issues/3). The sync engine from [#1](sync-engine.md) already follows tool repos and runs their install when the commit changes. This plan adds what it takes to trust that with the tools every agent depends on:

- A check after each install, so "installed" means "works".
- A way back when an install or its check fails, so a bad commit never leaves a machine without a working version.
- Installed copies edited by hand are kept and reported, never silently lost.
- A trial mode for one item, so a new tool can be watched in dry run on machines that already sync for real.

It then puts the tools into the config, tries them on two machines, and rolls them out to the rest.

## What changes in the engine

### A check after the install

`[[repo]]` and `[[tool]]` items get a `check` field: a command run in the checkout after the install. The install counts as done only if the check passes. A failed check is handled exactly like a failed install.

A check proves that the install works on this machine: the skill copies match the repo, the command runs, the service is up. It shouldn't depend on a login or the network. A login that stops working is a credentials problem ([#4](https://github.com/aelaguiz/devcluster/issues/4)), and treating it as a broken install would roll back every new commit of a tool that's fine.

### What the install wrote

`[[repo]]` and `[[tool]]` items get an `installs_into` field: the folders the install writes into, such as `~/.claude/skills`. A `*` in a path matches any one folder name.

Several tools install into the same folders, and devcluster mustn't need to know how each tool lays out its files. So it watches instead. Just before an install, it lists every file under those folders with its size, modification time, change time and inode. Just after, it lists them again. A file that's new or changed was written by this install. devcluster records each such file with a hash of its contents, as the item's own files. A file the install removed is dropped from the record.

### Installed copies edited by hand

Before an install, devcluster compares each of the item's own files with its record. A file whose contents changed since the last install was edited on this machine, by a person or an agent.

devcluster copies each edited file to `~/.devcluster/kept/<item>/<date and time>/`, under its path relative to the home folder, and then installs. After the install, a kept copy identical to what's now installed is dropped. That happens when someone ran the same install by hand. Every other kept copy stays, and the item reports "Needs you" on every run until the kept folder is gone:

> Needs you  tool skills: updated from b635d7d; now at 8dea39f; ran its install, and its check passed; 1 installed file was edited here after devcluster installed it (~/.claude/skills/deploy/SKILL.md), and the edited copy is kept in ~/.devcluster/kept/skills. Move the edits into the repo, then delete that folder

The issue asked for edited copies to be reported, not overwritten. Keeping a copy and installing anyway meets that without leaving the machine on old versions until someone notices: on November 4, one hand edit stopped a rollout this way. The update goes ahead, and the edit is kept and reported, never lost.

### A failed install keeps the last good version

Before an install, devcluster copies the item's own files to `state/backup/<item>/`. If the install or its check fails:

1. **The files go back.** Every file the failed install changed or removed is restored from the backup, and every file it added is removed. Only the item's own files are restored. A file the item has never installed before isn't touched.
2. **A tool goes back to its last good commit.** devcluster remembers the last commit whose install and check passed. It moves the checkout back to that commit, detached, as it already does when a list pins an older version, and runs that commit's install and check again. This also restarts any service with the old code.
3. **A repo stays where it is.** People work in a repo's checkout, so devcluster never moves it back. Its files go back, and that's all.
4. **The report says what happened:** which commit failed, the last lines of its output, and which commit is in use now.
5. **The failed commit isn't tried again** until `retry_failed_after` has passed (an hour by default), or until a newer commit arrives. While it waits, a tool stays on its last good commit, and its report says so.

On a tool's first install there's no last good commit, so a failure leaves the checkout where it is, and reports it.

### Trying one item in dry run

Any item can say `mode = "dry-run"`. On a machine that syncs for real, that item alone reports what it would do and changes nothing. A new tool can be added on machines that already sync their repos, watched, and then switched on.

### Status

`devcluster status --all` already lists every item on every machine with its commit ("up to date at 6ff64b6"). An item with kept edits shows under "Needs you" until they're dealt with.

## The config for Amir's cluster

| Tool | Kind | Machines | Install | Check |
|---|---|---|---|---|
| arch_skill | Repo, synced both ways: agents commit to it on several machines | All six | `make install` | `make verify_install` |
| agent-email | Tool; a repo on the M5, where it's developed | All six; each already has its settings and service | Its install steps without the final Google login check | Skill copies match the repo, the command runs, the listener is running |
| approvals | Repo on the M5 | The M5, the only machine with its Slack settings; Mini 1 gets only the command and skill | Its install steps without the final Slack check | Skill copies match the repo, the command runs, and on the M5 the listeners are running |
| Amir MCP | Repo on the M5, tool on the home server | The M5 and home server, where it runs today | `scripts/install-service.sh`, which restarts it | The server answers on its local port |
| AI Manager | Not in this rollout | | | |

**AI Manager isn't in this rollout.** Four machines (the home server, the M3 36 GB, the M3 Max and the Studio) run `aim` straight from their working checkout. That checkout is at the temporary release from the November 2 restore, 40 commits behind, with 22 to 24 uncommitted files. Moving them forward means installing the reviewed Claude runtime with administrator rights, and AI Manager's own README says that goes through its upgrade steps, not through a pull and an install. Once every machine runs a release with its runtime installed, AI Manager can follow a pinned commit, installed into a fixed release folder that's never the development checkout.

Every check here runs only on the machine itself. agent-email's and approvals' own `make install` end with a login check against Google or Slack, so their installs list the steps before it.

## Tests

On the fake cluster, as for #1:

| Area | Cases |
|---|---|
| Check | A passing check. A failing check rolls back like a failed install. A check isn't run in dry run. |
| What the install wrote | Files written, rewritten with the same contents (as `rm -rf` then `cp` does), and removed. Two tools installing into the same folder each record only their own files. A `*` in `installs_into`. |
| Edits | An edited file is kept and the install goes ahead. A kept copy identical to the new install is dropped. "Needs you" lasts until the kept folder is removed. An edit to a file the item doesn't own is ignored. |
| Rollback | A tool's failed install puts its files back, returns it to the last good commit and reinstalls it, and doesn't retry the bad commit until the wait is over or a newer commit arrives. A repo's failed install puts the files back and leaves the checkout. A failure on the first install leaves it where it is. A failed check rolls back. |
| Trial | An item in dry run on a machine that's on reports and changes nothing, while the other items sync. |

## Trying it on real machines

1. **Two machines first.** The config points the M5 and Mini 1 at the new engine version, through each machine's own `engine` setting. The other four keep the current version.
2. **A canary tool.** The canary repo the repos rollout uses gets a tiny install script and check that write into `~/.devcluster/canary-installed`, and a second checkout of it is listed as a tool on the M5 and Mini 1. With it, on real machines:
   - A commit pushed from the M5 is installed on Mini 1.
   - A hand edit to the installed file on Mini 1 is kept and reported, and the next install goes ahead.
   - A commit whose check fails rolls both machines back to the last good commit.
   - A fixed commit moves both forward again.
3. **The real tools, in dry run, on the M5 and Mini 1.** Read what each would do, then switch them on, one at a time: agent-email, approvals, Amir MCP (the M5 only), then arch_skill.
4. **The fleet.** The pull request is merged, `cluster.toml` names the merged version, and the two machine pins go. Each tool goes to the other four machines in dry run first, then on. `devcluster status --all` shows every tool at the same commit everywhere.
5. **Retire the old rollouts.** Once arch_skill and agent-email are live everywhere, the publish skill's loop that logs in to each machine and pulls goes away, and so does agent-email's `scripts/rollout.sh`, except for setting up a new machine.
