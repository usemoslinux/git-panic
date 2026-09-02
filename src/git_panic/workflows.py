from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath

from git_panic.git import GitRepository
from git_panic.models import FileAppend, GitCommand, RecoveryPlan, SafetyError, WorkflowKind
from git_panic.safety import SafetyValidator


class RecoveryPlanner:
    def __init__(
        self,
        repository: GitRepository,
        validator: SafetyValidator,
        *,
        create_safety_branch: bool = True,
    ) -> None:
        self.repository = repository
        self.validator = validator
        self.create_safety_branch = create_safety_branch

    def _backup_name(self, workflow: WorkflowKind) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"git-panic-rescue/{timestamp}-{workflow.value}"

    @staticmethod
    def _normalize_path(raw_path: str) -> str:
        path = PurePosixPath(raw_path.strip())
        if not raw_path.strip() or path.is_absolute() or ".." in path.parts:
            raise SafetyError("Enter a repository-relative file path without '..'.")
        return path.as_posix()

    def _backup_command(self, backup: str, head: str) -> GitCommand:
        return GitCommand(
            ("branch", backup, head),
            f"Create safety branch {backup} at the current commit",
        )

    def _base(self, workflow: WorkflowKind) -> tuple[str | None, tuple[GitCommand, ...]]:
        if not self.create_safety_branch:
            return None, ()
        head = self.repository.head()
        backup = self._backup_name(workflow)
        return backup, (self._backup_command(backup, head),)

    def wrong_branch(self, destination: str) -> RecoveryPlan:
        current = self.validator.require_attached_head()
        self.validator.require_clean_worktree("Wrong Branch")
        self.validator.refuse_published_head()
        if not destination or not self.repository.valid_branch_name(destination):
            raise SafetyError(f"{destination!r} is not a valid Git branch name.")
        if self.repository.branch_exists(destination):
            raise SafetyError(
                "The destination branch already exists. Git-Panic only moves a commit to a new branch "
                "to avoid an automatic cherry-pick and possible conflicts."
            )
        head = self.repository.head()
        parent = self.repository.head_parent()
        backup, backup_commands = self._base(WorkflowKind.WRONG_BRANCH)
        return RecoveryPlan(
            workflow=WorkflowKind.WRONG_BRANCH,
            title="Move the last commit to a new branch",
            summary=(
                f"Preserve {head[:10]}, create and switch to {destination!r} at that commit, then move "
                f"{current!r} back to its previous commit. Your working tree remains on {destination!r}."
            ),
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(("switch", "-c", destination, head), f"Create and switch to {destination}"),
                GitCommand(("branch", "-f", current, parent), f"Move {current} back one commit"),
            ),
        )

    def amend_changes(self) -> RecoveryPlan:
        self.validator.require_attached_head()
        self.validator.refuse_published_head()
        if not self.repository.has_staged_changes():
            raise SafetyError(
                "No staged changes were found. Stage only the intended correction, then run this workflow again."
            )
        backup, backup_commands = self._base(WorkflowKind.AMEND_CHANGES)
        return RecoveryPlan(
            workflow=WorkflowKind.AMEND_CHANGES,
            title="Add staged corrections to the last commit",
            summary=(
                "Amend the last unpublished commit with the currently staged changes while preserving its "
                "message. Unstaged and untracked changes are not included."
            ),
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(("commit", "--amend", "--no-edit"), "Add staged changes without changing the message"),
            ),
        )

    def sensitive_file(self, raw_path: str, *, published: bool) -> RecoveryPlan:
        self.validator.require_attached_head()
        normalized = self._normalize_path(raw_path)
        if not self.repository.tracked(normalized):
            raise SafetyError(f"{normalized!r} is not tracked by the current commit.")

        ignore_source = self.repository.repository_ignore_source(normalized)
        ignore_commands: tuple[GitCommand | FileAppend, ...] = ()
        if ignore_source is None:
            ignore_source = ".gitignore"
            if self.repository.path_has_changes(ignore_source):
                raise SafetyError(
                    ".gitignore already has changes. Commit or set those changes aside before allowing "
                    "Git-Panic to add the sensitive-file rule."
                )
            ignore_commands = (
                FileAppend(ignore_source, normalized, f"Add {normalized} to .gitignore"),
                GitCommand(("add", "--", ":(literal).gitignore"), "Stage the new ignore rule"),
            )
        else:
            if not self.repository.index_tracks(ignore_source):
                raise SafetyError(
                    f"{ignore_source!r} is not tracked. Commit it before continuing so collaborators "
                    "receive its existing ignore rule."
                )
            if self.repository.has_unstaged_changes(ignore_source):
                raise SafetyError(f"{ignore_source!r} has unstaged changes. Stage or set them aside first.")

        unrelated_staged = self.repository.staged_paths() - {ignore_source}
        if unrelated_staged:
            paths = ", ".join(sorted(unrelated_staged))
            raise SafetyError(
                f"Unstage unrelated changes before continuing. These paths would enter the commit: {paths}"
            )
        if not published and self.repository.head_is_published():
            raise SafetyError(
                "The current commit exists on the configured upstream. Treat the credential as published "
                "and choose the shared/pushed option."
            )

        command = (
            *ignore_commands,
            GitCommand(
                ("rm", "--cached", "--", f":(literal){normalized}"),
                f"Stop tracking {normalized} while keeping the local file",
            ),
            GitCommand(
                ("commit", "-m", "Stop tracking sensitive file")
                if published
                else ("commit", "--amend", "--no-edit"),
                "Create a removal commit without rewriting history"
                if published
                else "Remove the file from the latest unpublished commit",
            ),
        )
        warnings = [
            "Rotate or revoke the exposed credential immediately. Removing it from Git does not invalidate it.",
            "No safety branch will be created because that branch would intentionally preserve the sensitive commit.",
        ]
        if published:
            warnings.append(
                "The sensitive content remains in published history. Coordinate any git-filter-repo cleanup "
                "and subsequent force-push with repository owners and every collaborator."
            )
        else:
            warnings.append(
                "The old commit may remain in your local reflog and object database, but it will no longer be "
                "part of the branch you push."
            )
        return RecoveryPlan(
            workflow=WorkflowKind.SENSITIVE_FILE,
            title="Stop tracking a sensitive file",
            summary=(
                f"Keep {normalized!r} on disk, remove it from Git's index, and "
                + (
                    "create a new commit that does not rewrite published history."
                    if published
                    else "amend the latest unpublished commit without changing its message."
                )
            ),
            backup_ref=None,
            commands=command,
            warnings=tuple(warnings),
        )

    def undo_keep_changes(self, *, staged: bool = False) -> RecoveryPlan:
        self.validator.require_attached_head()
        self.validator.require_clean_worktree("Undo Commit, Keep Changes")
        self.validator.refuse_published_head()
        parent = self.repository.head_parent()
        backup, backup_commands = self._base(WorkflowKind.UNDO_KEEP_CHANGES)
        mode = "--soft" if staged else "--mixed"
        resulting_state = "staged" if staged else "unstaged"
        return RecoveryPlan(
            workflow=WorkflowKind.UNDO_KEEP_CHANGES,
            title="Undo the last commit and keep its changes",
            summary=(
                f"Move the current branch to the previous commit with a {mode.removeprefix('--')} reset. "
                f"The former commit's content remains in the working tree as {resulting_state} changes."
            ),
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(("reset", mode, parent), f"Move HEAD back and keep the changes {resulting_state}"),
            ),
        )

    def deleted_file(self, raw_path: str) -> RecoveryPlan:
        normalized = self._normalize_path(raw_path)
        absolute = self.repository.root.joinpath(*PurePosixPath(normalized).parts)
        if not self.repository.tracked(normalized):
            raise SafetyError(f"{normalized!r} is not tracked in HEAD and cannot be restored from it.")
        if absolute.exists() or absolute.is_symlink():
            raise SafetyError(f"{normalized!r} still exists. This workflow only restores a deleted path.")
        backup, backup_commands = self._base(WorkflowKind.DELETED_FILE)
        return RecoveryPlan(
            workflow=WorkflowKind.DELETED_FILE,
            title="Restore a deleted tracked file",
            summary=f"Restore {normalized!r} in both the index and working tree from the current commit.",
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(
                    (
                        "restore",
                        "--source=HEAD",
                        "--staged",
                        "--worktree",
                        "--",
                        f":(literal){normalized}",
                    ),
                    f"Restore {normalized} from HEAD",
                ),
            ),
        )

    def discard_changes(self, raw_path: str | None = None) -> RecoveryPlan:
        normalized = self._normalize_path(raw_path) if raw_path is not None else None
        if normalized is not None:
            if not self.repository.path_has_changes(normalized):
                raise SafetyError(f"{normalized!r} has no staged, unstaged, or untracked changes to discard.")
        elif not self.repository.is_dirty():
            raise SafetyError("The working tree has no staged, unstaged, or untracked changes to discard.")

        backup, backup_commands = self._base(WorkflowKind.DISCARD_CHANGES)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stash_message = f"git-panic discarded changes {timestamp}"
        scope = repr(normalized) if normalized is not None else "the entire working tree"
        pathspec = ("--", f":(literal){normalized}") if normalized is not None else ()
        return RecoveryPlan(
            workflow=WorkflowKind.DISCARD_CHANGES,
            title="Set uncommitted changes aside safely",
            summary=(
                f"Return {scope} to the committed state by storing its staged, unstaged, and untracked "
                "changes in Git's stash. The discarded work remains recoverable with `git stash list` "
                "and `git stash apply`."
            ),
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(
                    ("stash", "push", "--include-untracked", "--message", stash_message, *pathspec),
                    f"Stash changes from {scope} instead of deleting them",
                ),
            ),
        )

    def fix_message(self, new_message: str) -> RecoveryPlan:
        self.validator.require_attached_head()
        self.validator.refuse_published_head()
        if self.repository.has_staged_changes():
            raise SafetyError(
                "The index contains staged changes. Amending now would add them to the commit; unstage them first."
            )
        if not new_message.strip():
            raise SafetyError("The new commit message cannot be empty.")
        backup, backup_commands = self._base(WorkflowKind.FIX_MESSAGE)
        return RecoveryPlan(
            workflow=WorkflowKind.FIX_MESSAGE,
            title="Replace the last commit message",
            summary="Amend only the last commit message. Unstaged working-tree changes are left untouched.",
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(("commit", "--amend", "--only", "-m", new_message.strip()), "Rewrite the message"),
            ),
        )

    def revert_published_head(self) -> RecoveryPlan:
        self.validator.require_attached_head()
        self.validator.require_clean_worktree("Revert Published Commit")
        self.validator.require_published_head()
        if self.repository.head_parent_count() != 1:
            raise SafetyError(
                "The current commit is a root or merge commit. Git-Panic will not guess merge-parent semantics."
            )
        head = self.repository.head()
        backup, backup_commands = self._base(WorkflowKind.REVERT_PUBLISHED)
        return RecoveryPlan(
            workflow=WorkflowKind.REVERT_PUBLISHED,
            title="Revert the latest published commit",
            summary=(
                f"Create a new commit that reverses published commit {head[:10]}. Existing history remains "
                "intact, so collaborators can pull the correction normally."
            ),
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(("revert", "--no-edit", head), f"Create an inverse commit for {head[:10]}"),
            ),
        )

    def reflog_rescue(self, commit: str, destination: str) -> RecoveryPlan:
        self.validator.require_clean_worktree("Reflog Rescue")
        if not destination or not self.repository.valid_branch_name(destination):
            raise SafetyError(f"{destination!r} is not a valid Git branch name.")
        if self.repository.branch_exists(destination):
            raise SafetyError("The rescue destination must be a new branch.")
        allowed = {entry[0] for entry in self.repository.reflog()}
        if commit not in allowed:
            raise SafetyError("Select a commit from the displayed reflog entries.")
        backup, backup_commands = self._base(WorkflowKind.REFLOG_RESCUE)
        backup_summary = "Keep a backup at the current HEAD, then " if backup else ""
        return RecoveryPlan(
            workflow=WorkflowKind.REFLOG_RESCUE,
            title="Create a branch from a reflog entry",
            summary=(
                f"{backup_summary}create and switch to {destination!r} at "
                f"reflog commit {commit[:10]}. No existing branch is moved."
            ),
            backup_ref=backup,
            commands=(
                *backup_commands,
                GitCommand(("switch", "-c", destination, commit), f"Recover {commit[:10]} on {destination}"),
            ),
        )
