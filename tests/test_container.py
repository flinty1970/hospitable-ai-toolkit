import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from hosting.container_runtime import prepare, read_credentials
from hosting.config import load_registry
from hosting.indexing import active_index, markdown_chunks, rebuild
from hosting.reindex_schedule import due


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.secret_dir = self.root / "secrets"
        (self.data / "config").mkdir(parents=True)
        self.secret_dir.mkdir()
        self.config = {"account": {"id": "owner-a", "shadow": True,
            "notifications": {"email": {"enabled": False}},
            "properties": {"property-one": {"name": "One", "timezone": "Europe/London"},
                           "property-two": {"name": "Two", "timezone": "America/New_York"}}}}
        self.save()
        self.credentials = self.secret_dir / "credentials.env"
        self.credentials.write_text("HOSPITABLE_PAT=account-key\nHOSPITABLE_WEBHOOK_SECRET=hook-key\nTOOLKIT_ADMIN_SECRET=admin-key\nANTHROPIC_API_KEY=model-key\n")
        self.credentials.chmod(0o600)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def save(self):
        (self.data / "config/account.json").write_text(json.dumps(self.config))

    def start(self):
        return prepare(self.data, self.secret_dir)

    def test_empty_account_bootstrap_then_import_survives_restart(self):
        from hosting.property_setup import save_selection
        self.config['account']['properties'] = {}
        self.save()
        _, account, ports = self.start()
        self.assertEqual(ports, [])
        self.assertEqual(account['properties'], {})
        save_selection(self.data, 'owner-a', {'imported': {'name': 'Imported', 'timezone': 'UTC'}})
        _, account, ports = self.start()
        self.assertEqual(ports, [('imported', 9000)])
        self.assertFalse(account['properties']['imported']['enabled'])
        self.assertTrue((self.data / 'properties/imported/docs').is_dir())
        self.assertEqual(json.loads((self.data / 'config/account.json').read_text())['account']['properties'], {})
        from hosting.controls import Controls
        controls = Controls(self.data / 'state/controls.sqlite3')
        controls.set('admin-ui', 'owner-a', 'imported', enabled=True, shadow=True)
        _, account, _ = self.start()
        self.assertEqual(controls.effective('owner-a', account, 'imported')['mode'], 'shadow')

    def test_one_account_two_properties_have_separate_mounted_data(self):
        aid, account, ports = self.start()
        self.assertEqual(aid, "owner-a")
        self.assertEqual(ports, [("property-one", 9000), ("property-two", 9001)])
        for pid, prop in account["properties"].items():
            self.assertEqual(Path(prop["runtime_dir"]), self.data / "properties" / pid)
            for folder in ("docs", "source-documents", "index", "state", "logs"):
                self.assertTrue((Path(prop["runtime_dir"]) / folder).is_dir())
        self.assertEqual(len(load_registry(os.environ["TOOLKIT_ACCOUNTS_FILE"])), 1)

    def test_parallel_workers_serialize_control_schema_initialization(self):
        from concurrent.futures import ThreadPoolExecutor
        from hosting.controls import Controls
        path = self.data / "state/concurrent.sqlite3"
        with ThreadPoolExecutor(max_workers=8) as pool:
            controls = list(pool.map(lambda _: Controls(path), range(16)))
        with controls[0].connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(controls)")}
        self.assertIn("ha_enabled", columns)
        self.assertIn("email_enabled", columns)

    def test_restart_preserves_worker_secret_and_documents(self):
        _, first, _ = self.start()
        prop = first["properties"]["property-one"]
        token = os.environ[prop["worker_secret_env"]]
        doc = Path(prop["runtime_dir"]) / "docs/guide.md"
        doc.write_text("Property one's own guide")
        _, second, _ = self.start()
        self.assertEqual(token, os.environ[second["properties"]["property-one"]["worker_secret_env"]])
        self.assertEqual(doc.read_text(), "Property one's own guide")

    def test_reusing_data_for_different_account_is_rejected(self):
        self.start()
        self.config["account"]["id"] = "owner-b"
        self.save()
        with self.assertRaises(ValueError):
            self.start()

    def test_container_registry_rejects_second_account(self):
        _, account, _ = self.start()
        path = self.data / "state/extra.json"
        path.write_text(json.dumps({"accounts": {"a": account, "b": account}}))
        with self.assertRaises(ValueError):
            load_registry(path)

    def test_invalid_property_path_is_rejected(self):
        self.config["account"]["properties"]["property-one"]["folder"] = "../escape"
        self.save()
        with self.assertRaises(ValueError):
            self.start()

    def test_overlapping_property_folders_are_rejected(self):
        self.config["account"]["properties"]["property-two"]["folder"] = "property-one"
        self.save()
        with self.assertRaises(ValueError):
            self.start()

    def test_readonly_secret_file_permissions_are_required(self):
        self.credentials.chmod(0o644)
        with self.assertRaises(ValueError):
            read_credentials(self.credentials)

    def test_credentials_are_literal_not_shell_evaluated(self):
        self.credentials.write_text("HOSPITABLE_PAT=$(touch /tmp/do-not-create)\n")
        self.assertEqual(read_credentials(self.credentials)["HOSPITABLE_PAT"], "$(touch /tmp/do-not-create)")

    def test_unknown_environment_assignment_is_rejected(self):
        self.credentials.write_text(self.credentials.read_text() + "PATH=/malicious\n")
        with self.assertRaises(ValueError):
            self.start()

    def test_ingestion_uses_only_selected_property_curated_markdown(self):
        _, account, _ = self.start()
        one = Path(account["properties"]["property-one"]["runtime_dir"])
        two = Path(account["properties"]["property-two"]["runtime_dir"])
        (one / "docs/guide.md").write_text("One's guest-safe guide")
        (one / "source-documents/private.md").write_text("Private door secret")
        (two / "docs/guide.md").write_text("Two's different guide")
        records = markdown_chunks(one)
        self.assertEqual([x[1] for x in records], ["One's guest-safe guide"])

    def test_symlinked_docs_cannot_read_another_property(self):
        _, account, _ = self.start()
        one = Path(account["properties"]["property-one"]["runtime_dir"])
        two = Path(account["properties"]["property-two"]["runtime_dir"])
        (two / "docs/guide.md").write_text("Another property's facts")
        (one / "docs").rmdir()
        (one / "docs").symlink_to(two / "docs", target_is_directory=True)
        with self.assertRaises(ValueError):
            markdown_chunks(one)

    def test_failed_rebuild_preserves_previous_generation(self):
        _, account, _ = self.start()
        root = Path(account["properties"]["property-one"]["runtime_dir"])
        (root / "docs/guide.md").write_text("Approved facts")
        previous = rebuild(root, builder=lambda path, rows: None)
        with self.assertRaises(RuntimeError):
            rebuild(root, builder=Mock(side_effect=RuntimeError("builder failed")))
        self.assertEqual(active_index(root).name, previous["generation"])

    def test_empty_docs_preserve_existing_index(self):
        _, account, _ = self.start()
        root = Path(account["properties"]["property-one"]["runtime_dir"])
        guide = root / "docs/guide.md"
        guide.write_text("Approved")
        previous = rebuild(root, builder=lambda path, rows: None)
        guide.unlink()
        with self.assertRaises(ValueError):
            rebuild(root, builder=lambda path, rows: None)
        self.assertEqual(active_index(root).name, previous["generation"])

    def test_manifest_cannot_select_another_property(self):
        _, account, _ = self.start()
        root = Path(account["properties"]["property-one"]["runtime_dir"])
        (root / "index/current.json").write_text(json.dumps({"schema": 1, "generation": "../property-two"}))
        with self.assertRaises(ValueError):
            active_index(root)

    def test_daily_index_uses_explicit_timezone_and_catches_up_once(self):
        settings = {"daily_at": "00:00", "timezone": "Europe/London"}
        now = datetime(2026, 9, 30, 23, 1, tzinfo=timezone.utc)
        self.assertTrue(due(settings, now, "2026-09-30"))
        self.assertFalse(due(settings, now, "2026-10-01"))

    def test_disabled_mcp_and_worker_readiness(self):
        self.start()
        from hosting.container_app import create_app
        app = create_app()
        # No lifespan: avoid real background API calls in this test.
        client = TestClient(app)
        with patch("hosting.container_app.requests.get", return_value=Mock(status_code=200, json=lambda: {"ok": True})):
            self.assertEqual(client.get("/ready").status_code, 200)
        with patch("hosting.container_app.requests.get", side_effect=RuntimeError("offline")):
            self.assertEqual(client.get("/ready").status_code, 503)
        self.assertEqual(client.post("/toolkit/owner-a/mcp").status_code, 404)

    def test_same_port_authenticated_mcp_and_account_webhook(self):
        self.config["mcp"] = {"enabled": True, "resource_url": "https://example.com/toolkit/owner-a/mcp", "issuer_url": "https://example.com"}
        self.save()
        (self.data / "config/mcp_clients.json").write_text(json.dumps({"clients": {"owner": {"token_env": "OWNER_MCP_TOKEN", "accounts": {"owner-a": {"permissions": ["read"], "properties": {"property-one": ["read"]}}}}}}))
        self.credentials.write_text(self.credentials.read_text() + "OWNER_MCP_TOKEN=mcp-only-key\n")
        self.start()
        from hosting.container_app import create_app
        app = create_app()
        headers = {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-03-26"}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_access", "arguments": {}}}
        with TestClient(app, base_url="https://example.com") as client:
            self.assertEqual(client.post("/toolkit/owner-a/mcp", headers=headers, json=message).status_code, 401)
            response = client.post("/toolkit/owner-a/mcp", headers={**headers, "Authorization": "Bearer mcp-only-key"}, json=message)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("property-two", response.text)
            event = {"action": "message.created", "data": {"id": "persisted-event"}}
            self.assertEqual(client.post("/webhook/hospitable/other?token=hook-key", json=event).status_code, 401)
            # Ignored events exercise HTTP without launching provider API calls.
            self.assertEqual(client.post("/webhook/hospitable/owner-a?token=hook-key", json={"action": "unsupported", "data": {}}).status_code, 200)
