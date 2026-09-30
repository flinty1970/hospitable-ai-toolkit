"""Optional account-local daily ingestion; durable date markers and bounded retries."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from hosting.config import load_registry


def due(settings, now, last_date):
    hour, minute = map(int, settings.get("daily_at", "00:00").split(":"))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("daily_at must be HH:MM")
    local = now.astimezone(ZoneInfo(settings.get("timezone", "UTC")))
    date = local.date().isoformat()
    return date != last_date and (local.hour, local.minute) >= (hour, minute)


def main():
    accounts = load_registry(os.environ["TOOLKIT_ACCOUNTS_FILE"])
    aid, account = next(iter(accounts.items()))
    marker = Path(os.environ["TOOLKIT_DATA_DIR"]) / "state/index-schedule.json"
    completed = json.loads(marker.read_text()) if marker.exists() else {}
    retry_at = {}
    while True:
        for pid, prop in account["properties"].items():
            settings = {**account.get("indexing", {}), **prop.get("indexing", {})}
            now = datetime.now().astimezone()
            if not settings.get("enabled", False) or not due(settings, now, completed.get(pid)) or time.time() < retry_at.get(pid, 0):
                continue
            try:
                with (Path(prop["runtime_dir"]) / "logs/ingestion.log").open("a") as log:
                    log.write(f"{now.isoformat()} ingestion started\n")
                    log.flush()
                    subprocess.run([sys.executable, "-m", "hosting.manage", "reindex", aid, pid], stdout=log, stderr=log, check=True, timeout=3600)
                completed[pid] = now.astimezone(ZoneInfo(settings.get("timezone", "UTC"))).date().isoformat()
                temporary = marker.with_suffix(".tmp")
                temporary.write_text(json.dumps(completed))
                os.replace(temporary, marker)
            except Exception:
                retry_at[pid] = time.time() + 3600
                print("Property ingestion failed; retry in one hour", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
