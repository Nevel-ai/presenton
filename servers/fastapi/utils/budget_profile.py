"""Fail before paid work when deployment settings escape the metered profile."""
import logging
import os

from utils.budget import PresentationBudgetError


def assert_budget_profile(run):
    if run is None:
        return
    run.ensure_active()
    supported = (os.getenv("CAN_CHANGE_KEYS") == "false"
                 and os.getenv("LLM") == "openai"
                 and os.getenv("OPENAI_MODEL") == "gpt-5.6-luna"
                 and os.getenv("IMAGE_PROVIDER") == "ideogram")
    if supported:
        return
    if run.mode == "shadow":
        logging.getLogger(__name__).warning("Shadow presentation configuration is outside the metered profile")
        return
    run.deny("budget_invalid_transition")
    raise PresentationBudgetError("budget_invalid_transition")
