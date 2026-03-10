"""Voyage Rerank-2.5 second-pass reranker."""

import httpx

from open_brain.config import get_config

VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"


async def rerank(
    query: str,
    documents: list[str],
    model: str,
    top_k: int | None = None,
) -> list[int]:
    """Rerank documents by relevance to a query using the Voyage Rerank API.

    Args:
        query: The search query.
        documents: List of document strings to rerank.
        model: Voyage rerank model name (e.g. "rerank-2.5").
        top_k: If set, return only the top-K indices. Otherwise return all.

    Returns:
        List of original document indices in descending relevance order.

    Raises:
        RuntimeError: If the Voyage API returns a non-success status.
    """
    config = get_config()

    payload: dict = {
        "query": query,
        "documents": documents,
        "model": model,
    }
    if top_k is not None:
        payload["top_k"] = top_k

    async with httpx.AsyncClient() as client:
        response = await client.post(
            VOYAGE_RERANK_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.VOYAGE_API_KEY}",
            },
            json=payload,
            timeout=30.0,
        )

    if not response.is_success:
        raise RuntimeError(f"Voyage Rerank API error {response.status_code}: {response.text}")

    data = response.json()
    # data["data"] is sorted by relevance_score descending already
    return [item["index"] for item in data["data"]]
