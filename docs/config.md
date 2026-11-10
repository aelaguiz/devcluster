# Config reference

The config is a private Git repo. Every machine keeps a checkout of it in `~/.devcluster/config` and pulls it at the start of each run. [examples/config](../examples/config) is a complete working example.

```
cluster.toml          what every machine gets
machines/NAME.toml    one file per machine; the file name is the machine's name
files/                files that [[file]] items put in place
fixer.toml            optional: the agent that fixes stuck items, and how to reach you
cleanup.toml          optional: the agent that runs the nightly disk cleanup, and each machine's floors and notes
keep.toml             optional: folders each machine's cleanup keeps until a date; edit it with devcluster keep
```

Check a config before you push it:

```sh
devcluster check-config path/to/config
```

Paths can start with `~`, which means the home folder of whoever runs devcluster on that machine.

## cluster.toml

### [devcluster]

| Setting | Default | Meaning |
|---|---|---|
| `engine` | none | The devcluster version every machine runs: a tag, a branch or a commit ID. Without it, machines stay on the version they have. |
| `heartbeat` | 300 | Seconds between status pushes when nothing changed. |
| `stale_after` | 900 | Seconds without a report before `status` calls a machine stale. |
| `retry_failed_after` | 3600 | Seconds before a failed install is tried again at the same version. |
| `git_name`, `git_email` | the repo's own identity | Who merge commits are made as. |
| `follow_folder` | none | Where a machine that others copy leaves its copies for them, one file per item. Something else must carry this folder to every machine; a folder inside the secrets repo does, encrypted, which matters because settings files can hold tokens. Required when any item has `from`. |

### Fields every item can have

| Field | Meaning |
|---|---|
| `name` | How the item is shown and referred to. Defaults to the folder name for repos and tools, and to `secrets` for the secrets repo. Names are unique across the file. |
| `on` | A list of machine names. The item applies only to those machines. Without it, it applies to all of them. |
| `mode` | `"dry-run"` to try this one item: on a machine that syncs for real, it reports what it would do and changes nothing. Without it, the item follows the machine's mode. |

### [secrets]

A Git repo of encrypted secrets, with its own commands to check, decrypt and publish them. There's at most one.

| Field | Required | Meaning |
|---|---|---|
| `path` | yes | Where it's checked out |
| `url` | yes | Its origin |
| `check` | yes | A command, run in the checkout, that succeeds when the decrypted files match the encrypted ones |
| `decrypt` | yes | A command, run in the checkout, that writes the decrypted files |
| `publish` | no | A command, run in the checkout, that encrypts what changed on this machine into the files Git tracks. With it, the repo syncs both ways |
| `own` | no | Files in the checkout that are each machine's own and that the decrypt never writes, such as a log something on the machine keeps there, as paths in the checkout. `*` matches any characters, and `{machine}` is the machine's name: `["local/logins/{machine}.json"]`. A change to one of them doesn't count as a local edit after a failed decrypt. |
| `branch` | no | Defaults to `main` |
| `timeout` | no | Seconds for each command, default 600 |

Without `publish`, the repo only comes in. A run checks first. If the check fails, this machine may have an edit nobody published, so the run stops there. Otherwise it fast-forwards, decrypts and checks again.

With `publish`, this machine's changes go out too. A run publishes, commits every change Git sees (within the repo's own ignore rules), and checks. If the check still fails, the publish missed an edit, so the run stops before pulling anything over it. Otherwise it pulls, pushes and merges with origin the way a [[repo]] does, and decrypts and checks again when origin's commits came in. When this machine and origin changed the same file, the merge conflicts and the run stops with that file named, changing nothing, so the fixer or a person can merge the two versions.

Either way, if the decrypt fails, the next run retries it, unless a decrypted file has changed since, and nothing is published until a decrypt succeeds. No command's output is recorded.

### [[repo]]

A repo people and agents commit to directly on one branch. It syncs both ways.

