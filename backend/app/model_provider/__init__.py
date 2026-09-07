"""Model Provider package — OpenAI-compatible connection-test adapters."""

from app.model_provider.client import ModelProviderClient
from app.model_provider.openai_compatible import OPENAI_COMPATIBLE_PROVIDER

__all__ = ["ModelProviderClient", "OPENAI_COMPATIBLE_PROVIDER"]
