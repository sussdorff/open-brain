"""LLM provider abstraction: Anthropic and OpenRouter."""

from dataclasses import dataclass

import httpx

from open_brain.config import get_config


@dataclass
class LlmMessage:
    """A single message in a conversation."""

    role: str  # "user" | "assistant"
    content: str


async def llm_complete(
    messages: list[LlmMessage],
    model: str | None = None,
    max_tokens: int = 1024,
) -> str:
    """Send a message to the configured LLM provider.

    Args:
        messages: List of conversation messages
        model: Override the configured model
        max_tokens: Maximum tokens to generate

    Returns:
        Text response from the LLM
    """
    config = get_config()
    resolved_model = model or config.LLM_MODEL

    match config.LLM_PROVIDER:
        case "openrouter":
            return await _call_openrouter(messages, resolved_model, max_tokens)
        case _:
            return await _call_anthropic(messages, resolved_model, max_tokens)


async def _call_anthropic(
    messages: list[LlmMessage],
    model: str,
    max_tokens: int,
) -> str:
    """Call the Anthropic API."""
    config = get_config()
    if not config.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY required when LLM_PROVIDER=anthropic")

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

    if not response.is_success:
        raise RuntimeError(f"Anthropic API error {response.status_code}: {response.text}")

    data = response.json()
    content = data.get("content", [])
    return content[0].get("text", "") if content else ""


async def _call_openrouter(
    messages: list[LlmMessage],
    model: str,
    max_tokens: int,
) -> str:
    """Call the OpenRouter API."""
    config = get_config()
    if not config.OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY required when LLM_PROVIDER=openrouter")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
            },
            timeout=60.0,
        )

    if not response.is_success:
        raise RuntimeError(f"OpenRouter API error {response.status_code}: {response.text}")

    data = response.json()
    choices = data.get("choices", [])
    return choices[0]["message"]["content"] if choices else ""
