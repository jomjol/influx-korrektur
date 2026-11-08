import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "tests")]

import analysis as A  # noqa: E402
import fake_influx  # noqa: E402
from influx import InfluxClient, Point  # noqa: E402
from ops import Operations  # noqa: E402

TZ = "Europe/Berlin"


def ns(s):  # lokale Zeit -> ns
    return A.local_to_ns(s, TZ)


# ------------------------------------------------------------ Analyse
def test_daily_matches_influxql_semantics():
    raw = [(ns("2026-10-01 10:00"), 100.0), (ns("2026-10-01 23:00"), 110.0),
           (ns("2026-10-02 12:00"), 120.0),
           # 03.10. fehlt
           (ns("2026-10-04 08:00"), 140.0), (ns("2026-10-04 20:00"), 150.0)]
    d = A.daily(raw, TZ, date(2026, 10, 2), date(2026, 10, 4))
    assert list(d["verbrauch"]) == [10.0, 0.0, 30.0]
    assert d.loc[1, "status"] == A.GAP
    assert A.CATCHUP in d.loc[2, "status"]


def test_gap_proposal_linear():
    raw = [(ns("2026-10-01 23:00"), 90.0), (ns("2026-10-02 00:00"), 100.0), (ns("2026-10-05 00:00"), 130.0)]
    d = A.daily(raw, TZ, date(2026, 10, 2), date(2026, 10, 5))
    props, hints = A.gap_proposals(raw, TZ, d)
    # 02.10. hat nur einen Punkt um 00:00 -> auch dort wird das Tagesende ergänzt
    assert [p.tag for p in props] == [date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4)]
    assert [round(p.wert, 3) for p in props] == [110.0, 120.0, 130.0]
    after = A.daily(A.simulate(raw, inserts=[(p.time_ns, p.wert) for p in props]), TZ,
                    date(2026, 10, 2), date(2026, 10, 5))
    assert list(after["verbrauch"].round(3)) == [20.0, 10.0, 10.0, 0.0]
    assert not any(after["status"])


def test_partial_day_detected_and_fixed():
    # Sensor fällt am 09.10. um 00:30 aus, nächster Wert 10.10. 12:00
    raw = [(ns("2026-10-08 12:00"), 0.0), (ns("2026-10-08 23:30"), 10.0), (ns("2026-10-09 00:30"), 11.0),
           (ns("2026-10-10 12:00"), 46.0), (ns("2026-10-10 23:30"), 57.0)]
    d = A.daily(raw, TZ, date(2026, 10, 9), date(2026, 10, 10))
    assert A.PARTIAL in d.loc[0, "status"] and A.CATCHUP in d.loc[1, "status"]
    props, _ = A.gap_proposals(raw, TZ, d)
    assert len(props) == 1 and props[0].tag == date(2026, 10, 9)
    after = A.daily(A.simulate(raw, inserts=[(p.time_ns, p.wert) for p in props]), TZ,
                    date(2026, 10, 9), date(2026, 10, 10))
    assert not any(after["status"])
    assert list(after["verbrauch"].round(1)) == [24.2, 22.8]


def test_idle_days_without_consumption_not_flagged():
    raw = [(ns("2026-10-01 20:00"), 50.0), (ns("2026-10-04 18:00"), 50.0), (ns("2026-10-04 19:00"), 60.0)]
    d = A.daily(raw, TZ, date(2026, 10, 2), date(2026, 10, 4))
    assert not any(d["status"])


def test_dst_day_end():
    # 25.10.2026 Umstellung auf Winterzeit -> Tagesende 23:59:59 MEZ = 22:59:59 UTC
    t = A.day_end_ns(date(2026, 10, 25), TZ)
    assert str(A.to_local(t, TZ)) == "2026-10-25 23:59:59+01:00"


def test_line_protocol_escaping():
    p = Point("m³", {"entity_id": "a b,c", "domain": "sensor"},
              {"value": 1.0, "friendly_name_str": 'X "Y"'}, 123)
    assert p.to_line() == 'm³,domain=sensor,entity_id=a\\ b\\,c value=1.0,friendly_name_str="X \\"Y\\"" 123'


# ------------------------------------------------------------ End-to-End gegen Fake-Influx
@pytest.fixture(scope="module")
def env(tmp_path_factory):
    srv = fake_influx.serve(18099)
    fake_influx.STORE.clear(); fake_influx.FTYPES.clear()
    t0 = ns("2026-10-01 12:00:00") + 123456789          # ns-genauer HA-Zeitstempel
    fake_influx.write_lines("\n".join([
        f'kWh,domain=sensor,entity_id=zaehler value=100.0,friendly_name_str="Zähler" {t0}',
        f'kWh,domain=sensor,entity_id=zaehler value=130.0,friendly_name_str="Zähler" {t0 + 3 * 86400 * 10**9}',
        f'kWh,domain=sensor,entity_id=andere value=5.0 {t0}',
    ]))
    c = InfluxClient("http://127.0.0.1:18099", "homeassistant")
    c.ping()
    ops = Operations(c, str(tmp_path_factory.mktemp("d") / "audit.jsonl"))
    yield c, ops, t0
    srv.shutdown()


def test_e2e_metadata(env):
    c, _, _ = env
    assert c.version == "1.8.10" and not c.is_v2
    assert c.measurements() == ["kWh"]
    assert c.entities("kWh") == ["andere", "zaehler"]
    assert c.field_types("kWh")["value"] == "float"


def test_e2e_insert_edit_delete_undo(env):
    c, ops, t0 = env
    t_new = ns("2026-10-02 23:59:59")
    e_ins = ops.insert("kWh", "zaehler", t_new, 110.0)
    pts = c.points_at("kWh", "zaehler", t_new)
    assert pts[0].tags == {"domain": "sensor", "entity_id": "zaehler"}
    assert pts[0].fields["value"] == 110.0 and pts[0].fields["korrektur_str"] == "manuell"
    with pytest.raises(Exception):
        ops.insert("kWh", "zaehler", t_new, 1.0)

    # ns-genaues Ändern eines Original-HA-Punkts, Attribut-Field bleibt erhalten
    e_edit = ops.edit("kWh", "zaehler", t0, 101.5)
    p = c.points_at("kWh", "zaehler", t0)[0]
    assert p.fields["value"] == 101.5 and p.fields["friendly_name_str"] == "Zähler"
    ops.undo(e_edit["id"])
    p = c.points_at("kWh", "zaehler", t0)[0]
    assert p.fields == {"value": 100.0, "friendly_name_str": "Zähler"}

    # Löschen betrifft nur diese Entity
    e_del = ops.delete("kWh", "zaehler", t0)
    assert c.points_at("kWh", "zaehler", t0) == []
    assert c.points_at("kWh", "andere", t0) != []
    ops.undo(e_del["id"])
    assert c.points_at("kWh", "zaehler", t0)[0].fields["value"] == 100.0

    ops.undo(e_ins["id"])
    assert c.points_at("kWh", "zaehler", t_new) == []
    hist = ops.history()
    assert [h["action"] for h in hist] == ["insert", "edit", "undo", "delete", "undo", "undo"]
    assert all(h["undone"] for h in hist if h["action"] != "undo")


def test_e2e_neighbors(env):
    c, _, t0 = env
    t = ns("2026-10-03 00:00")
    assert c.neighbor("kWh", "zaehler", t, True) == (t0, 100.0)
    assert c.neighbor("kWh", "zaehler", t, False)[1] == 130.0
    assert c.neighbor("kWh", "zaehler", t0, True) is None
