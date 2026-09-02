# Git-Panic

Git-Panic is an interactive, safety-first terminal assistant for common local Git recovery scenarios. It diagnoses the problem, explains an exact command plan, creates a rescue branch by default, and asks for explicit confirmation before changing repository state.

## Recovery workflows

- Move the last unpublished commit from the current branch to a new branch.
- Add staged corrections to the last unpublished commit without changing its message.
- Stop tracking a committed sensitive file while keeping the local ignored copy.
- Undo the last unpublished commit while preserving its content as staged or unstaged changes.
- Replace the last unpublished commit message without including staged changes.
- Set aside one path or all uncommitted changes in a recoverable stash.
- Restore a locally deleted tracked file from `HEAD`.
- Revert the latest published commit by creating an inverse commit.
- Inspect the reflog and recover a selected state onto a new branch.

Git-Panic refuses to proceed during merge, rebase, cherry-pick, revert, or bisect operations; with unresolved conflicts; or when the local branch has diverged from its upstream. History-rewriting workflows also refuse to alter a commit already present upstream.

## Installation and Run

Python 3.10 or newer and Git are required.

### For Users (Recommended)
The easiest way to install and run `git-panic` globally without modifying your system Python environment is using [`pipx`](https://pypa.github.io/pipx/):

```bash
pipx install git-panic
git-panic --repo /path/to/repository
```

If you ever need to uninstall or upgrade:
```bash
pipx uninstall git-panic
pipx upgrade git-panic
```

### For Developers
If you want to contribute, run local tests, or modify the source code:

```bash
git clone https://github.com/usemoslinux/git-panic.git
cd git-panic
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

---

Inspect a repository without executing recovery commands:

```bash
git-panic --repo /path/to/repository --dry-run
```

## Safety model

By default, every plan starts with a branch named `git-panic-rescue/<timestamp>-<workflow>`. The full plan is displayed before execution and defaults to cancellation at the confirmation prompt. Commands are passed directly to Git without a shell.

The backup protects committed history. It does not snapshot arbitrary uncommitted files, so workflows that move branches require a clean working tree. The discard workflow uses `git stash push` instead of `git restore`, ensuring discarded changes remain recoverable. Deleted-file recovery is safe because the deleted file's current version remains in `HEAD`.

Git-Panic does not offer `git reset --hard` or force-push workflows. Published history is undone with `git revert`, preserving a reviewable record and avoiding disruption for collaborators.

### Sensitive files

The sensitive-file workflow adds a selected path to the root repository `.gitignore` and stages that file when no repository ignore rule already applies. The planned file edit and `git add` are shown before confirmation. Existing uncommitted `.gitignore` changes block the workflow so Git-Panic cannot accidentally commit them. Global excludes and `.git/info/exclude` are rejected because collaborators would not receive those rules.

For an unpublished latest commit, Git-Panic stops tracking the file and amends the commit. If the file was pushed or otherwise shared, it creates a normal removal commit without rewriting shared history. A safety branch is intentionally omitted because it would preserve another named reference to the sensitive commit.

Credential rotation or revocation is always recommended and is displayed prominently before execution. Removing a file does not invalidate exposed credentials. Published content also remains in historical commits; repository-wide cleanup with `git filter-repo` and coordinated force-pushing is left to repository owners and collaborators.

Safety branch creation can be explicitly disabled for one invocation. Git-Panic displays a warning and still requires confirmation before executing the recovery commands:

```bash
git-panic --no-safety-branch
```
