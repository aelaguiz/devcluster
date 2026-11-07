# Config reference

The config is a private Git repo. Every machine keeps a checkout of it in `~/.devcluster/config` and pulls it at the start of each run. [examples/config](../examples/config) is a complete working example.

```
cluster.toml          what every machine gets
machines/NAME.toml    one file per machine; the file name is the machine's name
files/                files that [[file]] items put in place
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

A Git repo of encrypted secrets, with its own commands to check and decrypt them. There's at most one.

| Field | Required | Meaning |
|---|---|---|
| `path` | yes | Where it's checked out |
| `url` | yes | Its origin |
| `check` | yes | A command, run in the checkout, that succeeds when the decrypted files match the encrypted ones |
| `decrypt` | yes | A command, run in the checkout, that writes the decrypted files |
| `branch` | no | Defaults to `main` |
| `timeout` | no | Seconds for each command, default 600 |

A run checks first. If the check fails, this machine may have an edit nobody published, so the run stops there. Otherwise it fast-forwards, decrypts and checks again. If the decrypt fails, the next run retries it, unless a decrypted file has changed since. Neither command's output is recorded.

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

A machine file can also hold `[secrets]`, `[[repo]]`, `[[tool]]`, `[[cli]]` and `[[file]]` items. An item with the same name as a shared one replaces it on this machine. Any other item is added. `on` has no meaning in a machine file.
