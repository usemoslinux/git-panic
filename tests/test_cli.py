from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from git_panic.cli import app


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_cli_dry_run_prints_plan_without_mutation(repository: Path) -> None:
    original_head = git(repository, "rev-parse", "HEAD")
    result = CliRunner().invoke(
        app,
        ["--repo", str(repository), "--dry-run"],
        input="7\n2\n",
        terminal_width=80,
    )

    assert result.exit_code == 0, result.output
    assert "What happened?" in result.output
    assert "UNCOMMITTED WORK" in result.output
    assert "LAST COMMIT, NOT SHARED" in result.output
    assert "PUSHED OR PUBLISHED HISTORY" in result.output
    assert "GO BACK IN TIME" in result.output
    assert "I need to recover an earlier state" in result.output
    assert "Choose what happened" in result.output
    assert "git reset --mixed" in result.output
    assert "Dry run complete" in result.output
    assert git(repository, "rev-parse", "HEAD") == original_head
    assert git(repository, "branch", "--list", "git-panic-rescue/*") == ""


def test_cli_no_safety_branch_omits_backup_command(repository: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--repo", str(repository), "--dry-run", "--no-safety-branch"],
        input="7\n2\n",
    )

    assert result.exit_code == 0, result.output
    assert "safety branch creation is disabled" in result.output
    assert "git branch git-panic-rescue/" not in result.output
    assert "git reset --mixed" in result.output


def test_sensitive_file_plan_prominently_recommends_rotation(repository: Path) -> None:
    (repository / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    git(repository, "add", "--force", ".env")
    git(repository, "commit", "-m", "add sensitive file")

    result = CliRunner().invoke(
        app,
        ["--repo", str(repository), "--dry-run"],
        input="6\n.env\n",
    )

    assert result.exit_code == 0, result.output
    assert "Rotate or revoke the exposed credential immediately" in result.output
    assert "append '.env' to .gitignore" in result.output
    assert "git add -- ':(literal).gitignore'" in result.output
    assert "git rm --cached" in result.output
    assert "git commit --amend --no-edit" in result.output
    assert "No safety branch will be created" in result.output
