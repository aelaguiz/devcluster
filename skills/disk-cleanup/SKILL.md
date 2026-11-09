---
name: disk-cleanup
description: Free disk space on a devcluster machine by finding what is still alive and clearing the rest. That means old task checkouts and worktrees (unsaved work goes to a salvage branch first), finished experiments' bulk output, leftover QA runs, captures and renders, reproducible builds and caches, stopped task simulators, emulators and VMs, and Time Machine snapshots the backup drive already has. Use when `devcluster cleanup` starts you with a cleanup brief, when a disk is full or has a free-space target, or when someone asks you to clean up a machine. Each machine's differences come from the config's machine notes and its keep list (`devcluster keep`), so this one skill serves every machine. Not for Codex's own databases and session history.
---

# Disk cleanup

A full disk stops every agent, build, CI job and Git operation on the machine, and these machines fill fast. Dozens of agents start checkouts, experiments and QA runs every day, and then their sessions end. Your job is to give the disk back while keeping what is still alive and what can't be replaced.

## Almost everything here is dead

A machine has two or three live projects at a time. Almost everything else on its disk is left over from tasks whose agents are gone. The agent that made a folder isn't coming back for it, won't be asked about it, and won't notice it's gone, and nobody else knows the folder exists. A folder you defer "for someone's decision" stays until the next run defers it again, so deferring is deciding to keep it forever.

So don't try to prove each folder is dead. You can't, and a cleanup that needs proof keeps everything. Earlier versions of this cleanup each asked for some proof before deleting: show that no agent is using it, prove it can be thrown away, report a folder no rule covers. For nearly every folder the answer came back "can't tell", the folder stayed, and the disks kept filling. One server freed about 23 GiB on a typical night that way. In its first two nights of looking for life instead, it freed 368 GiB and then 935 GiB. Another time, 10 of a project's 223 task checkouts on that server were live. Removing the other 213 took its main disk from 24 GB free to over 300 GB, and all their unsaved work fit in 450 MB.

Find what's alive, which you can see, and treat the rest as done.

## What alive looks like

Something alive leaves marks you can check in a few minutes:

- **Something runs from it.** A process has the folder open, works in it, or names it in its arguments. That covers a training run, a server, a build, a QA stack, or an agent session working there. Agent sessions are processes too: when none of a session's processes are left, its agent is gone.
- **Something points at it.** A service definition, a scheduled job, a container's mount, a link from a live checkout, or a pid file or socket inside it that names a running process. A setup script that could lay a folder out someday doesn't count: what counts is something that uses it now, or will tonight.
- **Someone is working in it.** Commits, edits or new output in the last couple of days, on a task that isn't finished. A merged or closed PR finishes the task now, whatever the clock says.
- **Someone wrote down that it's needed.** Above all an entry on the keep list (`devcluster keep list`), which your brief repeats for this machine. A note with a name and a date that hasn't passed, such as an `OWNER.md` in the folder, counts the same way.

[checking-use.md](references/checking-use.md) has the commands. You take one snapshot of every process and match all your candidates against it at once, then check the few other places use hides. Read it before the first deletion. It gets the answers in about a minute, instead of one slow probe per folder.

Then read what you found. A server answering players, a training run writing checkpoints, a build that started an hour ago: that's use. Some processes are only leftovers of finished work: a `tail -f` on the log of a job that ended, a shell nobody is attached to whose parent is gone, a language server indexing a checkout nobody edits, a tool server that happened to start in that folder. Stopping those exact processes is part of cleaning up after them. One fresh file doesn't make a big folder alive either, so look at what was written. A script that rewrites `.env` in every checkout in the same minute is not work in any of them.

The other way round matters as much:

- Nothing had a model store open, but the live checkout's `policies` link pointed into it.
- A backend had been quiet for days, and an `OWNER.md` in it reserved it until the next week.
- A QA folder whose files were a week old was serving that afternoon's release test from its binaries.

Each of those was alive, and each was one look away.

Before deleting anything, write down the live projects on the machine in a line, for example: "the owner's main checkout, the game server's worktree, two experiment rounds that are writing, tonight's QA run". If you can't name them, you haven't looked yet. Add their paths to the run's `keep.txt`.

