"""Erzeugt HA-typische Testdaten im Fake-Influx (Zählerstand mit Lücken)."""
import math
import random
from datetime import datetime, timedelta, timezone

from fake_influx import write_lines


def seed(days: int = 60, write=write_lines):
    random.seed(1)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    gaps = {(now - timedelta(days=d)).date() for d in (14, 15, 30)}
    v = 12345.0
    lines = []
    t = start
    while t < now:
        v += random.uniform(0.1, 0.8)
        local_day = (t + timedelta(hours=1)).date()
        if local_day not in gaps:
            ns = int(t.timestamp()) * 1_000_000_000 + random.randint(0, 999_999_999)
            lines.append(f'kWh,domain=sensor,entity_id=keba_p30_total_energy '
                         f'value={v:.3f},friendly_name_str="KEBA P30 Gesamtenergie" {ns}')
        t += timedelta(minutes=30)
    write("\n".join(lines))

    # Temperatur (Messwert): alle 10 min, Tagesgang, 1 Datenloch (9 h), 2 Ausreißer
    random.seed(2)
    lines = []
    t = start
    hole = (now - timedelta(days=5, hours=3), now - timedelta(days=4, hours=18))
    spikes = {now - timedelta(days=8, hours=7, minutes=20): 85.0, now - timedelta(days=12, hours=2): -40.0}
    while t < now:
        if not (hole[0] <= t < hole[1]):
            v = 20 + 3 * math.sin((t.hour + t.minute / 60 - 9) / 24 * 2 * math.pi) + random.uniform(-0.2, 0.2)
            v = spikes.get(t, v)
            ns = int(t.timestamp()) * 1_000_000_000 + random.randint(0, 999_999_999)
            lines.append(f'°C,domain=sensor,entity_id=wohnzimmer_temperatur value={v:.2f},'
                         f'friendly_name_str="Wohnzimmer Temperatur" {ns}')
        t += timedelta(minutes=10)
    write("\n".join(lines))
