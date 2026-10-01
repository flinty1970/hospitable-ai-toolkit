"""Optional property-scoped MCP endpoint for this account.

Operator-provisioned bearer tokens are supported; this server does not implement
an OAuth authorization/login flow. Its resource URL/issuer metadata must be set
explicitly for the chosen deployment and client authentication arrangement.
"""
import os
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from hosting.access import Denied
from hosting.config import load_registry
from hosting.mcp_tools import HostedTools


def create_server():
    accounts = load_registry(os.environ["TOOLKIT_ACCOUNTS_FILE"])
    tools = HostedTools(accounts, os.environ["TOOLKIT_MCP_CLIENTS_FILE"],
                        os.environ["TOOLKIT_CONTROLS_DB"], os.environ["TOOLKIT_INBOX_DB"])
    tools.access.clients()  # Fail closed at startup on malformed or overlapping credentials.
    resource = os.environ["TOOLKIT_MCP_RESOURCE_URL"]
    parsed = urlparse(resource)
    if parsed.scheme != "https" or not parsed.path.endswith("/mcp") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("TOOLKIT_MCP_RESOURCE_URL must be an HTTPS URL ending /mcp")

    class Verifier(TokenVerifier):
        async def verify_token(self, token):
            try:
                client_id, _ = tools.access.identify(token)
            except Denied:
                return None
            return AccessToken(token=token, client_id=client_id, scopes=["toolkit:access"], resource=resource)

    mcp = FastMCP("Hospitable AI Toolkit", token_verifier=Verifier(),
                  stateless_http=True, json_response=True, streamable_http_path=parsed.path,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                      allowed_hosts=[parsed.netloc, "127.0.0.1:*", "localhost:*"],
                      allowed_origins=[f"https://{parsed.netloc}"]),
                  auth=AuthSettings(issuer_url=AnyHttpUrl(os.environ["TOOLKIT_MCP_ISSUER_URL"]),
                                    resource_server_url=AnyHttpUrl(resource), required_scopes=["toolkit:access"],
                                    validate_token_resource=True))

    def token():
        access = get_access_token()
        if access is None:
            raise Denied("Access denied")
        return access.token

    @mcp.tool()
    def list_access() -> list[dict]:
        """List only the authenticated client's explicit account/property grants."""
        return tools.list_access(token())

    @mcp.tool()
    def get_settings(account_id: str, property_id: str | None = None) -> dict:
        """Read enabled/shadow settings in an authorized account or property scope."""
        return tools.get_settings(token(), account_id, property_id)

    @mcp.tool()
    def set_settings(account_id: str, property_id: str | None = None,
                     enabled: bool | None = None, shadow: bool | None = None,
                     email_enabled: bool | None = None) -> dict:
        """Change authorized processing settings. Community containers require explicit account and property automatic modes plus tested owner email to send."""
        return tools.set_settings(token(), account_id, property_id, enabled, shadow, email_enabled=email_enabled)

    @mcp.tool()
    def search_knowledge(account_id: str, property_id: str, query: str) -> list[dict]:
        """Search only an authorized property's knowledge index."""
        return tools.search(token(), account_id, property_id, query)

    @mcp.tool()
    def preview_reply(account_id: str, property_id: str, message: str) -> dict:
        """Generate a manual property-scoped preview; never sends a guest message."""
        return tools.preview(token(), account_id, property_id, message)

    @mcp.tool()
    def recent_drafts(account_id: str, property_id: str, limit: int = 20) -> list[dict]:
        """Read only an authorized property's drafts/review records."""
        return tools.recent_drafts(token(), account_id, property_id, limit)

    @mcp.tool()
    def inbox_status(account_id: str, property_id: str | None = None) -> list[dict]:
        """Read authorized account/property inbox counts without raw payloads."""
        return tools.inbox_status(token(), account_id, property_id)

    @mcp.tool()
    def notification_status(account_id: str, property_id: str | None = None) -> list[dict]:
        """Read authorized alert-delivery counts; no SMTP details or HA URLs."""
        return tools.notification_status(token(), account_id, property_id)

    return mcp


def create_app():
    return create_server().streamable_http_app()
