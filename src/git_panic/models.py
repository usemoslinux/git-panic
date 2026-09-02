from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum


class WorkflowKind(str, Enum):
    WRONG_BRANCH = "wrong-branch"
    AMEND_CHANGES = "amend-changes"
    SENSITIVE_FILE = "sensitive-file"
    UNDO_KEEP_CHANGES = "undo-keep-changes"
    FIX_MESSAGE = "fix-message"
    DISCARD_CHANGES = "discard-changes"
    DELETED_FILE = "deleted-file"
    REVERT_PUBLISHED = "revert-published"
    REFLOG_RESCUE = "reflog-rescue"


@dataclass(frozen=True)
class GitCommand:
    args: tuple[str, ...]
    explanation: str
    state_changing: bool = True

    @property
    def display(self) -> str:
        return shlex.join(("git", *self.args))


@dataclass(frozen=True)
class FileAppend:
    path: str
    line: str
    explanation: str
    state_changing: bool = True

    @property
    def display(self) -> str:
        return f"append {self.line!r} to {self.path}"


@dataclass(frozen=True)
class RecoveryPlan:
    workflow: WorkflowKind
    title: str
    summary: str
    backup_ref: str | None
    commands: tuple[GitCommand | FileAppend, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReflogEntry:
    commit: str
    selector: str
    subject: str


class GitPanicError(RuntimeError):
    """Base error for errors that should be shown without a traceback."""


class SafetyError(GitPanicError):
    """Raised when repository state makes an operation unsafe."""


class CommandError(GitPanicError):
    def __init__(self, command: str, detail: str, returncode: int) -> None:
        super().__init__(f"{command} failed ({returncode}): {detail}")
        self.command = command
        self.detail = detail
        self.returncode = returncode


class ConfirmationRequired(GitPanicError):
    """Raised when a mutating plan was not explicitly confirmed."""
