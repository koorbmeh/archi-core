"""
Provides a thin integration bridge that monkey-patches the kernel's model_interface.call_model
to use the prompt cacher's cached_call_model for all future model invocations.
"""

import logging

from typing import Optional

import src.kernel.model_interface as model_interface
from src.kernel import capability_registry, model_interface as mi
from capabilities.just_thought_possibly_spare import get_cache

logger = logging.getLogger(__name__)

_patched_call_model = None
_patch_applied = False

def _create_patched_call_model() -> mi.ModelResponse:
    def patched_call_model(
        prompt