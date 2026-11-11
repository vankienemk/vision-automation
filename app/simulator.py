"""Simulation helpers: fake presence in a zone and accelerated time.

Two independent features, both driven from the "Mô phỏng" tab:

* **Fake presence** — force a zone occupied/vacant without standing in front of
  the camera. Implemented by delegating to :meth:`ZoneEngine.force_zone`, so the
  whole pipeline (voting bypass, scenario engine, MQTT dry-run) behaves exactly
  as it does with a real detection.
* **Time speed-up** — run the scenario clock faster (x10 by default) so a
  "dim after 30 s" scenario can be verified in 3 seconds. Only the scenario
  clock is scaled; capture and inference keep real time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .scenarios import ScaledClock
from .zones import ZoneEngine

LOGGER = logging.getLogger(__name__)

EngineProvider = Callable[[], dict[str, ZoneEngine]]


@dataclass
class SimulationEntry:
    occupied: bool
    count: int
    class_counts: dict[str, int]
    started_at: float

    def as_dict(self, now: float) -> dict:
        return {
            "occupied": self.occupied,
            "count": self.count,
            "class_counts": self.class_counts,
            "age_s": round(max(0.0, now - self.started_at), 1),
        }


class Simulator:
    """Owns fake zone states and the time scale."""

    def __init__(
        self,
        clock: ScaledClock | None = None,
        *,
        logger: logging.Logger | None = None,
        engines: EngineProvider | None = None,
    ) -> None:
        self.log = logger or LOGGER.getChild("simulator")
        self.clock = clock
        self._engines = engines
        self._entries: dict[str, SimulationEntry] = {}

    # -- wiring ----------------------------------------------------------- #
    def bind_engines(self, provider: EngineProvider) -> None:
        self._engines = provider

    def _engine_for(self, zone_ref: str) -> tuple[ZoneEngine, str] | None:
        if "/" not in zone_ref:
            return None
        camera_id, zone_id = zone_ref.split("/", 1)
        if self._engines is None:
            return None
        engine = self._engines().get(camera_id)
        if engine is None or zone_id not in engine.zone_ids():
            return None
        return engine, zone_id

    # -- presence --------------------------------------------------------- #
    def occupy(
        self,
        zone_ref: str,
        *,
        count: int = 1,
        classes: dict[str, int] | None = None,
    ) -> dict:
        """Force ``zone_ref`` occupied (simulated)."""
        target = self._engine_for(zone_ref)
        if target is None:
            return {"ok": False, "error": f"Không thấy vùng '{zone_ref}' để mô phỏng."}
        engine, zone_id = target
        engine.force_zone(zone_id, True, count, classes)
        class_counts = dict(classes or {})
        if not class_counts:
            zone = next((z for z in engine.zones() if z.id == zone_id), None)
            class_counts = {(zone.classes[0] if zone and zone.classes else "person"): max(1, count)}
        self._entries[zone_ref] = SimulationEntry(True, max(1, count), class_counts, time.time())
        self.log.info("Mô phỏng: %s = có người (count=%d)", zone_ref, count)
        return {"ok": True, "zone": zone_ref, "occupied": True, "count": count}

    def vacate(self, zone_ref: str) -> dict:
        """Force ``zone_ref`` vacant (simulated)."""
        target = self._engine_for(zone_ref)
        if target is None:
            return {"ok": False, "error": f"Không thấy vùng '{zone_ref}' để mô phỏng."}
        engine, zone_id = target
        engine.force_zone(zone_id, False, 0)
        self._entries[zone_ref] = SimulationEntry(False, 0, {}, time.time())
        self.log.info("Mô phỏng: %s = trống", zone_ref)
        return {"ok": True, "zone": zone_ref, "occupied": False, "count": 0}

    def clear(self, zone_ref: str | None = None) -> dict:
        """Return one zone (or all zones) to real detection."""
        if zone_ref is None:
            refs = list(self._entries)
            if self._engines is not None:
                for engine in self._engines().values():
                    engine.clear_force()
            self._entries.clear()
            self.log.info("Mô phỏng: đã tắt toàn bộ (%d vùng)", len(refs))
            return {"ok": True, "cleared": refs}
        target = self._engine_for(zone_ref)
        if target is None:
            return {"ok": False, "error": f"Không thấy vùng '{zone_ref}'."}
        engine, zone_id = target
        engine.clear_force(zone_id)
        self._entries.pop(zone_ref, None)
        self.log.info("Mô phỏng: %s trở lại nhận diện thật", zone_ref)
        return {"ok": True, "cleared": [zone_ref]}

    def toggle(self, zone_ref: str) -> dict:
        entry = self._entries.get(zone_ref)
        if entry is not None and entry.occupied:
            return self.vacate(zone_ref)
        return self.occupy(zone_ref)

    def forced_zones(self) -> list[str]:
        return list(self._entries)

    # -- time ------------------------------------------------------------- #
    def set_scale(self, scale: float) -> dict:
        value = max(1.0, min(60.0, float(scale)))
        if self.clock is not None:
            self.clock.set_scale(value)
        self.log.info("Mô phỏng: tốc độ thời gian x%.0f", value)
        return {"ok": True, "scale": value}

    @property
    def scale(self) -> float:
        return float(self.clock.scale) if self.clock is not None else 1.0

    # -- introspection ---------------------------------------------------- #
    def active(self) -> bool:
        return bool(self._entries) or self.scale > 1.0 + 1e-9

    def state(self) -> dict:
        now = time.time()
        return {
            "active": self.active(),
            "scale": self.scale,
            "zones": {ref: entry.as_dict(now) for ref, entry in self._entries.items()},
        }


__all__ = ["Simulator", "SimulationEntry"]
