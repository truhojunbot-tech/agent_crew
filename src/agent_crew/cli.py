import click


@click.group()
def crew():
    """agent_crew — multi-agent development crew CLI."""


@crew.command()
@click.argument("project")
def setup(project: str):
    """Configure environment for PROJECT."""
    click.echo(f"Setting up project: {project}")


@crew.command()
def status():
    """Show status of running projects."""
    click.echo("No projects currently running.")


@crew.command()
@click.argument("task")
def run(task: str):
    """Run TASK. TASK must not be empty."""
    if not task.strip():
        raise click.UsageError("task must not be empty")
    click.echo(f"Running task: {task}")


@crew.command()
@click.argument("topic")
def discuss(topic: str):
    """Start a panel discussion on TOPIC. TOPIC must not be empty."""
    if not topic.strip():
        raise click.UsageError("topic must not be empty")
    click.echo(f"Starting discussion on: {topic}")


@crew.command()
def teardown():
    """Tear down the current environment."""
    click.echo("Teardown complete.")
