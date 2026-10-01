"""Shared persistent account/property controls. No secret values are stored."""
import os
import sqlite3
import time
from pathlib import Path


def control_path(default):
    return Path(os.environ.get("TOOLKIT_CONTROLS_DB", default))


class Controls:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS controls (account TEXT, property TEXT, enabled INTEGER, shadow INTEGER, PRIMARY KEY(account,property))")
            db.execute("CREATE TABLE IF NOT EXISTS control_audit (at REAL, actor TEXT, account TEXT, property TEXT, enabled INTEGER, shadow INTEGER)")
            for table in ("controls", "control_audit"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column in ("ha_enabled", "email_enabled", "heating_enabled", "ha_alerts_enabled"):
                    if column not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER")
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def get(self, account_id, property_id, defaults):
        result = {"enabled": defaults.get("enabled", True), "shadow": defaults.get("shadow", True)}
        notifications = defaults.get("notifications", {})
        result.update({channel + "_enabled": notifications.get(channel, {}).get("enabled", True if property_id else False)
                       for channel in ("ha", "email")})
        integration = defaults.get("home_assistant", {})
        result["ha_enabled"] = integration.get("enabled", result["ha_enabled"])
        result["heating_enabled"] = integration.get("heating_enabled", bool(property_id))
        result["ha_alerts_enabled"] = notifications.get("ha", {}).get("enabled", bool(property_id))
        with self.connect() as db:
            row = db.execute("SELECT enabled,shadow,ha_enabled,email_enabled,heating_enabled,ha_alerts_enabled FROM controls WHERE account=? AND property=?", (account_id, property_id or "")).fetchone()
        if row:
            for key in result:
                if row[key] is not None:
                    result[key] = bool(row[key])
        return result

    def set(self, actor, account_id, property_id=None, enabled=None, shadow=None, ha_enabled=None, email_enabled=None, heating_enabled=None, ha_alerts_enabled=None):
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if shadow is not None and type(shadow) is not bool:
            raise ValueError("shadow must be a boolean")
        for value in (ha_enabled, email_enabled, heating_enabled, ha_alerts_enabled):
            if value is not None and type(value) is not bool:
                raise ValueError("Notification switches must be boolean")
        if all(value is None for value in (enabled, shadow, ha_enabled, email_enabled, heating_enabled, ha_alerts_enabled)):
            raise ValueError("Specify a processing or notification switch")
        with self.connect() as db:
            db.execute("""INSERT INTO controls(account,property,enabled,shadow,ha_enabled,email_enabled,heating_enabled,ha_alerts_enabled) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account,property)
                DO UPDATE SET enabled=COALESCE(excluded.enabled,controls.enabled),
                shadow=COALESCE(excluded.shadow,controls.shadow),
                ha_enabled=COALESCE(excluded.ha_enabled,controls.ha_enabled),
                email_enabled=COALESCE(excluded.email_enabled,controls.email_enabled),
                heating_enabled=COALESCE(excluded.heating_enabled,controls.heating_enabled),
                ha_alerts_enabled=COALESCE(excluded.ha_alerts_enabled,controls.ha_alerts_enabled)""",
                (account_id, property_id or "", enabled, shadow, ha_enabled, email_enabled, heating_enabled, ha_alerts_enabled))
            db.execute("INSERT INTO control_audit(at,actor,account,property,enabled,shadow,ha_enabled,email_enabled,heating_enabled,ha_alerts_enabled) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), actor, account_id, property_id or "", enabled, shadow, ha_enabled, email_enabled, heating_enabled, ha_alerts_enabled))

    def effective(self, account_id, account, property_id=None):
        parent = self.get(account_id, None, account)
        child = self.get(account_id, property_id, account["properties"][property_id]) if property_id else None
        enabled = parent["enabled"] and (child is None or child["enabled"])
        shadow = parent["shadow"] or (child is not None and child["shadow"])
        result = {"account": parent, "property": child, "enabled": enabled, "shadow": shadow,
                  "mode": "disabled" if not enabled else "shadow" if shadow else "live_unavailable",
                  "live_sending_available": False}
        for channel in ("ha", "email"):
            result[channel + "_enabled"] = parent[channel + "_enabled"] and (child is None or child[channel + "_enabled"])
        result["heating_enabled"] = result["ha_enabled"] and parent["heating_enabled"] and (child is None or child["heating_enabled"])
        result["heating_available"] = False  # Existing local HA heating is outside this application.
        result["ha_alerts_enabled"] = result["ha_enabled"] and parent["ha_alerts_enabled"] and (child is None or child["ha_alerts_enabled"])
        import os
        if os.environ.get('TOOLKIT_INSTANCE_MODE') == 'container':
            for scope in (parent, child, result):
                if scope is not None:
                    for key in ('ha_enabled', 'ha_alerts_enabled', 'heating_enabled'):
                        scope[key] = False
        return result


def require_ha(effective, feature="alerts"):
    if not effective["enabled"] or not effective["ha_enabled"]:
        raise PermissionError("Home Assistant integration is disabled")
    if feature == "heating" and (not effective["heating_enabled"] or not effective["heating_available"]):
        raise PermissionError("Toolkit heating connector is unavailable or disabled")
    if feature == "alerts" and not effective["ha_alerts_enabled"]:
        raise PermissionError("Home Assistant alerts are disabled")
    if feature not in {"alerts", "heating"}:
        raise PermissionError("Unsupported Home Assistant feature")
