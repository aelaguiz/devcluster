# Plan: agent settings from the machine you work on

[Issue #20](https://github.com/aelaguiz/devcluster/issues/20). Every machine runs Codex and Claude Code with the settings, and the Codex version, of the one machine its owner works on.

## What's wrong today

In the setup this project comes from, the agent instructions and skills were the same on every machine, and nothing else about the agents was. On November 7 an agent had between 1 and 15 MCP servers in Claude Code and between 3 and 25 in Codex, depending on which of six machines it ran on. Five Codex versions were installed. One machine had been set up as a copy of the main one and still matched it; the rest had drifted.

## The rule

One machine is the source. Its files are the truth, every other machine's files are replaced with them, and nothing the other machines had is worth keeping, except state each machine writes for itself. To change a setting everywhere, change it on the source.

## What is copied

| File | What the other machines get | What each machine keeps |
|---|---|---|
| `~/.codex/config.toml` | Everything: model, thinking level, approvals, MCP servers, hooks, plugins, profiles | `projects`, the folders Codex has trusted on that machine, and `hooks.state`, the hooks it has approved there |
| `~/.claude/settings.json` | Everything | Nothing |
| `~/.claude.json` | `mcpServers` | Everything else: the sign-in, each project's history, counters |
| A file the settings run, such as a Codex hook script | The file | Nothing |
| Codex itself | The source's version | Its own way of installing it |

Kept entries merge: an entry the source has and the machine doesn't is added; an entry both have stays as the machine has it. A Codex hook approval is a hash of the hook as written on that machine, so the source's approval is wrong wherever the home folder differs. Keeping the machine's own approval means an approval made there isn't overwritten at the next run.

## How it works

- **A new kind, `[[settings]]`**, names the file, `from` (the source machine), and `own` or `only`. [Config reference](../config.md#settings).
- **On the source**, each run reads the file and, if it parses, saves a copy in `follow_folder`, a folder inside the encrypted secrets repo, which already travels to every machine every two minutes. These files hold tokens, so they never sit in the config repo. A file that doesn't parse isn't saved, so the others keep the last good copy.
- **Home paths** are written as `{{home}}` in the copy and turned into each machine's own home folder, so a machine with a different user name, or Linux's `/home`, gets working paths.
- **On every other machine**, each run builds the file it should have (the copy, plus its own entries) and replaces the file when it differs. For TOML, the source's text is spliced, so comments and layout survive, and the result is parsed and compared with what the merge should hold before anything is written. A file that is a link, because an agent launcher owns the real one, is written through the link. The first replacement keeps the old file in `~/.devcluster/state/settings/NAME.before`.
- **A CLI with `from`** installs the source's version: the source saves the version its check shows, and the others install it with their own `install` command, since each machine installed Codex its own way.
- **Timing**: a change on the source is saved at its next run and sent at the one after; each other machine applies it at its next run after that. About six minutes, ten at most.

## What isn't copied

- **Claude Code's version.** The approved build comes from the agent launcher's reviewed runtime, which needs administrator rights to install (#5).
- **The agent launcher's own config.** It says which scheduled jobs run on which machine; copying it would run the source's jobs everywhere.
- **Other files in the CLIs' folders**, such as alternate model configs used by scripts. They can be added as items when one matters.

## Risks

- **MCP servers that need a program or service a machine doesn't have** fail to start there. Both CLIs carry on without them. The rollout times a Codex session on each machine to make sure none waits on a server that can't start.
- **Moving Codex back to an older version** on machines that ran a newer one. The small-scale test does exactly this and then starts Codex.

## Test plan

1. **Fake cluster**: tests for saving and following, kept entries, home paths, links, the first-replacement copy, dry run, a broken source file, a broken machine file, a dropped key, the CLI version, and the config checks. Each core rule was broken on purpose once to see its test fail.
2. **Real files, nothing written**: merge the source's Codex config with each machine's real one and check the result parses and holds the source's settings plus that machine's trusted folders.
3. **Two machines**: the source and one other, first in dry run, then for real. Start Codex and Claude Code there and confirm they run, have the source's MCP servers, and a change made on the source arrives.
4. **Everyone**: every machine moves to the new engine first, because an older one stops on a config with a kind it doesn't know. Then the items go to all machines in dry run, then for real, and each machine's Codex start is timed.
