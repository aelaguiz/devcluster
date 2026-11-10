# AGENTS.md

This file is a few intuitions about working on devcluster and the machines it keeps; a new lesson deepens the one it belongs to.

This repository is public. It holds the engine and docs anyone with a few machines could use. Everything about one particular fleet, such as its machine names, accounts, hosts, addresses and where its credentials live, goes in that fleet's private config repo, checked out at `~/.devcluster/config` on every machine.

devcluster copies software, settings and encrypted secrets between machines. It never copies a machine's identity. Logins, keychains, browser cookies and the network a machine reaches the internet from belong to that machine, and each machine earns its own through the path built for it. Copying one across, or making one machine look like another, is the thing the fleet's protections exist to prevent: providers ban accounts that look shared or automated, and a credential that crosses machines can leak from any of them.

A protection that stops your task is telling you something. A sign-in limit, a per-machine login, an approved runtime, a provider's security check, a permission deny, a sync item that refuses: each one fires on a real state of the fleet. When it fires for the wrong reason, such as a bug in the check, fixing that reason is ordinary work, and you say what you fixed. When it fires for the right reason, making it quiet is the owner's decision. That covers raising a limit, retrying a frozen account, routing around a check, copying what should be earned, or turning an item off. Bring the owner what fired, why, and the options; don't apply one.

A machine is in sync when it converges on its own. A repair that has to happen again is drift, even when the latest status looks clean: the same fixer run, a setting rewritten every sync, a machine that catches up only when an agent steps in. The fix changes the cause, such as engine behaviour, a merge rule, a second program writing the same file, or a missing `own` entry. Another repair doesn't fix it.

Before any work that touches logins, accounts, browsers, secrets or AI Manager on a machine, read `~/.devcluster/config/AGENTS.md`. It says which documents explain each of this fleet's protections.

## Facts

- Tests: `python -m unittest -v`. CI runs the same on Ubuntu and macOS.
- Check a config change before pushing it: `devcluster check-config ~/.devcluster/config`.
- `docs/config.md` is the reference for every item type; `docs/architecture.md` says where the engine is going.
- The fixer agent works from `skills/devcluster-fixer/SKILL.md`; the nightly disk cleanup works from `skills/disk-cleanup/SKILL.md`.
