# Where space usually goes

These are leads, with paths relative to the home folder. They say where to look, not what to decide. The size pass in [checking-use.md](checking-use.md) shows where the space really is tonight. When the biggest folders are somewhere not listed here or in the machine notes, open them anyway: a cleanup that only looks in known places misses whatever grew somewhere new, and once missed more than 500 GB that way.

## Task checkouts, on every machine

| Where | What it is |
| --- | --- |
| `~/workspace/*-worktrees/`, `~/workspace/*-wt/` | Collections of task checkouts, often 2 to 5 GB each with their builds. |
| `~/workspace/<repo>-<task>` | Top-level task copies. Some are linked worktrees and some are standalone clones. |
| `.claude/worktrees/` inside a primary checkout | Agent worktrees nested in a checkout that stays. The worktree can go, and its parent stays. |
| `~/worktrees/`, `~/.codex/worktrees/`, `/tmp`, `/private/tmp` | Other places agents put checkouts. `git worktree list` from each primary repository shows more. |

A primary checkout is the working copy a project's live work runs from, and the snapshot shows which one that is. A folder named like a primary that nobody has worked in for months is a copy like any other. So are other people's projects cloned to read, and old copies of projects from before a move. Salvage the unsaved bits and they go.

## Build output and caches

| Where | What it is |
| --- | --- |
| A task checkout's `build/`, `.dart_tool/`, `ios/Pods/`, `target/`, `node_modules/`, `.venv/` | These go with the checkout. In a checkout that stays, see [checkouts.md](checkouts.md) on build output. |
| `~/Library/Developer/Xcode/DerivedData/` (Mac) | Xcode build output. Clear it when no Xcode build or device run is using it. |
| `~/Library/Caches/go-build/`, `~/.cache/go-build`, `~/.gradle/caches/`, `~/.npm/_cacache/`, `~/Library/Caches/Homebrew/`, `~/Library/Caches/CocoaPods/`, `~/.cargo/registry` | Download and build caches. Use the tool's own cleaner (`go clean -cache`, `brew cleanup -s`) when no build is running. CI refills its caches within the hour, so clearing them on a busy CI machine gains little. |
| `~/.cache/uv/` | Python environments and wheels. Tools may start straight from it all day. `uv cache prune` waits while any uv process runs, and often frees little, so check before counting on it. |
| Docker images and build cache | `docker system df` shows them. `docker image prune` and `docker builder prune` clear what no container uses. Volumes hold databases and stay with whatever owns them. |

## Devices and VMs

- **iOS simulators.** `xcrun simctl list devices --json` lists them with their state and last boot. A task simulator nobody is using goes with `xcrun simctl delete <udid>`, once no test runner, `idb` or Flutter session has it open. A booted device with no app or test activity can be shut down and then deleted. Never shut down all devices or kill the simulator services.
- **Android emulators.** Each lives in `~/.android/avd/<name>.avd`, at 3 to 17 GB, often named for the task that made it. A running one shows up as a `qemu-system-*` process with its files open. A finished task's emulator goes with `avdmanager delete avd -n <name>`. Keep the system images that the remaining emulators use.
- **Colima and other VMs.** `colima list` shows each profile and whether it runs, and `~/.colima/_lima/_disks/<profile>` shows each disk's size and last change. A stopped profile named for a finished task is a task copy, and `colima delete --profile <name>` removes it with its disk. The default profile, and any profile a current project runs its databases in, stays. Inside a running VM, prune through Docker and leave volumes alone.

## Agent and tool state

- **Agent session history** (`~/.codex/sessions`, Claude's `projects` folders) is what people read to see what past sessions did. How long to keep it is the owner's call, and it is not this cleanup's job.
- **Credential backups.** Timestamped backup copies of an agent manager's credential files, older than a week, can go. Never read their contents.
- **Browsers.** Caches under `~/Library/Caches/<browser>` or `~/.cache/<browser>` can go while that browser isn't running. Profiles hold logins and stay.
- **Logs.** A text log over 128 MiB that a process appends to can be cut to its last 4 MiB in place, keeping the file: `tail -c 4194304 "$f" > "$f.tail" && cat "$f.tail" > "$f" && rm "$f.tail"`. Rotated logs older than a week can go. A writer that rewrites its log needs its own rotation.
- **Trash.** A desktop trash folder on a data drive (`.Trash-1000`) holds nothing in use.
- **This cleanup's own records** under `~/.devcluster/state/cleanup/` are pruned by the engine. Older cleanup records elsewhere, from before devcluster ran the cleanup, can go once they are two weeks old.

## Models and data drives

Trained models, policies and datasets usually live on a data drive, with links into them from the checkouts that use them. A model store on an internal disk is often a copy that belongs on a drive: if nothing links to it, no process reads it and the keep list doesn't name it, it goes. Two folders with similar names and sizes aren't necessarily duplicates, and a hard link or an APFS clone holds no second copy, so deleting one of two hard-linked copies frees nothing while the other remains.

## Everything else

When these places don't explain the disk, the size pass over the rest of the home folder, `/tmp` (`/private/tmp` and `/private/var/folders` on a Mac), `/Library` and `/opt` will. The owner's own folders (Documents, Desktop, Downloads, Pictures, Movies, Games, and their personal projects) are theirs: count them, and leave them. When deleting doesn't raise free space on a Mac, Time Machine's local snapshots are almost always the reason ([time-machine.md](time-machine.md)).
