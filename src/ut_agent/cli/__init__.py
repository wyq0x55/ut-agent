"""Command routing for the public ``ut-agent`` CLI."""

__all__ = ["main"]


def main(argv=None):
    from .main import main as dispatch
    return dispatch(argv)
