---
name: devcluster-fixer
description: Fix a devcluster item that is stuck on this machine, whose status says "Needs you" or failed. Typical causes are files in the way of a pull, a merge conflict, a checkout on another branch, or a fetch or push that keeps failing. Work out whose work is in the way and whether it is still live. Bring real work onto the branch, set stale copies aside (the engine has already saved everything to a branch), finish the update, and record what was done, all without asking the cluster's owner. Use when the devcluster engine starts you with a fixer brief, or when someone asks you to fix a devcluster item or machine that needs attention. Reach the owner only when the fix needs their hands. Not for changing what devcluster syncs or how its engine works, which is work on the devcluster repo itself.
---

# Fixing a stuck devcluster item

devcluster keeps a set of machines in step with one list: the same repos on their branches, the same tools, the same files. Its engine runs every two minutes as plain code. It stops whenever going on could lose work, because it can't tell a month-old scratch edit from work someone needs. When it stops, the item is stuck, and you were started to unstick it.

The cluster's owner set this up so the machines look after themselves. A stuck item that waits for them is the failure this skill exists to prevent. Almost every stuck item is something an engineer at this machine would sort out in a few minutes by reading the diff. Do that, decide, and finish. Your work is done when the engine's next run updates the item, or finds it already up to date.

## What is already true when you start

**Everything local is saved.** Before starting you, the engine wrote one commit holding the whole checkout and put it on the branch the brief names, `devcluster/saved/<machine>/<item>/<time>`. That commit holds:

- the local commits, as its ancestors;
- what was staged, as its second parent, when that differs from the working tree;
- the uncommitted edits;
- the untracked files;
- any ignored files Git named as in the way.

Other files Git ignores, such as build output or a local `.env`, are not in it, so leave them where they are. In the secrets checkout the brief names a private folder holding a copy of the decrypted files instead, which must never go on a branch.

If the brief says the branch reached origin, nothing you change here can lose anything, even if this disk dies. If it stayed local, because origin refused something that large, it exists only on this disk. Then don't delete anything you could leave.

So you don't need to keep every local file in place. What you owe is that nothing worth keeping ends up only on the saved branch without anyone knowing. Say in your result what you set aside.

**The engine leaves this item alone while you work** and runs again after you exit. Don't run the engine yourself.

**The brief has what the engine knows**: the item, the checkout's path and branch, the stuck summary and Git's own output, when it got stuck, the saved branch, any process whose working folder is inside the checkout, when the newest local change was made, and where to write your result. Read it before anything else.

## Whose work is in the way, and is it still live?

What blocks an update is nearly always somebody's work: a person's, an agent session's, or what a sync left when it stopped partway. Before anything else, work out which of three things it is.

**Someone is working there now.** A process has its working folder in the checkout, files changed in the last hour, a session log is still growing. The edits are mid-flight. Committing them, or swapping origin's files in under a running session, breaks its work. Leave the checkout as it is and write `waiting`; the engine tries again later. A session that has plainly ended is not live, whatever state it left: its process is gone and nothing has changed for days.

**A checkout many sessions work in at once.** PS Brain and PS Agent Space are shared this way: dozens of sessions have their working folder there all day, each with its own uncommitted edits and one index between them. So "someone is working there now" is always true and `waiting` alone never ends, and bringing origin in has to leave all of that alone. `git merge` can't. It takes about a minute there, sessions commit and edit during it, and when a merge fails partway Git resets every tracked file to HEAD, then tries to put the edits back and ignores it if that fails. In such a checkout never run `git merge`, `git pull`, `git rebase`, `git stash`, `git reset`, or a `git checkout` or `git restore` of anything beyond the one file you've decided on. Both are set to `merge.ff=only`, so a plain merge refuses before it touches anything; don't pass `--no-ff` to get past it.

Bring origin in the way the repository's own ten-minute job does, then write your result: `python3 ~/workspace/psbrain/scripts/sync_github.py ~/workspace/psbrain private` for PS Brain, or `python3 ~/workspace/psbrain/scripts/sync_github.py ~/workspace/psagentspace` for PS Agent Space. It builds the merge with `merge-tree` and `commit-tree`, touches only the files origin changed, carries a session's edit into those it can, and moves HEAD by compare-and-swap, so a commit landing meanwhile restarts it instead of being lost.

- **Like this:** an uncommitted file blocks the pull. The script merges origin and pushes, the session's edit stays where it was, and the engine's next run comes back clean.
- **Not like that:** on November 9, 2026, three fixer runs ran `git merge origin/main` in PS Brain on the M5. Each failed partway and put sessions' uncommitted files back to their last commit, 53 at 9:13 am and 31 at 10:49 am across every pod, and one worker's corrected brief went back to a phone another team held.
- **A checkout this isn't:** one with a single session in it, or none. The three kinds below apply there, and a plain merge is fine once nobody is working in it.

