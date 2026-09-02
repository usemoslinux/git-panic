from __future__ import annotations

from git_panic.git import GitRepository
from git_panic.models import SafetyError


class SafetyValidator:
    IN_PROGRESS_MARKERS = {
        "MERGE_HEAD": "merge",
        "CHERRY_PICK_HEAD": "cherry-pick",
        "REVERT_HEAD": "revert",
        "BISECT_LOG": "bisect",
        "rebase-apply": "rebase",
        "rebase-merge": "rebase",
    }

    def __init__(self, repository: GitRepository) -> None:
        self.repository = repository

    def validate_preflight(self) -> None:
        active = {
            label
            for marker, label in self.IN_PROGRESS_MARKERS.items()
            if self.repository.git_path(marker).exists()
        }
        if active:
            operations = ", ".join(sorted(active))
            raise SafetyError(
                f"Git-Panic will not run during an active {operations} operation. "
                "Finish or abort it manually, then retry."
            )
        if self.repository.has_unmerged_paths():
            raise SafetyError(
                "The repository has unresolved conflicts. Resolve them or abort the current operation first."
            )
        divergence = self.repository.upstream_divergence()
        if divergence and divergence[0] > 0 and divergence[1] > 0:
            raise SafetyError(
                "The current branch has diverged from its upstream. Git-Panic's local-only workflows "
                "cannot safely choose how to reconcile the remote history."
            )

    def require_attached_head(self) -> str:
        branch = self.repository.current_branch()
        if branch is None:
            raise SafetyError("This workflow requires a named branch. Use Reflog Rescue for a detached HEAD.")
        return branch

    def require_clean_worktree(self, workflow: str) -> None:
        if self.repository.is_dirty():
            raise SafetyError(
                f"{workflow} requires a clean working tree so existing changes cannot be mixed into recovery."
            )

    def refuse_published_head(self) -> None:
        if self.repository.head_is_published():
            raise SafetyError(
                "The last commit appears to exist on the configured upstream. Git-Panic only rewrites "
                "unpublished local history and will not alter shared history."
            )

    def require_published_head(self) -> None:
        if not self.repository.head_is_upstream_tip():
            raise SafetyError(
                "The current commit is not the configured upstream tip. Update your remote-tracking state "
                "and check out the latest published commit before creating a revert commit."
            )
