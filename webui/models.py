"""Pydantic models for web-UI form submission.

Mirrors the keys that `cli.utils.get_user_selections` returns so the same
config-building code is reusable across CLI and web flows.
"""

from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

from cli.models import AnalystType


# Provider catalog. (display, key, default_base_url). Mirrors
# `cli.utils.select_llm_provider`'s PROVIDERS list — kept here so the web
# UI doesn't depend on a CLI-private helper. Region-split entries
# (qwen-cn / glm-cn / minimax-cn) are added separately so the dropdown
# matches the post-region-prompt CLI keys 1:1.
PROVIDERS: List[tuple[str, str, Optional[str]]] = [
    ("OpenAI",                  "openai",     "https://api.openai.com/v1"),
    ("Google",                  "google",     None),
    ("Anthropic",               "anthropic",  "https://api.anthropic.com/"),
    ("xAI",                     "xai",        "https://api.x.ai/v1"),
    ("DeepSeek",                "deepseek",   "https://api.deepseek.com"),
    ("Qwen (international)",    "qwen",       "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    ("Qwen (China)",            "qwen-cn",    "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("GLM via Z.AI (international)", "glm",   "https://api.z.ai/api/paas/v4/"),
    ("GLM via BigModel (China)",     "glm-cn", "https://open.bigmodel.cn/api/paas/v4/"),
    ("MiniMax (Global)",        "minimax",    "https://api.minimax.io/v1"),
    ("MiniMax (China)",         "minimax-cn", "https://api.minimaxi.com/v1"),
    ("OpenRouter",              "openrouter", "https://openrouter.ai/api/v1"),
    ("Azure OpenAI",            "azure",      None),
    # Default endpoint is http://localhost:11434 — the `/v1` suffix gets
    # appended by openai_client._resolve_provider_base_url(). Override
    # for a remote ollama-serve via OLLAMA_BASE_URL.
    ("Ollama (local - http://localhost:11434)",  "ollama",     None),
]


PROVIDER_BASE_URL = {key: url for _, key, url in PROVIDERS}


class RunSelections(BaseModel):
    """Form payload for starting an analysis run."""

    ticker: str = Field(min_length=1, max_length=32)
    analysis_date: str  # YYYY-MM-DD
    analysts: List[str] = Field(default_factory=lambda: [a.value for a in AnalystType])
    llm_provider: str
    quick_thinker: str
    deep_thinker: str
    research_depth: int = Field(default=1, ge=1, le=10)
    output_language: str = "English"

    # Provider-specific effort knobs. None lets the client choose its default.
    openai_reasoning_effort: Optional[str] = None
    anthropic_effort: Optional[str] = None
    google_thinking_level: Optional[str] = None

    # When True, skip the 7-day cache lookup and run the full pipeline
    # even if a recent analysis exists for this ticker+date.
    force_refresh: bool = False

    @field_validator("analysts")
    @classmethod
    def _validate_analysts(cls, v: List[str]) -> List[str]:
        valid = {a.value for a in AnalystType}
        bad = [a for a in v if a not in valid]
        if bad:
            raise ValueError(f"Unknown analyst key(s): {bad}")
        if not v:
            raise ValueError("Select at least one analyst.")
        return v

    @field_validator("llm_provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        if v not in PROVIDER_BASE_URL:
            raise ValueError(f"Unknown provider: {v}")
        return v
