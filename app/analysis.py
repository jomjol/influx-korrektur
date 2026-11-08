"""Auswertung von Zählerständen: Tagesverbrauch, Auffälligkeiten, Interpolationsvorschläge.

Die Tagesberechnung entspricht der bestehenden Auswertung
    SELECT derivative(max("value"), 1d) ... GROUP BY time(1d) fill(previous) tz('Europe/Berlin')
d.h. Verbrauch(Tag) = max(Tag) - max(Vortag), fehlende Tage übernehmen den Vortageswert.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import pandas as pd

NS = 1_000_000_000
END_TOLERANCE_S = 300   # Punkt <= 5 min vor Mitternacht gilt als Tagesendwert

# Status-Kennzeichen
GAP, CATCHUP, NEGATIVE, OUTLIER, PARTIAL = "Lücke", "Nachholwert", "negativ", "Ausreißer", "unvollständig"


def to_local(t_ns, tz: str) -> pd.Timestamp:
    return pd.Timestamp(int(t_ns), unit="ns", tz="UTC").tz_convert(tz)


def local_to_ns(ts: pd.Timestamp | str, tz: str) -> int:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize(tz, ambiguous=False, nonexistent="shift_forward")
    return int(ts.tz_convert("UTC").value)


def day_start_ns(d: date, tz: str) -> int:
    return local_to_ns(pd.Timestamp(d), tz)


def day_end_ns(d: date, tz: str, offset_s: int = 1) -> int:
    """Zeitpunkt kurz vor Mitternacht (Standard 23:59:59) des lokalen Tages."""
    return day_start_ns(d + timedelta(days=1), tz) - offset_s * NS


def raw_frame(raw: list[tuple[int, float]], tz: str) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=["time_ns", "value"])
    if df.empty:
        df["zeit"] = pd.Series(dtype=f"datetime64[ns, {tz}]")
        df["tag"] = pd.Series(dtype=object)
        return df
    df = df.sort_values("time_ns").reset_index(drop=True)
    df["zeit"] = pd.to_datetime(df["time_ns"], unit="ns", utc=True).dt.tz_convert(tz)
    df["tag"] = df["zeit"].dt.date
    return df


def daily(raw: list[tuple[int, float]], tz: str, first_day: date, last_day: date,
          outlier_factor: float = 5.0, hole_hours: float = 3.0, min_share: float = 0.1) -> pd.DataFrame:
    """Tagesübersicht von first_day bis last_day (inklusive).

    raw sollte mindestens den Tag vor first_day enthalten, damit der Verbrauch
    des ersten Tages berechnet werden kann.
    """
    df = raw_frame(raw, tz)
    days = [first_day - timedelta(days=1) + timedelta(days=i)
            for i in range((last_day - first_day).days + 2)]
    g = df.groupby("tag") if not df.empty else None
    rows = []
    for d in days:
        if g is not None and d in g.groups:
            sub = g.get_group(d)
            rows.append({"tag": d, "anzahl": len(sub), "min": sub["value"].min(),
                         "max": sub["value"].max(), "erster": sub["zeit"].iloc[0],
                         "letzter": sub["zeit"].iloc[-1]})
        else:
            rows.append({"tag": d, "anzahl": 0, "min": None, "max": None,
                         "erster": None, "letzter": None})
    out = pd.DataFrame(rows)
    out["max"] = out["max"].astype(float)
    out["max_gefuellt"] = out["max"].ffill()                       # fill(previous)
    out["verbrauch"] = out["max_gefuellt"].diff()                   # derivative(…, 1d)
    out = out.iloc[1:].reset_index(drop=True)                       # Vortag nur als Basis

    # Auffälligkeiten
    status: list[list[str]] = [[] for _ in range(len(out))]
    idx = {d: i for i, d in enumerate(out["tag"])}
    pos = [v for v, n in zip(out["verbrauch"], out["anzahl"]) if n > 0 and pd.notna(v) and v > 0]
    med = float(pd.Series(pos).median()) if pos else 0.0
    min_delta = min_share * med
    out.attrs["min_delta"] = min_delta
    last_t = max((t for t, _ in raw), default=None)
    for i, r in out.iterrows():
        # leerer Tag nach dem letzten Messwert: Sensor (noch) ausgefallen
        if r["anzahl"] == 0 and (last_t is None or day_start_ns(r["tag"], tz) > last_t):
            status[i].append(GAP)
        if pd.notna(r["verbrauch"]) and r["verbrauch"] < 0:
            status[i].append(NEGATIVE)
    for t_a, v_a, t_b, v_b in find_holes(raw, tz, hole_hours, min_delta):
        da, db = to_local(t_a, tz).date(), to_local(t_b, tz).date()
        if da in idx and PARTIAL not in status[idx[da]]:
            status[idx[da]].append(PARTIAL)
        if db in idx and CATCHUP not in status[idx[db]]:
            status[idx[db]].append(CATCHUP)
        d = da + timedelta(days=1)
        while d < db:                       # Tage komplett im Datenloch
            if d in idx and GAP not in status[idx[d]]:
                status[idx[d]].append(GAP)
            d += timedelta(days=1)

    normal = [v for v, st in zip(out["verbrauch"], status)
              if not st and pd.notna(v) and v > 0]
    if len(normal) >= 5 and outlier_factor > 0:
        med = float(pd.Series(normal).median())
        for i, v in enumerate(out["verbrauch"]):
            if pd.notna(v) and med > 0 and v > outlier_factor * med and NEGATIVE not in status[i]:
                status[i].append(OUTLIER)
    out["status"] = [", ".join(s) for s in status]
    return out


def find_holes(raw: list[tuple[int, float]], tz: str, hole_hours: float = 3.0,
               min_delta: float = 0.0) -> list[tuple[int, float, int, float]]:
    """Datenlöcher: aufeinanderfolgende Punkte über Mitternacht hinweg, Abstand > hole_hours
    und Zählerzuwachs > min_delta. Deren Verbrauch landet sonst komplett im Tag des 2. Punkts."""
    pts = sorted(raw)
    out = []
    for (t_a, v_a), (t_b, v_b) in zip(pts, pts[1:]):
        if t_b - t_a <= hole_hours * 3600 * NS or v_b - v_a <= min_delta:
            continue
        da, db = to_local(t_a, tz).date(), to_local(t_b, tz).date()
        if da == db:
            continue
        # Bereits durch einen Tagesend-Stützpunkt abgedeckt (nur 1 Mitternacht, kurz davor)?
        if (db - da).days == 1 and day_start_ns(db, tz) - t_a <= END_TOLERANCE_S * NS:
            continue
        out.append((t_a, v_a, t_b, v_b))
    return out


@dataclass
class Proposal:
    tag: date
    time_ns: int
    wert: float
    basis: str


def gap_proposals(raw: list[tuple[int, float]], tz: str, day_table: pd.DataFrame,
                  neighbor: Callable[[int, bool], tuple[int, float] | None] | None = None,
                  hole_hours: float = 3.0, end_offset_s: int = 1) -> tuple[list[Proposal], list[str]]:
    """Interpolierte Stützpunkte am Tagesende (23:59:59) für jedes Datenloch über Mitternacht.

    neighbor(t_ns, before) liefert bei Bedarf den ersten Punkt nach dem geladenen Bereich
    (Lücke am Ende des Zeitraums).
    """
    pts = sorted(raw)
    hinweise: list[str] = []
    if not day_table.empty and day_table.iloc[-1]["anzahl"] == 0 and neighbor:
        nxt = neighbor(day_end_ns(day_table.iloc[-1]["tag"], tz, 0) - 1, False)
        if nxt:
            pts.append(nxt)
        else:
            hinweise.append("Lücke am Ende des Zeitraums: noch kein Folgewert vorhanden – übersprungen.")
    min_delta = day_table.attrs.get("min_delta", 0.0)
    for (t_a, v_a), (t_b, v_b) in zip(pts, pts[1:]):
        if v_b < v_a and to_local(t_a, tz).date() != to_local(t_b, tz).date() \
                and t_b - t_a > hole_hours * 3600 * NS:
            hinweise.append(f"{to_local(t_a, tz):%d.%m. %H:%M} → {to_local(t_b, tz):%d.%m. %H:%M}: "
                            "Zählerstand sinkt (Reset/Tausch?) – übersprungen.")
    props: list[Proposal] = []
    for t_a, v_a, t_b, v_b in find_holes(pts, tz, hole_hours, min_delta):
        d = to_local(t_a, tz).date()
        while True:
            t = day_end_ns(d, tz, end_offset_s)
            if t >= t_b:
                break
            if t > t_a:
                v = v_a + (v_b - v_a) * (t - t_a) / (t_b - t_a)
                props.append(Proposal(d, t, round(v, 6),
                                      f"linear {to_local(t_a, tz):%d.%m. %H:%M} ({v_a:g}) → "
                                      f"{to_local(t_b, tz):%d.%m. %H:%M} ({v_b:g})"))
            d += timedelta(days=1)
    return props, hinweise


def interpolate_at(t_ns: int, p0: tuple[int, float] | None, p1: tuple[int, float] | None) -> float | None:
    if p0 is None or p1 is None:
        return None
    (t0, v0), (t1, v1) = p0, p1
    if t1 == t0:
        return v0
    return v0 + (v1 - v0) * (t_ns - t0) / (t1 - t0)


def simulate(raw: list[tuple[int, float]], inserts=(), deletes=(), edits=()) -> list[tuple[int, float]]:
    """Wendet Änderungen virtuell auf die Rohdaten an (für die Vorschau)."""
    d = dict(raw)
    for t in deletes:
        d.pop(int(t), None)
    for t, v in list(edits) + list(inserts):
        d[int(t)] = float(v)
    return sorted(d.items())


def compare(before: pd.DataFrame, after: pd.DataFrame) -> pd.DataFrame:
    m = before[["tag", "verbrauch", "status"]].merge(
        after[["tag", "verbrauch", "status"]], on="tag", suffixes=("_vorher", "_nachher"))
    diff = (m["verbrauch_vorher"].fillna(0) - m["verbrauch_nachher"].fillna(0)).abs() > 1e-9
    return m[diff | (m["status_vorher"] != m["status_nachher"])].reset_index(drop=True)


# =====================================================================================
# Messwert-Modus (Zeitreihen ohne Differenzbildung, z. B. Temperaturen)
# =====================================================================================
COUNTER, GAUGE = "counter", "gauge"
MODE_LABELS = {"auto": "Automatisch", COUNTER: "Zähler (Differenz/Tag)", GAUGE: "Messwert (Zeitreihe)"}


def detect_mode(raw: list[tuple[int, float]]) -> str:
    """Zähler, wenn die Werte (fast) monoton steigen, sonst Messwert."""
    vals = [v for _, v in sorted(raw)]
    if len(vals) < 3:
        return COUNTER
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    up = sum(d > 0 for d in diffs)
    down = sum(d < 0 for d in diffs)
    return COUNTER if up > 0 and down <= 0.02 * len(diffs) else GAUGE


def find_spikes(raw: list[tuple[int, float]], factor: float = 5.0, window: int = 11,
                vmin: float | None = None, vmax: float | None = None) -> list[tuple[int, float, float, str]]:
    """Ausreißer-Punkte (Hampel-Filter + optionaler Plausibilitätsbereich).

    Rückgabe: (time_ns, wert, referenz, grund). Referenz = gleitender Median.
    """
    pts = sorted(raw)
    if not pts:
        return []
    s = pd.Series([v for _, v in pts], dtype=float)
    med = s.rolling(window, center=True, min_periods=1).median()
    mad = (s - med).abs().rolling(window, center=True, min_periods=1).median() * 1.4826
    q = s.quantile([0.05, 0.95])
    floor = 0.1 * float(q.iloc[1] - q.iloc[0]) if len(s) > 1 else 0.0   # Rauschschwelle
    out = []
    for i, (t, v) in enumerate(pts):
        if vmin is not None and v < vmin:
            out.append((t, v, float(med.iloc[i]), f"< {vmin:g}"))
        elif vmax is not None and v > vmax:
            out.append((t, v, float(med.iloc[i]), f"> {vmax:g}"))
        elif factor > 0 and len(pts) >= 5:
            limit = factor * float(mad.iloc[i]) + floor
            if abs(v - med.iloc[i]) > limit and limit > 0:
                out.append((t, v, float(med.iloc[i]), "Sprung"))
    return out


def find_gauge_holes(raw: list[tuple[int, float]], hole_hours: float) -> list[tuple[int, float, int, float]]:
    pts = sorted(raw)
    return [(ta, va, tb, vb) for (ta, va), (tb, vb) in zip(pts, pts[1:])
            if tb - ta > hole_hours * 3600 * NS]


def daily_gauge(raw: list[tuple[int, float]], tz: str, first_day: date, last_day: date,
                hole_hours: float = 3.0, spikes: list | None = None) -> pd.DataFrame:
    """Tagesübersicht für Messwerte: Anzahl, Min, Mittel (arithmetisch wie InfluxQL mean()), Max."""
    df = raw_frame(raw, tz)
    days = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    g = df.groupby("tag") if not df.empty else None
    spike_days: dict[date, int] = {}
    for t, *_ in (spikes or []):
        d = to_local(t, tz).date()
        spike_days[d] = spike_days.get(d, 0) + 1
    hole_days: set[date] = set()
    gap_days: set[date] = set()
    for ta, _, tb, _ in find_gauge_holes(raw, hole_hours):
        da, db = to_local(ta, tz).date(), to_local(tb, tz).date()
        d = da
        while d <= db:
            (gap_days if da < d < db else hole_days).add(d)
            d += timedelta(days=1)
    last_t = max((t for t, _ in raw), default=None)
    rows = []
    for d in days:
        st = []
        if g is not None and d in g.groups:
            sub = g.get_group(d)
            row = {"tag": d, "anzahl": len(sub), "min": sub["value"].min(), "mittel": sub["value"].mean(),
                   "max": sub["value"].max(), "letzter": sub["zeit"].iloc[-1]}
        else:
            row = {"tag": d, "anzahl": 0, "min": None, "mittel": None, "max": None, "letzter": None}
        if d in gap_days or (row["anzahl"] == 0 and (last_t is None or day_start_ns(d, tz) > last_t)):
            st.append(GAP)
        elif d in hole_days:
            st.append(PARTIAL)
        if spike_days.get(d):
            st.append(OUTLIER)
        row["status"] = ", ".join(st)
        row["ausreisser"] = spike_days.get(d, 0)
        rows.append(row)
    out = pd.DataFrame(rows)
    for c in ("min", "mittel", "max"):
        out[c] = out[c].astype(float)
    return out


def gauge_gap_proposals(raw: list[tuple[int, float]], tz: str, hole_hours: float = 3.0,
                        step_min: int = 60, max_points: int = 2000) -> tuple[list[Proposal], list[str]]:
    """Füllt Datenlöcher mit linear interpolierten Punkten im Zeitraster (an der lokalen Uhr ausgerichtet)."""
    props: list[Proposal] = []
    hinweise: list[str] = []
    step = int(step_min) * 60 * NS
    margin = min(step // 2, 60 * NS)
    for ta, va, tb, vb in find_gauge_holes(raw, hole_hours):
        loc = to_local(ta, tz)
        midnight = local_to_ns(pd.Timestamp(loc.date()), tz)
        t = midnight + ((ta - midnight) // step + 1) * step
        basis = (f"linear {to_local(ta, tz):%d.%m. %H:%M} ({va:g}) → "
                 f"{to_local(tb, tz):%d.%m. %H:%M} ({vb:g})")
        while t < tb - margin:
            if t > ta + margin:
                v = va + (vb - va) * (t - ta) / (tb - ta)
                props.append(Proposal(to_local(t, tz).date(), t, round(v, 6), basis))
            t += step
        if len(props) > max_points:
            hinweise.append(f"Mehr als {max_points} Punkte – Raster vergrößern oder Zeitraum verkleinern.")
            return props[:max_points], hinweise
    if not props:
        hinweise.append("Keine Datenlöcher über der Schwelle gefunden.")
    return props, hinweise


def spike_replacements(raw: list[tuple[int, float]], spikes: list) -> dict[int, float | None]:
    """Ersatzwert je Ausreißer: lineare Interpolation zwischen den nächsten Nicht-Ausreißern."""
    bad = {t for t, *_ in spikes}
    good = [(t, v) for t, v in sorted(raw) if t not in bad]
    out: dict[int, float | None] = {}
    for t, *_ in spikes:
        p0 = next((p for p in reversed(good) if p[0] < t), None)
        p1 = next((p for p in good if p[0] > t), None)
        out[t] = interpolate_at(t, p0, p1)
    return out


def compare_cols(before: pd.DataFrame, after: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Generischer Vorher/Nachher-Vergleich der Tagestabelle."""
    keep = ["tag", "status", *cols]
    m = before[keep].merge(after[keep], on="tag", suffixes=("_vorher", "_nachher"))
    changed = m["status_vorher"] != m["status_nachher"]
    for c in cols:
        a, b = m[f"{c}_vorher"], m[f"{c}_nachher"]
        changed |= (a.fillna(0) - b.fillna(0)).abs() > 1e-9
        changed |= a.isna() != b.isna()
    return m[changed].reset_index(drop=True)
