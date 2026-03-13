import httpx
import pytest

from sysadmin_mcp_kit.auth import IntrospectionTokenVerifier


@pytest.mark.asyncio
async def test_introspection_token_verifier_accepts_valid_token(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/introspect"
        body = (await request.aread()).decode()
        assert "token=good-token" in body
        return httpx.Response(
            200,
            json={
                "active": True,
                "client_id": "client-a",
                "scope": "sysadmin:mcp another",
                "iss": "http://127.0.0.1/auth",
                "resource": "http://127.0.0.1/mcp",
                "exp": 1234,
            },
        )

    transport = httpx.MockTransport(handler)

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1")

    verifier = IntrospectionTokenVerifier(settings.oauth, http_client_factory=factory)
    token = await verifier.verify_token("good-token")

    assert token is not None
    assert token.client_id == "client-a"
    assert token.scopes == ["sysadmin:mcp", "another"]


@pytest.mark.asyncio
async def test_introspection_token_verifier_rejects_wrong_resource(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "active": True,
                "client_id": "client-a",
                "scope": "sysadmin:mcp",
                "iss": "http://127.0.0.1/auth",
                "resource": "http://127.0.0.1/other",
            },
        )

    verifier = IntrospectionTokenVerifier(
        settings.oauth,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"),
    )

    assert await verifier.verify_token("good-token") is None


@pytest.mark.asyncio
async def test_introspection_token_verifier_fails_closed_on_upstream_error(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server_error"})

    verifier = IntrospectionTokenVerifier(
        settings.oauth,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1"),
    )

    assert await verifier.verify_token("good-token") is None
