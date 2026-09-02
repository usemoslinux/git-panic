from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from git_panic.diagnosis import DiagnosisEngine
from git_panic.git import GitExecutor, GitRepository
from git_panic.models import GitPanicError, RecoveryPlan
from git_panic.safety import SafetyValidator
from git_panic.workflows import RecoveryPlanner

app = typer.Typer(
    name="git-panic",
    help="Safely recover from common local Git mistakes.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()


def _show_plan(plan: RecoveryPlan, dry_run: bool) -> None:
    label = "DRY RUN: no commands will execute" if dry_run else "Review before execution"
    console.print(Panel(plan.summary, title=plan.title, subtitle=label, border_style="cyan"))
    for warning in plan.warnings:
        console.print(Panel(warning, title="Important", border_style="bold red"))
    table = Table(title="Recovery commands")
    table.add_column("Step", justify="right", style="bold cyan")
    table.add_column("Why")
    table.add_column("Exact command", style="yellow")
    for index, command in enumerate(plan.commands, start=1):
        table.add_row(str(index), command.explanation, command.display)
    console.print(table)
    if plan.backup_ref:
        console.print(f"Safety branch: [bold green]{plan.backup_ref}[/bold green]")
    else:
        console.print("[bold red]Warning: safety branch creation is disabled.[/bold red]")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print commands without executing them."),
    safety_branch: bool = typer.Option(
        True,
        "--safety-branch/--no-safety-branch",
        help="Create a rescue branch before executing recovery commands.",
    ),
    repository: Path = typer.Option(
        Path("."),
        "--repo",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Git working tree to inspect.",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    try:
        repo = GitRepository(repository)
        validator = SafetyValidator(repo)
        validator.validate_preflight()
        planner = RecoveryPlanner(repo, validator, create_safety_branch=safety_branch)
        plan = DiagnosisEngine(repo, planner, console).diagnose()
        _show_plan(plan, dry_run)

        executor = GitExecutor(repo)
        if dry_run:
            executor.execute(plan, confirmed=False, dry_run=True)
            console.print("[bold cyan]Dry run complete. Repository state was not changed.[/bold cyan]")
            return

        confirmed = Confirm.ask(
            "Execute every command above in order?",
            default=False,
            console=console,
        )
        if not confirmed:
            console.print("[yellow]Cancelled. No commands were executed.[/yellow]")
            raise typer.Exit(0)

        executor.execute(plan, confirmed=True)
        result_message = "Recovery completed."
        if plan.backup_ref:
            result_message += f" Keep [bold]{plan.backup_ref}[/bold] until you have verified the result."
        console.print(
            Panel(
                result_message,
                border_style="green",
            )
        )
    except GitPanicError as error:
        console.print(Panel(str(error), title="Git-Panic stopped safely", border_style="red"))
        raise typer.Exit(1) from error
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled. No further commands were executed.[/yellow]")
        raise typer.Exit(130)


if __name__ == "__main__":
    app()
