"""Identify the model provider independently of the terminal application."""
import re


def model_provider(model):
    """Recognize model families, not the harness or the wire protocol.

    Unknown aliases stay unknown. In particular, an Anthropic-compatible API
    does not establish that the served model or subscription is Anthropic's.
    """
    if not isinstance(model, str):
        return None
    name = model.lower().rsplit("/", 1)[-1]
    if name.startswith(("gpt-", "chatgpt-")) or re.match(r"o[1-9]\d*(?:-|$)", name):
        return "openai"
    if name.startswith("claude-"):
        return "anthropic"
    if name.startswith("kimi-"):
        return "moonshot"
    return None
