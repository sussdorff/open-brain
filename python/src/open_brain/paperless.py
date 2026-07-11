"""Paperless-ngx retrieval client for memory document references.

This module is intentionally outside the IngestAdapter registry. It performs
lookup-by-id for the MCP tool layer, not poll-loop ingestion, and therefore
does not implement list_recent(), ingest(), run_id threading, or register().
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx

from open_brain.config import get_config

PaperlessResolveStatus = Literal[
    "found",
    "not_found",
    "unauthorized",
    "malformed",
    "transport_error",
    "not_configured",
]

_PAPERLESS_TIMEOUT_SECONDS = 10.0


@dataclass
class PaperlessResolveResult:
    """Result of resolving a Paperless document reference."""

    status: PaperlessResolveStatus
    document_id: int | None = None
    title: str | None = None
    mime_type: str | None = None
    added: str | None = None
    retrieval_targets: dict[str, str] | None = None
    error: str | None = None


class PaperlessClient:
    """Retrieval client for Paperless-ngx document references.

    Not an IngestAdapter (see ingest/adapters/base.py) — this is a
    synchronous lookup-by-id client composed directly by the MCP tool
    layer (server.py), not driven by the ingest orchestrator's poll loop.
    Intentionally does not call register().
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        config = get_config()
        self._base_url = config.PAPERLESS_BASE_URL if base_url is None else base_url
        self._api_token = config.PAPERLESS_API_TOKEN if api_token is None else api_token
        self._http_client = http_client

    async def resolve_reference(self, document_id: int) -> PaperlessResolveResult:
        """Resolve Paperless metadata and retrieval targets for a document id."""
        if (
            not isinstance(document_id, int)
            or isinstance(document_id, bool)
            or document_id <= 0
        ):
            return PaperlessResolveResult(
                status="malformed",
                error="document_id must be a positive integer",
            )

        base_url = self._base_url.strip().rstrip("/") if self._base_url else ""
        api_token = self._api_token.strip() if self._api_token else ""
        if not base_url or not api_token:
            return PaperlessResolveResult(
                status="not_configured",
                document_id=document_id,
                error="PAPERLESS_BASE_URL and PAPERLESS_API_TOKEN must both be configured",
            )

        parsed_base = urlparse(base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
            return PaperlessResolveResult(
                status="malformed",
                document_id=document_id,
                error="PAPERLESS_BASE_URL must be an http or https URL",
            )

        metadata_url = f"{base_url}/api/documents/{document_id}/"
        headers = {"Authorization": f"Token {api_token}"}

        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    metadata_url,
                    headers=headers,
                    timeout=_PAPERLESS_TIMEOUT_SECONDS,
                )
            else:
                async with httpx.AsyncClient(timeout=_PAPERLESS_TIMEOUT_SECONDS) as client:
                    response = await client.get(
                        metadata_url,
                        headers=headers,
                        timeout=_PAPERLESS_TIMEOUT_SECONDS,
                    )
        except httpx.TimeoutException:
            return PaperlessResolveResult(
                status="transport_error",
                document_id=document_id,
                error="Paperless request timed out",
            )
        except httpx.TransportError as exc:
            return PaperlessResolveResult(
                status="transport_error",
                document_id=document_id,
                error=f"Paperless transport error: {exc.__class__.__name__}",
            )

        if response.status_code == 404:
            return PaperlessResolveResult(
                status="not_found",
                document_id=document_id,
                error=f"Paperless document {document_id} was not found",
            )
        if response.status_code in {401, 403}:
            return PaperlessResolveResult(
                status="unauthorized",
                document_id=document_id,
                error="Paperless request was unauthorized or forbidden",
            )
        if response.status_code >= 500:
            return PaperlessResolveResult(
                status="transport_error",
                document_id=document_id,
                error=f"Paperless returned HTTP {response.status_code}",
            )
        if response.status_code != 200:
            return PaperlessResolveResult(
                status="transport_error",
                document_id=document_id,
                error=f"Paperless returned unexpected HTTP {response.status_code}",
            )

        try:
            payload = response.json()
        except ValueError:
            return PaperlessResolveResult(
                status="malformed",
                document_id=document_id,
                error="Paperless returned invalid JSON",
            )

        # A syntactically valid JSON body can still be null/list/string/number.
        # None of those support .get(...); treat them as malformed (non-destructive)
        # rather than raising AttributeError.
        if not isinstance(payload, dict):
            return PaperlessResolveResult(
                status="malformed",
                document_id=document_id,
                error="Paperless returned a non-object JSON body",
            )

        resolved_document_id = payload.get("id", document_id)
        if not isinstance(resolved_document_id, int) or isinstance(resolved_document_id, bool):
            resolved_document_id = document_id

        return PaperlessResolveResult(
            status="found",
            document_id=resolved_document_id,
            title=str(payload["title"]) if payload.get("title") is not None else None,
            mime_type=str(payload["mime_type"]) if payload.get("mime_type") is not None else None,
            added=str(payload["added"]) if payload.get("added") is not None else None,
            retrieval_targets={
                "download": f"{base_url}/api/documents/{resolved_document_id}/download/",
                "preview": f"{base_url}/api/documents/{resolved_document_id}/preview/",
                "thumb": f"{base_url}/api/documents/{resolved_document_id}/thumb/",
            },
        )