| Field | Required | Meaning |
|---|---|---|
| `path` | yes | Where it's checked out. A missing checkout is cloned. |
| `url` | yes | Its origin. A checkout whose origin is something else is left alone. |
| `branch` | no | Defaults to `main`. The checkout must be on this branch. |
| `install` | no | A command run in the checkout whenever its commit changes |
| `check` | no | A command run in the checkout after the install. See [Installs](#installs). |
| `installs_into` | no | The folders the install writes into. See [Installs](#installs). |
| `sync` | no | A command, run in the checkout, that brings origin's commits in and pushes this machine's, used instead of devcluster's own fetch, merge and push. Use it for a checkout many live sessions edit and commit to at once, where the repo ships a script that merges around their uncommitted edits and their commits landing mid-merge. |
| `timeout` | no | Seconds for a fetch or push, or for the `sync` command, default 600 |
| `install_timeout` | no | Seconds for the install, and for the check, default 1800 |

With `sync`, devcluster still clones a missing checkout, requires the branch, and runs the install and check when the commit changed. It runs the command only when the branch and origin differ, then fetches and compares them with how they were before, to report what came in and went out. A failing command fails the item, with its last line of errors as the summary. Origin moving on again straight after is normal; the item fails only if origin's earlier commits didn't come in or this machine's didn't reach origin. When the fixer takes a stuck one, the branch it saves first holds the edits to tracked files and the files in the way, not every untracked file the sessions left there.

### [[tool]]

A repo a machine only follows: it moves to `ref` and never pushes.

| Field | Required | Meaning |
|---|---|---|
| `path`, `url` | yes | As for a repo |
| `ref` | no | A branch, tag or commit ID. Defaults to `main`. |
| `install` | no | A command run in the checkout whenever its commit changes, and once on the first run |
| `check`, `installs_into` | no | As for a repo. See [Installs](#installs). |
| `timeout`, `install_timeout` | no | As for a repo |

A tool checkout with commits origin doesn't have is left alone. On a machine where you edit the tool, list it as a `[[repo]]` in that machine's file instead. Moving back, or to a pinned tag or commit, leaves the checkout detached at that commit: devcluster never moves a tool's branch backward. A commit made on a detached checkout while devcluster moves it is kept on a branch named `devcluster-kept-<commit>`.

### Installs

A repo's or tool's `install` runs when its commit changes. Then its `check` runs, if it has one. The install counts as done only when both pass. A check proves the install works on this machine: the copies match the repo, the command runs, the service is up. Keep logins and the network out of it, or a login that stops working looks like a broken install.

`installs_into` lists the folders the install writes into, such as `["~/.claude/skills", "~/.agents/skills"]`. A `*` matches any one folder name: `"~/.hermes/profiles/*/skills"`. Several items can name the same folder. devcluster lists the files there before and after the install, and the files that are new or changed are the item's own. It records a hash of each, in `~/.devcluster/state/files/NAME.json`.

With `installs_into`:

- **An installed file edited on this machine is kept.** Before an install, an own file whose contents changed since an install last wrote it is copied to `~/.devcluster/kept/NAME/<date and time>/`, under its path in the home folder, and the install goes ahead. A copy identical to what the install then writes is dropped. The item shows as "Needs you" until that folder is deleted.
- **A failed install or check puts the files back.** The item's own files go back as they were before the install. Files it added are moved to `~/.devcluster/state/removed/NAME/`, where they stay for a week: in a folder several tools and agents share, one of them could be someone else's. A file the item has never installed before isn't put back, and nothing is written or moved through a symlinked folder.

Whether or not it has `installs_into`, when a tool's install or check fails, the tool goes back to the last commit whose install and check passed, detached, and that commit's install runs again. If the checkout moved while the install ran, because someone committed there, it's left where it is. If going back or installing the good commit again fails, the next run tries again. A repo's checkout stays where it is, because people work in it. A failed commit is tried again after `retry_failed_after`, or as soon as a newer commit arrives. Until then, a tool stays on its last good commit.

### [[cli]]

A command-line tool at one version: a version written here, or whatever version one machine has.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Its name |
| `version` | one of these two | The version every machine should have |
| `from` | one of these two | The machine whose version every other machine installs. On that machine devcluster installs nothing: it reads the version from the check and saves it in `follow_folder`. Upgrading there is how a new version reaches the others. |
| `check` | yes | A command whose output includes the installed version |
| `install` | yes | A command that installs it. `{version}` is replaced with the version. |
| `timeout` | no | Seconds for the check, default 60 |
| `install_timeout` | no | Seconds for the install, default 1200 |

The version must appear in the check's output as a whole word: `0.163.0` doesn't match `0.163.0-alpha.5`. When machines installed the tool in different ways, give each one's `install` and `check` in its machine file.

### [[setup]]

Something each machine needs set up once that no repo's install covers: a toolchain, a service, a folder of files another machine has. A new machine gets it on its first sync, and a machine that loses it gets it back.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Its name |
| `check` | yes | A command that exits 0 when everything the item sets up is in place on this machine. Its last line says what's missing. |
| `setup` | yes | A command that sets up what's missing. Running it again when part is in place must be safe. |
| `timeout` | no | Seconds for the check, default 60 |
| `setup_timeout` | no | Seconds for the setup, default 1800 |

It follows the same rules as a `[[login]]`: the check runs every run, the setup when the check fails, a setup can do part of the work each run, and one that leaves the check's line as it was is tried again after `retry_failed_after`. A stuck setup is handed to the fixer like a stuck install.

### [[login]]

Logins a tool keeps for itself, made on each machine. Use this for a login that can't be copied between machines: an OAuth login whose refresh token changes every time it's used signs both copies out the first time both renew it. Each machine makes its own instead, usually from a password the secrets repo carries.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Its name |
| `check` | yes | A command that exits 0 when every login the item keeps works on this machine. Its last line says what's missing, such as which identities have no login here. |
| `login` | yes | A command that makes the missing logins |
| `timeout` | no | Seconds for the check, default 60 |
| `login_timeout` | no | Seconds for the login, default 1800 |

devcluster runs the check every run, and the login when the check fails. When the check's line changes after a login, some logins were made, and the next run carries on: so a login command can make a few at a time. A login that leaves the check's line as it was is tried again after `retry_failed_after`, or at once if the check later reports something different, such as a new identity to log in.

Neither command may print a credential. The check's last line is shown in `devcluster status` (with anything that looks like a token or password masked), so it should name only what's missing, and say the same thing until that changes.

### [[file]]

A file from the config repo, put in place on each machine.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Its name |
| `source` | yes | Its path inside the config repo |
| `targets` | yes | Where it goes on each machine |

devcluster copies the file and remembers what it wrote. A target edited on the machine since then is reported, not overwritten. A different file already at a target the first time is moved aside to `NAME.before-devcluster`.

### [[settings]]

A settings file copied from one machine, such as an agent CLI's config: you change settings on the machine you work on, and every other machine gets them. That machine's file is the truth, and the others' files are replaced with it.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Its name, also the name of its copy in `follow_folder` |
| `file` | yes | The file, such as `~/.codex/config.toml` |
| `from` | yes | The machine the others copy |
| `own` | no | For a TOML or JSON file: entries each machine keeps as it has them. Name an entry by its keys joined with dots, with `*` for any one key: `"projects.*"` is each folder a CLI has trusted on that machine, and `"mcp_servers.browser"` one MCP server that a program on that machine registers for itself. An own entry the machine has stays exactly as it is; one only the source has is added. |
| `only` | no | For a JSON file: the top-level keys copied, such as `["mcpServers"]`. The rest of the file is the machine's own. |

On the source machine, each run saves a copy of the file in `follow_folder`, with its home folder written as `{{home}}`. A file that doesn't parse isn't saved, so the others keep the last copy. On every other machine, the file is replaced with the copy, with `{{home}}` turned into that machine's home folder, so a machine with a different user name gets its own paths. A file that is a link, because another program owns the real file, is written through the link. The first time devcluster replaces a machine's file, the file as it was is kept in `~/.devcluster/state/settings/NAME.before`. After that, an edit made on the machine is replaced at the next run. When a program on one machine keeps adding its own entry back, such as a browser registering its MCP server, list that entry in `own` in the machine's file; otherwise the two take turns rewriting the file. If the file changes while devcluster is updating it, nothing is written and the next run tries again.

### [[nightly]]

A command that runs once a night on each machine, such as Git's maintenance on the repos it syncs.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Its name |
| `run` | yes | The command. It runs through `/bin/sh` in the home folder. `$DEVCLUSTER_REPOS` lists the checkouts this machine syncs (its repos, tools and secrets repo), one path to a line. |
| `hours` | no | The local hours it may start in, such as `"1-5"` (the default) or `"23-4"` |
| `timeout` | no | Seconds it may run before it is stopped, default 3600 |

The first run inside the hours starts the command in the background, so a long run doesn't hold up syncing, and later runs report how it went: running, how long it took and its last line, or what it exited with. It runs once a night. A machine that sleeps through the hours skips that night instead of running the next morning while someone may be using it. A failed run shows in `devcluster status` until the next night's run. Its output is in `~/.devcluster/state/nightly/NAME/log`.

## machines/NAME.toml

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"dry-run"` | `"on"` to sync for real. In dry run, devcluster reports what it would change and changes nothing, apart from updating itself and its config checkout. |
| `os` | none | `"macos"` or `"linux"`, for your own reference |
| `description` | none | What the machine is for. The disk cleanup's brief quotes it. |
| `engine` | none | A devcluster version for this machine only, so a new version can be tried on one machine first |
| `skip` | none | Names of items in cluster.toml that don't apply here |

A machine file can also hold `[secrets]`, `[[repo]]`, `[[tool]]`, `[[cli]]`, `[[login]]`, `[[file]]` and `[[settings]]` items. An item with the same name as a shared one replaces it on this machine. Any other item is added. `on` has no meaning in a machine file.

## fixer.toml

When an item stays stuck, devcluster saves everything local in it to a branch and starts an agent on that machine to fix it. The branch is pushed to the item's origin unless anyone can read that origin, as with a public GitHub repo: then it stays on the machine, because uncommitted notes and untracked files were never meant to be published. A secrets repo's branch always stays on the machine. A machine that stops reporting gets checked over SSH by the next machine in the list. You hear about it only when a fix needs your hands. [The fixer plan](plans/fixer.md) explains how it works, and the agent's instructions are [skills/devcluster-fixer](../skills/devcluster-fixer/SKILL.md).

Without this file, or without `command`, nothing here happens and stuck items wait for you. It is a file of its own so a machine still on an older engine ignores it rather than stopping on a setting it doesn't know.

| Setting | Default | Meaning |
|---|---|---|
| `command` | none | The agent to start. It runs through `/bin/sh` with the brief on standard input, in the stuck checkout. It must run unattended and exit when done. |
| `notify` | none | How to reach you. It runs through `/bin/sh` with the message on standard input; `$DEVCLUSTER_SUBJECT` holds a subject line and `$DEVCLUSTER_MESSAGE` the path of a file holding the message. Without it, problems show only in `devcluster status`. |
| `timeout` | `1800` | Seconds the agent may run before it is stopped |
| `wait` | `1800` | Seconds before trying again when the agent says someone is still working there. It doubles each time, up to four hours. |
| `failed_after` | `1800` | Seconds an item may keep failing before the agent looks at it. An item that needs you is handed over on its second run. |
| `quiet_after` | `1200` | Seconds without a report before the next machine checks on it |

Each `[machines.NAME]` table holds what is particular to one machine:

| Key | Default | Meaning |
|---|---|---|
| `ssh` | none | How the next machine reaches it, such as `you@server.local` |
| `watched` | `true` | `false` for a machine that is often off or asleep, such as a laptop, so its silence isn't reported |
| any setting above | | Overrides that setting on this machine. `command = ""` starts no agent there, for a machine where agents never run; its stuck items wait in `devcluster status`. |

One agent runs on a machine at a time, and none runs on a machine in dry run or for an item in dry run. Each run's brief, the agent's output and its result are kept in `~/.devcluster/state/fixer/`, and the messages sent are in `~/.devcluster/state/fixer/messages/`.

## cleanup.toml

The disk cleanup is one run on each machine, done by an agent following [skills/disk-cleanup](../skills/disk-cleanup/SKILL.md). Almost everything that fills these disks is left over from tasks whose agents are gone, so the agent looks for signs of life (something runs from it, something points at it, someone worked in it lately, or it's on the keep list) and clears the rest. [The cleanup plan](plans/cleanup.md) explains it.

`devcluster cleanup` runs it once, in the foreground. To run it each night, add a nightly item, with a `timeout` longer than the cleanup's own so the nightly limit never stops it first:

```toml
[[nightly]]
name = "disk-cleanup"
run = "devcluster cleanup"
timeout = 6000
```

Its last line, what it freed or why it failed, is what `devcluster status` shows. Without this file, or without `command`, `devcluster cleanup` fails and says so. It is a file of its own so a machine still on an older engine ignores it.

| Setting | Default | Meaning |
|---|---|---|
| `command` | none | The agent. It runs through `/bin/sh` with the brief on standard input, in the run's folder. It must run unattended and exit when done. |
| `timeout` | `5400` | Seconds the agent may run before it and everything it started are stopped |
| `keep_days` | `14` | Days a run's folder stays |
| `notes` | none | A file in the config repo with facts that hold on every machine. The brief lists it before the machine's own notes. |

Each `[machines.NAME]` table holds what is particular to one machine:

| Key | Default | Meaning |
|---|---|---|
| `notes` | none | A file in the config repo saying what is special about this machine: its drives, what serves from where, the links that mark a live store, where task output piles up. |
| `floors` | `{"/" = none}` | The volumes to measure and the free space each should keep, as a size (`"150G"`) or a share of the volume (`"10%"`). A run that ends below a floor reports a warning. A volume that isn't mounted is skipped and named. |

Each run gets a folder in `~/.devcluster/state/cleanup/runs/`, and `~/.devcluster/state/cleanup/latest` points at the newest. The engine writes `brief.md`, `keep.txt` (this machine's kept paths, which the agent adds its live work to) and `result.json` (free space it measured itself before and after, and the outcome). The agent writes `summary.json`:

```json
{"status": "ok", "said": "one sentence", "live": ["..."], "kept": [{"path": "...", "bytes": 0, "why": "..."}], "salvage": ["..."], "problems": ["..."]}
```

`status` is `ok`, `warning` or `failed`. A missing or unreadable summary, a status that isn't one of those, and a run stopped at its time limit all count as failed. One run goes at a time on a machine.

The agent gets `DEVCLUSTER_CLEANUP_RUN` (the run's folder), `DEVCLUSTER_MACHINE`, and, in a dry run, `DEVCLUSTER_CLEANUP_DRY_RUN=1`. It removes things only through `devcluster cleanup rm`.

### devcluster cleanup rm

`devcluster cleanup rm PATH...` is the one way a run deletes. For each path it refuses, says why, and removes nothing when the path doesn't exist, is a link (remove a link itself with plain `rm`, never what it points to), is `/`, the home folder, `~/workspace`, `~/worktrees`, a mount or a folder holding one, lies outside the places task folders live (the home folder, the machine's floor volumes other than `/`, and the temporary folders), or is kept, holds something kept, or is inside something kept. Kept means this machine's unexpired keep list entries plus the run's `keep.txt`. Otherwise it measures the folder, removes it (on Linux without crossing into another drive), and adds it to the run's `removed.jsonl`. In a dry run it removes nothing and adds it to `would-remove.jsonl`. `--sudo` removes with `sudo -n`, for files the user can't remove. Outside a run it applies the same guards.

## keep.toml

Folders a machine's cleanup leaves whole, with everything under them, until the end of a date. It is for folders someone still needs that show no sign of it, such as a finished run someone is still reading. Facts that don't expire, such as which store a live service reads, belong in the machine's cleanup notes.

```toml
[[keep]]
machine = "server"
path = "/data/experiments/line-b"
who = "the research agents"
why = "the only trained output of line B; graders read it this week"
until = "2026-11-23"
```

Every field is required, `path` is a full path, and `until` is the last day it's kept. An entry past its date keeps nothing and is dropped by the next write.

Edit it with `devcluster keep` from any machine. Each write checks the list, commits only `keep.toml` and pushes it, so every machine has the change on its next sync. When another machine pushed first, it makes the edit again on top of what that machine pushed. A push that still fails undoes its commit and says so, so the config checkout never holds a commit origin doesn't have.

| Command | What it does |
|---|---|
| `devcluster keep add PATH --until YYYY-MM-DD --who WHO --why WHY` | Keeps a folder until a date. On this machine the path is resolved through links and must exist; `--machine NAME` adds one on another machine. Adding a folder that is already there renews it. |
| `devcluster keep list` | This machine's entries. `--machine NAME` shows another's and `--all` every machine's. Expired entries are marked. |
| `devcluster keep remove PATH` | Takes a folder off the list |
