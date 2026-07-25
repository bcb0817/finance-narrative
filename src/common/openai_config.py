"""OpenAIモデルの許可リストと役割別設定。秘密情報は扱わない。"""
from __future__ import annotations

import os
from enum import Enum


class OpenAIRole(str, Enum):
    GENERATE = "generate"
    REVIEW = "review"
    CLASSIFY = "classify"
    ANALYZE = "analyze"
    DEEP_ANALYZE = "deep_analyze"
    EMBED = "embed"
    MODERATE = "moderate"
    IMAGE = "image"
    FALLBACK = "fallback"


ALLOWED_MODELS = frozenset({
    "gpt-5.4-mini", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5-nano",
    "omni-moderation-latest", "text-embedding-3-small", "gpt-image-2", "gpt-5-mini",
})
ROLE_ENV_DEFAULTS = {
    OpenAIRole.GENERATE: ("OPENAI_GENERATE_MODEL", "gpt-5.4-mini"),
    OpenAIRole.REVIEW: ("OPENAI_REVIEW_MODEL", "gpt-5-nano"),
    OpenAIRole.CLASSIFY: ("OPENAI_CLASSIFICATION_MODEL", "gpt-5-nano"),
    OpenAIRole.ANALYZE: ("OPENAI_ANALYSIS_MODEL", "gpt-5.6-terra"),
    OpenAIRole.DEEP_ANALYZE: ("OPENAI_DEEP_ANALYSIS_MODEL", "gpt-5.6-sol"),
    OpenAIRole.EMBED: ("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
    OpenAIRole.MODERATE: ("OPENAI_MODERATION_MODEL", "omni-moderation-latest"),
    OpenAIRole.IMAGE: ("OPENAI_IMAGE_MODEL", "gpt-image-2"),
    OpenAIRole.FALLBACK: ("OPENAI_FALLBACK_MODEL", "gpt-5-mini"),
}


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def model_for(role: OpenAIRole) -> str:
    env_name, default = ROLE_ENV_DEFAULTS[role]
    return os.getenv(env_name, default).strip() or default


def validate_models() -> list[str]:
    return [f"{ROLE_ENV_DEFAULTS[r][0]}={model_for(r)} is not allowed"
            for r in OpenAIRole if model_for(r) not in ALLOWED_MODELS]


def role_for_model(model: str) -> OpenAIRole:
    for role in OpenAIRole:
        if model == model_for(role):
            return role
    raise ValueError(f"OpenAI model is not configured for an allowed role: {model}")
