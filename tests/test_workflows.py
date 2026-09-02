from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_panic.git import GitExecutor, GitRepository
from git_panic.models import ConfirmationRequired, SafetyError
from git_panic.safety import SafetyValidator
from git_panic.workflows import RecoveryPlanner


def git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=path,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def components(path: Path) -> tuple[GitRepository, SafetyValidator, RecoveryPlanner, GitExecutor]:
    repository = GitRepository(path)
    validator = SafetyValidator(repository)
    return repository, validator, RecoveryPlanner(repository, validator), GitExecutor(repository)


def commit_sensitive_file(path: Path) -> None:
    (path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    git(path, "add", ".gitignore")
    git(path, "add", "--force", ".env")
    git(path, "commit", "-m", "accidentally add credentials")


def test_executor_requires_confirmation_and_dry_run_does_not_mutate(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    original_head = repo.head()
    plan = planner.undo_keep_changes()

    with pytest.raises(ConfirmationRequired):
        executor.execute(plan, confirmed=False)

    commands = executor.execute(plan, confirmed=False, dry_run=True)
    assert commands == [command.display for command in plan.commands]
    assert repo.head() == original_head
    assert not repo.branch_exists(plan.backup_ref)


def test_safety_branch_can_be_disabled(repository: Path) -> None:
    repo = GitRepository(repository)
    validator = SafetyValidator(repo)
    planner = RecoveryPlanner(repo, validator, create_safety_branch=False)
    original_head = repo.head()
    plan = planner.undo_keep_changes()

    assert plan.backup_ref is None
    assert all(command.args[0] != "branch" for command in plan.commands)

    GitExecutor(repo).execute(plan, confirmed=True)

    assert repo.head() != original_head
    assert git(repository, "branch", "--list", "git-panic-rescue/*") == ""


def test_wrong_branch_moves_commit_and_keeps_backup(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    original_head = repo.head()
    parent = repo.head_parent()
    plan = planner.wrong_branch("feature/recovered")

    executor.execute(plan, confirmed=True)

    assert repo.current_branch() == "feature/recovered"
    assert repo.head() == original_head
    assert git(repository, "rev-parse", "main") == parent
    assert git(repository, "rev-parse", plan.backup_ref) == original_head


def test_amend_changes_adds_only_staged_correction(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    original_head = repo.head()
    original_message = git(repository, "log", "-1", "--format=%s")
    (repository / "tracked.txt").write_text("corrected\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("leave me alone\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    plan = planner.amend_changes()

    executor.execute(plan, confirmed=True)

    assert repo.head() != original_head
    assert git(repository, "log", "-1", "--format=%s") == original_message
    assert git(repository, "show", "HEAD:tracked.txt") == "corrected"
    assert (repository / "untracked.txt").exists()
    assert git(repository, "rev-parse", plan.backup_ref) == original_head


def test_amend_changes_requires_staged_content(repository: Path) -> None:
    _, _, planner, _ = components(repository)
    (repository / "tracked.txt").write_text("unstaged\n", encoding="utf-8")

    with pytest.raises(SafetyError, match="No staged changes"):
        planner.amend_changes()


def test_sensitive_file_is_removed_from_unpublished_commit(repository: Path) -> None:
    commit_sensitive_file(repository)
    repo, _, planner, executor = components(repository)
    exposed_head = repo.head()
    plan = planner.sensitive_file(".env", published=False)

    assert plan.backup_ref is None
    assert any("Rotate or revoke" in warning for warning in plan.warnings)
    executor.execute(plan, confirmed=True)

    assert repo.head() != exposed_head
    assert (repository / ".env").read_text(encoding="utf-8") == "API_KEY=secret\n"
    assert git(repository, "ls-files", ".env") == ""
    assert git(repository, "status", "--porcelain=v1") == ""
    assert git(repository, "log", "-1", "--format=%s") == "accidentally add credentials"


def test_sensitive_file_published_plan_creates_removal_commit(repository: Path) -> None:
    commit_sensitive_file(repository)
    remote = repository.parent / f"{repository.name}-sensitive-remote.git"
    git(repository, "init", "--bare", str(remote))
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "--set-upstream", "origin", "main")
    repo, _, planner, executor = components(repository)
    published_head = repo.head()
    plan = planner.sensitive_file(".env", published=True)

    executor.execute(plan, confirmed=True)

    assert git(repository, "rev-parse", "HEAD^") == published_head
    assert git(repository, "log", "-1", "--format=%s") == "Stop tracking sensitive file"
    assert git(repository, "ls-files", ".env") == ""
    assert (repository / ".env").exists()
    assert any("published history" in warning for warning in plan.warnings)


def test_sensitive_file_adds_missing_repository_ignore_rule(repository: Path) -> None:
    (repository / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    git(repository, "add", ".env")
    git(repository, "commit", "-m", "accidentally add credentials")
    repo, _, planner, executor = components(repository)
    plan = planner.sensitive_file(".env", published=False)

    assert plan.commands[0].display == "append '.env' to .gitignore"
    assert plan.commands[1].display == "git add -- ':(literal).gitignore'"
    executor.execute(plan, confirmed=True)

    assert (repository / ".gitignore").read_text(encoding="utf-8") == ".env\n"
    assert git(repository, "ls-files", ".env") == ""
    assert git(repository, "show", "HEAD:.gitignore") == ".env"
    assert repo.current_branch() == "main"


def test_sensitive_file_refuses_to_modify_changed_gitignore(repository: Path) -> None:
    (repository / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    git(repository, "add", ".env")
    git(repository, "commit", "-m", "accidentally add credentials")
    (repository / ".gitignore").write_text("local-only-rule\n", encoding="utf-8")
    _, _, planner, _ = components(repository)

    with pytest.raises(SafetyError, match=".gitignore already has changes"):
        planner.sensitive_file(".env", published=False)


def test_sensitive_file_refuses_unpublished_mode_for_pushed_head(repository: Path) -> None:
    commit_sensitive_file(repository)
    remote = repository.parent / f"{repository.name}-sensitive-remote.git"
    git(repository, "init", "--bare", str(remote))
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "--set-upstream", "origin", "main")
    _, _, planner, _ = components(repository)

    with pytest.raises(SafetyError, match="exists on the configured upstream"):
        planner.sensitive_file(".env", published=False)


def test_undo_commit_keeps_changes_unstaged(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    original_head = repo.head()
    parent = repo.head_parent()
    plan = planner.undo_keep_changes()

    executor.execute(plan, confirmed=True)

    assert repo.head() == parent
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "first\nsecond\n"
    assert git(repository, "status", "--porcelain=v1") == "M tracked.txt"
    assert git(repository, "rev-parse", plan.backup_ref) == original_head


def test_soft_undo_keeps_changes_staged(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    parent = repo.head_parent()
    plan = planner.undo_keep_changes(staged=True)

    executor.execute(plan, confirmed=True)

    assert repo.head() == parent
    assert git(repository, "diff", "--cached", "--name-only") == "tracked.txt"
    assert git(repository, "diff", "--name-only") == ""


def test_deleted_file_restores_worktree_and_index(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    tracked = repository / "tracked.txt"
    tracked.unlink()
    plan = planner.deleted_file("tracked.txt")

    executor.execute(plan, confirmed=True)

    assert tracked.read_text(encoding="utf-8") == "first\nsecond\n"
    assert git(repository, "status", "--porcelain=v1") == ""
    assert repo.branch_exists(plan.backup_ref)


def test_deleted_file_restores_a_staged_deletion(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    git(repository, "rm", "tracked.txt")
    plan = planner.deleted_file("tracked.txt")

    executor.execute(plan, confirmed=True)

    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "first\nsecond\n"
    assert git(repository, "status", "--porcelain=v1") == ""
    assert repo.branch_exists(plan.backup_ref)


def test_discard_one_path_stashes_it_and_leaves_other_changes(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    other = repository / "other.txt"
    other.write_text("committed\n", encoding="utf-8")
    git(repository, "add", "other.txt")
    git(repository, "commit", "-m", "add other")
    (repository / "tracked.txt").write_text("discard this\n", encoding="utf-8")
    other.write_text("keep this change\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    plan = planner.discard_changes("tracked.txt")

    executor.execute(plan, confirmed=True)

    assert git(repository, "show", "HEAD:tracked.txt") == (repository / "tracked.txt").read_text(
        encoding="utf-8"
    ).strip()
    assert other.read_text(encoding="utf-8") == "keep this change\n"
    assert git(repository, "status", "--porcelain=v1") == "M other.txt"
    assert "git-panic discarded changes" in git(repository, "stash", "list", "-1", "--format=%s")
    assert repo.branch_exists(plan.backup_ref)


def test_discard_all_stashes_untracked_files(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    (repository / "tracked.txt").write_text("discard this\n", encoding="utf-8")
    untracked = repository / "scratch.txt"
    untracked.write_text("also discard\n", encoding="utf-8")
    plan = planner.discard_changes()

    executor.execute(plan, confirmed=True)

    assert not repo.is_dirty()
    assert not untracked.exists()
    assert "git-panic discarded changes" in git(repository, "stash", "list", "-1", "--format=%s")


def test_fix_message_does_not_include_unstaged_changes(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    (repository / "tracked.txt").write_text("uncommitted\n", encoding="utf-8")
    plan = planner.fix_message("better message")

    executor.execute(plan, confirmed=True)

    assert git(repository, "log", "-1", "--format=%s") == "better message"
    assert git(repository, "show", "HEAD:tracked.txt") == "first\nsecond"
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert repo.branch_exists(plan.backup_ref)


def test_fix_message_refuses_staged_changes(repository: Path) -> None:
    _, _, planner, _ = components(repository)
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")

    with pytest.raises(SafetyError, match="staged changes"):
        planner.fix_message("better message")


def test_reflog_rescue_creates_new_branch_at_selected_commit(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    first_commit = git(repository, "rev-parse", "HEAD^")
    plan = planner.reflog_rescue(first_commit, "rescue/first")

    executor.execute(plan, confirmed=True)

    assert repo.current_branch() == "rescue/first"
    assert repo.head() == first_commit
    assert repo.branch_exists(plan.backup_ref)


def test_revert_latest_published_commit_creates_inverse_commit(repository: Path) -> None:
    repo, _, planner, executor = components(repository)
    remote = repository.parent / f"{repository.name}-remote.git"
    git(repository, "init", "--bare", str(remote))
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "--set-upstream", "origin", "main")
    published_head = repo.head()
    plan = planner.revert_published_head()

    executor.execute(plan, confirmed=True)

    assert repo.head() != published_head
    assert git(repository, "show", "HEAD:tracked.txt") == "first"
    assert git(repository, "log", "-1", "--format=%s").startswith("Revert")
    assert git(repository, "rev-parse", plan.backup_ref) == published_head


def test_revert_refuses_unpublished_head(repository: Path) -> None:
    _, _, planner, _ = components(repository)

    with pytest.raises(SafetyError, match="not the configured upstream tip"):
        planner.revert_published_head()


def test_revert_refuses_when_local_branch_is_behind_upstream(repository: Path) -> None:
    remote = repository.parent / f"{repository.name}-remote.git"
    git(repository, "init", "--bare", str(remote))
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "--set-upstream", "origin", "main")
    git(repository, "reset", "--hard", "HEAD^")
    _, _, planner, _ = components(repository)

    with pytest.raises(SafetyError, match="not the configured upstream tip"):
        planner.revert_published_head()


def test_wrong_branch_refuses_dirty_worktree(repository: Path) -> None:
    _, _, planner, _ = components(repository)
    (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(SafetyError, match="clean working tree"):
        planner.wrong_branch("new-branch")
