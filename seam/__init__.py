"""Simulation boundary. Inert in production. One seeded run in a separate process."""

from seam.artifact import tape_from_artifact
from seam.errors import Fault, NoTimerBackend, Refuse
from seam.guard import install_guards
from seam.runtime import Runtime, in_sim, main
from seam.version import __version__

__all__ = [
    "Fault",
    "NoTimerBackend",
    "Refuse",
    "Runtime",
    "__version__",
    "in_sim",
    "install_guards",
    "main",
    "tape_from_artifact",
]
