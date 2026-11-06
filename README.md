# devcluster

Keep a small cluster of development machines in sync, and put work on the machine that has room for it.

It is built for one person running coding agents (Claude Code, Codex and others) across several Macs and a Linux server, where agents commit straight to `main`, share skills and credentials, and take turns with the same test phones.

## What it will do

- Keep the repos you work in on `main` current on every machine.
- Keep skills and agent tools at the same commit on every machine.
- Put the credentials those tools need on every machine, from an encrypted secrets repo.
- Run the same Codex and Claude Code versions everywhere.
- Give agents one set of rules for which machine takes which work.
- Let agents claim a phone or tablet before using it.
- Keep disks from filling up.

## How it works

Each machine keeps itself in sync. A small job on every machine reads one list kept in a private Git repo and brings that machine in line every few minutes. To change something on every machine, push a change to the list. A machine that was asleep catches up when it wakes.

The engine is public. Your machines, accounts, repos and secret paths stay in your own private config repo.

## Status

Design stage. The work is planned in [issues](https://github.com/aelaguiz/devcluster/issues).

## License

MIT
