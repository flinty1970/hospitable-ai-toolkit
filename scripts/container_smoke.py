"""Real Docker smoke; dummy credentials, no provider/model/SMTP calls."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def request(base, path, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=10) as response:
        return response.status, json.load(response)


def wait_ready(base):
    for _ in range(120):
        try:
            if request(base, "/ready")[0] == 200:
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise RuntimeError("Container did not become ready")


def main():
    image = sys.argv[1]
    with_index = '--with-index' in sys.argv[2:]
    names = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        try:
            for account_id in ("one", "two"):
                data = root / account_id / "data"
                secret_dir = root / account_id / "secrets"
                (data / "config").mkdir(parents=True)
                secret_dir.mkdir()
                config = {"account": {"id": account_id, "notifications": {"email": {"enabled": False}}, "properties": {
                    "property-one": {"name": "One", "timezone": "Europe/London"},
                    "property-two": {"name": "Two", "timezone": "UTC"}}}}
                (data / "config/account.json").write_text(json.dumps(config))
                credentials = secret_dir / "credentials.env"
                credentials.write_text(f"HOSPITABLE_PAT=unused-{account_id}-key\nHOSPITABLE_WEBHOOK_SECRET={account_id}-hook\nTOOLKIT_ADMIN_SECRET={account_id}-admin\nANTHROPIC_API_KEY=unused-{account_id}-model\n")
                credentials.chmod(0o600)
                name = "toolkit-smoke-" + uuid.uuid4().hex[:12]
                names.append(name)
                docker("run", "-d", "--name", name, "--user", f"{os.getuid()}:{os.getgid()}", "-p", "127.0.0.1::8790", "-v", f"{data}:/data", "-v", f"{data / 'config'}:/data/config:ro", "-v", f"{secret_dir}:/run/toolkit-secrets:ro", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m,mode=1777", "--cap-drop", "ALL", image)
                port = docker("port", name, "8790/tcp").split(":")[-1]
                base = "http://127.0.0.1:" + port
                wait_ready(base)
                settings = request(base, "/admin/settings", token=account_id + "-admin")[1]
                assert len(settings["properties"]) == 2 and settings["auto_responses_available"]
                assert request(base, '/admin/operations/probe', {}, token=account_id+'-admin')[1]['ok']
                assert request(base, '/admin/operations', token=account_id+'-admin')[1]['last_message_received'] is None
                assert request(base, '/admin/operations/reviews', token=account_id+'-admin')[1]['items'] == []
                assert request(base, "/admin/settings/controls", {"property_id": "property-one", "response_mode": "paused"}, token=account_id + "-admin")[1]["mode"] == "disabled"
                if account_id == "one" and with_index:
                    (data / "properties/property-one/docs/guide.md").write_text("Towels for property one are in the blue cupboard.")
                    (data / "properties/property-two/docs/guide.md").write_text("Towels for property two are in the green drawer.")
                    docker("exec", name, "python", "-m", "hosting.cli", "reindex", "property-one")
                    hits = json.loads(docker("exec", name, "python", "-c", "import json; from hosting.indexing import retrieve; print(json.dumps(retrieve('/data/properties/property-one', 'Where are towels?')))"))
                    assert hits and "blue cupboard" in hits[0]["text"]
                    assert all("green drawer" not in hit["text"] for hit in hits)
                try:
                    request(base, f"/webhook/hospitable/{account_id}?token=wrong", {"action": "message.created", "data": {"id": "dummy"}})
                    raise AssertionError("Unauthenticated webhook accepted")
                except urllib.error.HTTPError as error:
                    assert error.code == 401
                event = {"action": "message.created", "data": {"id": "dummy"}}
                assert request(base, f"/webhook/hospitable/{account_id}?token={account_id}-hook", event)[1]["action"] == "queued"
                docker("restart", "--time", "20", name)
                # An ephemeral published host port can change on restart.
                port = docker("port", name, "8790/tcp").split(":")[-1]
                base = "http://127.0.0.1:" + port
                wait_ready(base)
                settings = request(base, "/admin/settings", token=account_id + "-admin")[1]
                assert next(p for p in settings["properties"] if p["id"] == "property-one")["settings"]["mode"] == "disabled"
                try:
                    request(base, "/admin/settings/controls", {"property_id": "property-one", "response_mode": "draft"}, token=account_id + "-admin")
                    raise AssertionError("Processing enabled without tested owner email")
                except urllib.error.HTTPError as error:
                    assert error.code == 409
                request(base, f"/webhook/hospitable/{account_id}?token={account_id}-hook", event)
                counts = request(base, "/admin/inbox", token=account_id + "-admin")[1]["counts"]
                assert sum(row["count"] for row in counts) == 1
                assert all(row["account"] == account_id for row in counts)
                assert (data / "properties/property-one/state/worker-token").exists()
            assert len(names) == 2
            print("Two independent containers passed auth, readiness, restart and inbox persistence checks")
        finally:
            for name in names:
                subprocess.run(["docker", "logs", "--tail", "20", name], check=False)
                subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
