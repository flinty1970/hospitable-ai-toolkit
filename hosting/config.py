"""Validated, secret-free account registry. Secrets are environment references."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def secret(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise ValueError("Invalid secret environment reference")
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing secret environment variable: {name}")
    return value


def load_registry(path):
    accounts = json.loads(Path(path).read_text())["accounts"]
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("Configure at least one account")
    if os.environ.get("TOOLKIT_INSTANCE_MODE") == "container" and len(accounts) != 1:
        raise ValueError("Each container supports exactly one Hospitable account")
    ports, roots, tokens = set(), set(), set()
    worker_tokens = set()
    def validate_integrations(config):
        home_assistant = config.get("home_assistant", {})
        for key in ("enabled", "heating_enabled"):
            if key in home_assistant and type(home_assistant[key]) is not bool:
                raise ValueError("Home Assistant switches must be boolean")
        for channel, notification in config.get("notifications", {}).items():
            if channel not in {"ha", "email"}:
                raise ValueError("Unknown notification channel")
            if "enabled" in notification and type(notification["enabled"]) is not bool:
                raise ValueError("Notification enabled must be boolean")
    for account_id, account in accounts.items():
        validate_integrations(account)
        for key in ("enabled", "shadow"):
            if key in account and type(account[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", account_id):
            raise ValueError("Invalid account ID")
        secret(account["api_key_env"])
        token = secret(account["webhook_secret_env"])
        if token in tokens:
            raise ValueError("Accounts must use distinct webhook secrets")
        tokens.add(token)
        properties = account["properties"]
        if not isinstance(properties, dict) or (not properties and os.environ.get("TOOLKIT_INSTANCE_MODE") != "container"):
            raise ValueError("Select at least one property per account")
        for property_id, prop in properties.items():
            validate_integrations(prop)
            for key in ("enabled", "shadow"):
                if key in prop and type(prop[key]) is not bool:
                    raise ValueError(f"{key} must be a boolean")
            if not isinstance(property_id, str) or not property_id.strip():
                raise ValueError("Invalid property UUID")
            url = urlparse(prop["worker_url"])
            if (url.scheme != "http" or url.hostname != "127.0.0.1"
                    or not url.port or url.path not in ("", "/")
                    or url.query or url.fragment or url.username or url.password):
                raise ValueError("Workers must use an explicit loopback HTTP port")
            if url.port in ports:
                raise ValueError("Each property needs a distinct worker port")
            ports.add(url.port)
            worker_token = secret(prop["worker_secret_env"])
            if worker_token in worker_tokens:
                raise ValueError("Property worker secrets must be distinct")
            worker_tokens.add(worker_token)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", prop["model_key_env"]):
                raise ValueError("Invalid model credential environment reference")
            if not isinstance(prop["name"], str) or not prop["name"].strip():
                raise ValueError("Property name is required")
            ZoneInfo(prop["timezone"])
            root = Path(prop["runtime_dir"])
            if not root.is_absolute():
                raise ValueError("runtime_dir must be absolute")
            root = root.resolve()
            code_root = Path(__file__).resolve().parents[1]
            if root == code_root or root in code_root.parents:
                raise ValueError("Property data cannot replace the application directory")
            if any(root == old or root in old.parents or old in root.parents for old in roots):
                raise ValueError("Property runtime directories must not overlap")
            roots.add(root)
    return accounts


def property_database(prop):
    return Path(prop["runtime_dir"]) / "state/events.sqlite3"