## When the signs miss something

Some things are needed and leave no mark at all. An agent reading last week's results opens each file for a second and writes nothing beside it. One night a run removed the trained output of two finished experiments: no writes for days, no process holding it, and the only copy. Agents had been reading it that week to grade new work, and it had cost about two thousand dollars of compute to make. Nothing on the disk could have shown that.

The answer is the fourth sign of life. The agent that needs a folder puts it on the keep list with a date, and every run keeps that folder whole until the date passes. When you notice that your own work, or work you were told about, needs a folder that nothing holds open, add it yourself: `devcluster keep add PATH --until DATE --who WHO --why WHY`.

When a run removes something it shouldn't have, ask who needed it and why they couldn't say so, and make saying so easier. Don't answer it with a new test this cleanup must pass before it deletes, such as a longer quiet period, a size limit, or proof that no agent will ever want it. That test comes back "can't tell" for every folder, and it is how each earlier version of this cleanup went back to keeping everything. If you are editing this skill after a loss, the same goes for you.

## What dead things leave behind

**Checkouts** are caches of GitHub. The only thing of value in a dead one is work that exists nowhere else: uncommitted edits, commits not on the remote, stashes. That is small. Delete the dead checkouts with nothing unsaved first, because that takes seconds and frees the most space. Then push the rest to salvage branches, and delete each one as its push lands. Submodules, dirty files, ignored files, an open PR, a big size, and a name that doesn't look temporary are not reasons to keep a dead checkout. [checkouts.md](references/checkouts.md) has the salvage commands, including where work with no remote goes.

**Experiments** leave an answer. People come back for what a run found: the readings, the report, the manifest, the few small files that say what happened. They also come back for the winner, if it shipped. The bulk only got the run to its answer: checkpoints and models of finished runs, captures, generated datasets, intermediate rounds, logs. When nothing runs the experiment and nothing has been written for days, keep the small record and remove the bulk. What a run cost (billions of iterations, a week of compute) doesn't keep it. Someone who needs it writing that down does. The newest candidates of a live project, and the baseline its current work compares against, are part of that live project.

**Task output** (QA nights, device captures, renders, downloaded builds, recovery copies, scratch) is done when its task is. Usually one small thing in it is worth keeping, such as a final cut, a report or a configuration file. Keep that and remove the rest, including the kinds of file you didn't expect to find there. A round folder whose intermediates you cleared but whose videos and stills you left alone has been kept, not cleaned.

**Copies that regenerate** (build output, package caches, mirrors a tool copies again on its next run) can go when nothing is building from them. Clearing a cache that a running job refills within the hour gains nothing, and on a Mac with Time Machine it briefly costs double. A cache nothing uses gains its whole size.

**Devices, VMs and databases.** A stopped simulator, emulator or VM named for a finished task is a task copy. A database or VM disk belongs to whatever uses it. The live service's database stays, even when it is quiet tonight. A finished project's database, its stopped containers' volumes and its old backup dumps go with the project. The machine's main development VM, and the databases in it, stay.

[where-space-goes.md](references/where-space-goes.md) lists where space usually sits on a Mac and on a Linux server. The machine notes add this machine's own places.

## What stays

The list is short, because few things belong on it:

- what is alive, as above, including everything on the keep list until its date;
- credentials, keys and login state;
- what serves players, customers or production, and the data it reads;
- live databases and the service volumes under them;
- the owner's own files: documents, photos, downloads, and personal projects;
- one working copy of each project the machine works on, and any shared Git folders those copies use.

Before you add anything to this list, ask who would come back for it and what they would do with it.

## Safety lines

These hold in every case:

