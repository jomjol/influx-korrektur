"""Influx-Korrektur – Kontrolle und Korrektur von Zeitreihen in InfluxDB (Home Assistant).

Zwei Auswertungsmodi:
- Zähler   (counter): Zählerstände, Tagesverbrauch = Differenz der Tagesmaxima
- Messwert (gauge):   normale Zeitreihen (Temperatur, Feuchte, …), Tages-Min/Mittel/Max
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pandas as pd
from nicegui import run, ui

import analysis as A
from influx import InfluxClient, InfluxError
from ops import Operations
from settings import SeriesSettings

TZ = os.environ.get("TZ", "Europe/Berlin")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DEFAULT_DAYS = int(os.environ.get("DEFAULT_DAYS", "60"))
DEFAULT_MEASUREMENT = os.environ.get("DEFAULT_MEASUREMENT", "kWh")
OUTLIER_FACTOR = float(os.environ.get("OUTLIER_FACTOR", "5"))
HOLE_HOURS = float(os.environ.get("HOLE_HOURS", "3"))
GRID_MINUTES = int(os.environ.get("GRID_MINUTES", "60"))
EDITABLE_LIMIT = 60          # bis zu so vielen Vorschlägen einzeln editierbar

client = InfluxClient.from_env()
ops = Operations(client, os.path.join(DATA_DIR, "audit.jsonl"),
                 mark_field=os.environ.get("MARK_FIELD", "korrektur_str"))
series_settings = SeriesSettings(os.path.join(DATA_DIR, "settings.json"))

STATUS_COLOR = {"": "#4c78a8", A.GAP: "#e45756", A.CATCHUP: "#f58518",
                A.NEGATIVE: "#b279a2", A.OUTLIER: "#eeca3b", A.PARTIAL: "#72b7b2"}

COUNTER_COLUMNS = [
    {"name": "tag", "label": "Tag", "field": "tag", "align": "left", "sortable": True},
    {"name": "wt", "label": "WT", "field": "wt", "align": "left"},
    {"name": "anzahl", "label": "Werte", "field": "anzahl"},
    {"name": "letzter", "label": "letzter Wert um", "field": "letzter"},
    {"name": "max", "label": "Zählerstand (max)", "field": "max"},
    {"name": "verbrauch", "label": "Verbrauch", "field": "verbrauch"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
]
GAUGE_COLUMNS = [
    {"name": "tag", "label": "Tag", "field": "tag", "align": "left", "sortable": True},
    {"name": "wt", "label": "WT", "field": "wt", "align": "left"},
    {"name": "anzahl", "label": "Werte", "field": "anzahl"},
    {"name": "letzter", "label": "letzter Wert um", "field": "letzter"},
    {"name": "min", "label": "Min", "field": "min"},
    {"name": "mittel", "label": "Mittel", "field": "mittel"},
    {"name": "max", "label": "Max", "field": "max"},
    {"name": "status", "label": "Status", "field": "status", "align": "left"},
]


def fmt_t(t_ns: int, ms: bool = True) -> str:
    ts = A.to_local(t_ns, TZ)
    return ts.strftime("%Y-%m-%d %H:%M:%S") + (f".{ts.microsecond // 1000:03d}" if ms else "")


def fmt_v(v) -> str:
    return "" if v is None or pd.isna(v) else f"{v:,.3f}".replace(",", " ")


def status_color(status: str) -> str:
    for key in (A.GAP, A.NEGATIVE, A.CATCHUP, A.OUTLIER, A.PARTIAL):
        if key in status:
            return STATUS_COLOR[key]
    return STATUS_COLOR[""]


def num_or_none(v):
    return None if v in (None, "") else float(v)


class State:
    def __init__(self):
        self.measurement = ""
        self.entity = ""
        self.mode = A.COUNTER                       # aufgelöster Modus
        self.first: date = date.today() - timedelta(days=DEFAULT_DAYS)
        self.last: date = date.today()
        self.raw: list[tuple[int, float]] = []
        self.days: pd.DataFrame = pd.DataFrame()
        self.spikes: list = []
        self.raw_day: date = date.today()


@ui.page("/")
async def index():
    s = State()
    ui.page_title("Influx-Korrektur")

    # ------------------------------------------------------------------ Kopf
    with ui.header().classes("items-center bg-slate-800"):
        ui.icon("query_stats").classes("text-2xl")
        ui.label("Influx-Korrektur").classes("text-lg font-medium")
        ui.space()
        conn_label = ui.label("verbinde …").classes("text-sm opacity-80")

    with ui.row().classes("w-full items-end gap-4"):
        sel_meas = ui.select([], label="Measurement (Einheit)", with_input=True).classes("w-40")
        sel_ent = ui.select([], label="Entity", with_input=True).classes("w-96")
        inp_from = ui.date_input("Von", value=s.first.isoformat()).classes("w-44")
        inp_to = ui.date_input("Bis", value=s.last.isoformat()).classes("w-44")
        sel_mode = ui.select({k: v for k, v in A.MODE_LABELS.items()}, value="auto",
                             label="Auswertung").classes("w-52")
        btn_load = ui.button("Laden", icon="refresh")
    with ui.row().classes("w-full items-end gap-4"):
        inp_hole = ui.number("Datenloch ab [h]", value=HOLE_HOURS, min=0.1, step=0.5).classes("w-32") \
            .tooltip("Abstand zweier Messwerte, ab dem eine Datenlücke vorliegt")
        inp_factor = ui.number("Ausreißer-Faktor", value=OUTLIER_FACTOR, min=0, step=1).classes("w-32")
        with inp_factor:
            factor_tip = ui.tooltip("")
        gauge_box = ui.row().classes("items-end gap-4")
        with gauge_box:
            inp_step = ui.number("Füllraster [min]", value=GRID_MINUTES, min=1, step=5).classes("w-32") \
                .tooltip("Abstand der interpolierten Punkte beim Füllen von Lücken")
            inp_vmin = ui.number("Plausibel min").classes("w-32") \
                .tooltip("Werte darunter gelten als Ausreißer (leer = aus)")
            inp_vmax = ui.number("Plausibel max").classes("w-32") \
                .tooltip("Werte darüber gelten als Ausreißer (leer = aus)")
        mode_label = ui.label("").classes("text-sm opacity-70")

    with ui.tabs().classes("w-full") as tabs:
        tab_days = ui.tab("Tagesübersicht", icon="calendar_month")
        tab_raw = ui.tab("Rohdaten", icon="table_rows")
        tab_log = ui.tab("Protokoll", icon="history")

    with ui.tab_panels(tabs, value=tab_days).classes("w-full"):
        # -------------------------------------------------------------- Tagesübersicht
        with ui.tab_panel(tab_days):
            with ui.row().classes("items-center gap-4 w-full"):
                summary = ui.label("Noch keine Daten geladen.")
                ui.space()
                only_issues = ui.switch("nur auffällige Tage")
                btn_spikes = ui.button("Ausreißer bereinigen …", icon="cleaning_services", color="amber-8")
                btn_fix = ui.button("Lücken korrigieren …", icon="auto_fix_high", color="orange")
            day_plot = ui.plotly({"data": [], "layout": {}}).classes("w-full h-96")
            day_table = ui.table(rows=[], row_key="tag", pagination=31, columns=COUNTER_COLUMNS) \
                .classes("w-full").props("dense flat bordered")
            day_table.add_slot("body-cell-status", r'''
                <q-td :props="props">
                  <q-badge v-for="s in (props.value ? props.value.split(', ') : [])" :key="s" class="q-mr-xs"
                    :color="{'Lücke':'red','Nachholwert':'orange','negativ':'purple','Ausreißer':'amber-8','unvollständig':'teal'}[s]">
                    {{ s }}</q-badge>
                </q-td>''')
            ui.label("Klick auf eine Zeile öffnet die Rohdaten dieses Tages.").classes("text-xs opacity-60")

        # -------------------------------------------------------------- Rohdaten
        with ui.tab_panel(tab_raw):
            with ui.row().classes("items-end gap-4"):
                inp_raw_day = ui.date_input("Tag", value=s.raw_day.isoformat()).classes("w-44")
                inp_span = ui.number("± Tage", value=1, min=0, max=7, step=1).classes("w-24")
                ui.button("Anzeigen", icon="visibility", on_click=lambda: load_raw())
                ui.space()
                ui.button("Punkt einfügen …", icon="add", on_click=lambda: dlg_insert())
                ui.button("Wert ändern …", icon="edit", on_click=lambda: dlg_edit())
                ui.button("Löschen …", icon="delete", color="red", on_click=lambda: dlg_delete())
            raw_plot = ui.plotly({"data": [], "layout": {}}).classes("w-full h-80")
            raw_table = ui.table(
                rows=[], row_key="t", selection="multiple", pagination=50,
                columns=[{"name": "zeit", "label": "Zeit (lokal)", "field": "zeit", "align": "left", "sortable": True},
                         {"name": "wert", "label": "Wert", "field": "wert"},
                         {"name": "delta", "label": "Δ zum Vorwert", "field": "delta"},
                         {"name": "hinweis", "label": "", "field": "hinweis", "align": "left"}],
            ).classes("w-full").props("dense flat bordered")

        # -------------------------------------------------------------- Protokoll
        with ui.tab_panel(tab_log):
            with ui.row().classes("items-center w-full"):
                ui.label("Alle Änderungen werden mit den Originalpunkten gesichert "
                         f"({os.path.join(DATA_DIR, 'audit.jsonl')}).").classes("text-sm opacity-70")
                ui.space()
                ui.button("Ausgewählte Aktion rückgängig", icon="undo", on_click=lambda: do_undo())
            log_table = ui.table(
                rows=[], row_key="id", selection="single", pagination=25,
                columns=[{"name": "logged_at", "label": "Zeitpunkt", "field": "logged_at", "align": "left"},
                         {"name": "action", "label": "Aktion", "field": "action", "align": "left"},
                         {"name": "entity", "label": "Entity", "field": "entity", "align": "left"},
                         {"name": "zeit", "label": "Punkt-Zeit", "field": "zeit", "align": "left"},
                         {"name": "vorher", "label": "vorher", "field": "vorher"},
                         {"name": "nachher", "label": "nachher", "field": "nachher"},
                         {"name": "comment", "label": "Kommentar", "field": "comment", "align": "left"},
                         {"name": "undone", "label": "rückgängig", "field": "undone"}],
            ).classes("w-full").props("dense flat bordered")

    # ================================================================== Modus
    def apply_mode_ui():
        gauge = s.mode == A.GAUGE
        gauge_box.set_visibility(gauge)
        btn_spikes.set_visibility(gauge)
        btn_fix.set_text("Lücken füllen …" if gauge else "Lücken korrigieren …")
        factor_tip.set_text("Punkt-Ausreißer: Abweichung > Faktor × MAD vom gleitenden Median (Hampel-Filter)"
                            if gauge else "Tag ist Ausreißer, wenn Verbrauch > Faktor × Median-Tagesverbrauch")
        day_table.columns = GAUGE_COLUMNS if gauge else COUNTER_COLUMNS
        day_table.update()
        auto = " (automatisch erkannt)" if sel_mode.value == "auto" else ""
        mode_label.set_text(f"Modus: {A.MODE_LABELS[s.mode]}{auto}")

    def params() -> dict:
        return {"mode": sel_mode.value, "hole": float(inp_hole.value or HOLE_HOURS),
                "factor": float(inp_factor.value or 0), "step": int(inp_step.value or GRID_MINUTES),
                "vmin": num_or_none(inp_vmin.value), "vmax": num_or_none(inp_vmax.value)}

    def compute_days(raw) -> tuple[pd.DataFrame, list]:
        p = params()
        if s.mode == A.GAUGE:
            spikes = A.find_spikes(raw, p["factor"], vmin=p["vmin"], vmax=p["vmax"])
            return A.daily_gauge(raw, TZ, s.first, s.last, p["hole"], spikes), spikes
        return A.daily(raw, TZ, s.first, s.last, outlier_factor=p["factor"], hole_hours=p["hole"]), []

    # ================================================================== Logik
    async def call(fn, *args, **kw):
        try:
            return await run.io_bound(fn, *args, **kw)
        except (InfluxError, OSError, ValueError) as e:
            ui.notify(str(e), type="negative", multi_line=True, timeout=10000)
            raise

    async def connect():
        try:
            v = await call(client.ping)
            meas = await call(client.measurements)
        except Exception:  # noqa: BLE001
            conn_label.set_text(f"⚠️ keine Verbindung zu {client.url}")
            return
        conn_label.set_text(f"{client.url} · DB {client.database} · InfluxDB {v}")
        sel_meas.set_options(meas)
        if DEFAULT_MEASUREMENT in meas:
            sel_meas.set_value(DEFAULT_MEASUREMENT)

    async def on_meas(e):
        if not e.value:
            return
        ents = await call(client.entities, e.value)
        sel_ent.set_options(ents)
        sel_ent.set_value(ents[0] if len(ents) == 1 else None)

    sel_meas.on_value_change(on_meas)

    def on_entity(e):
        """Gespeicherte Einstellungen der Serie übernehmen."""
        if not e.value or not sel_meas.value:
            return
        cfg = series_settings.get(sel_meas.value, e.value)
        sel_mode.set_value(cfg.get("mode", "auto"))
        inp_hole.set_value(cfg.get("hole", HOLE_HOURS))
        inp_factor.set_value(cfg.get("factor", OUTLIER_FACTOR))
        inp_step.set_value(cfg.get("step", GRID_MINUTES))
        inp_vmin.set_value(cfg.get("vmin"))
        inp_vmax.set_value(cfg.get("vmax"))

    sel_ent.on_value_change(on_entity)

    def parse_day(v: str) -> date:
        return datetime.strptime(v.strip(), "%Y-%m-%d").date()

    async def load():
        if not sel_meas.value or not sel_ent.value:
            ui.notify("Bitte Measurement und Entity wählen.", type="warning")
            return
        try:
            s.first, s.last = parse_day(inp_from.value), parse_day(inp_to.value)
        except ValueError:
            ui.notify("Datum im Format JJJJ-MM-TT angeben.", type="warning")
            return
        if s.last < s.first:
            s.first, s.last = s.last, s.first
        s.measurement, s.entity = sel_meas.value, sel_ent.value
        start = A.day_start_ns(s.first - timedelta(days=1), TZ)      # Vortag als Basis
        end = A.day_start_ns(s.last + timedelta(days=1), TZ)
        btn_load.props("loading")
        try:
            s.raw = await call(client.values, s.measurement, s.entity, start, end)
            if not s.raw or s.raw[0][0] >= A.day_start_ns(s.first, TZ):
                prev = await call(client.neighbor, s.measurement, s.entity, start, True)
                if prev:
                    s.raw = [prev] + s.raw
        finally:
            btn_load.props(remove="loading")
        s.mode = A.detect_mode(s.raw) if sel_mode.value == "auto" else sel_mode.value
        series_settings.set(s.measurement, s.entity, params())
        apply_mode_ui()
        s.days, s.spikes = compute_days(s.raw)
        render_days()
        if s.first <= s.raw_day <= s.last:
            await load_raw()

    btn_load.on_click(load)

    def render_days():
        d = s.days
        if d.empty:
            return
        counts = {k: int(d["status"].str.contains(k).sum())
                  for k in ((A.GAP, A.PARTIAL, A.OUTLIER) if s.mode == A.GAUGE
                            else (A.GAP, A.CATCHUP, A.NEGATIVE, A.OUTLIER, A.PARTIAL))}
        x = [t.isoformat() for t in d["tag"]]
        nn = lambda col: [None if pd.isna(v) else v for v in d[col]]  # noqa: E731
        if s.mode == A.GAUGE:
            summary.set_text(f"{len(d)} Tage · {len(s.raw)} Werte · Ausreißer-Punkte: {len(s.spikes)} · "
                             + " · ".join(f"Tage {k}: {v}" for k, v in counts.items()))
            data = [
                {"type": "scatter", "mode": "lines", "x": x, "y": nn("min"), "name": "Min",
                 "line": {"width": 0}, "showlegend": False, "hoverinfo": "skip"},
                {"type": "scatter", "mode": "lines", "x": x, "y": nn("max"), "name": "Min–Max",
                 "fill": "tonexty", "fillcolor": "rgba(76,120,168,0.18)", "line": {"width": 0}},
                {"type": "scatter", "mode": "lines+markers", "x": x, "y": nn("mittel"),
                 "name": f"Tagesmittel [{s.measurement}]", "line": {"color": "#4c78a8"},
                 "marker": {"size": 8, "color": [status_color(st) for st in d["status"]]},
                 "text": list(d["status"]), "hovertemplate": "%{x}<br>%{y:.3f}<br>%{text}<extra></extra>"},
            ]
            layout_y = {"yaxis": {"title": s.measurement}}
        else:
            total = d["verbrauch"].sum(skipna=True)
            summary.set_text(f"{len(d)} Tage · Summe {fmt_v(total)} {s.measurement} · "
                             + " · ".join(f"{k}: {v}" for k, v in counts.items()))
            data = [
                {"type": "bar", "x": x, "y": nn("verbrauch"), "name": f"Verbrauch [{s.measurement}/Tag]",
                 "marker": {"color": [status_color(st) for st in d["status"]]},
                 "text": list(d["status"]), "hovertemplate": "%{x}<br>%{y:.3f}<br>%{text}<extra></extra>"},
                {"type": "scatter", "mode": "lines", "x": x, "yaxis": "y2", "name": "Zählerstand",
                 "y": nn("max_gefuellt"), "line": {"color": "#888"}},
            ]
            layout_y = {"yaxis": {"title": f"{s.measurement}/Tag"},
                        "yaxis2": {"overlaying": "y", "side": "right", "showgrid": False}}
        day_plot.figure = {"data": data, "layout": {
            "margin": {"l": 50, "r": 60, "t": 20, "b": 40}, "showlegend": True,
            "legend": {"orientation": "h", "y": 1.08}, **layout_y}}
        day_plot.update()
        render_day_table()

    def render_day_table():
        d = s.days
        if d.empty:
            return
        if only_issues.value:
            d = d[d["status"] != ""]
        wt = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        rows = []
        for _, r in d.iloc[::-1].iterrows():
            row = {"tag": r["tag"].isoformat(), "wt": wt[r["tag"].weekday()], "anzahl": int(r["anzahl"]),
                   "letzter": "" if r["letzter"] is None or pd.isna(r["letzter"]) else r["letzter"].strftime("%H:%M"),
                   "max": fmt_v(r["max"]), "status": r["status"]}
            if s.mode == A.GAUGE:
                row.update({"min": fmt_v(r["min"]), "mittel": fmt_v(r["mittel"])})
            else:
                row["verbrauch"] = fmt_v(r["verbrauch"])
            rows.append(row)
        day_table.rows = rows
        day_table.update()

    only_issues.on_value_change(lambda _: render_day_table())

    async def on_day_click(e):
        row = e.args[1]
        inp_raw_day.set_value(row["tag"])
        tabs.set_value(tab_raw)
        await load_raw()

    day_table.on("rowClick", on_day_click)

    # ------------------------------------------------------------------ Rohdaten
    async def load_raw():
        if not s.entity:
            ui.notify("Erst oben eine Serie laden.", type="warning")
            return
        try:
            s.raw_day = parse_day(inp_raw_day.value)
        except ValueError:
            ui.notify("Datum im Format JJJJ-MM-TT angeben.", type="warning")
            return
        span = int(inp_span.value or 0)
        start = A.day_start_ns(s.raw_day - timedelta(days=span), TZ)
        end = A.day_start_ns(s.raw_day + timedelta(days=span + 1), TZ)
        pts = await call(client.values, s.measurement, s.entity, start, end)
        spike_t = {t: reason for t, _, _, reason in s.spikes}
        if s.mode == A.GAUGE and not (s.first <= s.raw_day <= s.last):
            spike_t = {t: r for t, _, _, r in A.find_spikes(pts, float(inp_factor.value or 0),
                                                            vmin=num_or_none(inp_vmin.value),
                                                            vmax=num_or_none(inp_vmax.value))}
        rows, prev = [], None
        for t, v in pts:
            rows.append({"t": str(t), "zeit": fmt_t(t), "wert": fmt_v(v), "v": v,
                         "delta": "" if prev is None else fmt_v(v - prev),
                         "hinweis": f"⚠️ Ausreißer ({spike_t[t]})" if t in spike_t else ""})
            prev = v
        raw_table.rows = rows
        raw_table.selected = []
        raw_table.update()
        shapes = [{"type": "line", "xref": "x", "yref": "paper", "y0": 0, "y1": 1,
                   "x0": str(pd.Timestamp(s.raw_day + timedelta(days=i)).tz_localize(TZ)),
                   "x1": str(pd.Timestamp(s.raw_day + timedelta(days=i)).tz_localize(TZ)),
                   "line": {"dash": "dot", "color": "#aaa"}} for i in range(-span, span + 2)]
        data = [{"type": "scatter", "mode": "lines+markers", "name": "Wert",
                 "x": [str(A.to_local(t, TZ)) for t, _ in pts], "y": [v for _, v in pts]}]
        sp = [(t, v) for t, v in pts if t in spike_t]
        if sp:
            data.append({"type": "scatter", "mode": "markers", "name": "Ausreißer",
                         "x": [str(A.to_local(t, TZ)) for t, _ in sp], "y": [v for _, v in sp],
                         "marker": {"color": "#e45756", "size": 11, "symbol": "x"}})
        raw_plot.figure = {"data": data, "layout": {
            "margin": {"l": 60, "r": 20, "t": 20, "b": 40}, "shapes": shapes,
            "yaxis": {"title": s.measurement}, "legend": {"orientation": "h", "y": 1.1}}}
        raw_plot.update()

    async def after_change():
        await load()
        await load_raw()
        render_log()

    # ------------------------------------------------------------------ Vorschau
    def preview_table(inserts=(), deletes=(), edits=()):
        if s.days.empty:
            return
        after, _ = compute_days(A.simulate(s.raw, inserts, deletes, edits))
        cols = ["min", "mittel", "max"] if s.mode == A.GAUGE else ["verbrauch"]
        cmp_ = A.compare_cols(s.days, after, cols)
        if cmp_.empty:
            ui.label("Keine Auswirkung auf die Tageswerte im geladenen Zeitraum.").classes("text-sm opacity-70")
            return
        ui.label(f"Auswirkung auf die Tageswerte ({len(cmp_)} Tage):").classes("text-sm font-medium")
        rows = []
        for _, r in cmp_.head(40).iterrows():
            row = {"tag": r["tag"].isoformat(),
                   "status": f'{r["status_vorher"] or "–"} → {r["status_nachher"] or "–"}'}
            for c in cols:
                row[c] = f'{fmt_v(r[f"{c}_vorher"])} → {fmt_v(r[f"{c}_nachher"])}'
            rows.append(row)
        columns = [{"name": "tag", "label": "Tag", "field": "tag", "align": "left"}] + \
                  [{"name": c, "label": c.capitalize(), "field": c} for c in cols] + \
                  [{"name": "status", "label": "Status", "field": "status"}]
        ui.table(rows=rows, columns=columns, row_key="tag").props("dense flat bordered").classes("w-full")

    def selected_points() -> list[tuple[int, float]]:
        return [(int(r["t"]), r["v"]) for r in raw_table.selected]

    # ------------------------------------------------------------------ Dialoge
    async def dlg_insert():
        if not s.entity:
            ui.notify("Erst oben eine Serie laden.", type="warning")
            return
        default = f"{s.raw_day.isoformat()} 23:59:59"
        sel = selected_points()
        if len(sel) == 2:  # zwischen zwei markierten Punkten -> Mitte
            default = fmt_t((sel[0][0] + sel[1][0]) // 2, ms=False)
        with ui.dialog() as dlg, ui.card().classes("w-[40rem]"):
            ui.label(f"Punkt einfügen – {s.entity}").classes("text-lg")
            inp_t = ui.input("Zeitpunkt (lokal, JJJJ-MM-TT HH:MM:SS)", value=default).classes("w-full")
            inp_v = ui.number("Wert", format="%.3f").classes("w-full")
            info = ui.label("").classes("text-sm opacity-70")
            inp_c = ui.input("Kommentar (optional)").classes("w-full")
            prev_box = ui.column().classes("w-full")

            async def interpolate():
                try:
                    t = A.local_to_ns(inp_t.value, TZ)
                except Exception:  # noqa: BLE001
                    ui.notify("Zeitpunkt ungültig.", type="warning")
                    return
                p0 = await call(client.neighbor, s.measurement, s.entity, t, True)
                p1 = await call(client.neighbor, s.measurement, s.entity, t, False)
                v = A.interpolate_at(t, p0, p1)
                if v is None:
                    info.set_text("Interpolation nicht möglich – Nachbarwert fehlt.")
                    return
                inp_v.set_value(round(v, 3))
                info.set_text(f"linear zwischen {fmt_t(p0[0])} ({fmt_v(p0[1])}) und {fmt_t(p1[0])} ({fmt_v(p1[1])})")

            def refresh_preview():
                prev_box.clear()
                try:
                    t = A.local_to_ns(inp_t.value, TZ)
                except Exception:  # noqa: BLE001
                    return
                if inp_v.value is not None:
                    with prev_box:
                        preview_table(inserts=[(t, inp_v.value)])

            inp_v.on_value_change(lambda _: refresh_preview())
            inp_t.on("blur", lambda _: refresh_preview())

            async def save():
                try:
                    t = A.local_to_ns(inp_t.value, TZ)
                except Exception:  # noqa: BLE001
                    ui.notify("Zeitpunkt ungültig.", type="warning")
                    return
                if inp_v.value is None:
                    ui.notify("Wert fehlt.", type="warning")
                    return
                await call(ops.insert, s.measurement, s.entity, t, float(inp_v.value), inp_c.value)
                ui.notify("Punkt eingefügt.", type="positive")
                dlg.close()
                await after_change()

            with ui.row().classes("w-full justify-end"):
                ui.button("Interpolieren", icon="timeline", on_click=interpolate).props("flat")
                ui.button("Abbrechen", on_click=dlg.close).props("flat")
                ui.button("Schreiben", icon="save", on_click=save)
        dlg.open()
        await interpolate()

    async def dlg_edit():
        sel = selected_points()
        if len(sel) != 1:
            ui.notify("Genau einen Punkt in der Tabelle markieren.", type="warning")
            return
        t, v_old = sel[0]
        with ui.dialog() as dlg, ui.card().classes("w-[40rem]"):
            ui.label(f"Wert ändern – {fmt_t(t)}").classes("text-lg")
            ui.label(f"bisher: {fmt_v(v_old)}").classes("text-sm opacity-70")
            inp_v = ui.number("neuer Wert", value=v_old, format="%.3f").classes("w-full")
            inp_c = ui.input("Kommentar (optional)").classes("w-full")
            prev_box = ui.column().classes("w-full")

            def refresh_preview():
                prev_box.clear()
                if inp_v.value is not None:
                    with prev_box:
                        preview_table(edits=[(t, inp_v.value)])
            inp_v.on_value_change(lambda _: refresh_preview())

            async def interpolate():
                p0 = await call(client.neighbor, s.measurement, s.entity, t, True)
                p1 = await call(client.neighbor, s.measurement, s.entity, t, False)
                v = A.interpolate_at(t, p0, p1)
                if v is not None:
                    inp_v.set_value(round(v, 3))

            async def save():
                await call(ops.edit, s.measurement, s.entity, t, float(inp_v.value), inp_c.value)
                ui.notify("Wert geändert.", type="positive")
                dlg.close()
                await after_change()

            with ui.row().classes("w-full justify-end"):
                ui.button("aus Nachbarn interpolieren", icon="timeline", on_click=interpolate).props("flat")
                ui.button("Abbrechen", on_click=dlg.close).props("flat")
                ui.button("Schreiben", icon="save", on_click=save)
        dlg.open()

    async def dlg_delete():
        sel = selected_points()
        if not sel:
            ui.notify("Punkte in der Tabelle markieren.", type="warning")
            return
        with ui.dialog() as dlg, ui.card().classes("w-[40rem]"):
            ui.label(f"{len(sel)} Punkt(e) löschen – {s.entity}").classes("text-lg")
            ui.label("Gelöscht werden alle Fields (auch HA-Attribute) dieser Entity zu den Zeitstempeln. "
                     "Die Originalpunkte werden im Protokoll gesichert.").classes("text-sm opacity-70")
            for t, v in sel[:10]:
                ui.label(f"• {fmt_t(t)}  –  {fmt_v(v)}").classes("text-sm font-mono")
            if len(sel) > 10:
                ui.label(f"… und {len(sel) - 10} weitere").classes("text-sm")
            inp_c = ui.input("Kommentar (optional)").classes("w-full")
            preview_table(deletes=[t for t, _ in sel])

            async def save():
                for t, _ in sel:
                    await call(ops.delete, s.measurement, s.entity, t, inp_c.value)
                ui.notify(f"{len(sel)} Punkt(e) gelöscht.", type="positive")
                dlg.close()
                await after_change()

            with ui.row().classes("w-full justify-end"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat")
                ui.button("Löschen", icon="delete", color="red", on_click=save)
        dlg.open()

    async def dlg_fix_gaps():
        if s.days.empty:
            ui.notify("Erst Daten laden.", type="warning")
            return
        p = params()
        if s.mode == A.GAUGE:
            props, hints = await run.io_bound(A.gauge_gap_proposals, s.raw, TZ, p["hole"], p["step"])
            intro = (f"Datenlöcher > {p['hole']:g} h werden im {p['step']}-Minuten-Raster mit linear "
                     "interpolierten Werten gefüllt.")
        else:
            props, hints = await run.io_bound(
                A.gap_proposals, s.raw, TZ, s.days,
                lambda t, before: client.neighbor(s.measurement, s.entity, t, before), p["hole"])
            intro = ("Für jedes Datenloch über Mitternacht wird pro betroffenem Tag ein linear interpolierter "
                     "Zählerstand um 23:59:59 eingefügt.")
        with ui.dialog() as dlg, ui.card().classes("w-[52rem] max-w-none"):
            ui.label(f"Lücken {'füllen' if s.mode == A.GAUGE else 'korrigieren'} – {s.entity}").classes("text-lg")
            ui.label(intro + " Werte lassen sich vor dem Schreiben anpassen.").classes("text-sm opacity-70")
            for h in hints:
                ui.label(f"ℹ️ {h}").classes("text-sm")
            if not props:
                ui.button("Schließen", on_click=dlg.close)
                dlg.open()
                return
            prev_box = ui.column().classes("w-full")
            if len(props) <= EDITABLE_LIMIT:
                checks, nums = [], []
                with ui.scroll_area().classes("w-full max-h-96"):
                    with ui.grid(columns=4).classes("w-full items-center gap-x-4 gap-y-0"):
                        for h in ("übernehmen", "Zeitpunkt", "Wert", "Basis"):
                            ui.label(h).classes("text-xs font-medium opacity-70")
                        for pr in props:
                            checks.append(ui.checkbox(value=True))
                            ui.label(fmt_t(pr.time_ns, ms=False)).classes("font-mono text-sm")
                            nums.append(ui.number(value=round(pr.wert, 3), format="%.3f").props("dense"))
                            ui.label(pr.basis).classes("text-xs opacity-70")

                def chosen():
                    return [(pr.time_ns, float(n.value)) for pr, c, n in zip(props, checks, nums)
                            if c.value and n.value is not None]
                widgets = checks + nums
            else:
                ui.label(f"{len(props)} Punkte – Auszug:").classes("text-sm font-medium")
                ui.table(rows=[{"zeit": fmt_t(pr.time_ns, ms=False), "wert": fmt_v(pr.wert), "basis": pr.basis}
                               for pr in props], row_key="zeit", pagination=10,
                         columns=[{"name": "zeit", "label": "Zeitpunkt", "field": "zeit", "align": "left"},
                                  {"name": "wert", "label": "Wert", "field": "wert"},
                                  {"name": "basis", "label": "Basis", "field": "basis", "align": "left"}]) \
                    .props("dense flat bordered").classes("w-full")

                def chosen():
                    return [(pr.time_ns, pr.wert) for pr in props]
                widgets = []

            def refresh_preview():
                prev_box.clear()
                with prev_box:
                    preview_table(inserts=chosen())
            for w in widgets:
                w.on_value_change(lambda _: refresh_preview())
            prev_box.move(target_index=-1)
            refresh_preview()

            async def save():
                items = chosen()
                if not items:
                    return
                await call(ops.insert_many, s.measurement, s.entity, items,
                           "Lückenfüllung" if s.mode == A.GAUGE else "Lückenkorrektur")
                ui.notify(f"{len(items)} Punkt(e) eingefügt.", type="positive")
                dlg.close()
                await after_change()

            with ui.row().classes("w-full justify-end"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat")
                ui.button("Schreiben", icon="save", on_click=save)
        dlg.open()

    btn_fix.on_click(dlg_fix_gaps)

    async def dlg_spikes():
        if not s.spikes:
            ui.notify("Keine Ausreißer gefunden.", type="info")
            return
        repl = A.spike_replacements(s.raw, s.spikes)
        with ui.dialog() as dlg, ui.card().classes("w-[52rem] max-w-none"):
            ui.label(f"Ausreißer bereinigen – {s.entity}").classes("text-lg")
            ui.label("Erkannt per Hampel-Filter (Abweichung vom gleitenden Median) bzw. Plausibilitätsbereich.") \
                .classes("text-sm opacity-70")
            action = ui.radio({"edit": "durch interpolierten Wert ersetzen", "delete": "löschen"},
                              value="edit").props("inline")
            checks = []
            with ui.scroll_area().classes("w-full max-h-96"):
                with ui.grid(columns=5).classes("w-full items-center gap-x-4 gap-y-0"):
                    for h in ("", "Zeitpunkt", "Wert", "Referenz (Median)", "Ersatzwert"):
                        ui.label(h).classes("text-xs font-medium opacity-70")
                    for t, v, ref, reason in s.spikes:
                        checks.append(ui.checkbox(value=True))
                        ui.label(fmt_t(t)).classes("font-mono text-sm")
                        ui.label(f"{fmt_v(v)} ({reason})").classes("text-sm")
                        ui.label(fmt_v(ref)).classes("text-sm")
                        ui.label(fmt_v(repl.get(t)) or "–").classes("text-sm")
            prev_box = ui.column().classes("w-full")

            def chosen():
                return [sp for sp, c in zip(s.spikes, checks) if c.value]

            def refresh_preview():
                prev_box.clear()
                sel = chosen()
                with prev_box:
                    if action.value == "delete":
                        preview_table(deletes=[t for t, *_ in sel])
                    else:
                        preview_table(edits=[(t, repl[t]) for t, *_ in sel if repl.get(t) is not None])
            for w in checks + [action]:
                w.on_value_change(lambda _: refresh_preview())
            refresh_preview()

            async def save():
                n = 0
                for t, *_ in chosen():
                    if action.value == "delete":
                        await call(ops.delete, s.measurement, s.entity, t, "Ausreißer gelöscht")
                    elif repl.get(t) is not None:
                        await call(ops.edit, s.measurement, s.entity, t, round(repl[t], 6), "Ausreißer ersetzt")
                    else:
                        continue
                    n += 1
                ui.notify(f"{n} Ausreißer bereinigt.", type="positive")
                dlg.close()
                await after_change()

            with ui.row().classes("w-full justify-end"):
                ui.button("Abbrechen", on_click=dlg.close).props("flat")
                ui.button("Ausführen", icon="save", on_click=save)
        dlg.open()

    btn_spikes.on_click(dlg_spikes)

    # ------------------------------------------------------------------ Protokoll
    def render_log():
        def val(lst):
            if len(lst) > 3:
                return f"{len(lst)} Punkte"
            return ", ".join(fmt_v(p["fields"].get("value")) for p in lst)
        rows = []
        for e in reversed(ops.history()):
            rows.append({"id": e["id"], "logged_at": e["logged_at"].replace("T", " ")[:19],
                         "action": e["action"], "entity": e.get("entity", ""),
                         "zeit": fmt_t(e["time_ns"]) if e.get("time_ns") else "",
                         "vorher": val(e.get("before", [])), "nachher": val(e.get("after", [])),
                         "comment": e.get("comment", ""), "undone": "✓" if e.get("undone") else ""})
        log_table.rows = rows
        log_table.selected = []
        log_table.update()

    async def do_undo():
        if not log_table.selected:
            ui.notify("Einen Protokolleintrag markieren.", type="warning")
            return
        await call(ops.undo, log_table.selected[0]["id"])
        ui.notify("Rückgängig gemacht.", type="positive")
        render_log()
        if s.entity:
            await load()
            await load_raw()

    apply_mode_ui()
    gauge_box.set_visibility(False)
    btn_spikes.set_visibility(False)
    mode_label.set_text("")
    render_log()
    ui.timer(0.1, connect, once=True)


ui.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), title="Influx-Korrektur",
       favicon="📈", reload=False, show=False, language="de")