**Real work that never reached origin.** Someone meant to keep it and never committed or pushed it: a fix to a script, a doc improved during a session, notes, a log with new entries. If the item's branch is one people work on directly, that branch is where the work belongs. Bring it onto origin's latest version, merging by hand where origin changed the same file. Commit it with a message that says what it is and that it came from this machine, and push. A push reaches everyone who can read origin, and when the brief says origin is public that is anyone: a script fix or a doc written for the repo goes up, but working notes, plans and anything about people, accounts or money were never meant for it, and stay on this machine.

**Stale or superseded.** An old draft of something origin has since finished, a file origin now has in a newer form, or a copy someone made to get unstuck. Origin's version wins; the saved branch already holds the old one.

To tell real from superseded, compare what the local edit does with what origin did to the same file since this checkout's base:

```sh
git diff <base> -- <file>                        # what this machine changed
git log -p <base>..origin/<branch> -- <file>     # what origin did since
```

Here `<base>` is `git merge-base HEAD origin/<branch>`. If origin's history holds the same change, or a different one that does what the local edit was after, the local edit is superseded. If the local edit adds something origin doesn't have, it is real. Often a file is both: the local copy repeats some of what origin did and adds one new thing. Carry over the new thing.

Some edits only make sense on this machine: a local path, a hook switched off because it fails here, a port, a credential location. They are real to the machine, but they don't belong on a shared branch, because committing one changes every machine. Look for the repo's way to keep machine-only settings out of Git, such as an ignored local override file. If there is one, move the edit there. If there isn't, origin's version wins, and your result says what this machine lost so it can be put back properly.

A few cases that look alike and come out differently:

- **A log or notes file both sides added to.** The answer is both sets of entries, in order. Neither "take origin's" nor "take mine" is right.
- **A script edited here weeks ago, while origin rewrote the same part since.** If origin's version does what the local edit did, take origin's and set the local edit aside. If origin's version fixed something else and the local fix is still missing, apply the local fix on top of origin's version.
- **An edit that switches something off.** If it was switched off because it breaks on this machine, it belongs in a local override. If it was switched off because it was being replaced, and origin has the replacement, it is superseded.
- **A whole file origin also added, with different contents.** Two machines wrote the same thing independently. Read both: usually one is a later version of the other, and occasionally both are needed under different names.

Fix only what is in the way. Untracked files that don't block the update stay where they are, and so does an edit to a file origin didn't touch. Cleaning up the checkout is not the job. Look one step past the update, though, because what the update puts in the way counts too. In a sparse checkout, an untracked file outside the sparse folders becomes an edited tracked file the moment origin's version of it arrives, and then it blocks the next pull instead of this one.

## Other ways an item gets stuck

A merge conflict, a checkout on another branch, a half-finished merge or rebase, a fetch or push that keeps failing, a tool whose install fails, the secrets checkout, a managed file edited by hand. [references/situations.md](references/situations.md) says what each one usually means and how it usually goes. Read the part that matches the brief's summary.

## Finishing

Once the way is clear, do the update yourself: fast-forward or merge origin's branch, and push what you committed. The engine's next run should find nothing to do. Then check what the engine will check:

- the checkout is on the item's branch;
- that branch contains origin's tip;
- origin has everything you committed;
- `git status` shows nothing that would block a fast-forward.

Write the result where the brief says, as JSON:

```json
{"outcome": "fixed", "summary": "...", "details": "..."}
```

- **outcome**: `fixed`; `waiting` (someone is working there; try later); `needs-owner` (only their hands can do it; add `"message"` with what they need to do and where); or `could-not-fix`.
- **summary**: one sentence for the status line, in plain words about the work. For example: "merged this machine's two log entries and its script fix into origin's version and pushed; set aside an older draft of the setup notes".
- **details**: each file or commit you decided about, what you did with it and why, and the saved branch.

## When to reach the owner

Only when the fix needs their hands:

- a login only they can do, such as a passkey or a phone approval;
- something physical, such as a machine that is off or a device unplugged;
- a permission you can't get, such as sudo they haven't granted.

Everything else you decide. A judgment call is yours to make, because the saved branch makes it reversible. A file you're unsure about gets a decision and a line in your details. Asking them to choose between two versions hands back exactly the work this exists to take off them.

## Lines that hold in every case

- Never force-push, and never rewrite history that origin already has. Unpushed local commits are yours to rewrite when that is the fix.
- Never delete, move or rewrite a `devcluster/saved/` branch.
- Work in the stuck checkout. The config repo is fair game only when the config itself is the problem, such as a time limit too short for this machine.
- Never print, log or commit a secret value.
- Never turn sync off for the machine or the item, or narrow the config, to stop an item showing as stuck.

## A machine that stopped reporting

Sometimes the engine starts you on a neighbouring machine because another one has gone quiet. Log in over SSH and find out why it stopped: memory, disk, a stopped timer, or the engine failing on every run. Fix the cause the way that machine's owner would. Restarting a system service that is leaking memory, stopping a runaway process nobody is waiting on, and freeing disk the way the cleanup tooling does are all fair. An agent session or a test run someone is in the middle of is not yours to stop. If SSH can't connect at all, the machine is off, asleep or unreachable, and that is `needs-owner`.
