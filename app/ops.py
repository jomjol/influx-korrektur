"""Änderungs-Operationen mit Protokoll (Audit-Log) und Undo.

Jede Änderung sichert vorher die betroffenen Originalpunkte (alle Fields, exakte Tags)
als JSON-Zeile in AUDIT_FILE. Darüber ist jede Aktion rückgängig zu machen.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime

from influx import InfluxClient, InfluxError, Point

_lock = threading.Lock()


class Operations:
    def __init__(self, client: InfluxClient, audit_file: str, mark_field: str = "korrektur_str"):
        self.c = client
        self.audit_file = audit_file
        self.mark_field = mark_field
        self._ftypes: dict[str, dict] = {}
        os.makedirs(os.path.dirname(os.path.abspath(audit_file)), exist_ok=True)

    # -------------------------------------------------- Protokoll
    def _log(self, entry: dict) -> dict:
        entry = {"id": uuid.uuid4().hex[:10],
                 "logged_at": datetime.now().astimezone().isoformat(timespec="seconds"), **entry}
        with _lock, open(self.audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def history(self) -> list[dict]:
        if not os.path.exists(self.audit_file):
            return []
        with open(self.audit_file, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        undone = {e["undo_of"] for e in entries if e.get("action") == "undo"}
        for e in entries:
            e["undone"] = e["id"] in undone
        return entries

    def _types(self, measurement: str) -> dict:
        if measurement not in self._ftypes:
            self._ftypes[measurement] = self.c.field_types(measurement)
        return self._ftypes[measurement]

    def _write(self, points: list[Point]) -> None:
        if points:
            self.c.write_points(points, self._types(points[0].measurement))
            self._ftypes.pop(points[0].measurement, None)  # neue Fields möglich

    # -------------------------------------------------- Aktionen
    def insert(self, measurement: str, entity: str, t_ns: int, value: float, comment: str = "") -> dict:
        existing = self.c.points_at(measurement, entity, t_ns)
        if existing:
            raise InfluxError("Zu diesem Zeitstempel existiert bereits ein Punkt – bitte 'Wert ändern' verwenden.")
        tags = self.c.tags_near(measurement, entity, t_ns)
        fields = {"value": float(value)}
        if self.mark_field:
            fields[self.mark_field] = "manuell"
        p = Point(measurement, tags, fields, int(t_ns))
        self._write([p])
        return self._log({"action": "insert", "measurement": measurement, "entity": entity,
                          "time_ns": int(t_ns), "comment": comment, "before": [], "after": [p.to_dict()]})

    def edit(self, measurement: str, entity: str, t_ns: int, value: float, comment: str = "") -> dict:
        before = self.c.points_at(measurement, entity, t_ns)
        if not before:
            raise InfluxError("Kein Punkt zu diesem Zeitstempel gefunden.")
        after = []
        for b in before:
            fields = {"value": float(value)}
            if self.mark_field:
                fields[self.mark_field] = "geändert"
            after.append(Point(measurement, b.tags, fields, b.time_ns))
        self._write(after)
        return self._log({"action": "edit", "measurement": measurement, "entity": entity,
                          "time_ns": int(t_ns), "comment": comment,
                          "before": [b.to_dict() for b in before], "after": [a.to_dict() for a in after]})

    def delete(self, measurement: str, entity: str, t_ns: int, comment: str = "") -> dict:
        before = self.c.points_at(measurement, entity, t_ns)
        if not before:
            raise InfluxError("Kein Punkt zu diesem Zeitstempel gefunden.")
        self.c.delete_at(measurement, entity, t_ns)
        return self._log({"action": "delete", "measurement": measurement, "entity": entity,
                          "time_ns": int(t_ns), "comment": comment,
                          "before": [b.to_dict() for b in before], "after": []})

    def undo(self, entry_id: str) -> dict:
        entries = {e["id"]: e for e in self.history()}
        e = entries.get(entry_id)
        if e is None:
            raise InfluxError("Protokolleintrag nicht gefunden.")
        if e.get("undone"):
            raise InfluxError("Diese Aktion wurde bereits rückgängig gemacht.")
        if e["action"] == "undo":
            raise InfluxError("Ein Undo kann nicht rückgängig gemacht werden.")
        m, ent, t = e["measurement"], e["entity"], int(e["time_ns"])
        if e["action"] == "insert":
            self.c.delete_at(m, ent, t)
        elif e["action"] == "edit":
            # Originalpunkte komplett wiederherstellen (inkl. ursprünglichem korrektur_str-Zustand)
            self.c.delete_at(m, ent, t)
            self._write([Point.from_dict(p) for p in e["before"]])
        elif e["action"] == "delete":
            self._write([Point.from_dict(p) for p in e["before"]])
        return self._log({"action": "undo", "undo_of": entry_id, "measurement": m, "entity": ent,
                          "time_ns": t, "comment": f"Undo von {e['action']}", "before": [], "after": []})
