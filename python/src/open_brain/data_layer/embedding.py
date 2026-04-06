"""Voyage-4 embedding integration."""

import asyncio

import httpx

from open_brain.config import get_config

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
EMBEDDING_DIM = 1024
BATCH_SIZE = 64


async def embed_with_usage(text: str) -> tuple[list[float], int]:
    """Embed a single text using Voyage API (document type), returning token usage.

    Args:
        text: Text to embed

    Returns:
        Tuple of (embedding vector, total_tokens used)
    """
    config = get_config()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            VOYAGE_API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.VOYAGE_API_KEY}",
            },
            json={
                "model": config.VOYAGE_MODEL,
                "input": [text],
                "input_type": "document",
                "output_dimension": EMBEDDING_DIM,
            },
            timeout=30.0,
        )
    if not response.is_success:
        raise RuntimeError(f"Voyage API error {response.status_code}: {response.text}")

    data = response.json()
    token_count: int = data.get("usage", {}).get("total_tokens", 0)
    return data["data"][0]["embedding"], token_count


async def embed(text: str) -> list[float]:
    """Embed a single text using Voyage API (document type).

    Args:
        text: Text to embed

    Returns:
        Embedding vector as list of floats
    """
    embedding, _ = await embed_with_usage(text)
    return embedding


async def embed_query_with_usage(text: str) -> tuple[list[float], int]:
    """Embed a query text (uses 'query' input_type), returning token usage.

    Args:
        text: Query text to embed

    Returns:
        Tuple of (embedding vector, total_tokens used)
    """
    config = get_config()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            VOYAGE_API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.VOYAGE_API_KEY}",
            },
            json={
                "model": config.VOYAGE_MODEL,
                "input": [text],
                "input_type": "query",
                "output_dimension": EMBEDDING_DIM,
            },
            timeout=30.0,
        )
    if not response.is_success:
        raise RuntimeError(f"Voyage API error {response.status_code}: {response.text}")

    data = response.json()
    token_count: int = data.get("usage", {}).get("total_tokens", 0)
    return data["data"][0]["embedding"], token_count


async def embed_query(text: str) -> list[float]:
    """Embed a query text (uses 'query' input_type for asymmetric retrieval).

    Args:
        text: Query text to embed

    Returns:
        Embedding vector as list of floats
    """
    embedding, _ = await embed_query_with_usage(text)
    return embedding


async def embed_batch_with_usage(texts: list[str]) -> tuple[list[list[float]], int]:
    """Batch embed multiple texts, returning embeddings and total token usage.

    Args:
        texts: List of texts to embed

    Returns:
        Tuple of (list of embedding vectors, total tokens used across all batches)
    """
    config = get_config()
    results: list[list[float]] = []
    total_tokens: int = 0

    async with httpx.AsyncClient() as client:
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            response = await client.post(
                VOYAGE_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {config.VOYAGE_API_KEY}",
                },
                json={
                    "model": config.VOYAGE_MODEL,
                    "input": batch,
                    "input_type": "document",
                    "output_dimension": EMBEDDING_DIM,
                },
                timeout=60.0,
            )
            if not response.is_success:
                raise RuntimeError(
                    f"Voyage batch embed error {response.status_code}: {response.text}"
                )
            data = response.json()
            results.extend(d["embedding"] for d in data["data"])
            total_tokens += data.get("usage", {}).get("total_tokens", 0)

            # Rate limit: small delay between batches
            if i + BATCH_SIZE < len(texts):
                await asyncio.sleep(0.2)

    return results, total_tokens


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts (max ~128 per batch).

    Args:
        texts: List of texts to embed

    Returns:
        List of embedding vectors
    """
    results, _ = await embed_batch_with_usage(texts)
    return results


def to_pg_vector(embedding: list[float]) -> str:
    """Format embedding as pgvector literal string.

    Args:
        embedding: List of float values

    Returns:
        pgvector-compatible string like '[0.1,0.2,...]'
    """
    return f"[{','.join(str(x) for x in embedding)}]"
