"""Screen-based M2 automation primitives.

The package deliberately keeps recognition, window binding, and action policy
separate.  This makes the safety checks testable without opening a browser and
keeps the M2 mock page independent from the eventual intranet profile.
"""

from .modes import Action, ExecutionResult, Mode, ModeRunner
from .engine import (
    AttachmentSnapshot,
    AutomationEngine,
    AutomationError,
    AutomationReport,
    InMemoryPageAdapter,
    InputBackend,
    PageAdapter,
    PageSnapshot,
    ScreenActionExecutor,
    StepResult,
    StopRequested,
    VerificationStatus,
    Win32InputBackend,
    run_m3_poc,
)
from .profile import ControlSpec, PageProfile, load_profile
from .recognizer import (
    AnchorRecognizer,
    BoundingBox,
    RecognitionResult,
    TemplateMatcher,
    TextObservation,
)
from .window import WindowBinding, WindowBinder, WindowRect

__all__ = [
    "Action",
    "AttachmentSnapshot",
    "AnchorRecognizer",
    "AutomationEngine",
    "AutomationError",
    "AutomationReport",
    "BoundingBox",
    "ControlSpec",
    "ExecutionResult",
    "InMemoryPageAdapter",
    "InputBackend",
    "Mode",
    "ModeRunner",
    "PageProfile",
    "PageAdapter",
    "PageSnapshot",
    "RecognitionResult",
    "ScreenActionExecutor",
    "StepResult",
    "StopRequested",
    "TemplateMatcher",
    "TextObservation",
    "WindowBinding",
    "WindowBinder",
    "WindowRect",
    "VerificationStatus",
    "Win32InputBackend",
    "load_profile",
    "run_m3_poc",
]
