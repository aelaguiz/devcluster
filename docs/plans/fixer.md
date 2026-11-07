# Plan: an agent fixes what the engine can't settle

The sync engine is plain code, and it stops whenever going on could lose work. A file in the way that differs from origin, a merge conflict, a checkout on the wrong branch, a fetch that keeps timing out: each one turns into "Needs you", and the item stays stuck until a person looks. Almost none of these need the cluster's owner. They need someone who can read the diff, see whose work it is, and do what an engineer at that machine would do.

This plan adds that someone. When an item stays stuck, the engine saves everything local in that checkout to a branch, then starts an agent on the same machine with a brief and the [fixer skill](../../skills/devcluster-fixer/SKILL.md). The agent fixes the item and the engine's next run confirms it. The owner hears about it only when the fix needs their hands. It belongs with [issue #13](https://github.com/aelaguiz/devcluster/issues/13), fleet health.

## What stays the same

The engine stays plain code. It still runs every two minutes, handles everything routine, and stops rather than risk work. On a healthy day no agent runs and nothing is spent. Everything here happens only on the path where an item is already stuck.

## How it works

1. **The engine notices an item that stays stuck.** It already remembers state between runs in `state/items.json`. Each item gets the time it first got stuck and a fingerprint of how (its state, summary and detail). An item that is still blocked or failed with the same fingerprint on the next run, about two minutes later, is handed to the fixer. A held Git lock (`busy`), an install in its backoff wait, and machines in dry run are never handed over.

