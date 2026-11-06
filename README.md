# devcluster

Keep a small cluster of development machines in sync, and put work on the machine that has room for it.

It is built for one person running coding agents (Claude Code, Codex and others) across several Macs and a Linux server, where agents commit straight to `main`, share skills and credentials, and take turns with the same test phones.

## What it does now

Each machine keeps itself in sync. A timer on every machine runs `devcluster sync` every two minutes. Each run reads one list in a private config repo and brings that machine in line with it:

- **Repos worked on directly on `main`:** pulled, merged and pushed, both ways, without ever force-pushing or overwriting uncommitted work.
- **Tools and skills:** kept at the branch, tag or commit the list names, and installed when that moves.
- **Command-line tools:** kept at one version.
- **An encrypted secrets repo:** pulled and decrypted with its own commands.
- **Files:** for example, the instructions every agent reads.

Each machine pushes a report of its run, so `devcluster status` on any machine shows what every machine did, what failed, and what needs a person. To change something everywhere, push a change to the list.

## What's coming

Putting heavy work on the machine with room for it, device claims that hold, cleanup that keeps disks from filling, and one consistent set of agent rules. [The architecture](docs/architecture.md) sets out the direction, and the [issues](https://github.com/aelaguiz/devcluster/issues) track the work.

## Set up a machine

You need Git 2.38 or newer and Python 3.11 or newer. There are no other dependencies.

1. Make a private config repo. Start from [examples/config](examples/config), and see the [config reference](docs/config.md).
2. On each machine:

   ```sh
   git clone https://github.com/aelaguiz/devcluster.git ~/.devcluster/engine
   ~/.devcluster/engine/bin/devcluster install --machine laptop --config git@github.com:you/devcluster-config.git
   ```

That puts `devcluster` in `~/.local/bin` and starts the timer: launchd on macOS, a systemd user timer on Linux. A machine starts in dry run, reporting what it would change, until its file in the config says `mode = "on"`.

## Commands

| Command | What it does |
|---|---|
| `devcluster sync` | One run. `--dry-run` reports without changing anything. `--only NAME` runs one item. |
| `devcluster status` | Every machine's last run. `--all` lists every item, and `--json` prints the raw reports. |
| `devcluster check-config [DIR]` | Checks a config checkout and shows what each machine gets |
| `devcluster install` | Sets the machine up |
| `devcluster uninstall` | Stops the timer and removes the command |

## How it was planned

- [The architecture](docs/architecture.md): where the whole project is going
- [The sync engine plan](docs/plans/sync-engine.md): how sync works and how it's tested

## Tests

```sh
python3 -m unittest
```

The tests build a fake cluster in a temporary folder: bare Git repos stand in for GitHub, and each fake machine has its own home folder. They need no network and touch nothing outside that folder.

## License

MIT
