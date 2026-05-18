import logging
from typing import Any, Optional

from langchain_anthropic import ChatAnthropic

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model


log = logging.getLogger(__name__)


_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "max_tokens",
    "callbacks", "http_client", "http_async_client", "effort",
)


class NormalizedChatAnthropic(ChatAnthropic):
    """ChatAnthropic with normalized content output.

    Claude models with extended thinking or tool use return content as a
    list of typed blocks. This normalizes to string for consistent
    downstream handling.

    Also silently retries once without the ``effort`` parameter when the
    API rejects it. Anthropic only accepts ``effort`` on a subset of
    Claude models (extended-thinking tier); pre-flighting that with a
    static lookup is brittle, so we recover from the 400 instead of
    failing the whole pipeline.
    """

    def _retry_without_effort(self, exc: Exception) -> bool:
        """True if ``exc`` is the 'effort not supported' 400 and we can retry."""
        msg = str(exc).lower()
        return "effort" in msg and "does not support" in msg

    def invoke(self, input, config=None, **kwargs):
        try:
            return normalize_content(super().invoke(input, config, **kwargs))
        except Exception as e:
            if not self._retry_without_effort(e) or not getattr(self, "effort", None):
                raise
            log.warning(
                "Anthropic model %s rejected `effort`; retrying without it. "
                "Pick a different model (e.g. Claude Opus 4.7) or remove the "
                "effort setting to silence this warning.",
                getattr(self, "model", "?"),
            )
            self.effort = None
            return normalize_content(super().invoke(input, config, **kwargs))


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude models."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured ChatAnthropic instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Anthropic."""
        return validate_model("anthropic", self.model)
