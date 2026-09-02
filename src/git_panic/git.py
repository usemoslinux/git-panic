from __future__ import annotations

import subprocess
from pathlib import Path

from git_panic.models import CommandError, FileAppend, GitCommand, RecoveryPlan


class GitRepository:
    """Read-only queries for one Git working tree."""

    def __init__(self, path: Path | str = ".") -> None:
        requested = Path(path).resolve()
        result = self._run_at(requested, ("rev-parse", "--show-toplevel"), check=False)
        if result.returncode != 0:
            raise CommandError(
                "git rev-parse --show-toplevel",
                result.stderr.strip() or "not inside a Git working tree",
                result.returncode,
            )
        self.root = Path(result.stdout.strip()).resolve()

    @staticmethod
    def _run_at(
        path: Path,
        args: tuple[str, ...],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ("git", *args),
                cwd=path,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as error:
            command = GitCommand(args, "Read repository state", state_changing=False)
            raise CommandError(command.display, str(error), 127) from error
        if check and result.returncode != 0:
            command = GitCommand(args, "Read repository state", state_changing=False)
            raise CommandError(
                command.display,
                result.stderr.strip() or result.stdout.strip() or "unknown Git error",
                result.returncode,
            )
        return result

    def run(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_at(self.root, tuple(args), check=check)

    def output(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def git_path(self, name: str) -> Path:
        path = Path(self.output("rev-parse", "--git-path", name))
        return path if path.is_absolute() else self.root / path

    def head(self) -> str:
        return self.output("rev-parse", "--verify", "HEAD")

    def head_parent(self) -> str:
        result = self.run("rev-parse", "--verify", "HEAD^", check=False)
        if result.returncode != 0:
            raise CommandError(
                "git rev-parse --verify 'HEAD^'",
                "the current commit has no parent",
                result.returncode,
            )
        return result.stdout.strip()

    def head_parent_count(self) -> int:
        fields = self.output("rev-list", "--parents", "-n", "1", "HEAD").split()
        return max(0, len(fields) - 1)

    def current_branch(self) -> str | None:
        result = self.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def branch_exists(self, name: str) -> bool:
        return self.run("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0

    def valid_branch_name(self, name: str) -> bool:
        return self.run("check-ref-format", "--branch", name, check=False).returncode == 0

    def is_dirty(self) -> bool:
        return bool(self.output("status", "--porcelain=v1", "--untracked-files=normal"))

    def has_staged_changes(self) -> bool:
        return self.run("diff", "--cached", "--quiet", check=False).returncode != 0

    def staged_paths(self) -> set[str]:
        output = self.run("diff", "--cached", "--name-only", "-z").stdout
        return {path for path in output.split("\0") if path}

    def has_unmerged_paths(self) -> bool:
        return bool(self.output("diff", "--name-only", "--diff-filter=U"))

    def upstream_divergence(self) -> tuple[int, int] | None:
        status = self.output("status", "--porcelain=v2", "--branch")
        for line in status.splitlines():
            if line.startswith("# branch.ab "):
                ahead_text, behind_text = line.removeprefix("# branch.ab ").split()
                return int(ahead_text), abs(int(behind_text))
        return None

    def head_is_published(self) -> bool:
        if self.current_branch() is None:
            return False
        upstream = self.run("rev-parse", "--verify", "@{upstream}", check=False)
        if upstream.returncode != 0:
            return False
        return self.run("merge-base", "--is-ancestor", "HEAD", "@{upstream}", check=False).returncode == 0

    def head_is_upstream_tip(self) -> bool:
        upstream = self.run("rev-parse", "--verify", "@{upstream}", check=False)
        return upstream.returncode == 0 and self.head() == upstream.stdout.strip()

    def tracked(self, path: str) -> bool:
        literal_path = f":(literal){path}"
        result = self.output("ls-tree", "-r", "--name-only", "HEAD", "--", literal_path)
        return path in result.splitlines()

    def path_has_changes(self, path: str) -> bool:
        literal_path = f":(literal){path}"
        return bool(
            self.output(
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
                "--",
                literal_path,
            )
        )

    def repository_ignore_source(self, path: str) -> str | None:
        result = self.run("check-ignore", "--verbose", "--no-index", "--", path, check=False)
        if result.returncode != 0 or not result.stdout:
            return None
        metadata = result.stdout.split("\t", 1)[0]
        parts = metadata.rsplit(":", 2)
        if len(parts) != 3:
            return None
        source = Path(parts[0])
        candidate = source if source.is_absolute() else self.root / source
        try:
            relative = candidate.resolve().relative_to(self.root)
        except ValueError:
            return None
        if relative.name != ".gitignore":
            return None
        return relative.as_posix()

    def index_tracks(self, path: str) -> bool:
        literal_path = f":(literal){path}"
        return self.run("ls-files", "--error-unmatch", "--", literal_path, check=False).returncode == 0

    def has_unstaged_changes(self, path: str) -> bool:
        literal_path = f":(literal){path}"
        return self.run("diff", "--quiet", "--", literal_path, check=False).returncode != 0

    def reflog(self, limit: int = 15) -> list[tuple[str, str, str]]:
        result = self.output(
            "reflog",
            "show",
            f"-{limit}",
            "--format=%H%x09%gd%x09%gs",
        )
        entries: list[tuple[str, str, str]] = []
        for line in result.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                entries.append((parts[0], parts[1], parts[2]))
        return entries


class GitExecutor:
    """The only component permitted to execute mutating Git commands."""

    def __init__(self, repository: GitRepository) -> None:
        self.repository = repository

    def execute(
        self,
        plan: RecoveryPlan,
        *,
        confirmed: bool,
        dry_run: bool = False,
    ) -> list[str]:
        displays = [command.display for command in plan.commands]
        if dry_run:
            return displays
        if any(command.state_changing for command in plan.commands) and not confirmed:
            from git_panic.models import ConfirmationRequired

            raise ConfirmationRequired("Explicit confirmation is required before changing repository state.")

        completed: list[str] = []
        for command in plan.commands:
            if isinstance(command, FileAppend):
                target = self.repository.root / command.path
                try:
                    existing = target.read_text(encoding="utf-8") if target.exists() else ""
                    prefix = "" if not existing or existing.endswith("\n") else "\n"
                    with target.open("a", encoding="utf-8") as stream:
                        stream.write(f"{prefix}{command.line}\n")
                except OSError as error:
                    raise CommandError(command.display, str(error), 1) from error
            else:
                self.repository.run(*command.args)
            completed.append(command.display)
        return completed
