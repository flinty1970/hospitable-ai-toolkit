"""Provision data directories, run scoped ingestion, or start a property worker."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from hosting.config import load_registry

CODE_ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "reindex", "worker", "review"])
    parser.add_argument("account")
    parser.add_argument("property_uuid")
    args = parser.parse_args()
    account = load_registry(os.environ["TOOLKIT_ACCOUNTS_FILE"])[args.account]
    prop = account["properties"][args.property_uuid]
    root = Path(prop["runtime_dir"])
    if args.action == "init":
        for name in ("docs", "source-documents", "document-review", "index", "state", "logs"):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        print("Created empty property directories. Add this property's curated Markdown to docs/.")
        return
    # No subprocess may inherit another account's credentials from another instance.
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "HF_HOME", "SENTENCE_TRANSFORMERS_HOME",
        "TOOLKIT_INSTANCE_MODE", "TOOLKIT_DATA_DIR",
    }}
    env.update({"TOOLKIT_PROPERTY_DIR": str(root), "HOSPITABLE_PROPERTY_UUID": args.property_uuid,
                "TOOLKIT_ACCOUNT_ID": args.account, "PROPERTY_TIMEZONE": prop["timezone"],
                "TOOLKIT_ACCOUNTS_FILE": os.environ["TOOLKIT_ACCOUNTS_FILE"]})
    if args.action == "worker":
        if not os.environ.get("TOOLKIT_CONTROLS_DB"):
            raise SystemExit("Set TOOLKIT_CONTROLS_DB to the shared gateway/worker controls database")
        env["TOOLKIT_CONTROLS_DB"] = os.environ["TOOLKIT_CONTROLS_DB"]
    if args.action == "reindex":
        if not root.is_dir():
            raise SystemExit("Run init and add curated docs first")
        # Extract new PDFs to review; only approved Markdown is indexed.
        subprocess.run([sys.executable, str(CODE_ROOT / "hosting/pdf_ingestion.py"), "convert"], env=env, cwd=root, check=True, timeout=300)
        subprocess.run([sys.executable, str(CODE_ROOT / "ingest.py")], env=env, cwd=root, check=True)
    elif args.action == "worker":
        # Registry validation inside a worker requires the registry references.
        # These are used only by this trusted process; the selected account is fixed.
        # Use per-worker registries to limit even configuration visibility.
        for key in {account["api_key_env"], account["webhook_secret_env"], prop["worker_secret_env"]}:
            env[key] = os.environ[key]
        # AI setup can happen after startup. Do not require a Claude key when
        # this account chooses another provider or has not configured AI yet.
        from hosting.ai_service import PROVIDERS
        optional_keys = {prop['model_key_env']}
        if os.environ.get('TOOLKIT_INSTANCE_MODE') == 'container':
            optional_keys.update(provider['env'] for provider in PROVIDERS.values())
        for key in optional_keys:
            if key in os.environ:
                env[key] = os.environ[key]
        # Only the selected property's effective HA endpoint, when configured.
        from hosting.notifications import channel_config
        ha = channel_config(account, args.property_uuid, "ha")
        if ha.get("webhook_url_env") and ha["webhook_url_env"] in os.environ:
            env[ha["webhook_url_env"]] = os.environ[ha["webhook_url_env"]]
        worker_registry = root / "state/worker-registry.json"
        worker_registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        worker_registry.write_text(json.dumps({"accounts": {args.account: {
            **account, "properties": {args.property_uuid: prop}}}}))
        os.chmod(worker_registry, 0o600)
        env["TOOLKIT_ACCOUNTS_FILE"] = str(worker_registry)
        os.execve(sys.executable, [sys.executable, "-m", "uvicorn", "hosting.worker:create_app",
                  "--factory", "--host", "127.0.0.1", "--port", str(urlparse(prop["worker_url"]).port),
                  "--no-access-log", "--app-dir", str(CODE_ROOT)], env)
    else:
        import sqlite3
        # Local operator access only; no public cross-account review API.
        with sqlite3.connect(f"file:{root / 'state/events.sqlite3'}?mode=ro", uri=True) as db:
            for row in db.execute("SELECT event_key,result,received FROM events ORDER BY received DESC LIMIT 50"):
                print(json.dumps({"message_id": row[0], "result": json.loads(row[1]), "received": row[2]}))


if __name__ == "__main__":
    main()
