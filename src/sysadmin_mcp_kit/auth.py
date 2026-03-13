from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

from .config import OAuthSettings

logger = logging.getLogger(__name__)


class IntrospectionTokenVerifier(TokenVerifier):
    def __init__(
        self,
        settings: OAuthSettings,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self._settings = settings
        self._http_client_factory = http_client_factory or self._default_client_factory
        self._resource = resource_url_from_server_url(settings.resource_server_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        print(f"[DEBUG] server received bearer token: {token}")
        try:
            async with self._http_client_factory() as client:
                response = await client.post(
                    str(self._settings.introspection_endpoint),
                    data={"token": token, "resource": self._resource},
                    auth=(self._settings.client_id, self._settings.client_secret),
                    headers={"Accept": "application/json"},
                )
            response.raise_for_status()
            payload = response.json()
            print(f"[DEBUG] server introspection payload: {payload}")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OAuth introspection failed closed: %s", exc.__class__.__name__)
            return None

        if not payload.get("active"):
            print("[DEBUG] server rejected token: inactive")
            return None

        issuer = payload.get("iss")
        if issuer and str(issuer).rstrip("/") != str(self._settings.issuer_url).rstrip("/"):
            print(f"[DEBUG] server rejected token: issuer mismatch expected={self._settings.issuer_url} actual={issuer}")
            return None

        scope_value = payload.get("scope") or payload.get("scp") or ""
        if isinstance(scope_value, str):
            scopes = [scope for scope in scope_value.split() if scope]
        elif isinstance(scope_value, list):
            scopes = [str(scope) for scope in scope_value]
        else:
            scopes = []

        resource = payload.get("resource") or payload.get("aud")
        if resource is not None:
            if isinstance(resource, list):
                if not any(check_resource_allowed(str(item), self._resource) for item in resource):
                    print(f"[DEBUG] server rejected token: resource mismatch expected={self._resource} actual={resource}")
                    return None
            else:
                if not check_resource_allowed(str(resource), self._resource):
                    print(f"[DEBUG] server rejected token: resource mismatch expected={self._resource} actual={resource}")
                    return None

        client_id = str(payload.get("client_id") or payload.get("sub") or "")
        if not client_id:
            print("[DEBUG] server rejected token: missing client_id/sub")
            return None

        exp = payload.get("exp")
        expires_at = int(exp) if isinstance(exp, (int, float)) else None
        print(f"[DEBUG] server accepted token for client_id={client_id} scopes={scopes} resource={self._resource}")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=self._resource,
        )

    @staticmethod
    def _default_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))