- **Delete through `devcluster cleanup rm`.** It refuses a link, a home folder, a workspace root, a mount, anything outside the places task folders live, and anything that is on the keep list, holds something on it or sits inside it. Never get around a refusal with plain `rm`. A refusal means you aimed at the wrong path, or that someone kept it. For files your user can't remove, add `--sudo`; the guards still apply. Git's own `worktree remove` and a tool's own cleaner (`xcrun simctl delete`, `colima delete`, `docker image prune`, `tmutil deletelocalsnapshots`) are fine for what they own. Check those targets against the keep list yourself.
- **Delete only what you looked at.** Use its full resolved path. A session that ran `rm -rf` on a machine's whole home folder emptied about 72 workspaces, and took their logins and CI runners with it.
- **A backup that didn't land isn't a backup.** Delete a checkout with unsaved work only after you have seen its salvage commit on the remote, or in a local salvage ref. If a push fails, a commit id comes back empty, or a check errors, that checkout stays this run and you say why.
- **A failed check has told you nothing.** If the process snapshot came back empty, a permission was denied or a check timed out, run it again, or leave the paths it covered for this run. A folder you don't understand is different. That isn't a failed check, it's a reason to look inside.
- **Leave the scripts you write honest.** Most deletions happen inside a loop written minutes earlier. `|| true`, a bare `except`, or an empty default can turn a failed check into a pass nobody sees. Make each path's deletion depend on that path's own evidence, and read the output for skips and errors before you report.
- **Leave model payloads unread.** Never read, hash, expand or fetch trained model or policy payloads to decide anything. Their manifests, names, dates and links are enough.
- **Keep secrets off the page.** Never print command lines, environments or credential files. The use checks report process ids and names only.
- **Stop only exact leftovers.** Stop a process only when you've identified it as a leftover of finished work, by its pid, never by name pattern. Other people's running work stays running.

Whether a wrong deletion can be undone depends on the machine. A Mac whose Time Machine backups are current keeps a deleted folder on its backup drive for about a month. A server with no backup loses it for good. The machine notes say which this is. That doesn't change what counts as dead. It means that on a machine with no backup, it's worth looking twice at a large folder you aren't sure about.

## The run

Your brief comes from `devcluster cleanup`. It names the machine, its drives and floors, this machine's keep entries, the machine notes to read, the last run's folder, this run's folder (`$DEVCLUSTER_CLEANUP_RUN`) and your deadline. In a dry run, change nothing: `devcluster cleanup rm` records what it would remove, `devcluster keep add` records what it would keep, and you record everything else you would do in the summary.

1. Read the cluster notes and this machine's notes, if the brief names them. They say what serves from where, which drives exist, and anything on this machine that hides its owner.
2. Measure free space on each volume the brief lists. On a Mac with Time Machine, read [time-machine.md](references/time-machine.md) first, because deletions don't show as free space until the snapshots holding them go.
3. Make one size pass that also records each folder's newest file. Save it in the run folder for the rest of this run and for the next one.
4. Take one process snapshot, and run the other use checks for all the large candidates at once.
5. Name the live projects, and add their paths to `keep.txt` in the run folder, one per line. The keep list's entries are already there. Everything else that's large is a candidate.
6. Delete the dead checkouts with nothing unsaved, and the dead output. Salvage the rest in a batch, and delete each one as its push lands. Take a fresh snapshot before each batch, not before each path.
7. Clear experiments' bulk, task output, unused caches, and stopped task devices and VMs.
8. On a Mac with Time Machine, delete the local snapshots the backup drive already has, then measure again.
9. Write `summary.json` in the run folder, in the shape the brief gives: the live work you named, the largest things you kept, each with a reason a person would accept, the salvage refs, and any problems. "A server runs from it" and "kept until the 23rd for the graders" are reasons. "Could not establish that it was unused" is not. Then end with a short report: free space before and after, what went and how big, and anything that truly needs the owner.

Earlier reports tell you where things are, not what to decide. Decide each large folder again from what you see tonight. A free-space floor is a floor, not a finish line: keep clearing dead things after you pass it. When something really is the owner's call (their own files, or something that serves players and looks unused), leave it and say so in one line. Don't build a list of deferrals.

Work directly. Don't start other agents, change the schedule or the config (the keep list aside), or send messages. The only commits you make are salvage commits, and each one carries `[skip ci]`. If part of the machine can't be checked, such as a VM that won't answer, say so as a problem with that part and carry on with the rest. If something fails, say so in the report, rather than retrying forever.

When someone asks you to clean a machine by hand, `devcluster cleanup` starts a run with its brief. If you are doing it yourself instead, make a run folder of your own, export `DEVCLUSTER_CLEANUP_RUN` to it with a `keep.txt` naming the live work, and follow the same steps.
