import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from hosting.config import load_registry
from hosting.gateway import Inbox, Unresolved, create_app, deliver, resolve_property
from hosting import worker


class HostingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "A_PAT": "account-a-key", "B_PAT": "account-b-key",
            "A_SECRET": "account-a-hook", "B_SECRET": "account-b-hook",
            "A_WORKER": "worker-a-secret", "B_WORKER": "worker-b-secret",
            "A_MODEL": "model-key", "B_MODEL": "model-key",
            "TOOLKIT_ADMIN_SECRET": "admin-token",
            "TOOLKIT_CONTROLS_DB": str(self.root / "controls.sqlite3"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.accounts = {}
        for label, port in (("a", 8791), ("b", 8792)):
            prefix = label.upper()
            self.accounts[label] = {"api_key_env": prefix + "_PAT", "webhook_secret_env": prefix + "_SECRET",
                "properties": {"property-" + label: {"name": label, "timezone": "Europe/London",
                    "runtime_dir": str(self.root / label), "worker_url": f"http://127.0.0.1:{port}",
                    "worker_secret_env": prefix + "_WORKER", "model_key_env": prefix + "_MODEL"}}}
        self.registry = self.root / "accounts.json"
        self.save()
        self.inbox = Inbox(self.root / "inbox.sqlite3")
        self.payload = {"action": "message.created", "data": {"id": "same-message-id",
            "reservation_id": "reservation-id", "sender_type": "guest", "body": "Where are the towels?"}}

    def save(self):
        self.registry.write_text(json.dumps({"accounts": self.accounts}))

    def response(self, property_id="property-a", status=200):
        response = Mock(status_code=status)
        response.json.return_value = {"data": {"property": {"id": property_id}}}
        return response

    def test_account_key_is_used_for_resolution(self):
        get = Mock(return_value=self.response("property-b"))
        self.assertEqual(resolve_property(self.accounts["b"], self.payload, get), "property-b")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer account-b-key")

    def test_unmanaged_property_never_falls_back(self):
        with self.assertRaises(Unresolved):
            resolve_property(self.accounts["b"], self.payload, Mock(return_value=self.response()))

    def test_spoofed_payload_property_rejected(self):
        self.payload["data"]["property_id"] = "property-b"
        with self.assertRaises(Unresolved):
            resolve_property(self.accounts["a"], self.payload, Mock(return_value=self.response()))

    def test_payload_property_alone_is_insufficient(self):
        self.payload["data"].pop("reservation_id")
        self.payload["data"]["property_id"] = "property-a"
        with self.assertRaises(Unresolved):
            resolve_property(self.accounts["a"], self.payload, Mock())

    def test_inquiry_resolution(self):
        self.payload["data"].pop("reservation_id")
        self.payload["data"]["conversation_id"] = "inquiry/id"
        get = Mock(return_value=self.response())
        resolve_property(self.accounts["a"], self.payload, get)
        self.assertTrue(get.call_args.args[0].endswith("/inquiries/inquiry%2Fid"))

    def test_missing_api_property_requires_review(self):
        response = self.response()
        response.json.return_value = {"data": {}}
        with self.assertRaises(Unresolved):
            resolve_property(self.accounts["a"], self.payload, Mock(return_value=response))

    def test_account_deduplication_is_isolated_and_durable(self):
        self.inbox.add("a", self.payload)
        self.inbox.add("a", self.payload)
        self.inbox.add("b", self.payload)
        reopened = Inbox(self.inbox.path)
        self.assertEqual(len(reopened.pending("a")), 1)
        self.assertEqual(len(reopened.pending("b")), 1)

    def test_wrong_account_token_rejected(self):
        client = TestClient(create_app(self.registry, self.inbox.path))
        self.assertEqual(client.post("/webhook/hospitable/a?token=account-b-hook", json=self.payload).status_code, 401)
        self.assertEqual(self.inbox.counts(), [])

    def test_action_format_accepted_and_stored_before_ack(self):
        client = TestClient(create_app(self.registry, self.inbox.path))
        response = client.post("/webhook/hospitable/a?token=account-a-hook", json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "queued")
        self.assertEqual(len(self.inbox.pending("a")), 1)

    def test_legacy_route_not_registered_on_gateway(self):
        client = TestClient(create_app(self.registry, self.inbox.path))
        self.assertEqual(client.post("/webhook/hospitable", json=self.payload).status_code, 404)

    def test_reservation_events_cannot_enter_message_dedupe(self):
        self.payload["action"] = "reservation.changed"
        client = TestClient(create_app(self.registry, self.inbox.path))
        self.assertEqual(client.post("/webhook/hospitable/a?token=account-a-hook", json=self.payload).json()["action"], "ignored")
        self.assertEqual(self.inbox.counts(), [])

    def test_body_limit(self):
        client = TestClient(create_app(self.registry, self.inbox.path))
        self.assertEqual(client.post("/webhook/hospitable/a?token=account-a-hook", content="x" * (1024 * 1024 + 1)).status_code, 413)

    def test_unavailable_worker_retries_without_secret_in_reason(self):
        self.inbox.add("a", self.payload)
        row = self.inbox.pending("a")[0]
        with patch("hosting.gateway.resolve_property", return_value="property-a"), patch("hosting.gateway.requests.post", side_effect=RuntimeError("worker-a-secret")):
            deliver(self.inbox, self.accounts["a"], row)
        with self.inbox.connect() as db:
            saved = db.execute("SELECT * FROM inbox").fetchone()
        self.assertEqual(saved["state"], "pending")
        self.assertGreater(saved["next_attempt"], saved["received"])
        self.assertNotIn("worker-a-secret", saved["reason"])

    def test_unknown_property_retained_for_review(self):
        self.inbox.add("a", self.payload)
        with patch("hosting.gateway.resolve_property", side_effect=Unresolved("Unmanaged property")), patch("hosting.gateway.requests.post") as post:
            deliver(self.inbox, self.accounts["a"], self.inbox.pending("a")[0])
        post.assert_not_called()
        self.assertEqual(self.inbox.counts()[0]["state"], "review")

    def test_overlapping_runtime_directories_rejected(self):
        self.accounts["b"]["properties"]["property-b"]["runtime_dir"] = str(self.root / "a" / "nested")
        self.save()
        with self.assertRaises(ValueError):
            load_registry(self.registry)

    def test_duplicate_worker_ports_rejected(self):
        self.accounts["b"]["properties"]["property-b"]["worker_url"] = "http://127.0.0.1:8791"
        self.save()
        with self.assertRaises(ValueError):
            load_registry(self.registry)

    def test_remote_worker_rejected(self):
        self.accounts["b"]["properties"]["property-b"]["worker_url"] = "http://example.com:8792"
        self.save()
        with self.assertRaises(ValueError):
            load_registry(self.registry)

    def test_property_worker_rechecks_routing_and_stores_draft(self):
        with patch.dict(os.environ, {"TOOLKIT_ACCOUNT_ID": "a", "HOSPITABLE_PROPERTY_UUID": "property-a",
                                    "TOOLKIT_ACCOUNTS_FILE": str(self.registry)}):
            client = TestClient(worker.create_app())
        with patch("hosting.worker.resolve_property", return_value="property-a"), patch("hosting.worker.prepare_draft", return_value={"action": "draft", "answer": "In the cupboard.", "reason": "Property guide"}) as draft:
            first = client.post("/webhook/hospitable?token=worker-a-secret", json=self.payload)
            second = client.post("/webhook/hospitable?token=worker-a-secret", json=self.payload)
        self.assertEqual(first.json()["sent"], False)
        self.assertEqual(second.json()["action"], "duplicate")
        self.assertEqual(draft.call_count, 1)

    def test_worker_rejects_other_property(self):
        with patch.dict(os.environ, {"TOOLKIT_ACCOUNT_ID": "a", "HOSPITABLE_PROPERTY_UUID": "property-a",
                                    "TOOLKIT_ACCOUNTS_FILE": str(self.registry)}):
            client = TestClient(worker.create_app())
        with patch("hosting.worker.resolve_property", return_value="property-b"), patch("hosting.worker.prepare_draft") as draft:
            self.assertEqual(client.post("/webhook/hospitable?token=worker-a-secret", json=self.payload).status_code, 409)
        draft.assert_not_called()

    def test_incident_gate_needs_no_model_call(self):
        self.assertEqual(worker.prepare_draft({}, "There is a gas leak")["action"], "review")

    def test_two_properties_within_one_account(self):
        self.accounts["a"]["properties"]["property-b"] = self.accounts["b"]["properties"]["property-b"]
        get = Mock(return_value=self.response("property-b"))
        self.assertEqual(resolve_property(self.accounts["a"], self.payload, get), "property-b")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer account-a-key")

    def test_worker_launch_does_not_inherit_other_account_secrets(self):
        from hosting import manage
        (self.root / "a").mkdir()
        with patch.dict(os.environ, {"TOOLKIT_ACCOUNTS_FILE": str(self.registry), "OTHER_INSTANCE_HA_TOKEN": "private-ha-key"}), \
                patch("sys.argv", ["manage", "worker", "a", "property-a"]), patch("hosting.manage.os.execve") as execute:
            manage.main()
        env = execute.call_args.args[2]
        self.assertIn("A_PAT", env)
        self.assertNotIn("B_PAT", env)
        self.assertNotIn("B_SECRET", env)
        self.assertNotIn("OTHER_INSTANCE_HA_TOKEN", env)
        selected = json.loads((self.root / "a" / "state/worker-registry.json").read_text())
        self.assertEqual(list(selected["accounts"]), ["a"])
        self.assertEqual(list(selected["accounts"]["a"]["properties"]), ["property-a"])

    def test_reindex_scopes_data_without_source_conversion(self):
        from hosting import manage
        (self.root / "a").mkdir()
        with patch.dict(os.environ, {"TOOLKIT_ACCOUNTS_FILE": str(self.registry)}), \
                patch("sys.argv", ["manage", "reindex", "a", "property-a"]), patch("hosting.manage.subprocess.run") as run:
            manage.main()
        self.assertTrue(run.call_args.args[0][-1].endswith("ingest.py"))
        self.assertEqual(run.call_args.kwargs["env"]["TOOLKIT_PROPERTY_DIR"], str(self.root / "a"))
        self.assertNotIn("A_PAT", run.call_args.kwargs["env"])

    def make_tools(self):
        from hosting.mcp_tools import HostedTools
        self.clients_file = self.root / "clients.json"
        self.clients_file.write_text(json.dumps({"clients": {
            "reader": {"token_env": "READER_TOKEN", "accounts": {
                "a": {"properties": {"property-a": ["read"]}}}},
            "owner": {"token_env": "OWNER_TOKEN", "accounts": {
                "a": {"permissions": ["read", "settings"], "properties": {"property-a": ["read", "preview", "settings"]}}}}
        }}))
        os.environ["READER_TOKEN"] = "reader-token"
        os.environ["OWNER_TOKEN"] = "owner-token"
        return HostedTools(self.accounts, self.clients_file, self.root / "controls.sqlite3", self.inbox.path)

    def test_account_shadow_overrides_property_live_request(self):
        tools = self.make_tools()
        tools.set_settings("owner-token", "a", "property-a", shadow=False)
        self.assertEqual(tools.get_settings("owner-token", "a", "property-a")["mode"], "shadow")

    def test_property_shadow_overrides_account_live_request(self):
        tools = self.make_tools()
        tools.set_settings("owner-token", "a", shadow=False)
        self.assertEqual(tools.get_settings("owner-token", "a", "property-a")["mode"], "shadow")

    def test_both_shadow_off_never_enables_unimplemented_sends(self):
        tools = self.make_tools()
        tools.set_settings("owner-token", "a", shadow=False)
        tools.set_settings("owner-token", "a", "property-a", shadow=False)
        settings = tools.get_settings("owner-token", "a", "property-a")
        self.assertEqual(settings["mode"], "live_unavailable")
        self.assertFalse(settings["live_sending_available"])

    def test_account_disable_overrides_property_enable_and_persists(self):
        from hosting.controls import Controls
        tools = self.make_tools()
        tools.set_settings("owner-token", "a", enabled=False)
        tools.set_settings("owner-token", "a", "property-a", enabled=True)
        reloaded = Controls(self.root / "controls.sqlite3")
        self.assertEqual(reloaded.effective("a", self.accounts["a"], "property-a")["mode"], "disabled")
        self.assertEqual(reloaded.effective("b", self.accounts["b"], "property-b")["mode"], "shadow")

    def test_disabled_property_remains_pending_without_worker_call(self):
        tools = self.make_tools()
        tools.set_settings("owner-token", "a", "property-a", enabled=False)
        self.inbox.add("a", self.payload)
        with patch("hosting.gateway.resolve_property", return_value="property-a"), patch("hosting.gateway.requests.post") as post:
            deliver(self.inbox, self.accounts["a"], self.inbox.pending("a")[0], controls=tools.controls)
        post.assert_not_called()
        self.assertEqual(self.inbox.counts()[0]["state"], "pending")

    def test_disabled_worker_rejects_before_claiming_event(self):
        tools = self.make_tools()
        with patch.dict(os.environ, {"TOOLKIT_ACCOUNT_ID": "a", "HOSPITABLE_PROPERTY_UUID": "property-a",
                                    "TOOLKIT_ACCOUNTS_FILE": str(self.registry)}):
            client = TestClient(worker.create_app())
        tools.set_settings("owner-token", "a", "property-a", enabled=False)
        with patch("hosting.worker.resolve_property") as resolve:
            self.assertEqual(client.post("/webhook/hospitable?token=worker-a-secret", json=self.payload).status_code, 503)
        resolve.assert_not_called()
        self.assertEqual(tools.recent_drafts("owner-token", "a", "property-a"), [])

    def test_every_scoped_tool_rejects_other_account(self):
        from hosting.access import Denied
        tools = self.make_tools()
        calls = [lambda: tools.get_settings("owner-token", "b", "property-b"),
                 lambda: tools.set_settings("owner-token", "b", "property-b", enabled=False),
                 lambda: tools.search("owner-token", "b", "property-b", "wifi"),
                 lambda: tools.preview("owner-token", "b", "property-b", "wifi"),
                 lambda: tools.recent_drafts("owner-token", "b", "property-b"),
                 lambda: tools.inbox_status("owner-token", "b", "property-b")]
        for call in calls:
            with self.assertRaises(Denied):
                call()

    def test_other_property_in_same_account_rejected(self):
        from hosting.access import Denied
        tools = self.make_tools()
        self.accounts["a"]["properties"]["second"] = {**self.accounts["a"]["properties"]["property-a"], "runtime_dir": str(self.root / "second")}
        with self.assertRaises(Denied):
            tools.recent_drafts("owner-token", "a", "second")
        with self.assertRaises(Denied):
            tools.set_settings("owner-token", "a", "second", shadow=False)

    def test_reader_cannot_preview_change_settings_or_read_account_totals(self):
        from hosting.access import Denied
        tools = self.make_tools()
        self.assertEqual(tools.get_settings("reader-token", "a", "property-a")["mode"], "shadow")
        for call in [lambda: tools.preview("reader-token", "a", "property-a", "hello"),
                     lambda: tools.set_settings("reader-token", "a", "property-a", shadow=False),
                     lambda: tools.inbox_status("reader-token", "a")]:
            with self.assertRaises(Denied):
                call()

    def test_revoked_client_and_rotated_token_take_effect_without_restart(self):
        from hosting.access import Denied
        tools = self.make_tools()
        os.environ["READER_TOKEN"] = "rotated-token"
        with self.assertRaises(Denied):
            tools.list_access("reader-token")
        self.assertEqual(len(tools.list_access("rotated-token")), 1)
        registry = json.loads(self.clients_file.read_text())
        registry["clients"]["reader"]["enabled"] = False
        self.clients_file.write_text(json.dumps(registry))
        with self.assertRaises(Denied):
            tools.list_access("rotated-token")

    def test_mcp_bearer_tokens_are_not_webhook_tokens(self):
        from hosting.access import Denied
        tools = self.make_tools()
        with self.assertRaises(Denied):
            tools.list_access("account-a-hook")
        self.assertEqual([x["account_id"] for x in tools.list_access("owner-token")], ["a"])

    def test_controls_audit_records_actor_and_scope(self):
        tools = self.make_tools()
        tools.set_settings("owner-token", "a", "property-a", enabled=False)
        with tools.controls.connect() as db:
            row = db.execute("SELECT actor,account,property FROM control_audit").fetchone()
        self.assertEqual(tuple(row), ("owner", "a", "property-a"))

    def test_mcp_transport_requires_auth_and_enforces_tool_scope(self):
        from hosting.mcp_server import create_app
        self.make_tools()
        with patch.dict(os.environ, {"TOOLKIT_ACCOUNTS_FILE": str(self.registry),
                "TOOLKIT_MCP_CLIENTS_FILE": str(self.clients_file), "TOOLKIT_INBOX_DB": str(self.inbox.path),
                "TOOLKIT_MCP_RESOURCE_URL": "https://example.com/toolkit/mcp", "TOOLKIT_MCP_ISSUER_URL": "https://example.com"}):
            app = create_app()
        headers = {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-03-26"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_settings", "arguments": {"account_id": "b", "property_id": "property-b"}}}
        with TestClient(app, base_url="https://example.com") as client:
            self.assertEqual(client.post("/toolkit/mcp", headers=headers, json=request).status_code, 401)
            response = client.post("/toolkit/mcp", headers={**headers, "Authorization": "Bearer owner-token"}, json=request)
            allowed = client.post("/toolkit/mcp", headers={**headers, "Authorization": "Bearer owner-token"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_access", "arguments": {}}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"]["isError"])
        self.assertNotIn("runtime_dir", response.text)
        self.assertFalse(allowed.json()["result"].get("isError", False))
        self.assertNotIn("property-b", allowed.text)

    def test_reenable_property_wakes_backed_off_events(self):
        tools = self.make_tools()
        self.inbox.add("a", self.payload)
        row = self.inbox.pending("a")[0]
        self.inbox.update(row["id"], "pending", "property-a", retry=True)
        self.assertEqual(self.inbox.pending("a"), [])
        tools.set_settings("owner-token", "a", "property-a", enabled=True)
        self.assertEqual(len(self.inbox.pending("a")), 1)

    def test_mcp_token_reuse_of_webhook_secret_rejected(self):
        tools = self.make_tools()
        os.environ["READER_TOKEN"] = "account-a-hook"
        with self.assertRaises(ValueError):
            tools.access.clients()

    def setup_notifications(self):
        tools = self.make_tools()
        os.environ["A_HA_URL"] = "http://192.0.2.10:8123/api/webhook/test-only"
        self.accounts["a"]["home_assistant"] = {"enabled": False, "heating_enabled": True}
        self.accounts["a"]["notifications"] = {
            "ha": {"enabled": True, "webhook_url_env": "A_HA_URL"},
            "email": {"enabled": True, "msmtp_config_file": str(self.root / "a-msmtp.conf"),
                      "msmtp_account": "owner-a", "sender": "host-a@example.com", "recipients": ["host-a@example.com"]}}
        return tools

    def test_email_works_without_home_assistant_or_vpn(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        outbox = Outbox(self.root / "alerts.sqlite3")
        outbox.enqueue("one", "a", "property-a", {"reason": "Host review"})
        with patch("hosting.notifications.requests.post") as ha, patch("hosting.notifications.subprocess.run") as smtp:
            outbox.drain(self.accounts, tools.controls)
        ha.assert_not_called()
        self.assertEqual(smtp.call_count, 1)
        self.assertIn("--account=owner-a", smtp.call_args.args[0])
        self.assertIn("--file=" + str(self.root / "a-msmtp.conf"), smtp.call_args.args[0])
        self.assertIn(b"To: host-a@example.com", smtp.call_args.kwargs["input"])

    def test_paused_alerts_do_not_starve_enabled_channels_across_pages(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        outbox = Outbox(self.root / "alerts.sqlite3")
        for number in range(120):
            outbox.enqueue(str(number), "a", "property-a", {"reason": "Review"})
        with patch("hosting.notifications.requests.post") as ha, patch("hosting.notifications.subprocess.run") as smtp:
            for _ in range(6):
                outbox.drain(self.accounts, tools.controls)
        ha.assert_not_called()
        self.assertEqual(smtp.call_count, 120)
        with outbox.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM notification_outbox WHERE channel='ha' AND state='pending'").fetchone()[0], 120)

    def test_ha_and_heating_master_off_override_property_on(self):
        from hosting.controls import require_ha
        tools = self.setup_notifications()
        tools.controls.set("owner", "a", "property-a", ha_enabled=True, heating_enabled=True)
        effective = tools.get_settings("owner-token", "a", "property-a")
        self.assertFalse(effective["ha_enabled"])
        self.assertFalse(effective["heating_enabled"])
        with self.assertRaises(PermissionError):
            require_ha(effective, "heating")

    def test_no_toolkit_heating_operations_are_available_even_when_requested(self):
        from hosting.controls import require_ha
        tools = self.setup_notifications()
        tools.set_settings("owner-token", "a", ha_enabled=True, heating_enabled=True)
        effective = tools.get_settings("owner-token", "a", "property-a")
        self.assertTrue(effective["heating_enabled"])
        self.assertFalse(effective["heating_available"])
        with self.assertRaises(PermissionError):
            require_ha(effective, "heating")

    def test_email_account_master_off_overrides_property_on(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        tools.set_settings("owner-token", "a", email_enabled=False)
        tools.set_settings("owner-token", "a", "property-a", email_enabled=True)
        outbox = Outbox(self.root / "alerts.sqlite3")
        outbox.enqueue("one", "a", "property-a", {})
        with patch("hosting.notifications.subprocess.run") as smtp:
            outbox.drain(self.accounts, tools.controls)
        smtp.assert_not_called()
        self.assertFalse(tools.get_settings("owner-token", "a", "property-a")["email_enabled"])

    def test_property_email_uses_its_override_recipients(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        self.accounts["a"]["properties"]["property-a"]["notifications"] = {"email": {"recipients": ["property-manager@example.com"]}}
        outbox = Outbox(self.root / "alerts.sqlite3")
        outbox.enqueue("one", "a", "property-a", {})
        with patch("hosting.notifications.subprocess.run") as smtp:
            outbox.drain(self.accounts, tools.controls)
        self.assertIn(b"To: property-manager@example.com", smtp.call_args.kwargs["input"])

    def test_ha_property_disable_prevents_requests(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        tools.set_settings("owner-token", "a", ha_enabled=True)
        tools.set_settings("owner-token", "a", "property-a", ha_enabled=False)
        outbox = Outbox(self.root / "alerts.sqlite3")
        outbox.enqueue("one", "a", "property-a", {})
        with patch("hosting.notifications.requests.post") as ha, patch("hosting.notifications.subprocess.run"):
            outbox.drain(self.accounts, tools.controls)
        ha.assert_not_called()

    def test_ha_enabled_uses_only_this_account_endpoint(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        tools.set_settings("owner-token", "a", ha_enabled=True, email_enabled=False)
        outbox = Outbox(self.root / "alerts.sqlite3")
        outbox.enqueue("one", "a", "property-a", {})
        with patch("hosting.notifications.requests.post", return_value=Mock(status_code=200)) as ha:
            outbox.drain(self.accounts, tools.controls)
        self.assertEqual(ha.call_args.args[0], os.environ["A_HA_URL"])

    def test_failed_alert_is_durable_and_sanitized(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        outbox = Outbox(self.root / "alerts.sqlite3")
        outbox.enqueue("one", "a", "property-a", {})
        with patch("hosting.notifications.subprocess.run", side_effect=RuntimeError("smtp-password-secret")):
            outbox.drain(self.accounts, tools.controls)
        reopened = Outbox(outbox.path)
        with reopened.connect() as db:
            row = db.execute("SELECT state,reason,attempts FROM notification_outbox WHERE channel='email'").fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertNotIn("smtp-password-secret", row["reason"])

    def test_duplicate_review_does_not_resend_completed_email(self):
        from hosting.notifications import Outbox
        tools = self.setup_notifications()
        outbox = Outbox(self.root / "alerts.sqlite3")
        outbox.enqueue("one", "a", "property-a", {})
        with patch("hosting.notifications.subprocess.run") as smtp:
            outbox.drain(self.accounts, tools.controls)
            outbox.enqueue("one", "a", "property-a", {})
            outbox.drain(self.accounts, tools.controls)
        self.assertEqual(smtp.call_count, 1)

    def test_reader_cannot_enable_notifications_or_read_other_account_alerts(self):
        from hosting.access import Denied
        tools = self.setup_notifications()
        with self.assertRaises(Denied):
            tools.set_settings("reader-token", "a", "property-a", email_enabled=True)
        with self.assertRaises(Denied):
            tools.notification_status("owner-token", "b", "property-b")

    def test_old_control_database_migrates_without_losing_paused_state(self):
        import sqlite3
        from hosting.controls import Controls
        path = self.root / "old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE controls(account TEXT,property TEXT,enabled INTEGER,shadow INTEGER,PRIMARY KEY(account,property))")
            db.execute("INSERT INTO controls VALUES('a','',0,1)")
        controls = Controls(path)
        self.assertEqual(controls.effective("a", self.accounts["a"], "property-a")["mode"], "disabled")


if __name__ == "__main__":
    unittest.main()
