"""Einstellungen je Serie (Measurement + Entity) dauerhaft in DATA_DIR/settings.json."""
from __future__ import annotations

import json
import os
import threading

_lock = threading.Lock()


class SeriesSettings:
    def __init__(self, path: str):
        self.path = path

    def _all(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def get(self, measurement: str, entity: str) -> dict:
        return self._all().get(f"{measurement}|{entity}", {})

    def set(self, measurement: str, entity: str, values: dict) -> None:
        with _lock:
            data = self._all()
            data[f"{measurement}|{entity}"] = values
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
