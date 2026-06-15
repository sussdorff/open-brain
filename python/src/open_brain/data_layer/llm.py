"""LLM provider abstraction: Anthropic and OpenRouter."""

import logging
import time
from dataclasses import dataclass

import httpx

from open_brain.config import get_config

logger = logging.getLogger(__name__)

_SLOW_LLM_THRESHOLD_MS = 5000


@dataclass
class LlmMessage:
    """A single message in a conversation."""

    role: str  # "user" | "assistant"
    content: str


async def llm_complete(
    messages: list[LlmMessage],
    model: str | None = None,
    max_tokens: int = 1024,
    response_format: dict | None = None,
    disable_reasoning: bool = False,
) -> str:
    """Send a message to the configured LLM provider.

    Args:
        messages: List of conversation messages
        model: Override the configured model
        max_tokens: Maximum tokens to generate
        response_format: Optional OpenRouter response_format (e.g.
            {"type": "json_object"} or a json_schema block) to enforce valid
            structured output. OpenRouter-only; ignored by the Anthropic path.
        disable_reasoning: When True, ask reasoning-capable models to skip
            chain-of-thought so thinking tokens don't consume the output
            budget. OpenRouter-only.

    Returns:
        Text response from the LLM
    """
    config = get_config()
    resolved_model = model or config.LLM_MODEL

    match config.LLM_PROVIDER:
        case "openrouter":
            return await _call_openrouter(
                messages,
                resolved_model,
                max_tokens,
                response_format=response_format,
                disable_reasoning=disable_reasoning,
            )
        case _:
            return await _call_anthropic(messages, resolved_model, max_tokens)


def _openrouter_provider_routing(config) -> dict | None:
    """Build the OpenRouter `provider` routing block from config.

    Enforces the configured data-collection policy (default "deny" = only
    providers with zero prompt retention) and an optional provider preference
    order. Returns None when no routing constraints apply.

    Note: we deliberately do NOT set `require_parameters`. It would force every
    request parameter (including the `reasoning` hint) to be supported by the
    provider, which excludes non-reasoning models like gpt-4.1-nano whose
    endpoints don't advertise a `reasoning` parameter (OpenRouter then returns
    404 "No endpoints found that can handle the requested parameters"). Without
    it, OpenRouter simply drops unsupported params for the chosen provider, and
    our callers already degrade gracefully if structured output isn't honored.
    """
    provider: dict = {}
    if config.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = config.OPENROUTER_DATA_COLLECTION
    if config.OPENROUTER_PROVIDER_ORDER:
        order = [p.strip() for p in config.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
        if order:
            provider["order"] = order
            provider["allow_fallbacks"] = True
    return provider or None


async def _call_anthropic(
    messages: list[LlmMessage],
    model: str,
    max_tokens: int,
) -> str:
    """Call the Anthropic API."""
    config = get_config()
    if not config.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY required when LLM_PROVIDER=anthropic")

    t0 = time.monotonic()
    logger.debug(
        "llm_http_start provider=anthropic model=%r max_tokens=%d messages=%d",
        model, max_tokens, len(messages),
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
            },
            timeout=60.0,
        )

    duration_ms = int((time.monotonic() - t0) * 1000)

    if not response.is_success:
        if duration_ms > _SLOW_LLM_THRESHOLD_MS:
            logger.warning(
                "slow_llm_call provider=anthropic model=%r duration_ms=%d",
                model, duration_ms,
            )
        logger.error(
            "llm_http_error provider=anthropic status=%d body=%r",
            response.status_code, response.text[:200],
        )
        raise RuntimeError(f"Anthropic API error {response.status_code}: {response.text}")

    logger.info(
        "llm_http_end provider=anthropic status=%d duration_ms=%d response_bytes=%d",
        response.status_code, duration_ms, len(response.content),
    )
    if duration_ms > _SLOW_LLM_THRESHOLD_MS:
        logger.warning(
            "slow_llm_call provider=anthropic model=%r duration_ms=%d",
            model, duration_ms,
        )

    data = response.json()
    content = data.get("content", [])
    return content[0].get("text", "") if content else ""


async def _call_openrouter(
    messages: list[LlmMessage],
    model: str,
    max_tokens: int,
    response_format: dict | None = None,
    disable_reasoning: bool = False,
) -> str:
    """Call the OpenRouter API."""
    config = get_config()
    if not config.OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY required when LLM_PROVIDER=openrouter")

    t0 = time.monotonic()
    logger.debug(
        "llm_http_start provider=openrouter model=%r max_tokens=%d messages=%d",
        model, max_tokens, len(messages),
    )

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    if response_format is not None:
        body["response_format"] = response_format
    if disable_reasoning:
        # Suppress chain-of-thought on reasoning-capable models so thinking
        # tokens don't eat into max_tokens (a frequent cause of truncated JSON).
        body["reasoning"] = {"enabled": False}
    provider = _openrouter_provider_routing(config)
    if provider:
        body["provider"] = provider

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            },
            json=body,
            timeout=60.0,
        )

    duration_ms = int((time.monotonic() - t0) * 1000)

    if not response.is_success:
        if duration_ms > _SLOW_LLM_THRESHOLD_MS:
            logger.warning(
                "slow_llm_call provider=openrouter model=%r duration_ms=%d",
                model, duration_ms,
            )
        logger.error(
            "llm_http_error provider=openrouter status=%d body=%r",
            response.status_code, response.text[:200],
        )
        raise RuntimeError(f"OpenRouter API error {response.status_code}: {response.text}")

    logger.info(
        "llm_http_end provider=openrouter status=%d duration_ms=%d response_bytes=%d",
        response.status_code, duration_ms, len(response.content),
    )
    if duration_ms > _SLOW_LLM_THRESHOLD_MS:
        logger.warning(
            "slow_llm_call provider=openrouter model=%r duration_ms=%d",
            model, duration_ms,
        )

    data = response.json()
    choices = data.get("choices", [])
    return choices[0]["message"]["content"] if choices else ""
