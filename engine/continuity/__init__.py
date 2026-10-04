"""Continuity: model-agnostic semantic state for long-horizon agents."""

from .runtime import ConflictError, Continuity, ContinuityError

__all__ = ["Continuity", "ContinuityError", "ConflictError"]
__version__ = "0.1.0"
