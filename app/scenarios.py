"""Scenario state machines (pure Python, injectable clock).

The engine is deliberately free of asyncio, OpenCV and MQTT: it consumes presence
snapshots, produces :class:`CommandRequest` objects and can be driven by a fake
clock in tests. All real-time behaviour (timers, cooldowns, schedules) is expressed
as arithmetic on ``clock.now()``.

Timeline semantics
------------------
* ``on_enter`` steps run when a zone becomes occupied. Any pending step of the
  previous vacancy timeline is cancelled first — that is what makes the light pop
  back to 100 % when somebody returns while it was dimmed.
* ``on_vacant`` steps are relative to the moment the zone became vacant
  (``after_s`` seconds later).
* The very first reliable state after boot does **not** trigger the vacancy
  timeline: we never publish "OFF" just because the app started in an empty room.
  The first ``occupied`` state does trigger ``on_enter`` (turning a light on is
  harmless, and the user expects it).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Protocol, Sequence

from .config import PublishAction, ScenarioConfig, StepSpec, TriggerSpec
from .zones import ZoneState

logger = logging.getLogger(__name__)


class Clock(Protocol):
    """Monotonic-ish seconds. Injected so tests can travel in time."""

    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    """Manual clock for unit tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now

    def set(self, seconds: float) -> float:
        self._now = float(seconds)
        return self._now


class ScaledClock:
    """Wraps another clock and can run faster (simulation "x10")."""

    def __init__(self, base: Clock, scale: float = 1.0) -> None:
        self.base = base
        self.scale = max(0.1, float(scale))
        self._virtual = base.now()
        self._last_raw = base.now()

    def now(self) -> float:
        raw = self.base.now()
        self._virtual += (raw - self._last_raw) * self.scale
        self._last_raw = raw
        return self._virtual

    def set_scale(self, scale: float) -> None:
        # sync to the current virtual position before changing speed
        self.now()
        self.scale = max(0.1, float(scale))


# --------------------------------------------------------------------------- #
# output objects
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CommandRequest:
    """A publish request produced by a scenario step."""

    scenario_id: str
    scenario_name: str
    zone_ref: str
    camera_id: str
    zone_id: str
    zone_name: str
    trigger: str
    step_label: str
    action: PublishAction
    context: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    wall_time: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "scenario_name": self.scenario_name,
            "zone": self.zone_ref,
            "trigger": self.trigger,
            "label": self.step_label,
            "topic": self.action.topic,
            "payload": self.action.payload,
            "qos": self.action.qos,
            "retain": self.action.retain,
            "context": dict(self.context),
            "wall_time": self.wall_time,
        }


@dataclass(slots=True)
class PendingStep:
    due: float
    after_s: float
    trigger: str
    label: str
    action: PublishAction
    seq: int


