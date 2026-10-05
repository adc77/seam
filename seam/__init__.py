"""Simulation boundary. Inert in production. One seeded run in a separate process."""

from seam.artifact import tape_from_artifact
from seam.backend import Backend
from seam.dataset import dataset_ref
from seam.checkpoint import checkpoint_ref, write_checkpoint
from seam.errors import Fault, NoTimerBackend, PortError, Refuse
from seam.guard import install_guards
from seam.runtime import Runtime, in_sim, main, sim_env
from seam.runner import run_product
from seam.version import __version__
from seam.world import World, WorldContext
from seam.capture import (
    CaptureError, capture_bundle, capture_source, read_capture, run_capture,
    validate_capture, write_capture_case, write_capture_json,
)

__all__ = [
    "CaptureError",
    "capture_bundle",
    "capture_source",
    "read_capture",
    "run_capture",
    "validate_capture",
    "write_capture_case",
    "write_capture_json",
    "Fault",
    "Backend",
    "World",
    "WorldContext",
    "dataset_ref",
    "checkpoint_ref",
    "write_checkpoint",
    "run_product",
    "NoTimerBackend",
    "PortError",
    "Refuse",
    "Runtime",
    "__version__",
    "in_sim",
    "install_guards",
    "main",
    "sim_env",
    "tape_from_artifact",
]
