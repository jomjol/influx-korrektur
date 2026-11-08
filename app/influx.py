"""Zugriffsschicht für InfluxDB 1.x und 2.x (über die v1-kompatible HTTP-API).

- Lesen:    /query  (InfluxQL, epoch=ns – Zeitstempel bleiben verlustfrei)
- Schreiben: /write (Line Protocol, precision=ns)
- Löschen:  1.x -> InfluxQL DELETE, 2.x -> /api/v2/delete
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests


# ---------------------------------------------------------------- Hilfsfunktionen
def q_ident(name: str) -> str:
    """Bezeichner für InfluxQL quoten ("kWh")."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def q_str(value: str) -> str:
    """String-Literal für InfluxQL quoten ('sensor.x')."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _esc_meas(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _esc_tag(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(",", "\\,")
             .replace("=", "\\=").replace(" ", "\\ "))


def _fmt_field(value, ftype: str | None) -> str:
    if ftype == "string" or (ftype is None and isinstance(value, str)):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    if ftype == "boolean" or (ftype is None and isinstance(value, bool)):
        return "true" if value else "false"
    if ftype == "integer":
        return f"{int(value)}i"
    if ftype == "unsigned":
        return f"{int(value)}u"
    return repr(float(value))  # float (Standard bei Home Assistant)


@dataclass
class Point:
    measurement: str
    tags: dict
    fields: dict
    time_ns: int

    def to_line(self, field_types: dict | None = None) -> str:
        field_types = field_types or {}
        tags = "".join(f",{_esc_tag(k)}={_esc_tag(str(v))}"
                       for k, v in sorted(self.tags.items()) if v not in (None, ""))
        fields = ",".join(f"{_esc_tag(k)}={_fmt_field(v, field_types.get(k))}"
                          for k, v in self.fields.items() if v is not None)
        if not fields:
            raise ValueError("Punkt ohne Fields kann nicht geschrieben werden")
        return f"{_esc_meas(self.measurement)}{tags} {fields} {int(self.time_ns)}"

    def to_dict(self) -> dict:
        return {"measurement": self.measurement, "tags": self.tags,
                "fields": self.fields, "time_ns": self.time_ns}

    @staticmethod
    def from_dict(d: dict) -> "Point":
        return Point(d["measurement"], dict(d["tags"]), dict(d["fields"]), int(d["time_ns"]))


class InfluxError(RuntimeError):
    pass


# ---------------------------------------------------------------- Client
@dataclass
class InfluxClient:
    url: str
    database: str
    username: str = ""
    password: str = ""
    token: str = ""            # nur 2.x
    org: str = ""              # nur 2.x (für Delete)
    bucket: str = ""           # nur 2.x (für Delete); Standard = database
    retention_policy: str = ""
    entity_tag: str = "entity_id"
    timeout: float = 30.0
    version: str = field(default="", init=False)

    @staticmethod
    def from_env() -> "InfluxClient":
        e = os.environ.get
        return InfluxClient(
            url=e("INFLUX_URL", "http://localhost:8086").rstrip("/"),
            database=e("INFLUX_DB", "homeassistant"),
            username=e("INFLUX_USER", ""),
            password=e("INFLUX_PASSWORD", ""),
            token=e("INFLUX_TOKEN", ""),
            org=e("INFLUX_ORG", ""),
            bucket=e("INFLUX_BUCKET", ""),
            retention_policy=e("INFLUX_RP", ""),
            entity_tag=e("ENTITY_TAG", "entity_id"),
        )

    # -------------------------------------------------- Transport
    def _auth(self) -> tuple[dict, dict]:
        headers, params = {}, {}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        elif self.username:
            params["u"], params["p"] = self.username, self.password
        return headers, params

    def ping(self) -> str:
        r = requests.get(f"{self.url}/ping", timeout=self.timeout)
        if r.status_code not in (200, 204):
            raise InfluxError(f"Ping fehlgeschlagen: HTTP {r.status_code}")
        self.version = r.headers.get("X-Influxdb-Version", "unbekannt")
        return self.version

    @property
    def is_v2(self) -> bool:
        v = self.version.lstrip("v")
        return v[:1] in ("2", "3") or "OSS" in self.version.upper()

    def query(self, q: str, method: str = "GET") -> list[dict]:
        headers, params = self._auth()
        params.update({"db": self.database, "q": q, "epoch": "ns"})
        if self.retention_policy:
            params["rp"] = self.retention_policy
        if method == "POST":
            r = requests.post(f"{self.url}/query", params=params, headers=headers, timeout=self.timeout)
        else:
            r = requests.get(f"{self.url}/query", params=params, headers=headers, timeout=self.timeout)
        if r.status_code >= 400:
            raise InfluxError(f"Query fehlgeschlagen (HTTP {r.status_code}): {r.text[:300]}")
        data = json.loads(r.text)  # json -> int bleibt int (ns-Zeitstempel verlustfrei)
        results = data.get("results", [])
        for res in results:
            if "error" in res:
                raise InfluxError(f"InfluxQL-Fehler: {res['error']}\nQuery: {q}")
        return results

    def _series(self, q: str) -> list[dict]:
        res = self.query(q)
        return res[0].get("series", []) if res else []

    def write_points(self, points: list[Point], field_types: dict | None = None) -> None:
        if not points:
            return
        body = "\n".join(p.to_line(field_types) for p in points)
        headers, params = self._auth()
        params.update({"db": self.database, "precision": "ns"})
        if self.retention_policy:
            params["rp"] = self.retention_policy
        r = requests.post(f"{self.url}/write", params=params, headers=headers,
                          data=body.encode("utf-8"), timeout=self.timeout)
        if r.status_code >= 400:
            raise InfluxError(f"Write fehlgeschlagen (HTTP {r.status_code}): {r.text[:300]}")

    # -------------------------------------------------- Metadaten
    def measurements(self) -> list[str]:
        s = self._series("SHOW MEASUREMENTS")
        return sorted(v[0] for v in s[0]["values"]) if s else []

    def entities(self, measurement: str) -> list[str]:
        s = self._series(f"SHOW TAG VALUES FROM {q_ident(measurement)} WITH KEY = {q_ident(self.entity_tag)}")
        return sorted(v[1] for v in s[0]["values"]) if s else []

    def field_types(self, measurement: str) -> dict[str, str]:
        s = self._series(f"SHOW FIELD KEYS FROM {q_ident(measurement)}")
        return {v[0]: v[1] for v in s[0]["values"]} if s else {}

    # -------------------------------------------------- Daten
    def _where(self, entity: str) -> str:
        return f"{q_ident(self.entity_tag)} = {q_str(entity)}"

    def values(self, measurement: str, entity: str, start_ns: int, end_ns: int,
               field_name: str = "value") -> list[tuple[int, float]]:
        """Rohwerte (time_ns, value) im Intervall [start, end)."""
        q = (f"SELECT {q_ident(field_name)} FROM {q_ident(measurement)} "
             f"WHERE {self._where(entity)} AND time >= {int(start_ns)} AND time < {int(end_ns)}")
        out = []
        for s in self._series(q):
            out += [(int(t), v) for t, v in s["values"] if v is not None]
        out.sort()
        return out

    def neighbor(self, measurement: str, entity: str, t_ns: int, before: bool,
                 field_name: str = "value") -> tuple[int, float] | None:
        """Letzter Wert vor bzw. erster Wert nach t (exklusiv)."""
        cmp_, order = ("<", "DESC") if before else (">", "ASC")
        q = (f"SELECT {q_ident(field_name)} FROM {q_ident(measurement)} "
             f"WHERE {self._where(entity)} AND time {cmp_} {int(t_ns)} "
             f"ORDER BY time {order} LIMIT 1")
        cand = [(int(t), v) for s in self._series(q) for t, v in s["values"] if v is not None]
        if not cand:
            return None
        return max(cand) if before else min(cand)

    def points_at(self, measurement: str, entity: str, t_ns: int) -> list[Point]:
        """Alle Punkte (alle Fields, exakte Tags) einer Entity zu genau einem Zeitstempel."""
        q = (f"SELECT * FROM {q_ident(measurement)} WHERE {self._where(entity)} "
             f"AND time >= {int(t_ns)} AND time <= {int(t_ns)} GROUP BY *")
        pts = []
        for s in self._series(q):
            cols = s["columns"]
            for row in s["values"]:
                rec = dict(zip(cols, row))
                t = int(rec.pop("time"))
                fields = {k: v for k, v in rec.items() if v is not None}
                pts.append(Point(measurement, dict(s.get("tags", {})), fields, t))
        return pts

    def tags_near(self, measurement: str, entity: str, t_ns: int) -> dict:
        """Tag-Set der Serie, die zeitlich am nächsten liegt (für neue Punkte)."""
        best = None
        for cmp_, order in (("<=", "DESC"), (">", "ASC")):
            q = (f"SELECT {q_ident('value')} FROM {q_ident(measurement)} WHERE {self._where(entity)} "
                 f"AND time {cmp_} {int(t_ns)} GROUP BY * ORDER BY time {order} LIMIT 1")
            for s in self._series(q):
                for t, _ in s["values"]:
                    d = abs(int(t) - t_ns)
                    if best is None or d < best[0]:
                        best = (d, dict(s.get("tags", {})))
        if best is None:
            return {self.entity_tag: entity}
        return best[1]

    def delete_at(self, measurement: str, entity: str, t_ns: int) -> None:
        """Löscht alle Fields der Entity zu genau diesem Zeitstempel."""
        if not self.version:
            self.ping()
        if self.is_v2:
            ts = _rfc3339_ns(t_ns)
            headers, _ = self._auth()
            r = requests.post(
                f"{self.url}/api/v2/delete",
                params={"org": self.org, "bucket": self.bucket or self.database},
                headers={**headers, "Content-Type": "application/json"},
                json={"start": ts, "stop": ts,
                      "predicate": f'_measurement="{measurement}" AND {self.entity_tag}="{entity}"'},
                timeout=self.timeout)
            if r.status_code >= 400:
                raise InfluxError(f"Delete fehlgeschlagen (HTTP {r.status_code}): {r.text[:300]}")
        else:
            q = (f"DELETE FROM {q_ident(measurement)} WHERE {self._where(entity)} "
                 f"AND time >= {int(t_ns)} AND time <= {int(t_ns)}")
            self.query(q, method="POST")


def _rfc3339_ns(t_ns: int) -> str:
    sec, ns = divmod(int(t_ns), 1_000_000_000)
    return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{ns:09d}Z"
