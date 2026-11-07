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

Several tools install into the same folders, and devcluster mustn't need to know how each tool lays out its files. So it watches instead. Just before an install, it lists every file under those folders with its size, modification time, change time and inode. Just after, it lists them again. A file that's new or changed was written by this install. devcluster records each such file with a hash of its contents, as the item's own files. A file the install removed is dropped from the record, and one it left alone keeps its old hash, so an edit to it is still noticed before a later install overwrites it.

### Installed copies edited by hand

Before an install, devcluster compares each of the item's own files with its record. A file whose contents changed since the last install was edited on this machine, by a person or an agent.

devcluster copies each edited file to `~/.devcluster/kept/<item>/<date and time>/`, under its path relative to the home folder, and then installs. After the install, a kept copy identical to what's now installed is dropped. That happens when someone ran the same install by hand. Every other kept copy stays, and the item reports "Needs you" on every run until the kept folder is gone:

> Needs you  tool skills: updated from b635d7d; now at 8dea39f; ran its install, and its check passed; 1 installed file was edited here after devcluster installed it (~/.claude/skills/deploy/SKILL.md), and the edited copy is kept in ~/.devcluster/kept/skills. Move the edits into the repo, then delete that folder

The issue asked for edited copies to be reported, not overwritten. Keeping a copy and installing anyway meets that without leaving the machine on old versions until someone notices: on November 4, one hand edit stopped a rollout this way. The update goes ahead, and the edit is kept and reported, never lost.

### A failed install keeps the last good version

Before an install, devcluster copies the item's own files to `state/backup/<item>/`. If the install or its check fails:

1. **The files go back.** Every file of the item's own that the failed install changed or removed is restored from the backup. Every file it added is moved to `state/removed/<item>/` and kept there a week, not deleted: in a shared folder, another agent may have made one while the install ran. A file the item has never installed before isn't put back. Nothing is written or moved through a symlinked folder, since skill folders link into other repos.
2. **A tool goes back to its last good commit.** devcluster remembers the last commit whose install and check passed. It moves the checkout back to that commit, detached, as it already does when a list pins an older version, and runs that commit's install and check again. This also restarts any service with the old code. If someone committed in the checkout while the install ran, it's left where it is. If going back or reinstalling fails, say because another Git command held the checkout, the next run tries again.
3. **A repo stays where it is.** People work in a repo's checkout, so devcluster never moves it back. Its files go back, and that's all.
4. **The report says what happened:** which commit failed, the last lines of its output, and which commit is in use now.
5. **The failed commit isn't tried again** until `retry_failed_after` has passed (an hour by default), or until a newer commit arrives. While it waits, a tool stays on its last good commit, and its report says so.

On a tool's first install there's no last good commit, so a failure leaves the checkout where it is, and reports it.

### Trying one item in dry run

Any item can say `mode = "dry-run"`. On a machine that syncs for real, that item alone reports what it would do and changes nothing. A new tool can be added on machines that already sync their repos, watched, and then switched on.

### Status

`devcluster status --all` already lists every item on every machine with its commit ("up to date at 6ff64b6"). An item with kept edits shows under "Needs you" until they're dealt with.

## Using it for a set of agent tools

In the setup this project comes from, the tools fall into four kinds:

| Kind of tool | Listed as | Install | Check |
|---|---|---|---|
| A skills repo agents commit to on several machines | `[[repo]]`, synced both ways | Its own `make install` | Its own verify target |
| A command with a background service, developed on one machine | `[[tool]]`, and `[[repo]]` in that machine's file | Its install steps, without any sign-in check at the end | The skill copies match the repo, the command runs, the service is running |
| A server that has to answer | `[[tool]]` | Its service install, which restarts it | It answers on its local port |
| The launcher every agent session starts through | Not listed | | |

**A tool with its own upgrade procedure isn't listed.** A launcher whose new versions can need a reviewed runtime installed with administrator rights goes through its own upgrade steps, not a pull and an install. Once every machine runs a fixed release of it, it can be listed pinned to a commit and installed into a release folder that's never its development checkout.

Every check runs only on the machine itself. Some tools' own install ends by signing in to every account they use, so their item lists the steps before that.

## Tests

On the fake cluster, as for #1:

| Area | Cases |
|---|---|
| Check | A passing check. A failing check rolls back like a failed install. A check isn't run in dry run. |
| What the install wrote | Files written, rewritten with the same contents (as `rm -rf` then `cp` does), and removed. Two tools installing into the same folder each record only their own files. A `*` in `installs_into`. |
| Edits | An edited file is kept and the install goes ahead. A kept copy identical to the new install is dropped. "Needs you" lasts until the kept folder is removed. An edit to a file the item doesn't own is ignored. |
| Rollback | A tool's failed install puts its files back, returns it to the last good commit and reinstalls it, and doesn't retry the bad commit until the wait is over or a newer commit arrives. A repo's failed install puts the files back and leaves the checkout. A failure on the first install leaves it where it is. A failed check rolls back. Added files are moved aside, not deleted. A symlinked folder is never written or moved through. A commit made during the install stays put. Going back, and reinstalling the good commit, are retried on the next run when they fail. |
| Trial | An item in dry run on a machine that's on reports and changes nothing, while the other items sync. |

## Trying it on real machines

1. **Two machines first.** The config points two machines at the new engine through each machine's own `engine` setting. An engine stops on a config field it doesn't know, and a config error stops every machine still on it. So once those two run cleanly, every machine moves to the new engine before the config uses `check`, `installs_into` or `mode`. With the config unchanged, the new engine does what the old one did.
2. **A canary tool.** The canary repo gets a tiny install script and check that write into `~/.devcluster/canary-installed`, and a second checkout of it is listed as a tool on the two machines. With it, on real machines:
   - A commit pushed from one machine is installed on the other.
   - A hand edit to the installed file is kept and reported, and the install goes ahead.
   - A commit whose check fails takes both machines back to the last good commit.
   - A fixed commit moves both forward again.
3. **The real tools on the two machines,** in dry run first, then on, one at a time.
4. **Everywhere.** The pull request is merged and `cluster.toml` names the merged version. Each tool goes to the other machines in dry run first, then on. `devcluster status --all` shows every tool at the same commit everywhere.
5. **Retire the old rollouts:** the scripts that logged in to each machine to pull and install, except for setting up a new machine.
