from __future__ import annotations

from rich import box
from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from git_panic.git import GitRepository
from git_panic.models import RecoveryPlan, SafetyError, WorkflowKind
from git_panic.workflows import RecoveryPlanner


MENU_GROUPS: tuple[tuple[str, tuple[tuple[int, str, WorkflowKind, bool | None], ...]], ...] = (
    (
        "UNCOMMITTED WORK",
        (
            (1, "I want to discard all changes I have not committed yet", WorkflowKind.DISCARD_CHANGES, None),
            (2, "I deleted a local file and want it back", WorkflowKind.DELETED_FILE, None),
        ),
    ),
    (
        "LAST COMMIT, NOT SHARED",
        (
            (3, "I committed on the wrong branch", WorkflowKind.WRONG_BRANCH, None),
            (4, "I forgot a small change in my last commit", WorkflowKind.AMEND_CHANGES, None),
            (5, "I need to change my last commit message", WorkflowKind.FIX_MESSAGE, None),
            (6, "I committed sensitive information, but have not pushed it yet", WorkflowKind.SENSITIVE_FILE, False),
            (7, "I want to undo my last commit but keep the changes", WorkflowKind.UNDO_KEEP_CHANGES, None),
        ),
    ),
    (
        "PUSHED OR PUBLISHED HISTORY",
        (
            (8, "I committed and pushed sensitive information", WorkflowKind.SENSITIVE_FILE, True),
            (9, "I need to undo the latest published commit safely", WorkflowKind.REVERT_PUBLISHED, None),
        ),
    ),
    (
        "GO BACK IN TIME",
        (
            (10, "I need to recover an earlier state", WorkflowKind.REFLOG_RESCUE, None),
        ),
    ),
)


class DiagnosisEngine:
    def __init__(self, repository: GitRepository, planner: RecoveryPlanner, console: Console) -> None:
        self.repository = repository
        self.planner = planner
        self.console = console

    def diagnose(self) -> RecoveryPlan:
        choices = {
            number: (workflow, published)
            for _, scenarios in MENU_GROUPS
            for number, _, workflow, published in scenarios
        }
        self.console.print("[bold]What happened?[/bold]")
        self.console.print("Choose the situation that best matches your repository.\n")
        for group, scenarios in MENU_GROUPS:
            self.console.print(f"[bold cyan]{group}[/bold cyan]")
            table = Table(show_header=False, box=box.SIMPLE, pad_edge=False)
            table.add_column("Choice", style="bold cyan", justify="right", width=2)
            table.add_column("Situation")
            for number, label, _, _ in scenarios:
                table.add_row(str(number), label)
            self.console.print(table)
        choice = IntPrompt.ask(
            "Choose what happened",
            choices=[str(number) for number in choices],
        )
        workflow, published = choices[choice]

        if workflow is WorkflowKind.WRONG_BRANCH:
            destination = Prompt.ask("Name for the new branch that should keep the commit")
            return self.planner.wrong_branch(destination)
        if workflow is WorkflowKind.AMEND_CHANGES:
            return self.planner.amend_changes()
        if workflow is WorkflowKind.SENSITIVE_FILE:
            path = Prompt.ask("Repository-relative path of the sensitive file")
            return self.planner.sensitive_file(path, published=bool(published))
        if workflow is WorkflowKind.UNDO_KEEP_CHANGES:
            state = IntPrompt.ask(
                "Keep the former commit's changes as [1] staged or [2] unstaged",
                choices=["1", "2"],
                default=2,
            )
            return self.planner.undo_keep_changes(staged=state == 1)
        if workflow is WorkflowKind.FIX_MESSAGE:
            message = Prompt.ask("New commit message")
            return self.planner.fix_message(message)
        if workflow is WorkflowKind.DISCARD_CHANGES:
            scope = IntPrompt.ask(
                "Set aside changes from [1] one path or [2] the entire working tree",
                choices=["1", "2"],
                default=1,
            )
            path = Prompt.ask("Repository-relative path") if scope == 1 else None
            return self.planner.discard_changes(path)
        if workflow is WorkflowKind.DELETED_FILE:
            path = Prompt.ask("Repository-relative path of the deleted file")
            return self.planner.deleted_file(path)
        if workflow is WorkflowKind.REVERT_PUBLISHED:
            return self.planner.revert_published_head()
        return self._reflog_plan()

    def _reflog_plan(self) -> RecoveryPlan:
        entries = self.repository.reflog()
        if not entries:
            raise SafetyError("No reflog entries are available for recovery.")
        table = Table(title="Recent reflog entries")
        table.add_column("Choice", style="bold cyan", justify="right")
        table.add_column("Commit", style="yellow")
        table.add_column("Selector")
        table.add_column("Action")
        for index, (commit, selector, subject) in enumerate(entries, start=1):
            table.add_row(str(index), commit[:10], selector, subject)
        self.console.print(table)
        choice = IntPrompt.ask(
            "Choose the state to recover",
            choices=[str(i) for i in range(1, len(entries) + 1)],
        )
        destination = Prompt.ask("Name for the new recovery branch", default="recovered-work")
        return self.planner.reflog_rescue(entries[choice - 1][0], destination)
