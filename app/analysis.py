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
