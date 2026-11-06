# Plan: each machine keeps itself in sync from one list

This is the plan for [issue #1](https://github.com/aelaguiz/devcluster/issues/1). It builds `devcluster sync`, which every machine runs on a timer to bring itself in line with a list kept in a private config repo, plus `devcluster status`, which shows what every machine did. It is tested end to end against a fake cluster (local Git remotes, fake home folders, fake tools) before it touches a real machine.

[The architecture](../architecture.md) describes where the whole project is going. This plan covers only the sync engine.

## What gets built

| Command | What it does |
|---|---|
| `devcluster install --machine NAME --config URL` | Sets up `~/.devcluster` on this machine, puts `devcluster` on the path and starts the timer: launchd on macOS, a systemd user timer on Linux. |
| `devcluster sync` | One run. The timer calls it every two minutes. `--dry-run` reports what it would change without changing anything. |
| `devcluster status` | Every machine's last run, read from GitHub. It needs no SSH, so it works from any machine and from inside an agent session. |
| `devcluster check-config DIR` | Checks a config checkout before you push it, and prints what each machine would get. |
| `devcluster uninstall` | Stops the timer and removes the command. It leaves `~/.devcluster` alone. |

## On each machine

```
~/.devcluster/
  engine/    a checkout of this repo at the version the config names
  config/    a checkout of the private config repo
  state/     the last run's results, what each item last installed, the log
  machine    this machine's name in the config
```

The timer runs `~/.devcluster/engine/bin/devcluster sync`. It never runs the checkout you edit, so a change goes live only after it is pushed and each machine pulls it.

## The config

The config is one private repo:

```
cluster.toml          what every machine gets
machines/NAME.toml    one file per machine: its mode and what's different there
files/                files the config puts in place, such as the global agent instructions
```

`cluster.toml` lists five kinds of item, run in this order:

| Kind | Example | What a run does |
|---|---|---|
| `[secrets]` | an encrypted secrets repo | Checks for local edits, pulls, decrypts, checks again |
| `[[repo]]` | a repo agents commit to directly on `main` | Pulls, merges and pushes both ways |
| `[[tool]]` | a skills repo with `install = "make install"` | Follows a branch, tag or commit and runs the install when it moves |
| `[[cli]]` | Codex at one version | Runs a check command; installs when the version differs |
| `[[file]]` | the global agent instructions | Writes the config's copy into place |

Any item can have `on = ["laptop", "server"]` to limit it to some machines. A machine file can add items, replace a shared item by giving one with the same name, or drop one with `skip = ["name"]`. A machine starts in dry run, and syncs for real only once its file says `mode = "on"`.

[docs/config.md](../config.md) is the full reference, and [examples/config](../../examples/config) is a working example.

## One run

1. **One run at a time.** A run takes a lock in `state/`. If the last run is still going, the new one exits.
2. **Pull the config.** It fast-forwards only, and never overwrites a local edit.
3. **Update devcluster itself** when the config names a different version (`[devcluster] engine = "<tag or commit>"`), then restart on the new version. This also happens in dry run, so a machine in dry run still tests new versions.
4. **Run each item that applies to this machine.** Each item compares, changes only what differs, and checks the result. One item failing doesn't stop the others.
5. **Record the result** in `state/last.json` and push it to the machine's own branch, `status/<machine>`, in the config repo. That branch belongs to the one machine, so it's the only branch devcluster ever force-pushes. A run that changed nothing pushes at most every five minutes, as a heartbeat.

Every item ends in one of six states:

| State | Meaning |
|---|---|
| OK | Already matches the list |
| Updated | devcluster changed it to match |
| Would change | Dry run: devcluster would change it |
| Needs you | devcluster can't change it safely, so a person has to look |
| Failed | A command failed; a later run retries it |
| Busy | Something else was using it; the next run retries it |

## How each kind works

### Repos worked on directly on `main`

On each run: fetch `main` from origin, then compare.

- **Same:** nothing to do.
- **Behind only:** fast-forward.
- **Ahead only:** push. If origin moved in the meantime, fetch and go round again, up to three times.
- **Both:** test the merge with `git merge-tree`, which doesn't touch the working tree. If it would conflict, stop and report the conflicted files. Nothing has changed at that point. If it's clean, make the merge commit, fast-forward to it, and push.

Safety comes from Git itself. A fast-forward refuses to overwrite uncommitted changes, so devcluster passes that refusal on as "Needs you" with the file names. devcluster also turns off the two ways a fast-forward can still replace work: it never overwrites an ignored file in the way, and it never autostashes, whatever the machine's Git config says. It checks the branch again right before it changes anything, in case an agent switched it during a long fetch. Uncommitted changes in other files stay where they are. devcluster also leaves a checkout alone when it's mid-merge or mid-rebase, when it's checked out on another branch, or when its origin isn't the one in the list. It never force-pushes a repo, and it never rebases, so commit IDs never change.

Testing a merge without touching the working tree needs Git 2.38 or newer.

### Tools

A tool follows `ref`: a branch (the default is `main`), a tag or a commit. When the checkout is behind on that branch, it fast-forwards. Going back, or to a pinned tag or commit, checks out that commit detached rather than moving a branch, so no commit can be dropped, not even one an agent makes during the run. A tool checkout with commits that aren't on origin is reported, not moved. A machine where you edit the tool lists it as a `[[repo]]` instead.

The `install` command runs when the commit changes, and once on the first run. When an install fails, devcluster records it and doesn't retry for an hour, unless the commit changes first.

### CLIs

Each CLI has a `version`, a `check` command and an `install` command, for example `npm install -g @openai/codex@{version}`. If the check's output doesn't contain the version as a whole word, devcluster runs the install and checks again. So `0.163.0` doesn't match `0.163.0-alpha.5`. Failed installs back off the same way as tools.

### Secrets

devcluster drives the secrets repo's own commands in the order that repo documents:

1. Run its `check`. If this machine has an edit nobody published, stop and report it.
2. Fast-forward.
3. Run `decrypt`, then `check` again.

If a decrypt fails, for example because the key couldn't be read, the next run retries the decrypt instead of mistaking the stale files for a local edit.

Its output is never recorded or pushed, only the exit code.

### Files

devcluster writes the config's copy of each file to its targets. It remembers what it wrote:

- **Target missing:** write it.
- **Target matches the config:** nothing to do.
- **Target unchanged since devcluster wrote it, and the config moved on:** update it.
- **Target edited on this machine since devcluster wrote it:** report it, don't overwrite it.
- **A different file is there the first time:** move it aside to `NAME.before-devcluster`, then write.

It copies rather than links. With a link, an edit on one machine would land inside `~/.devcluster/config` and stop that machine pulling any config change at all. A copy confines a local edit to that one file.

## Seeing it

`devcluster status` fetches every `status/*` branch and prints one row per machine: its last run, its mode and its item counts. Then it lists what needs a person, what failed and what would change. A machine that hasn't reported in 15 minutes shows as stale. `--json` prints the raw results.

## Safety

- The timer runs at background priority: launchd's `Background` process type and low-priority disk access on macOS, `Nice=10` and idle disk priority on Linux.
- Every command has a timeout. A command that times out gets its whole process group killed.
- Git never prompts (`GIT_TERMINAL_PROMPT=0`), so a missing key fails instead of hanging.
- Anything recorded has URL passwords and common token formats removed.

## Testing with a fake cluster

The tests build a fake cluster in a temporary folder, with no network and no real machines:

- **"GitHub"** is a set of bare Git repos on disk: the config repo, a repo worked on directly on `main`, a tool repo with an install script, and a secrets repo whose fake `bin/secrets` "encrypts" with base64.
- **Each "machine"** is its own home folder with its own `~/.devcluster`. It's installed with the real `devcluster install --no-schedule`, from an engine repo built out of this checkout, and runs the real command as a separate process.
- **The fake CLI** is a version stored in a file, so a test can make an install fail or succeed.

What the tests prove:

| Area | Cases |
|---|---|
| Repos | Fast-forward. Push. A clean merge both ways, where every machine ends up with every commit. A conflict, where nothing changes and no merge is left half-done. An uncommitted file in the way, an untracked one, and an ignored one. Uncommitted files out of the way. A Git config with autostash on. Mid-merge. A held Git lock. The wrong branch, and a switch to another branch during the fetch. A wrong origin, and a URL Git rewrites. A clone. A push race, where another machine pushes first. Dry run predicting each of these. |
| Tools | Update and install. No reinstall when nothing moved. A failed install backs off, then retries when the commit changes. Local commits block. A pinned commit. Origin's branch moved back, which detaches without moving the branch. |
| CLIs | A match, a mismatch, a version that only shares a prefix, a failed install backing off |
| Secrets | Pull and decrypt. A local edit blocks the pull. A failed decrypt is retried. No secret value ever appears in the results, the log or the status branch. |
| Files | Write, update, an edit kept, a first-time file moved aside |
| Config | Validation errors stop the run and are reported. `on`, `skip` and replacement work. A machine missing from the config is reported. |
| Engine | Updates itself to the version the config names, then restarts on it. A second run exits while one is going. The launchd and systemd files are correct. |
| Status | Three machines report. `status` from a fourth place shows them all, a stale one, and what needs a person. |
| Whole cluster | Three machines commit at the same time and sync until every machine has every commit, every tool is installed, and every file is in place. |

Tests run with `python3 -m unittest` and need nothing beyond Python 3.11 and Git 2.38. GitHub Actions runs them on macOS and Ubuntu.

## Trying it on real machines

1. Create the private config repo with the real machines. List only the repos worked on directly on `main`, and leave every machine in dry run.
2. Install on two machines, the laptop and the server, and read `devcluster status`. Dry run already shows, for each repo, whether a merge with origin would be clean or which files would conflict.
3. Merge any repo that has split from origin by hand, while someone watches.
4. Turn on repo sync on those two machines, then the rest. Retire any push-only autopush job.
5. Add tools, then CLIs, secrets and files, one kind at a time, each starting in dry run.

## Left for later issues

Issue #1 delivers the engine, and the first version of each kind. Each later issue finishes its own part:

- [#2](https://github.com/aelaguiz/devcluster/issues/2): proving five-minute convergence across real machines.
- [#3](https://github.com/aelaguiz/devcluster/issues/3): reporting installed skill copies that were edited by hand, and keeping the previous version when an install fails partway.
- [#4](https://github.com/aelaguiz/devcluster/issues/4): saying which tool needs which credential.
- [#5](https://github.com/aelaguiz/devcluster/issues/5): fetching approved Claude Code builds from storage.
- [#6](https://github.com/aelaguiz/devcluster/issues/6): moving the rules into the config.
