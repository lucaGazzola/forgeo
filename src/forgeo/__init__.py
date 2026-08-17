"""Forgeo: a scheduled software forgeo that executes backlog tasks
on the main branch and refactors when idle."""

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("forgeo-cli")
except PackageNotFoundError:  # standalone binary: no installed package metadata
    __version__ = "0.7.2"
