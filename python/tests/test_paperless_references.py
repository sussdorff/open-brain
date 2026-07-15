"""Tests for Paperless-ngx document references in memory metadata."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import httpx
import pytest


def _valid_reference_metadata() -> dict:
    """Return valid Paperless reference metadata for tests."""
    return {
        "document_id": 17,
        "instance": "paperless.example",
        "title": "Home insurance policy",
        "added": "2026-07-11T09:30:00Z",
    }


class TestPaperlessReferenceMetadataValidation:
    def test_paperless_reference_validates_for_unrelated_memory_type(self):
        """AC1: paperless_reference validation runs for non-paperless memory types."""
        from open_brain.data_layer.interface import validate_domain_metadata

        warnings = validate_domain_metadata(
            "decision",
            {"paperless_reference": _valid_reference_metadata()},
        )

        assert warnings == []

    def test_paperless_reference_validates_when_memory_type_is_none(self):
        """AC1: paperless_reference validation runs before the type=None early return."""
        from open_brain.data_layer.interface import validate_domain_metadata

        warnings = validate_domain_metadata(
            None,
            {
                "paperless_reference": {
                    "document_id": 0,
                    "instance": "",
                    "title": "",
                    "added": "not-a-date",
                }
            },
        )

        assert any("document_id" in warning for warning in warnings)
        assert any("instance" in warning for warning in warnings)
        assert any("title" in warning for warning in warnings)
        assert any("added" in warning for warning in warnings)


def _paperless_document_response(request: httpx.Request) -> httpx.Response:
    """Return a minimal Paperless document response."""
    return httpx.Response(
        200,
        json={
            "id": 17,
            "title": "Home insurance policy",
            "mime_type": "application/pdf",
            "added": "2026-07-11T09:30:00Z",
        },
        request=request,
    )


def _get_tool_description(srv_module, tool_name: str) -> str | None:
    """Extract an MCP tool description from the local FastMCP registry."""
    mcp = srv_module.mcp
    for attr in ("_tool_manager", "tool_manager", "_tools", "tools"):
        manager = getattr(mcp, attr, None)
        if manager is None:
            continue
        tools = getattr(manager, "_tools", None) or getattr(manager, "tools", None)
        if isinstance(tools, dict) and tool_name in tools:
            return getattr(tools[tool_name], "description", None)
    return None


class TestPaperlessClientFound:
    @pytest.mark.asyncio
    async def test_found_document_returns_metadata_and_retrieval_targets(self):
        """AC2: a valid Paperless id resolves metadata and retrieval targets."""
        from open_brain.paperless import PaperlessClient

        called_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called_urls.append(str(request.url))
            assert request.headers["authorization"] == "Token test-token"
            return _paperless_document_response(request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "found"
        assert result.document_id == 17
        assert result.title == "Home insurance policy"
        assert result.mime_type == "application/pdf"
        assert result.added == "2026-07-11T09:30:00Z"
        assert result.retrieval_targets == {
            "download": "https://paperless.example/api/documents/17/download/",
            "preview": "https://paperless.example/api/documents/17/preview/",
            "thumb": "https://paperless.example/api/documents/17/thumb/",
        }
        assert called_urls == ["https://paperless.example/api/documents/17/"]


class TestResolvePaperlessReferenceTool:
    @pytest.mark.asyncio
    async def test_resolve_paperless_reference_tool_returns_json(self):
        """AC2: agents can resolve references through the MCP tool surface."""
        from open_brain.paperless import PaperlessResolveResult
        import open_brain.server as srv

        class FakePaperlessClient:
            async def resolve_reference(self, document_id: int) -> PaperlessResolveResult:
                assert document_id == 17
                return PaperlessResolveResult(
                    status="found",
                    document_id=17,
                    title="Home insurance policy",
                    mime_type="application/pdf",
                    added="2026-07-11T09:30:00Z",
                    retrieval_targets={
                        "download": "https://paperless.example/api/documents/17/download/",
                        "preview": "https://paperless.example/api/documents/17/preview/",
                        "thumb": "https://paperless.example/api/documents/17/thumb/",
                    },
                )

        with patch.object(srv, "PaperlessClient", return_value=FakePaperlessClient()):
            raw = await srv.resolve_paperless_reference(17)

        data = json.loads(raw)
        assert data["status"] == "found"
        assert data["document_id"] == 17
        assert data["retrieval_targets"]["download"].endswith("/api/documents/17/download/")

    def test_save_memory_description_documents_paperless_reference_schema(self):
        """AC2: save_memory tells agents how to cite a Paperless document."""
        import open_brain.server as srv

        description = _get_tool_description(srv, "save_memory")

        assert description is not None
        assert "paperless_reference" in description
        assert "document_id" in description
        assert "instance" in description


class TestPaperlessBinaryInvariant:
    @pytest.mark.asyncio
    async def test_resolve_reference_never_fetches_binary_endpoints(self):
        """AC3: resolving metadata never calls Paperless binary endpoints."""
        from open_brain.paperless import PaperlessClient

        called_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called_urls.append(str(request.url))
            assert request.url.path == "/api/documents/17/"
            return _paperless_document_response(request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example/",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "found"
        assert set(called_urls) == {"https://paperless.example/api/documents/17/"}
        assert not any(url.endswith(("/download/", "/preview/", "/thumb/")) for url in called_urls)

    def test_reference_metadata_contains_no_binary_payload_keys(self):
        """AC3: Paperless reference metadata contains identity/provenance only."""
        from open_brain.data_layer.interface import PaperlessReferenceMetadata

        metadata: PaperlessReferenceMetadata = _valid_reference_metadata()
        forbidden_keys = {"bytes", "base64", "content", "data", "attachment"}

        assert forbidden_keys.isdisjoint({key.lower() for key in metadata})

        payload = {"paperless_reference": metadata}
        serialized = json.dumps(payload)
        assert json.loads(serialized) == payload
        assert "\\u0000" not in serialized

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "forbidden_key", ["bytes", "base64", "content", "data", "attachment"]
    )
    async def test_save_memory_rejects_forbidden_binary_payload_key(self, forbidden_key):
        """AC3: save_memory HARD-REJECTS a paperless_reference carrying a binary payload.

        A forbidden binary key must abort the write with an explicit error response —
        never persist with only a warning — so document bytes cannot enter memory
        tables or exports. The guard runs before any persistence, so no DB is needed.
        """
        import open_brain.server as srv

        metadata = {
            "paperless_reference": {
                **_valid_reference_metadata(),
                forbidden_key: "SGVsbG8gd29ybGQ=",  # would be the document binary
            }
        }

        raw = await srv.save_memory(text="A memory citing a document", metadata=metadata, provenance={"producer": "test-suite", "source_ref": "test-suite:test_paperless_references"})
        data = json.loads(raw)

        assert data.get("error") == "paperless_reference_binary_payload", (
            f"expected hard rejection for forbidden key {forbidden_key!r}, got {data!r}"
        )
        assert forbidden_key in data.get("message", "")
        # Nothing was persisted: a successful save returns an id, a rejection must not.
        assert "id" not in data


class TestPaperlessExplicitMissingResults:
    @pytest.mark.asyncio
    async def test_not_found_returns_explicit_non_destructive_result(self):
        """AC4: a missing Paperless document returns not_found without raising."""
        from open_brain.paperless import PaperlessClient

        called_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called_urls.append(str(request.url))
            return httpx.Response(404, request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(404)

        assert result.status == "not_found"
        assert result.document_id == 404
        assert result.error is not None
        assert "not found" in result.error.lower()
        assert called_urls == ["https://paperless.example/api/documents/404/"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_unauthorized_returns_explicit_non_destructive_result(self, status_code: int):
        """AC4: inaccessible Paperless documents return unauthorized without raising."""
        from open_brain.paperless import PaperlessClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "unauthorized"
        assert result.document_id == 17
        assert result.error is not None
        assert "unauthorized" in result.error.lower()
        assert "test-token" not in result.error


class TestPaperlessTransportAndInputFailures:
    @pytest.mark.asyncio
    async def test_timeout_returns_transport_error(self):
        """AC4: timeout maps to transport_error, distinct from not_found."""
        from open_brain.paperless import PaperlessClient

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "transport_error"
        assert result.status != "not_found"

    @pytest.mark.asyncio
    async def test_connection_error_returns_transport_error(self):
        """AC4: connection failure maps to transport_error."""
        from open_brain.paperless import PaperlessClient

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS failure", request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "transport_error"

    @pytest.mark.asyncio
    async def test_5xx_returns_transport_error(self):
        """AC4: Paperless server errors map to transport_error."""
        from open_brain.paperless import PaperlessClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "transport_error"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("base_url", "api_token"),
        [
            ("", "test-token"),
            ("https://paperless.example", ""),
            ("", ""),
        ],
    )
    async def test_not_configured_returns_without_http_call(self, base_url: str, api_token: str):
        """AC4: missing config returns not_configured and never calls HTTP."""
        from open_brain.paperless import PaperlessClient

        called_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called_urls.append(str(request.url))
            return httpx.Response(500, request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url=base_url,
                api_token=api_token,
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "not_configured"
        assert called_urls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [b"[]", b"null", b'"just a string"', b"42"])
    async def test_non_object_json_body_returns_malformed(self, body):
        """AC4: a valid-JSON but non-object body returns malformed, never AttributeError.

        A 200 response whose body parses to null/list/string/number has no .get(),
        so it must map to the explicit non-destructive 'malformed' result instead of
        raising.
        """
        from open_brain.paperless import PaperlessClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body, request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(17)

        assert result.status == "malformed"
        assert result.document_id == 17
        assert result.error is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("document_id", [0, -1, "17", True])
    async def test_malformed_document_id_returns_without_http_call(self, document_id):
        """AC4: malformed document ids return malformed and never call HTTP."""
        from open_brain.paperless import PaperlessClient

        called_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called_urls.append(str(request.url))
            return httpx.Response(500, request=request)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            result = await PaperlessClient(
                base_url="https://paperless.example",
                api_token="test-token",
                http_client=http_client,
            ).resolve_reference(document_id)  # type: ignore[arg-type]

        assert result.status == "malformed"
        assert called_urls == []


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not (os.environ.get("PAPERLESS_BASE_URL") and os.environ.get("PAPERLESS_API_TOKEN")),
    reason="PAPERLESS_BASE_URL and PAPERLESS_API_TOKEN are required for live Paperless smoke test",
)
async def test_live_paperless_reference_resolution_boundary():
    """AC2/AC4: opt-in live Paperless boundary smoke test."""
    from open_brain.paperless import PaperlessClient

    document_id_raw = os.environ.get("PAPERLESS_DOCUMENT_ID", "1")
    try:
        document_id = int(document_id_raw)
    except ValueError:
        pytest.skip("PAPERLESS_DOCUMENT_ID must be a positive integer")
    if document_id <= 0:
        pytest.skip("PAPERLESS_DOCUMENT_ID must be a positive integer")

    result = await PaperlessClient().resolve_reference(document_id)

    assert result.status in {"found", "not_found", "unauthorized"}
