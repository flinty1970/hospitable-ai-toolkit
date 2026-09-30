"""Explicit MCP client allowlists; no wildcard or implicit property grants."""
import hmac
import json
import re
from pathlib import Path
from hosting.config import secret

PERMISSIONS = {"read", "preview", "settings"}


class Denied(PermissionError):
    pass


class Access:
    def __init__(self, path, accounts):
        self.path = Path(path)
        self.accounts = accounts

    def clients(self):
        clients = json.loads(self.path.read_text())["clients"]
        if not isinstance(clients, dict):
            raise ValueError("clients must be an object")
        tokens = set()
        service_tokens = set()
        for account in self.accounts.values():
            service_tokens.update((secret(account["api_key_env"]), secret(account["webhook_secret_env"])))
            service_tokens.update(secret(prop["worker_secret_env"]) for prop in account["properties"].values())
        for client_id, client in clients.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", client_id):
                raise ValueError("Invalid MCP client ID")
            token = secret(client["token_env"])
            if token in service_tokens:
                raise ValueError("MCP tokens must be independent of API/webhook/worker credentials")
            if token in tokens:
                raise ValueError("MCP client tokens must be distinct")
            tokens.add(token)
            if type(client.get("enabled", True)) is not bool:
                raise ValueError("Client enabled must be a boolean")
            for account_id, grant in client["accounts"].items():
                if account_id not in self.accounts:
                    raise ValueError("Grant references an unknown account")
                account_permissions = grant.get("permissions", [])
                if not isinstance(account_permissions, list) or not set(account_permissions) <= PERMISSIONS:
                    raise ValueError("Unknown account permission")
                for property_id, permissions in grant.get("properties", {}).items():
                    if property_id not in self.accounts[account_id]["properties"]:
                        raise ValueError("Grant references an unknown property")
                    if not isinstance(permissions, list) or not set(permissions) <= PERMISSIONS:
                        raise ValueError("Unknown property permission")
        return clients

    def identify(self, token):
        if not token:
            raise Denied("Access denied")
        for client_id, client in self.clients().items():
            if client.get("enabled", True) and hmac.compare_digest(token.encode(), secret(client["token_env"]).encode()):
                return client_id, client
        raise Denied("Access denied")

    def require(self, token, account_id, property_id, permission):
        client_id, client = self.identify(token)
        grant = client["accounts"].get(account_id, {})
        permissions = grant.get("permissions", []) if property_id is None else grant.get("properties", {}).get(property_id, [])
        if permission not in permissions:
            # Same error for unknown and unauthorized pairs; no existence leak.
            raise Denied("Access denied")
        return client_id