def _fmt_seconds(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} giờ"
    if seconds >= 60:
        return f"{seconds / 60:.0f} phút"
    return f"{seconds:.0f} giây"


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class ScenarioRunner:
    """One scenario, one state machine."""

    def __init__(
        self,
        scenario: ScenarioConfig,
        clock: Clock,
        *,
        wall_clock: Callable[[], float] = time.time,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.scenario = scenario
        self.clock = clock
        self.wall_clock = wall_clock
        self.log = logger_ or logger
        self._pending: list[PendingStep] = []
        self._seq = 0
        self._occupied: bool | None = None
        self._count = 0
        self._class_counts: dict[str, int] = {}
        self._occupied_since: float | None = None
        self._last_fire: dict[str, float] = {}
        self._paused_until: float | None = None
        self._dwell_fired: set[int] = set()
        self._class_active: set[str] = set()
        self._count_flags: dict[int, bool] = {}
        self._last_command: dict[str, Any] | None = None
        self._resume_needed = False
        self._zone_name = ""
        self._camera_id = ""
        self._zone_id = ""
        if "/" in scenario.zone:
            self._camera_id, self._zone_id = scenario.zone.split("/", 1)

    # -- helpers ---------------------------------------------------------- #
    @property
    def id(self) -> str:
        return self.scenario.id

    def set_zone_name(self, name: str) -> None:
        self._zone_name = name

    def reconfigure(self, scenario: ScenarioConfig) -> None:
        """Apply an edited scenario without dropping the current timeline."""
        self.scenario = scenario
        if "/" in scenario.zone:
            self._camera_id, self._zone_id = scenario.zone.split("/", 1)
        if not scenario.enabled:
            self.clear_timers()

    def clear_timers(self) -> None:
        self._pending.clear()
        self._dwell_fired.clear()

    def _context(self) -> dict[str, Any]:
        return {
            "count": self._count,
            "class_counts": dict(self._class_counts),
            "zone": self._zone_id,
            "zone_name": self._zone_name or self._zone_id,
            "camera": self._camera_id,
            "timestamp": datetime.fromtimestamp(self.wall_clock()).isoformat(timespec="seconds"),
            "scenario": self.scenario.id,
        }

    # -- schedule / pause ------------------------------------------------- #
    def schedule_active(self) -> bool:
        schedule = self.scenario.active_schedule
        if schedule is None:
            return True
        moment = datetime.fromtimestamp(self.wall_clock())
        return schedule.is_active(moment.hour, moment.minute, moment.weekday())

    def paused_remaining(self, now: float | None = None) -> float:
        if self._paused_until is None:
            return 0.0
        remaining = self._paused_until - (self.clock.now() if now is None else now)
        if remaining <= 0:
            return 0.0
        return remaining

    def notify_manual_override(self) -> None:
        """The physical device was operated by hand -> pause automation."""
        override = self.scenario.manual_override
        if override is None:
            return
        pause_s = max(0.0, override.pause_minutes * 60.0)
        if pause_s <= 0:
            return
        self._paused_until = self.clock.now() + pause_s
        self.clear_timers()
        self.log.info(
            "Kịch bản '%s': người dùng chỉnh tay, tạm dừng tự động %.0f phút",
            self.scenario.name,
            override.pause_minutes,
        )

    # -- presence --------------------------------------------------------- #
    def apply_presence(self, state: ZoneState) -> list[CommandRequest]:
        """Feed a zone snapshot; returns commands that are due immediately."""
        now = self.clock.now()
        commands: list[CommandRequest] = []
        self._count = state.count
        self._class_counts = dict(state.class_counts)
        if state.name:
            self._zone_name = state.name
        occupied = state.occupied

        if not self.scenario.enabled:
            self._occupied = occupied
            self.clear_timers()
            return commands

        previous = self._occupied
        self._occupied = occupied
        if occupied is None:
            return commands

        if previous is not True and occupied:
            # somebody came back: cancel the pending dim/off timeline first
            self.clear_timers()
            self._count_flags.clear()
            self._occupied_since = state.since if state.since is not None else now
            self.log.debug("Kịch bản '%s': zone có người -> chạy on_enter", self.scenario.name)
            commands.extend(self._start_trigger("enter", now))
        elif previous is True and not occupied:
            self.clear_timers()
            self._count_flags.clear()
            self._occupied_since = None
            self.log.debug("Kịch bản '%s': zone vắng -> chạy on_vacant", self.scenario.name)
            commands.extend(self._start_trigger("vacant", now))
        elif previous is None and not occupied:
            # first reliable "nobody here" after boot: adopt it silently
            self.log.debug(
                "Kịch bản '%s': trạng thái ban đầu là vắng, không publish OFF", self.scenario.name
            )
            self.clear_timers()

        commands.extend(self._check_level_triggers(now))
        return commands

    # -- timers ----------------------------------------------------------- #
    def tick(self, now: float | None = None) -> list[CommandRequest]:
        """Advance timers; returns commands whose due time has arrived."""
        moment = self.clock.now() if now is None else now
        commands: list[CommandRequest] = []

        if self._paused_until is not None:
            if moment < self._paused_until:
                if self._pending:
                    self.log.debug(
                        "Kịch bản '%s': đang tạm dừng do chỉnh tay, bỏ %d bước chờ",
                        self.scenario.name,
                        len(self._pending),
                    )
                self._pending.clear()
                return commands
            self._paused_until = None
            self._resume_needed = True
            self.log.info("Kịch bản '%s': hết thời gian tạm dừng, bật lại tự động", self.scenario.name)

        if not self.scenario.enabled:
            self._pending.clear()
            return commands

        # The active window is re-checked at firing time: a step that was queued
        # while the window was open must not fire after it has closed.
        if not self.schedule_active():
            if self._pending:
                self.log.debug(
                    "Kịch bản '%s': ngoài khung giờ hoạt động, bỏ %d bước đang chờ",
                    self.scenario.name,
                    len(self._pending),
                )
                self._pending.clear()
            return commands

        if self._resume_needed:
            self._resume_needed = False
            if self._occupied is False:
                commands.extend(self._start_trigger("vacant", moment, resync=True))

        commands.extend(self._check_dwell(moment))

        due = sorted((step for step in self._pending if step.due <= moment), key=lambda s: (s.due, s.seq))
        if due:
            self._pending = [step for step in self._pending if step.due > moment]
            for step in due:
                command = self._make_command(step.trigger, step.label, step.action)
                self._last_command = command.as_dict()
                commands.append(command)
        return commands

    # -- internals -------------------------------------------------------- #
    def _triggers_of(self, trigger_type: str) -> list[TriggerSpec]:
        return [t for t in self.scenario.all_triggers() if t.type == trigger_type]

    def _effective_cooldown(self, trigger: TriggerSpec) -> float:
        if trigger.cooldown_s is not None:
            return trigger.cooldown_s
        return self.scenario.cooldown_s

    def _cooldown_ok(self, key: str, cooldown_s: float, now: float) -> bool:
        if cooldown_s <= 0:
            return True
        last = self._last_fire.get(key)
        return last is None or (now - last) >= cooldown_s

    def _start_trigger(
        self,
        trigger_type: str,
        now: float,
        *,
        trigger: TriggerSpec | None = None,
        resync: bool = False,
    ) -> list[CommandRequest]:
        commands: list[CommandRequest] = []
        if self.paused_remaining(now) > 0:
            self.log.debug(
                "Kịch bản '%s': đang tạm dừng do chỉnh tay, bỏ trigger %s",
                self.scenario.name,
                trigger_type,
            )
            return commands
        candidates = [trigger] if trigger is not None else self._triggers_of(trigger_type)
        for spec in candidates:
            if spec is None:
                continue
            key = f"{spec.type}:{spec.count}:{spec.seconds}:{','.join(spec.classes)}"
            cooldown = self._effective_cooldown(spec)
            if not resync and not self._cooldown_ok(key, cooldown, now):
                self.log.debug(
                    "Kịch bản '%s': bỏ qua trigger %s (cooldown %.0fs)",
                    self.scenario.name,
                    spec.type,
                    cooldown,
                )
                continue
            if not self.schedule_active():
                self.log.debug(
                    "Kịch bản '%s': ngoài khung giờ hoạt động, bỏ qua trigger %s",
                    self.scenario.name,
                    spec.type,
                )
                continue
            self._last_fire[key] = now
            if spec.type == "vacant":
                # a fresh vacancy timeline replaces any older one
                self.clear_timers()
            for step in spec.steps:
                if not step.enabled:
                    continue
                due = now + max(0.0, step.after_s)
                if step.after_s <= 1e-6 or due <= now:
                    command = self._make_command(spec.type, step.label or step.description, step.publish)
                    self._last_command = command.as_dict()
                    commands.append(command)
                else:
                    self._seq += 1
                    self._pending.append(
                        PendingStep(
                            due=due,
                            after_s=step.after_s,
                            trigger=spec.type,
                            label=step.label or step.description,
                            action=step.publish,  # type: ignore[arg-type]
                            seq=self._seq,
                        )
                    )
                    self.log.debug(
                        "Kịch bản '%s': hẹn '%s' sau %s",
                        self.scenario.name,
                        step.label or step.description,
                        _fmt_seconds(step.after_s),
                    )
        return commands

    def _check_level_triggers(self, now: float) -> list[CommandRequest]:
        """count_ge / count_le / class_present triggers."""
        commands: list[CommandRequest] = []
        for index, spec in enumerate(self.scenario.all_triggers()):
            if spec.type in ("count_ge", "count_le"):
                threshold = spec.count or 0
                matches = self._count >= threshold if spec.type == "count_ge" else self._count <= threshold
                was = self._count_flags.get(index, False)
                self._count_flags[index] = matches
                if matches and not was:
                    commands.extend(self._start_trigger(spec.type, now, trigger=spec))
            elif spec.type == "class_present":
                for class_name in spec.classes:
                    present = self._class_counts.get(class_name, 0) > 0
                    key = f"class_present:{class_name}"
                    if present and key not in self._class_active:
                        self._class_active.add(key)
                        commands.extend(self._start_trigger(spec.type, now, trigger=spec))
                    elif not present and key in self._class_active:
                        self._class_active.discard(key)
        return commands

    def _check_dwell(self, now: float) -> list[CommandRequest]:
        commands: list[CommandRequest] = []
        if not self._occupied or self._occupied_since is None:
            return commands
        for index, spec in enumerate(self.scenario.all_triggers()):
            if spec.type != "dwell" or index in self._dwell_fired:
                continue
            threshold = spec.seconds or 0
            if now - self._occupied_since >= threshold:
                self._dwell_fired.add(index)
                self.log.info(
                    "Kịch bản '%s': có người liên tục %s -> trigger dwell",
                    self.scenario.name,
                    _fmt_seconds(threshold),
                )
                commands.extend(self._start_trigger(spec.type, now, trigger=spec))
        return commands

    def _make_command(self, trigger: str, label: str, action: PublishAction) -> CommandRequest:
        return CommandRequest(
            scenario_id=self.scenario.id,
            scenario_name=self.scenario.name,
            zone_ref=self.scenario.zone,
            camera_id=self._camera_id,
            zone_id=self._zone_id,
            zone_name=self._zone_name or self._zone_id,
            trigger=trigger,
            step_label=label,
            action=action,
            context=self._context(),
            created_at=self.clock.now(),
            wall_time=self.wall_clock(),
        )

    # -- introspection ---------------------------------------------------- #
    def status(self, now: float | None = None) -> dict[str, Any]:
        moment = self.clock.now() if now is None else now
        pending = sorted(self._pending, key=lambda s: s.due)
        return {
            "id": self.scenario.id,
            "name": self.scenario.name,
            "enabled": self.scenario.enabled,
            "zone": self.scenario.zone,
            "occupied": self._occupied,
            "count": self._count,
            "class_counts": dict(self._class_counts),
            "timeline": "occupied" if self._occupied else ("vacant" if self._occupied is False else "unknown"),
            "schedule_active": self.schedule_active(),
            "paused_remaining_s": round(self.paused_remaining(moment), 1),
            "pending": [
                {
                    "trigger": step.trigger,
                    "label": step.label,
                    "in_s": round(max(0.0, step.due - moment), 1),
                    "topic": step.action.topic,
                }
                for step in pending[:10]
            ],
            "last_command": self._last_command,
        }


# --------------------------------------------------------------------------- #
# manager for all scenarios of an app
# --------------------------------------------------------------------------- #
class ScenarioEngine:
    """Routes zone presence snapshots to every scenario watching that zone."""

    def __init__(self, scenarios: Sequence[ScenarioConfig], clock: Clock, *, wall_clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.wall_clock = wall_clock
        self._runners: dict[str, ScenarioRunner] = {}
        self._by_zone: dict[str, list[str]] = {}
        for scenario in scenarios:
            self.add_scenario(scenario)

    # -- lifecycle -------------------------------------------------------- #
    def add_scenario(self, scenario: ScenarioConfig) -> None:
        runner = self._runners.get(scenario.id)
        if runner is None:
            runner = ScenarioRunner(scenario, self.clock, wall_clock=self.wall_clock)
            self._runners[scenario.id] = runner
        else:
            runner.reconfigure(scenario)
        self._index()

    def remove_scenario(self, scenario_id: str) -> None:
        self._runners.pop(scenario_id, None)
        self._index()

    def reconfigure(self, scenarios: Sequence[ScenarioConfig]) -> None:
        wanted = {s.id: s for s in scenarios}
        for scenario_id in list(self._runners):
            if scenario_id not in wanted:
                self._runners.pop(scenario_id, None)
        for scenario in scenarios:
            self.add_scenario(scenario)
        self._index()

    def _index(self) -> None:
        index: dict[str, list[str]] = {}
        for scenario_id, runner in self._runners.items():
            index.setdefault(runner.scenario.zone, []).append(scenario_id)
        self._by_zone = index

    def runners(self) -> Iterable[ScenarioRunner]:
        return self._runners.values()

    def runner(self, scenario_id: str) -> ScenarioRunner | None:
        return self._runners.get(scenario_id)

    def zones(self) -> list[str]:
        return list(self._by_zone)

    # -- events ----------------------------------------------------------- #
    def on_zone_state(self, state: ZoneState) -> list[CommandRequest]:
        commands: list[CommandRequest] = []
        for scenario_id in self._by_zone.get(state.ref, []):
            runner = self._runners.get(scenario_id)
            if runner is None:
                continue
            if state.name:
                runner.set_zone_name(state.name)
            commands.extend(runner.apply_presence(state))
        return commands

    def tick(self) -> list[CommandRequest]:
        commands: list[CommandRequest] = []
        for runner in self._runners.values():
            commands.extend(runner.tick())
        return commands

    def notify_manual_override(self, zone_ref: str) -> None:
        for scenario_id in self._by_zone.get(zone_ref, []):
            runner = self._runners.get(scenario_id)
            if runner is not None:
                runner.notify_manual_override()

    def status(self) -> list[dict[str, Any]]:
        return [runner.status() for runner in self._runners.values()]

    def reset(self) -> None:
        for runner in self._runners.values():
            runner.clear_timers()
            runner._occupied = None  # noqa: SLF001 - full reset for tests/simulation
            runner._occupied_since = None  # noqa: SLF001


__all__ = [
    "Clock",
    "CommandRequest",
    "FakeClock",
    "PendingStep",
    "ScaledClock",
    "ScenarioEngine",
    "ScenarioRunner",
    "SystemClock",
]
