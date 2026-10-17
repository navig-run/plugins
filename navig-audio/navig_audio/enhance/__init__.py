"""Audio Enhance — restore and master existing recordings without changing what they are.

``analysis`` measures (deterministic DSP), ``planner`` decides what to do and why,
``engine`` renders + exports without touching the original, ``verify`` proves the
result is still the same recording. Modes and their typed settings are universal
presets (tool ``audio.enhance``) shipped in ``navig_audio/presets/``.
"""

from navig_audio.enhance.analysis import AnalyzeError, AudioReport, Finding, analyze
from navig_audio.enhance.engine import Cancelled, EnhanceError, EnhanceResult, enhance
from navig_audio.enhance.planner import MODES, Plan, plan

__all__ = [
    "MODES", "AnalyzeError", "AudioReport", "Cancelled", "EnhanceError", "EnhanceResult", "Finding",
    "Plan", "analyze", "enhance", "plan",
]
