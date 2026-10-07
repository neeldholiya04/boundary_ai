"""boundary_guard: composable input/output filtering for tool-calling LLM agents."""

from boundary_guard.core.config import GuardConfig, PolicyConfig, load_config
from boundary_guard.core.detector import Detector, register_detector
from boundary_guard.core.pipeline import DecisionEvent, DecisionSink, Guard
from boundary_guard.core.types import (
    Action,
    CheckContext,
    Detection,
    Execution,
    GuardResult,
    Mode,
    OnError,
    PolicyDecision,
    Span,
    Stage,
)

__all__ = [
    "Action",
    "CheckContext",
    "DecisionEvent",
    "DecisionSink",
    "Detection",
    "Detector",
    "Execution",
    "Guard",
    "GuardConfig",
    "GuardResult",
    "Mode",
    "OnError",
    "PolicyConfig",
    "PolicyDecision",
    "Span",
    "Stage",
    "load_config",
    "register_detector",
]
