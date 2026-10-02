"""One Hospitable account per container; persistent data never lives in /app."""
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}")


def read_credentials(path):
    if path.stat().st_mode & 0o077:
        raise ValueError("credentials.env must have mode 600 or 400")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError("Invalid credential assignment")
        if name in values:
            raise ValueError("Duplicate credential assignment")
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Credential values must be nonempty strings")
        values[name] = value
    return values


def prepare(data_root=None, secrets_root=None):
    root = Path(data_root or os.environ.get("TOOLKIT_DATA_DIR", "/data")).resolve()
    config = json.loads((root / "config/account.json").read_text())
    from hosting.property_setup import apply_selection
    account = apply_selection(root, config["account"])
    if type(config.get("mcp", {}).get("enabled", False)) is not bool:
        raise ValueError("MCP enabled must be a boolean")
    from datetime import datetime
    from hosting.reindex_schedule import due
    for settings in (account, *account.get("properties", {}).values()):
        indexing = {**account.get("indexing", {}), **settings.get("indexing", {})}
        if type(indexing.get("enabled", False)) is not bool:
            raise ValueError("Indexing enabled must be a boolean")
        due(indexing, datetime.now().astimezone(), None)
    account_id = account["id"]
    if not isinstance(account_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", account_id):
        raise ValueError("Invalid account ID")
    if not isinstance(account.get("properties"), dict) or len(account["properties"]) > 64:
        raise ValueError("Select up to 64 properties for this account")
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = state / "instance.json"
    expected = {"schema": 1, "account_id": account_id}
    if identity.exists() and json.loads(identity.read_text()) != expected:
        raise ValueError("Data directory belongs to another account or schema")
    identity.write_text(json.dumps(expected))
    identity.chmod(0o600)
    if (root / "properties").is_symlink():
        raise ValueError("Property directory must not be a symlink")
    credentials = read_credentials(Path(secrets_root or os.environ.get("TOOLKIT_SECRETS_DIR", "/run/toolkit-secrets")) / "credentials.env")
    bootstrap_key = account.get("model_key_env", "ANTHROPIC_API_KEY")
    from hosting.ai_service import PROVIDERS
    references = {v["env"] for v in PROVIDERS.values()} | {bootstrap_key, account.get("api_key_env", "HOSPITABLE_PAT"), account.get("webhook_secret_env", "HOSPITABLE_WEBHOOK_SECRET"), "TOOLKIT_ADMIN_SECRET"}
    account = {**account, "api_key_env": account.get("api_key_env", "HOSPITABLE_PAT"), "webhook_secret_env": account.get("webhook_secret_env", "HOSPITABLE_WEBHOOK_SECRET")}
    ports = []
    for number, (pid, prop) in enumerate(account["properties"].items()):
        folder = prop.get("folder", pid)
        if not isinstance(pid, str) or not SAFE_ID.fullmatch(pid) or not isinstance(folder, str) or not SAFE_ID.fullmatch(folder):
            raise ValueError("Property IDs and folders must be safe identifiers")
        property_root = (root / "properties" / folder).resolve()
        if (root / "properties").resolve() not in property_root.parents:
            raise ValueError("Property data escapes mounted data directory")
        for name in ("docs", "source-documents", "document-review", "index", "state", "logs"):
            (property_root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file = property_root / "state/worker-token"
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(48))
        token_file.chmod(0o600)
        worker_env = f"TOOLKIT_WORKER_SECRET_{number}"
        os.environ[worker_env] = token_file.read_text().strip()
        prop = {**prop, "runtime_dir": str(property_root), "worker_url": f"http://127.0.0.1:{9000 + number}", "worker_secret_env": worker_env, "model_key_env": prop.get("model_key_env", "ANTHROPIC_API_KEY")}
        account["properties"][pid] = prop
        references.add(prop["model_key_env"])
        ports.append((pid, 9000 + number))
    for settings in (account, *account["properties"].values()):
        endpoint = settings.get("notifications", {}).get("ha", {}).get("webhook_url_env")
        if endpoint:
            references.add(endpoint)
    mcp = config.get("mcp", {})
    if mcp.get("enabled", False):
        clients = json.loads((root / "config/mcp_clients.json").read_text())["clients"]
        references.update(client["token_env"] for client in clients.values())
    required = {account["api_key_env"], account["webhook_secret_env"], "TOOLKIT_ADMIN_SECRET"}
    if not required <= set(credentials):
        raise ValueError("Required account credentials are missing")
    if set(credentials) - references:
        raise ValueError("Credential file contains names not referenced by this instance")
    for name, value in credentials.items():
        os.environ[name] = value
    registry = state / "runtime-registry.json"
    registry.write_text(json.dumps({"accounts": {account_id: account}}))
    registry.chmod(0o600)
    os.environ.update({"TOOLKIT_ACCOUNT_ID": account_id, "TOOLKIT_INSTANCE_MODE": "container", "TOOLKIT_DATA_DIR": str(root), "TOOLKIT_ACCOUNTS_FILE": str(registry), "TOOLKIT_CONTROLS_DB": str(state / "controls.sqlite3"), "TOOLKIT_INBOX_DB": str(state / "inbox.sqlite3"), "HF_HOME": str(root / "model-cache"), "SENTENCE_TRANSFORMERS_HOME": str(root / "model-cache"), "TOOLKIT_MCP_ENABLED": "true" if mcp.get("enabled", False) else "false"})
    if mcp.get("enabled", False):
        os.environ.update({"TOOLKIT_MCP_CLIENTS_FILE": str(root / "config/mcp_clients.json"), "TOOLKIT_MCP_RESOURCE_URL": mcp["resource_url"], "TOOLKIT_MCP_ISSUER_URL": mcp["issuer_url"]})
    from hosting.config import load_registry
    load_registry(registry)
    if mcp.get("enabled", False):
        from hosting.access import Access
        Access(root / "config/mcp_clients.json", {account_id: account}).clients()
    return account_id, account, ports


def main():
    os.umask(0o077)
    try:
        account_id, account, ports = prepare()
    except Exception:
        # Configuration errors must not expose secrets in container logs.
        print("Instance configuration failed; check config, mounts, permissions and credentials", file=sys.stderr)
        raise SystemExit(2)
    from hosting.tls_settings import ensure
    certificate, private_key = ensure(os.environ["TOOLKIT_DATA_DIR"])
    os.environ["TOOLKIT_HTTPS_ADMIN"] = "true"
    children = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        for pid, _ in ports:
            children.append(subprocess.Popen([sys.executable, "-m", "hosting.manage", "worker", account_id, pid], start_new_session=True))
        children.append(subprocess.Popen([sys.executable, "-m", "hosting.scheduled_messages"], start_new_session=True))
        if account.get("indexing", {}).get("enabled", False):
            children.append(subprocess.Popen([sys.executable, "-m", "hosting.reindex_schedule"], start_new_session=True))
        children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "hosting.container_app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8790", "--no-access-log", "--no-proxy-headers"], start_new_session=True))
        children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "hosting.container_app:create_tls_app", "--factory", "--host", "0.0.0.0", "--port", "9443", "--no-access-log", "--no-proxy-headers", "--lifespan", "off", "--ssl-certfile", certificate, "--ssl-keyfile", private_key], start_new_session=True))
        restart_request = Path(os.environ["TOOLKIT_DATA_DIR"]) / "state/restart-request.json"
        while not stopping:
            if restart_request.exists() and time.time() - restart_request.stat().st_mtime > 2:
                restart_request.unlink()
                break  # Graceful exit; Compose unless-stopped starts the new selection.
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A required child exited")
            time.sleep(0.5)
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 15
        for child in children:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


if __name__ == "__main__":
    main()

