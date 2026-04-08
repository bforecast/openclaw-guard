"""Allow `python -m guard ...` to dispatch to the Typer CLI."""

from guard.cli import app

if __name__ == "__main__":
    app()
