"""PrintDAO CLI module."""

# Lazy imports to avoid RuntimeWarning when running as __main__
__all__ = ["PrintDAOCLI", "run"]


def __getattr__(name):
    """Lazy import to avoid circular import issues."""
    if name in ("PrintDAOCLI", "run"):
        from .simple import PrintDAOCLI, run
        return PrintDAOCLI if name == "PrintDAOCLI" else run
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