2. **The engine saves everything first.** Before any agent touches the checkout, plain code writes one commit holding all of it: local commits (they are the commit's ancestors), uncommitted edits, untracked files, and any ignored file Git named as in the way. It builds that commit from a copy of the index, so the working tree, the real index and the branch are untouched; copying the index keeps a sparse checkout's skip flags, so files outside the sparse folders are not recorded as deleted. It pushes the commit to `devcluster/saved/<machine>/<item>/<YYYYMMDD-HHMM>` on origin. When origin refuses it (a file over GitHub's 100 MB limit, a push over 2 GB), the branch stays local and the brief says so. A repo the engine hasn't cloned yet has nothing to save.

3. **The engine starts the fixer.** It writes the brief to a folder of its own under `state/fixer/`, and runs the command the config names, with the brief on standard input, in the background so the timer keeps running. On Linux the timer's service stops everything it started when the run ends, so there the fixer gets a systemd unit of its own; on macOS it gets its own session. While the fixer works, the engine leaves that item alone and reports it as being fixed. One fixer runs on a machine at a time, with a time limit of 30 minutes by default. The command is the private config's choice, so the public engine names no agent or account. It lives in `fixer.toml`, a file of its own, so a machine still on an engine from before the fixer ignores it instead of stopping on a section it doesn't know:

   ```toml
   command = "claude -p --model opus --effort medium --permission-mode bypassPermissions"
   notify = "..."   # how to reach the owner; gets one message on standard input
   timeout = 1800

   [machines.server]
   ssh = "you@server.local"   # how a neighbour reaches it
   ```

4. **The brief carries the case.** What the engine already knows, so the agent doesn't rediscover it: the machine and item, the repo's path, URL and branch, the stuck summary and Git's own output, when it got stuck, the saved branch and whether it reached origin, any process whose working folder is inside the checkout, when the newest local change was made, and where to write the result. It points the agent at the skill on disk, `~/.devcluster/engine/skills/devcluster-fixer/SKILL.md`.

5. **The agent fixes it and says what it did.** The skill teaches the judgment: whose work this is, whether it is still live, what the repo wants on its branch, and how to finish. It writes `result.json` where the brief says:

   ```json
   {"outcome": "fixed", "summary": "one sentence for the status line", "details": "what it kept, what it set aside, and where"}
   ```

   `outcome` is `fixed`, `waiting` (someone is still working there; try later), `needs-owner` (only their hands can do it; add `message`), or `could-not-fix`.

6. **The next run checks it.** The engine's next run is the test. If the item syncs, the status shows the agent's summary with the saved branch, so anyone can see what was done and get anything back. If it is stuck in a new way, the fixer gets one more try. If it is stuck the same way after a try, or after two tries, the engine sends the owner one message through `notify`, with the agent's details. `waiting` waits 30 minutes before the next try.

7. **Machines watch each other.** A machine that is out of memory, out of disk or off the network can't fix itself. Every engine run already fetches the config repo; it also fetches the other machines' status branches, which are small. When a machine that is `on` hasn't reported for 20 minutes, the first machine after it in the config's list whose own heartbeat is fresh starts the fixer in machine mode, over SSH. If SSH doesn't connect, the machine is off, asleep or unreachable, and that is the owner's: one message.

## What the owner gets

Nothing on a normal day. When the fixer clears something, the status line says what it did and where the saved copy is, for anyone who looks. A message goes out only when no agent can act: a login only the owner can do (a passkey, a phone approval), a machine that is off, missing permission such as sudo, or a fix that failed twice. Each problem sends one message, not one per run.

## What the agent may and may not do

Inside the stuck checkout it does what an engineer would: commit real work and push it, merge, resolve conflicts, take origin's copy of a stale file, switch an abandoned branch back, raise a time limit in the config when the limit is what's wrong, load history from a nearby machine. Because the saved branch holds everything that was there, a wrong call costs a restore, not lost work.

A few lines hold in every case. It never force-pushes or rewrites history that origin already has, never deletes or moves a saved branch, touches only the stuck checkout (and in machine mode only that machine's processes, services and disk), never prints a secret, and never turns sync off or narrows the config just to make a stuck item stop showing.

## Which agent

The config names it. The suggestion is Opus at medium thinking: the brief and the repo carry the evidence, the engine's next run checks the result, and Opus at medium works through an odd case and says what it assumed instead of stopping. Codex's Sol at medium is the stand-in when the Claude accounts are short. The [backtest](#backtest) measures what one fix costs.

## Building it

All of it is built and tested, in [src/devcluster/fixer.py](../../src/devcluster/fixer.py) and [tests/test_fixer.py](../../tests/test_fixer.py):

1. **The skill**, `skills/devcluster-fixer/`, backtested once on a real case (below).
2. **The save step**: a sparse checkout records no deletions, ignored files in the way are kept, local commits are the saved commit's ancestors, and a push origin refuses leaves the branch on the machine and says so.
3. **Stuck tracking and the hand-off**: the fingerprint, the brief, the background run and its time limit, reading `result.json`, the retry and message rules, and the status lines. The tests use a fake agent that edits the checkout the way an agent would.
4. **Machine mode**: each run reads the other machines' reports, picks the neighbour, checks SSH, and starts the fixer or sends one message. An unreachable machine is checked again every ten minutes, so one that wakes without reporting still gets an agent.

Each fix the agents keep making the same way is a candidate to move into the engine as plain code, the way identical copies were in [PR #15](https://github.com/aelaguiz/devcluster/pull/15).

## Backtest

The skill was run once, before any engine code, on a real stuck case from the setup this project comes from. A sparse checkout was three weeks and 2,228 commits behind. Its pull was blocked by eight edited files: six differed from origin, and two already matched it. The case was recreated away from the machine it happened on, with:

- the same base commit, edits and file times;
- origin's commit from that hour;
- a saved branch made the way step 2 will make it;
- pushes going to a local copy of origin, with network Git switched off.

The save step worked as planned. The copied index kept the sparse flags, and the saved commit held exactly the 18 local files and recorded no deletions.

The agent, Opus at medium thinking, took 3 minutes 19 seconds and cost $1.39. It:

- found that four of the differing files were older copies of what origin already had, and kept origin's versions of them and of the two that matched;
- found that a run log held ten entries origin never got, merged them into origin's version where they belonged, and pushed that as one commit touching only the log;
- noticed that ten untracked notes outside the sparse folders would become edited tracked files once origin's versions arrived, which would block the next pull. It checked they matched the saved commit, restored origin's versions, and let the sparse folders remove them;
- left the checkout on main, level with origin, with nothing in the way and the saved branch untouched. Every command it ran stayed inside the checkout.

That afternoon the same case had been handled by hand on the real machine. The checkout was moved to origin's main, the ten log entries were left only on the saved branch, and the ten notes stayed behind as edited files. The agent did better on both counts. The ten entries were then put back on origin from the agent's commit. The backtest added one thing to the skill: the update itself can put files in the way of the next pull.
