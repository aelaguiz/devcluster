# Architecture

devcluster is for one person, or a small team, running coding agents across a handful of machines. It should feel like one computer. Work is current everywhere, every agent follows the same rules, heavy work lands where there's room, phones are taken in turn, and disks don't fill.

This document is the direction for the whole project. The first piece, the sync engine, is planned in [plans/sync-engine.md](plans/sync-engine.md) and built, and so are skills and tools ([plans/tools.md](plans/tools.md)). Everything else here is design, and each part names the issue that will build it.

## The picture

```
                       GitHub (or any Git host)
   ┌──────────────────────────────────────────────────────────────┐
   │ devcluster (public)   config repo (private)   your repos     │
   │   the engine            main: the list          worked on    │
   │                         status/<machine>        on main      │
   └──────▲──────────────────────▲─────────▲───────────▲──────────┘
          │ pulls its version    │ pulls   │ pushes    │ pull + push
          │                      │ main    │ its report│
   ┌──────┴──────────────────────┴─────────┴───────────┴──────────┐
   │ each machine: ~/.devcluster                                  │
   │   engine/  config/  state/  machine                          │
   │   a timer runs `devcluster sync` every two minutes           │
   └──────────────────────────────────────────────────────────────┘
          ▲
          │ devcluster status / run / hold / clean
   agents and you, on any machine
```

There is no server, and no daemon that must stay up. Each machine pulls what it should be and pushes what it is. Git hosting is the only shared infrastructure.

## What it rests on

These are the ideas each part is built from. A design for a new part should be explainable from them.

**Each machine converges itself.** A machine reads the desired state and moves toward it on a timer. Nothing pushes changes into a machine. So a machine that was asleep catches up when it wakes, a change everywhere is one commit, and nothing needs to reach into a machine over SSH to keep it right.

**One place for each fact.** Which repos a machine keeps, which version a tool runs, what a machine is for, which phone is whose: each is written once, in the config repo. The engine copies a fact into place. It never keeps a second version that could drift.

**The engine is public and the config is private.** The engine knows nothing about any one person. Hostnames, accounts, repo names, secret paths and device logins live only in the config repo. Anyone can use the engine with their own config.

**Never lose work.** devcluster treats uncommitted changes, commits that exist only on one machine, and files someone edited by hand as the most valuable things on a disk. It never force-pushes a shared branch, never rebases, and never overwrites an edit. When it can't move something without risk, it leaves it alone and says so.

**Say what happened, in words.** Every run ends each item in one of six states (OK, Updated, Would change, Needs you, Failed, Busy) with a sentence a person can act on. A machine's report is pushed where any machine can read it, so the state of the cluster is never a guess.

**Try before trusting.** A new machine starts in dry run, reporting what it would change. A new devcluster version can run on one machine first. Each new kind of item is switched on in dry run before it's switched on for real.

**Quiet on the machine you work from.** devcluster runs at background priority, does nothing when nothing changed, and is built so heavy work leaves the laptop instead of landing on it.

## The pieces

### The config repo

One private repo holds everything about one cluster:

```
cluster.toml          what every machine gets: repos, tools, CLIs, secrets, files
machines/NAME.toml    one file per machine: its mode, its role, what's different there
devices.toml          phones and tablets: where each is plugged in, its own logins as secret paths  (#8)
files/                files put in place on every machine, such as agent instructions          (#6, #10)
```

Over time, each machine's file grows to describe the machine as well as what it syncs: what kind of work it takes, what it never takes, how much of it agents may use, and its free-space target. Placement and cleanup read those fields (see below).

The config is checked by `devcluster check-config` before it's pushed. A config with errors stops runs on every machine and is reported, so a typo can't half-apply.

### The engine on each machine

`~/.devcluster` holds the engine (a checkout of this repo at a pinned version), a checkout of the config, and the machine's state. A timer runs `devcluster sync`: launchd on macOS, a systemd user timer on Linux. The engine updates itself to the version the config names, so upgrading every machine is one edit to the config.

