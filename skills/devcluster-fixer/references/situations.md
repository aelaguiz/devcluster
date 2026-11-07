# What each stuck message usually means

Each section starts with words from the status line the engine wrote. What follows is what usually lies behind them, and how it usually goes. "Usually" matters: read the actual case.

## "would overwrite uncommitted changes in ..." or "would overwrite untracked files in ..."

Origin changed or added files that this machine also has, with different contents. The engine has already cleared any file in the way that matched origin exactly, so every file named here really differs. This is the case the main skill is about: whose work is it, is it live, and is it real or superseded. A note like "(3 more files in the way are identical...)" means those three already match and need nothing.

## "has N commits, and merging them conflicts in ..."

This machine committed work, and origin changed the same lines in its own commits. Both sides are usually real. Read both, work out what each was for, and write the version that does both: on a repo people work on directly, that's what the next person to touch the file would do. Make the merge commit, run the repo's own quick check if it has one, and push.

When the code doesn't tell you what the combined version should be, don't guess into the shared branch, because every machine gets it. Leave origin's side on the branch, and say in your result which local commits stay only on the saved branch and why.

## "is checked out on X, not main"

Someone switched branches, usually a person or session working on a feature. The question is whether that work is live: a process in the folder, recent commits on the branch, recent edits.

- **Live:** write `waiting`.
- **Done or abandoned:** make sure the branch is on origin (push it if it isn't), then switch the checkout back to the item's branch.
- **Had commits but was never merged:** say so in your result. Leave merging it to the people working on it.

## "was switched to X during the run" or "while devcluster updated it"

An agent switched branches at the moment the engine was updating. In the second case, the other branch received origin's commits; that adds commits and loses none. Look at both branches and put each where its owner would expect it. Usually the item's branch needs a plain fast-forward and the other branch can stay as it now is.

## "is in the middle of a merge" or "rebase", "cherry-pick", "revert"

Someone started an operation and didn't finish. If a session is still in it, write `waiting`. If it's old, finish it when the right resolution is clear from what was being done. Otherwise abort it (`git merge --abort`, `git rebase --abort`): the saved branch holds the state it was in.

## "couldn't fetch from origin" or "timed out"

Usually one of three things:

- **A slow link:** the repo is large and this machine is far behind.
- **Credentials:** the SSH key or token stopped working.
- **The network is down.**

For a slow link, you can load the missing history from a nearby machine that has it (`git fetch <that machine>:<path> <branch>`), then fetch from origin again for the rest. Raise the repo's time limit in the config when the limit really is too short for this machine; that change reaches every machine, so make it a fair limit, not an unlimited one. A fetch that left `tmp_pack_*` files in `.git/objects/pack` after being killed can leave gigabytes behind; remove them once no Git process is running in the checkout.

A credential that needs a login only the owner can do is `needs-owner`.

## "couldn't push: ..."

Usually one of two things:

- **Origin refused something.** Most often a file over the host's size limit (100 MB on GitHub), or a push over 2 GB in one go.
- **Origin moved during the push.** The next run handles that by itself.

For a refusal, find the offending files in the unpushed commits. If they are build or run outputs that don't belong in Git, rewrite only those unpushed commits without them, and keep the originals on the saved branch. Then push. If they are real and large, push in smaller batches, or keep them out of the shared history and say so.

## "has N commits that origin doesn't have" (a tool or the secrets checkout)

Someone committed inside a checkout that this machine only follows. Find out whether the commits are worth having. If they are, push them to a branch on origin, and to the main branch if that's how that repo is worked on. Then let the checkout follow again. If they aren't, the saved branch holds them, and the checkout can move to where the list says.

## "its origin is X, but the list says Y"

Either the repo moved (a new name or a new owner, the same history), or this folder is a different repo. If it's the same repo under a new address, point `origin` at the listed URL. If it's genuinely a different repo sitting where the list expects this one, it's someone's other project. Leave it, write `could-not-fix`, and say so: the list or the folder has to change, and that's not yours to choose.

## "exists but isn't a Git checkout" or "has no origin remote"

Something else is at that path: a folder someone made, or a copy made without Git. Read what's there. If its contents are a copy of the repo, the saved branch can't hold them, because it isn't a checkout. Move the folder aside next to it (`<name>.before-devcluster`), and let the next run clone. If it's something unrelated, write `could-not-fix` with what it is.

## "its install failed" or "exited N" (a tool)

Read the install output in the item's detail. The usual causes are a missing dependency on this machine, a path that differs here, or a broken commit on the tool's own branch.

- **A cause on this machine:** fix it here.
- **A broken commit:** the fix belongs in the tool's repo, made the way that repo is normally changed. Say so if you can't make it from here.

## "its check failed here" or "decrypt failed" (secrets)

Someone edited a decrypted credential on this machine without publishing it, or this machine's key can't read a new secret. Never print a value while you look. Compare files by hash or by which keys are present, not by contents.

- **An edit nobody published:** publish it the way the secrets repo's own tooling does, or, if it's stale, let the decrypt replace it.
- **A key problem:** usually needs the owner, because a new key is theirs to grant.

## "was edited here since devcluster wrote it" (a managed file)

Someone edited a file the config puts in place, such as global agent instructions. If the edit should apply everywhere, the right home is the config repo's copy; make the same edit there. If it was a local experiment, or older than the config's version, let the config's version back in by removing the local copy. The next run writes it.
