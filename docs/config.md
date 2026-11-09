# Config reference

The config is a private Git repo. Every machine keeps a checkout of it in `~/.devcluster/config` and pulls it at the start of each run. [examples/config](../examples/config) is a complete working example.

```
cluster.toml          what every machine gets
machines/NAME.toml    one file per machine; the file name is the machine's name
files/                files that [[file]] items put in place
fixer.toml            optional: the agent that fixes stuck items, and how to reach you
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
| `timeout` | no | Seconds for a fetch or push, default 600 |
| `install_timeout` | no | Seconds for the install, and for the check, default 1800 |

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

A command-line tool pinned to one version.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Its name |
| `version` | yes | The version every machine should have |
| `check` | yes | A command whose output includes the installed version |
| `install` | yes | A command that installs it. `{version}` is replaced with the version. |
| `timeout` | no | Seconds for the check, default 60 |
| `install_timeout` | no | Seconds for the install, default 1200 |

The version must appear in the check's output as a whole word: `0.163.0` doesn't match `0.163.0-alpha.5`.

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

## machines/NAME.toml

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"dry-run"` | `"on"` to sync for real. In dry run, devcluster reports what it would change and changes nothing, apart from updating itself and its config checkout. |
| `os` | none | `"macos"` or `"linux"`, for your own reference |
| `description` | none | Your own note |
| `engine` | none | A devcluster version for this machine only, so a new version can be tried on one machine first |
| `skip` | none | Names of items in cluster.toml that don't apply here |

A machine file can also hold `[secrets]`, `[[repo]]`, `[[tool]]`, `[[cli]]`, `[[login]]` and `[[file]]` items. An item with the same name as a shared one replaces it on this machine. Any other item is added. `on` has no meaning in a machine file.

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
| any setting above | | Overrides that setting on this machine |

One agent runs on a machine at a time, and none runs on a machine in dry run or for an item in dry run. Each run's brief, the agent's output and its result are kept in `~/.devcluster/state/fixer/`, and the messages sent are in `~/.devcluster/state/fixer/messages/`.