### Items

Everything sync keeps in step is an item of some kind. Every kind follows the same contract:

1. Read the current state.
2. Change only what differs from the list.
3. Check the result.
4. Report one of the six states, with a sentence.

An item can't block or break another item. The seven kinds today are secrets, repos, tools, CLIs, logins, files and settings copied from one machine. A new kind (a Homebrew bundle, an MCP server registration, a launchd service) is one more handler with the same contract. An engine refuses a config with a kind it doesn't know, and then can't update itself, so every machine moves to the engine that has a new kind before the config uses it.

### Reports

After each run a machine writes `state/last.json` and pushes it to `status/<machine>` in the config repo. That branch has one writer, so replacing it is safe, and it's the only branch devcluster ever force-pushes. `devcluster status` fetches every machine's branch, so it works from any machine, including from inside an agent session that can't open SSH connections.

Reports grow as the parts below arrive: free space, load and memory for placement, cleanup results, and which devices are plugged in.

## Where the project goes

### Sync (#1 to #6)

| Issue | What it adds on top of the engine |
|---|---|
| [#2](https://github.com/aelaguiz/devcluster/issues/2) Repos on main | Proof on real machines that every commit reaches every awake machine within five minutes, and a quick way to see and resolve a conflict |
| [#3](https://github.com/aelaguiz/devcluster/issues/3) Skills and tools | A check after each install, keeping installed copies that were edited by hand, and going back to the last good version when an install fails ([plan](plans/tools.md)) |
| [#4](https://github.com/aelaguiz/devcluster/issues/4) Credentials | Saying which tool needs which credential, so a missing one is named in the report. Some sign-ins can't be copied (a keychain, a browser profile), so those are reported as missing, never copied |
| [#5](https://github.com/aelaguiz/devcluster/issues/5) CLI versions | Fetching an approved build from storage by its hash when the CLI isn't installed from a package manager |
| [#20](https://github.com/aelaguiz/devcluster/issues/20) Agent settings | Codex and Claude Code settings, and Codex's version, copied from the machine its owner works on ([plan](plans/settings.md)) |
| [#6](https://github.com/aelaguiz/devcluster/issues/6) Rules files | The global agent instructions and the where-work-goes guide moved into `files/`, with every other copy retired |

### Putting work where there's room (#7)

Each machine's file says what it's for, in plain words, plus what it takes and what it never takes:

```toml
role = "Home server. AI training; overflow goes to the cloud."
takes = ["build", "test", "training", "solver"]
never = ["inference"]
keep_free = { cores = 4, memory_gb = 16 }
```

`devcluster run -- <command>` picks a machine and runs the command there with the current folder synced over. To pick, it:

- Keeps the machines whose role takes this kind of work.
- Drops any machine above what agents may use.
- Takes the one with the most room.

Room is read live from the candidates, not from a report a few minutes old. Each machine holds a small number of run slots, as leases (below), so a burst of agents doesn't all pick the same "emptiest" machine. `--on NAME` names a machine. The laptop is a candidate only for light work.

The rules agents read are generated from the same machine files. The roles are written once, and the guide agents read can't disagree with what `run` does.

### Device claims (#8)

A phone is plugged into one machine, so that machine is the authority for its claims. A claim is a lease on that machine: who holds it, from which session, until when. The holding session renews it while it works. A lease that isn't renewed expires, so a crashed session can't hold a phone forever.

- `devcluster hold DEVICE -- <command>` takes the lease, runs the command, and releases it.
- `devcluster hold DEVICE --owner` marks a device as the owner's. No agent can claim it until the owner releases it.
- Every way agents reach a device goes through the claim: the test runners, the device automation servers, and the build scripts. A runner that finds no lease refuses.

`devices.toml` lists each device: where it's plugged in, its kind and OS, whether it's ever for testing, and its own logins as paths into the secrets repo.

### Cleanup that frees space (#9)

One cleanup for every machine, started by devcluster each night, following one skill and one keep list ([#28](https://github.com/aelaguiz/devcluster/issues/28), [the cleanup plan](plans/cleanup.md)).

Almost everything on these disks is left over from tasks whose agents are gone. They aren't coming back for their folders, and nothing will ever prove a folder is dead, so a cleanup that waits for proof keeps everything. The cleanup looks for life instead, which can be seen: something runs from the folder, something points at it, someone worked in it in the last couple of days on an unfinished task, or someone wrote down that it's needed. Everything else is finished. A dead checkout's unsaved work goes to a salvage branch first, and a finished experiment keeps its readings and loses its bulk.

Some things are needed and show no sign of it, such as results someone is reading. Whoever needs one puts it on the keep list, `keep.toml` in the config, with who, why and until when, from any machine with `devcluster keep add`. A wrong deletion is answered by making that easier, never by a new test the cleanup has to pass before it deletes.

- **One skill**, [skills/disk-cleanup](../skills/disk-cleanup/SKILL.md), carries the judgment and names no machine. What is special about a machine (its drives, what serves from where) goes in its notes in the config.
- **One run**, `devcluster cleanup`, from a nightly item. It writes the agent's brief, measures free space itself before and after, checks the agent's summary, and reports in `devcluster status`.
- **One way to delete**, `devcluster cleanup rm`, which refuses in code what is a fact rather than a judgment: kept folders and what holds them, mounts, home folders, links, and anything outside the places task folders live.

### One set of rules, built as intuitions (#10)

Each topic agents need guidance on has one source in `files/`: where work runs, how to use devices, what cleanup may delete, how sync behaves. Each is written as an intuition an agent can reason from, not a list of rules that collide. The machine roles and device list feed the generated parts, so the words and the behavior agree. Old memories and rules that contradict a source are found and retired.

### Replacing the machines skill (#11)

The private `machines` skill this project comes from maps onto devcluster commands:

| Today | devcluster |
|---|---|
| `machines status` | `devcluster status`, with live load and devices |
| `machines list` | `devcluster machines`, `devcluster devices` |
| `machines hold` / `release` | `devcluster hold` / `release` |
| `machines run` | `devcluster run` |
| The deploy script that copies the skill to every machine | Gone. The skill is a file in the config, synced like any other |

One skill teaches agents these commands, installed into every agent's skill folder by sync itself.

## Reaching other machines

Sync never connects to another machine. `run`, `hold` and live load do. They use SSH by default, with hostnames from the machine files. Some agent sandboxes can't open new SSH connections. For those, a small helper started by the same timer system keeps the connections open, and agents talk to it through a local socket. That helper is the one long-running process devcluster would add, and it's only needed for the commands that reach other machines.

## Failure

- **A machine is off or offline:** it misses runs and catches up later. Its status shows as stale after 15 minutes.
- **The Git host is down:** every machine keeps what it has. Runs report the fetch failure and try again.
- **A bad devcluster version:** pin the last good version in the config. Machines that can still run pick it up. A version that can't start can't fix itself, so new versions go to one machine first with that machine's own `engine` setting.
- **A bad config push:** every machine stops and reports the error. Nothing half-applies.
- **Two machines commit at once:** sync merges, and the next run pushes the merge. A real conflict is reported with its files, and nothing changes until a person resolves it.

## Open questions

- **The shared store for leases.** Run slots and device claims need a store every machine can reach. The device's own machine is the natural authority for device claims. Run slots might live on each target machine the same way, which would avoid shared infrastructure.
- **How heavy work is identified.** Should `run` work out the kind of work from the command, or should callers say it?
- **When a cloud machine is worth starting.** Overflow to rented machines needs a cost limit and a rule for when a person approves the spend.
- **Teams.** Several people sharing one cluster need per-person mode and ownership of devices, without changing the one-list model.
