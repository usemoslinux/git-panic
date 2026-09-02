from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_panic.git import GitRepository
from git_panic.models import SafetyError
from git_panic.safety import SafetyValidator


def test_preflight_refuses_active_merge(repository: Path) -> None:
    repo = GitRepository(repository)
    repo.git_path("MERGE_HEAD").write_text(repo.head() + "\n", encoding="ascii")

    with pytest.raises(SafetyError, match="active merge"):
        SafetyValidator(repo).validate_preflight()


def test_preflight_refuses_unmerged_paths(repository: Path) -> None:
    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ("git", *args),
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(result.stderr)
        return result

    git("switch", "-c", "conflict")
    (repository / "tracked.txt").write_text("branch\n", encoding="utf-8")
    git("commit", "-am", "branch change")
    git("switch", "main")
    (repository / "tracked.txt").write_text("main\n", encoding="utf-8")
    git("commit", "-am", "main change")
    assert git("merge", "conflict", check=False).returncode != 0

    with pytest.raises(SafetyError, match="active merge|unresolved conflicts"):
        SafetyValidator(GitRepository(repository)).validate_preflight()
