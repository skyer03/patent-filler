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
from .profile import ControlSpec, PageProfile, ReadbackSpec, load_profile
from .recognizer import (
    AnchorRecognizer,
    BoundingBox,
    RecognitionResult,
    PaddleTextDetector,
    TemplateMatcher,
    TextObservation,
)
from .window import WindowBinding, WindowBinder, WindowRect
from .screen_adapter import (
    ProfileScreenReadback,
    ScreenPageAdapter,
    Win32Clipboard,
    auto_update_profile_issues,
)

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
    "PaddleTextDetector",
    "RecognitionResult",
    "ReadbackSpec",
    "ScreenActionExecutor",
    "ScreenPageAdapter",
    "StepResult",
    "StopRequested",
    "TemplateMatcher",
    "TextObservation",
    "WindowBinding",
    "WindowBinder",
    "WindowRect",
    "VerificationStatus",
    "Win32InputBackend",
    "Win32Clipboard",
    "ProfileScreenReadback",
    "auto_update_profile_issues",
    "load_profile",
    "run_m3_poc",
]
