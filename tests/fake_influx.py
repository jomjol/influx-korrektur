"""Minimaler InfluxDB-1.8-Simulator für Tests (nur die vom Tool genutzten InfluxQL-Befehle)."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# store[(measurement, tags_tuple)][time_ns] = {field: value}
STORE: dict[tuple, dict[int, dict]] = {}
FTYPES: dict[str, dict[str, str]] = {}
LOCK = threading.Lock()


def _split_unescaped(s: str, sep: str) -> list[str]:
    out, cur, i, inq = [], "", 0, False
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            cur += s[i:i + 2]; i += 2; continue
        if ch == '"':
            inq = not inq
        if ch == sep and not inq:
            out.append(cur); cur = ""
        else:
            cur += ch
        i += 1
    out.append(cur)
    return out


def _unesc(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def write_lines(body: str) -> None:
    for line in body.splitlines():
        if not line.strip():
            continue
        parts = _split_unescaped(line, " ")
        key, fields_s, t = parts[0], parts[1], int(parts[2])
        kparts = _split_unescaped(key, ",")
        meas = _unesc(kparts[0])
        tags = tuple(sorted((_unesc(a), _unesc(b)) for a, b in
                            (_split_unescaped(kv, "=") for kv in kparts[1:])))
        fields = {}
        for kv in _split_unescaped(fields_s, ","):
            k, v = _split_unescaped(kv, "=")[0], "=".join(_split_unescaped(kv, "=")[1:])
            k = _unesc(k)
            if v.startswith('"'):
                val, ft = _unesc(v[1:-1]), "string"
            elif v.endswith("i"):
                val, ft = int(v[:-1]), "integer"
            elif v in ("true", "false"):
                val, ft = v == "true", "boolean"
            else:
                val, ft = float(v), "float"
            old = FTYPES.setdefault(meas, {}).get(k)
            if old and old != ft:
                raise ValueError(f"field type conflict: {k} {old} != {ft}")
            FTYPES[meas][k] = ft
            fields[k] = val
        with LOCK:
            STORE.setdefault((meas, tags), {}).setdefault(t, {}).update(fields)


def _conds(where: str):
    ent = re.search(r'"(\w+)"\s*=\s*\'((?:[^\'\\]|\\.)*)\'', where)
    times = re.findall(r"time\s*(>=|<=|<|>|=)\s*(-?\d+)", where)
    return ent, times


def _time_ok(t, times):
    for op, v in times:
        v = int(v)
        if not {">=": t >= v, "<=": t <= v, "<": t < v, ">": t > v, "=": t == v}[op]:
            return False
    return True


def run_query(q: str) -> dict:
    q = q.strip()
    if q == "SHOW MEASUREMENTS":
        names = sorted({m for m, _ in STORE})
        return {"series": [{"name": "measurements", "columns": ["name"], "values": [[n] for n in names]}]} if names else {}
    m = re.match(r'SHOW TAG VALUES FROM "(.+?)" WITH KEY = "(.+?)"', q)
    if m:
        vals = sorted({dict(tags).get(m[2]) for (mm, tags) in STORE if mm == m[1]} - {None})
        return {"series": [{"name": m[1], "columns": ["key", "value"], "values": [[m[2], v] for v in vals]}]}
    m = re.match(r'SHOW FIELD KEYS FROM "(.+?)"', q)
    if m:
        ft = FTYPES.get(m[1], {})
        return {"series": [{"name": m[1], "columns": ["fieldKey", "fieldType"],
                            "values": [[k, v] for k, v in sorted(ft.items())]}]} if ft else {}
    m = re.match(r'DELETE FROM "(.+?)" WHERE (.*)$', q)
    if m:
        ent, times = _conds(m[2])
        with LOCK:
            for (mm, tags), pts in STORE.items():
                if mm == m[1] and dict(tags).get(ent[1]) == ent[2]:
                    for t in [t for t in pts if _time_ok(t, times)]:
                        del pts[t]
        return {}
    m = re.match(r'SELECT (.+?) FROM "(.+?)" WHERE (.+?)( GROUP BY \*)?( ORDER BY time (ASC|DESC))?( LIMIT (\d+))?$', q)
    if m:
        sel, meas, where = m[1], m[2], m[3]
        group, desc, limit = bool(m[4]), m[6] == "DESC", int(m[8]) if m[8] else None
        ent, times = _conds(where)
        field = None if sel.strip() == "*" else sel.strip().strip('"')
        series = []
        merged = []
        for (mm, tags), pts in sorted(STORE.items()):
            if mm != meas or dict(tags).get(ent[1]) != ent[2]:
                continue
            rows = []
            for t in sorted(pts, reverse=desc):
                if not _time_ok(t, times):
                    continue
                f = pts[t]
                if field and field not in f:
                    continue
                rows.append((t, f, dict(tags)))
            if group:
                if limit:
                    rows = rows[:limit]
                if not rows:
                    continue
                cols = ["time"] + ([field] if field else sorted(FTYPES.get(meas, {})))
                series.append({"name": meas, "tags": dict(tags), "columns": cols,
                               "values": [[t] + [f.get(c) for c in cols[1:]] for t, f, _ in rows]})
            else:
                merged += rows
        if not group and merged:
            merged.sort(key=lambda r: r[0], reverse=desc)
            if limit:
                merged = merged[:limit]
            if field:
                cols = ["time", field]
                vals = [[t, f[field]] for t, f, _ in merged]
            else:
                tagk = sorted({k for _, _, tg in merged for k in tg})
                cols = ["time"] + sorted(set(FTYPES.get(meas, {})) | set(tagk))
                vals = [[t] + [{**tg, **f}.get(c) for c in cols[1:]] for t, f, tg in merged]
            series = [{"name": meas, "columns": cols, "values": vals}]
        return {"series": series} if series else {}
    return {"error": f"fake: unsupported query: {q}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("X-Influxdb-Version", "1.8.10")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ping":
            return self._send(204)
        if u.path == "/query":
            q = parse_qs(u.query)["q"][0]
            return self._send(200, {"results": [{"statement_id": 0, **run_query(q)}]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if u.path == "/write":
            try:
                write_lines(body)
            except Exception as e:  # noqa: BLE001
                return self._send(400, {"error": str(e)})
            return self._send(204)
        if u.path == "/query":
            q = parse_qs(u.query)["q"][0]
            return self._send(200, {"results": [{"statement_id": 0, **run_query(q)}]})
        self._send(404, {"error": "not found"})


def serve(port: int = 18086) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    import sys
    import time
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from seed import seed
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 18086)
    seed(write=write_lines)
    print("Fake-InfluxDB läuft")
    while True:
        time.sleep(3600)
