# Influx-Korrektur

Weboberfläche zum Kontrollieren und Korrigieren von Zählerständen (Strom, Wasser, …), die Home Assistant
über die InfluxDB-Integration schreibt. Läuft als eigenständiger Docker-Container, z. B. auf der Synology.

## Funktionen

- **Tagesübersicht**: Tagesverbrauch wie in der bestehenden Auswertung
  (`derivative(max(value),1d) … fill(previous) tz('Europe/Berlin')`), mit Diagramm und Statusmarken:
  - **Lücke**: Tag liegt komplett in einem Datenloch
  - **unvollständig**: letzter Wert des Tages liegt deutlich vor Mitternacht, danach Datenloch
  - **Nachholwert**: Tag, dem der Verbrauch aus dem Datenloch zugeschlagen wird
  - **negativ** / **Ausreißer**: Zählerrücksprung bzw. Tagesverbrauch > Faktor × Median
- **Lücken korrigieren**: setzt pro betroffenem Tag einen linear interpolierten Stützpunkt um 23:59:59
  (lokale Zeit, sommerzeitfest). Werte sind vor dem Schreiben änderbar; eine Vorschau zeigt den
  Tagesverbrauch vorher und nachher.
- **Rohdaten**: Punkte eines Tages (± n Tage), einzeln einfügen (mit Interpolation), ändern, löschen.
- **Protokoll + Undo**: Vor jeder Änderung werden die Originalpunkte mit allen Fields und exakten Tags
  in `data/audit.jsonl` gesichert. Jede Aktion lässt sich rückgängig machen.

Als Datenloch zählen zwei aufeinanderfolgende Messwerte über Mitternacht mit mehr als `HOLE_HOURS`
Abstand **und** Zählerzuwachs. Tage ohne Werte, an denen sich der Zähler nicht geändert hat (z. B. die
Wallbox ist nicht aktiv), werden bewusst nicht markiert, weil dort kein Verbrauch verschoben wird.

## Installation auf der Synology (Container Manager)

1. Den Ordner nach `/volume1/docker/influx-korrektur` kopieren.
2. `.env.example` nach `.env` kopieren und die Zugangsdaten eintragen.
3. Container Manager → **Projekt** → Erstellen → Pfad wählen → `docker-compose.yml` wird erkannt → bauen und starten.
   Alternativ per SSH: `cd /volume1/docker/influx-korrektur && sudo docker compose up -d --build`
4. Im Browser `http://<synology-ip>:8099` öffnen.

## Rechte in InfluxDB 1.8 (HA-Add-on)

Der Benutzer braucht Lese- und Schreibrechte auf die Datenbank `homeassistant` (`ALL`).
Meldet das Löschen einen Rechtefehler, braucht der Benutzer Admin-Rechte. Die Rechte vergibst du in
der Add-on-Oberfläche (Chronograf → InfluxDB Admin → Users).

## Technische Hinweise

- Zeitstempel werden ns-genau gelesen und geschrieben. Das ist nötig, damit Ändern und Löschen den
  exakten HA-Punkt treffen.
- Neue Punkte übernehmen das Tag-Set (`domain`, `entity_id`, …) des zeitlich nächsten Punkts. Damit
  landen sie in derselben Serie. Zusätzlich wird das Field `korrektur_str = manuell|geändert` gesetzt.
  Das ist ein Field, kein Tag, und erzeugt deshalb keine neue Serie.
- **Löschen** entfernt in InfluxDB immer alle Fields eines Zeitstempels, also auch HA-Attribute wie
  `friendly_name_str`. Das Undo stellt sie aus dem Backup wieder her.
- InfluxDB 2.x wird erkannt (`/ping`). Lesen und Schreiben laufen dann über die v1-Kompatibilitäts-API
  (DBRP-Mapping nötig), Löschen über `/api/v2/delete`.

## Entwicklung / Tests

```bash
pip install -r requirements.txt pytest
pytest tests                                   # Logik + End-to-End gegen InfluxDB-Simulator
python tests/fake_influx.py 18086 &            # Simulator mit Testdaten (Lücken)
cd app && INFLUX_URL=http://127.0.0.1:18086 DATA_DIR=/tmp/data python main.py
```
