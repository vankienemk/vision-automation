"""Tests for the scenario state machine driven by a fake clock."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import (
    PublishAction,
    ScheduleConfig,
    ScenarioConfig,
    StepSpec,
    TriggerSpec,
    default_demo_scenario,
)
from app.scenarios import FakeClock, ScaledClock, ScenarioEngine, ScenarioRunner
from app.zones import ZoneState

CAMERA = "cam_bep"
ZONE = "khu_bep"
REF = f"{CAMERA}/{ZONE}"


def zs(
    occupied: bool | None,
    count: int = 1,
    classes: dict[str, int] | None = None,
    *,
    since: float | None = None,
    now: float = 0.0,
) -> ZoneState:
    if occupied:
        class_counts = classes if classes is not None else {"person": max(1, count)}
    else:
        class_counts = classes if classes is not None else {}
    return ZoneState(
        camera_id=CAMERA,
        zone_id=ZONE,
        name="Khu bếp",
        occupied=occupied,
        count=count if occupied else 0,
        class_counts=class_counts,
        since=since,
        updated_at=now,
    )


def make_runner(scenario: ScenarioConfig | None = None) -> tuple[ScenarioRunner, FakeClock]:
    clock = FakeClock(0.0)
    runner = ScenarioRunner(scenario or default_demo_scenario(REF), clock)
    return runner, clock


def payloads(commands) -> list[dict]:
    return [c.action.payload for c in commands]


def wall_at(hour: int, minute: int, day: int = 15) -> float:
    return datetime(2024, 5, day, hour, minute, 0).timestamp()


# --------------------------------------------------------------------------- #
# the reference "kitchen light" scenario
# --------------------------------------------------------------------------- #
def test_full_timeline_dim_then_off() -> None:
    runner, clock = make_runner()

    assert runner.apply_presence(zs(False)) == []  # first state after boot: adopt silently
    assert runner.status()["pending"] == []

    entered = runner.apply_presence(zs(True, since=0.0))
    assert len(entered) == 1
    assert entered[0].action.payload == {"state": "ON", "brightness": 254, "transition": 1}

    clock.advance(10.0)
    assert runner.apply_presence(zs(False, since=10.0)) == []

    pending = runner.status()["pending"]
    assert [p["in_s"] for p in pending] == [30.0, 120.0]

    clock.advance(29.0)  # t=39
    assert runner.tick() == []

    clock.advance(1.0)  # t=40 -> 30s after vacancy
    dimmed = runner.tick()
    assert len(dimmed) == 1
    assert dimmed[0].action.payload == {"state": "ON", "brightness": 80, "transition": 3}
    assert dimmed[0].trigger == "vacant"

    clock.advance(89.0)  # t=129
    assert runner.tick() == []

    clock.advance(1.0)  # t=130 -> 120s after vacancy
    off = runner.tick()
    assert len(off) == 1
    assert off[0].action.payload == {"state": "OFF", "transition": 2}
    assert runner.status()["pending"] == []


def test_returning_mid_timeline_restores_full_brightness_immediately() -> None:
    runner, clock = make_runner()
    runner.apply_presence(zs(False))
    clock.advance(1.0)
    runner.apply_presence(zs(True, since=1.0))

    clock.advance(9.0)  # t=10 -> leave
    runner.apply_presence(zs(False, since=10.0))
    clock.advance(30.0)  # t=40 -> dimmed to 30 %
    dimmed = runner.tick()
    assert dimmed and dimmed[0].action.payload["brightness"] == 80

    clock.advance(5.0)  # t=45 -> person comes back
    back = runner.apply_presence(zs(True, since=45.0))
    assert len(back) == 1
    assert back[0].action.payload["brightness"] == 254
    assert back[0].trigger == "enter"

    # the pending "OFF" of the old vacancy timeline must be gone
    assert runner.status()["pending"] == []
    clock.advance(1000.0)
    assert runner.tick() == []


def test_no_off_command_right_after_boot() -> None:
    runner, clock = make_runner()
    for _ in range(10):
        assert runner.apply_presence(zs(False, now=clock.now())) == []
        clock.advance(1.0)
    clock.advance(3600.0)
    assert runner.tick() == []
    assert runner.status()["pending"] == []


def test_first_occupied_after_boot_turns_light_on() -> None:
    runner, _ = make_runner()
    commands = runner.apply_presence(zs(True))
    assert len(commands) == 1
    assert commands[0].action.payload["brightness"] == 254


def test_cooldown_prevents_double_fire() -> None:
    scenario = default_demo_scenario(REF)
    scenario.cooldown_s = 5
    runner, clock = make_runner(scenario)
    runner.apply_presence(zs(False))
    first = runner.apply_presence(zs(True, since=0.0))
    assert len(first) == 1

    clock.advance(1.0)
    runner.apply_presence(zs(False, since=1.0))
    second = runner.apply_presence(zs(True, since=1.0))  # within cooldown
    assert second == []

    clock.advance(10.0)
    runner.apply_presence(zs(False, since=11.0))
    third = runner.apply_presence(zs(True, since=11.0))
    assert len(third) == 1


def test_disabled_scenario_does_nothing() -> None:
    scenario = default_demo_scenario(REF)
    scenario.enabled = False
    runner, clock = make_runner(scenario)
    assert runner.apply_presence(zs(True)) == []
    clock.advance(500.0)
    assert runner.tick() == []


# --------------------------------------------------------------------------- #
# schedules
# --------------------------------------------------------------------------- #
def test_schedule_blocks_outside_window() -> None:
    scenario = default_demo_scenario(REF)
    scenario.active_schedule = ScheduleConfig.model_validate({"from": "08:00", "to": "17:00"})
    clock = FakeClock(0.0)
    runner = ScenarioRunner(scenario, clock, wall_clock=lambda: wall_at(20, 0))
    assert runner.apply_presence(zs(True)) == []
    assert runner.schedule_active() is False


def test_schedule_across_midnight_allows_late_night() -> None:
    scenario = default_demo_scenario(REF)
    scenario.active_schedule = ScheduleConfig.model_validate({"from": "18:00", "to": "06:00"})
    clock = FakeClock(0.0)
    runner = ScenarioRunner(scenario, clock, wall_clock=lambda: wall_at(23, 30))
    assert runner.schedule_active() is True
    commands = runner.apply_presence(zs(True))
    assert len(commands) == 1


def test_vacancy_timeline_paused_when_schedule_ends() -> None:
    """Steps scheduled before the window closes are skipped once it is closed."""
    scenario = default_demo_scenario(REF)
    scenario.active_schedule = ScheduleConfig.model_validate({"from": "08:00", "to": "17:00"})
    clock = FakeClock(0.0)
    state = {"now": wall_at(16, 59)}
    runner = ScenarioRunner(scenario, clock, wall_clock=lambda: state["now"])
    runner.apply_presence(zs(False))
    runner.apply_presence(zs(True, since=0.0))
    clock.advance(1.0)
    runner.apply_presence(zs(False, since=1.0))
    state["now"] = wall_at(17, 30)  # window closed
    clock.advance(30.0)
    assert runner.tick() == []


# --------------------------------------------------------------------------- #
# extra trigger types
# --------------------------------------------------------------------------- #
def trigger_scenario(*triggers: TriggerSpec, cooldown_s: float = 0.0) -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "id": "custom",
            "name": "Custom",
            "zone": REF,
            "cooldown_s": cooldown_s,
            "triggers": [t.model_dump() for t in triggers],
        }
    )


def test_dwell_trigger_fires_after_staying() -> None:
    scenario = trigger_scenario(
        TriggerSpec(
            type="dwell",
            seconds=60,
            steps=[StepSpec(publish=PublishAction(topic="t/dwell", payload={"dwell": True}))],
        )
    )
    runner, clock = make_runner(scenario)
    runner.apply_presence(zs(True, since=0.0))
    clock.advance(59.0)
    assert runner.tick() == []
    clock.advance(1.0)
    commands = runner.tick()
    assert len(commands) == 1
    assert commands[0].action.topic == "t/dwell"
    # only once per occupancy episode
    clock.advance(120.0)
    assert runner.tick() == []


def test_count_ge_trigger() -> None:
    scenario = trigger_scenario(
        TriggerSpec(
            type="count_ge",
            count=2,
            steps=[StepSpec(publish=PublishAction(topic="t/count", payload={"count": "{{count}}"}))],
        )
    )
    runner, clock = make_runner(scenario)
    assert runner.apply_presence(zs(True, count=1)) == []
    commands = runner.apply_presence(zs(True, count=2))
    assert len(commands) == 1
    assert commands[0].context["count"] == 2
    assert runner.apply_presence(zs(True, count=3)) == []  # still >= 2, no re-fire


def test_count_le_trigger() -> None:
    scenario = trigger_scenario(
        TriggerSpec(
            type="count_le",
            count=0,
            steps=[StepSpec(publish=PublishAction(topic="t/empty", payload={"empty": True}))],
        )
    )
    runner, clock = make_runner(scenario)
    runner.apply_presence(zs(True, count=1))
    commands = runner.apply_presence(zs(False))
    assert len(commands) == 1
    assert commands[0].action.topic == "t/empty"


def test_class_present_trigger_for_pets() -> None:
    scenario = trigger_scenario(
        TriggerSpec(
            type="class_present",
            classes=["cat"],
            steps=[StepSpec(publish=PublishAction(topic="t/cat", payload={"cat": True}))],
        )
    )
    runner, clock = make_runner(scenario)
    commands = runner.apply_presence(zs(True, count=1, classes={"cat": 1}))
    assert len(commands) == 1
    assert runner.apply_presence(zs(True, count=1, classes={"cat": 1})) == []
    runner.apply_presence(zs(True, count=1, classes={}))
    commands = runner.apply_presence(zs(True, count=1, classes={"cat": 1}))
    assert len(commands) == 1


def test_steps_with_delay_are_scheduled() -> None:
    scenario = ScenarioConfig.model_validate(
        {
            "id": "delayed",
            "name": "Delayed",
            "zone": REF,
            "on_enter": [
                {"after_s": 0, "publish": {"topic": "t/a", "payload": {"n": 1}}},
                {"after_s": 15, "publish": {"topic": "t/b", "payload": {"n": 2}}},
            ],
        }
    )
    runner, clock = make_runner(scenario)
    runner.apply_presence(zs(False))
    immediate = runner.apply_presence(zs(True, since=0.0))
    assert [c.action.topic for c in immediate] == ["t/a"]
    assert runner.status()["pending"][0]["in_s"] == 15.0
    clock.advance(15.0)
    later = runner.tick()
    assert [c.action.topic for c in later] == ["t/b"]


# --------------------------------------------------------------------------- #
# manual override
# --------------------------------------------------------------------------- #
def test_manual_override_pauses_and_resumes_timeline() -> None:
    scenario = default_demo_scenario(REF)
    scenario.manual_override.pause_minutes = 10
    runner, clock = make_runner(scenario)

    runner.apply_presence(zs(False))
    assert len(runner.apply_presence(zs(True, since=0.0))) == 1

    clock.advance(5.0)
    runner.notify_manual_override()
    assert runner.paused_remaining() == pytest.approx(600.0)

    # while paused nothing may be published, not even on a new transition
    clock.advance(1.0)
    assert runner.apply_presence(zs(False, since=6.0)) == []
    clock.advance(20.0)
    assert runner.tick() == []

    clock.advance(600.0)  # pause expired, still vacant
    runner.tick()
    pending = runner.status()["pending"]
    assert len(pending) == 2
    assert pending[0]["in_s"] == pytest.approx(30.0)

    clock.advance(30.0)
    dimmed = runner.tick()
    assert dimmed and dimmed[0].action.payload["brightness"] == 80


def test_manual_override_does_not_turn_light_on_after_resume() -> None:
    scenario = default_demo_scenario(REF)
    scenario.manual_override.pause_minutes = 1
    runner, clock = make_runner(scenario)
    runner.apply_presence(zs(False))
    runner.notify_manual_override()
    assert runner.apply_presence(zs(True, since=0.0)) == []
    clock.advance(120.0)
    assert runner.tick() == []


# --------------------------------------------------------------------------- #
# metadata / introspection
# --------------------------------------------------------------------------- #
def test_command_context_has_template_variables() -> None:
    runner, _ = make_runner()
    runner.apply_presence(zs(False))
    commands = runner.apply_presence(zs(True, count=2, since=0.0))
    context = commands[0].context
    assert context["count"] == 2
    assert context["class_counts"] == {"person": 2}
    assert context["zone"] == ZONE
    assert context["camera"] == CAMERA
    assert context["zone_name"] == "Khu bếp"
    assert "T" in context["timestamp"]


def test_status_reports_timeline_and_countdown() -> None:
    runner, clock = make_runner()
    runner.apply_presence(zs(False))
    runner.apply_presence(zs(True, since=0.0))
    assert runner.status()["timeline"] == "occupied"
    clock.advance(1.0)
    runner.apply_presence(zs(False, since=1.0))
    status = runner.status()
    assert status["timeline"] == "vacant"
    assert status["pending"][0]["label"]
    assert status["last_command"]["scenario"] == "den_bep"


def test_scaled_clock_runs_faster() -> None:
    base = FakeClock(0.0)
    scaled = ScaledClock(base, scale=10.0)
    start = scaled.now()
    base.advance(1.0)
    assert scaled.now() - start == pytest.approx(10.0)
    scaled.set_scale(1.0)
    position = scaled.now()
    base.advance(1.0)
    assert scaled.now() - position == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# engine routing
# --------------------------------------------------------------------------- #
def test_engine_routes_only_to_matching_zone() -> None:
    kitchen = default_demo_scenario(REF, scenario_id="den_bep", name="Đèn bếp")
    other = default_demo_scenario("cam_khach/khu_khach", scenario_id="den_khach", name="Đèn khách")
    clock = FakeClock(0.0)
    engine = ScenarioEngine([kitchen, other], clock)

    engine.on_zone_state(zs(False))
    commands = engine.on_zone_state(zs(True))
    assert [c.scenario_id for c in commands] == ["den_bep"]

    other_state = ZoneState(camera_id="cam_khach", zone_id="khu_khach", name="Khách", occupied=True, count=1, class_counts={"person": 1})
    commands = engine.on_zone_state(other_state)
    assert [c.scenario_id for c in commands] == ["den_khach"]

    statuses = {s["id"]: s for s in engine.status()}
    assert statuses["den_bep"]["occupied"] is True
    assert statuses["den_khach"]["occupied"] is True


def test_engine_tick_collects_from_all_scenarios() -> None:
    one = default_demo_scenario(REF, scenario_id="a", name="A")
    two = default_demo_scenario(REF, scenario_id="b", name="B")
    clock = FakeClock(0.0)
    engine = ScenarioEngine([one, two], clock)
    engine.on_zone_state(zs(False))
    engine.on_zone_state(zs(True, since=0.0))
    clock.advance(1.0)
    engine.on_zone_state(zs(False, since=1.0))
    clock.advance(30.0)
    commands = engine.tick()
    assert sorted(c.scenario_id for c in commands) == ["a", "b"]


def test_engine_reconfigure_adds_and_removes() -> None:
    clock = FakeClock(0.0)
    engine = ScenarioEngine([], clock)
    scenario = default_demo_scenario(REF)
    engine.reconfigure([scenario])
    engine.on_zone_state(zs(False))
    assert engine.on_zone_state(zs(True))
    engine.reconfigure([])
    assert engine.status() == []


# --------------------------------------------------------------------------- #
# Hồi quy: vắng lần 2 trong thời gian cooldown không được làm mất timeline
# (lỗi thật: đèn ở mãi trạng thái ON vì trigger "vacant" bị cooldown chặn và
#  không có cơ chế hẹn lại).
# --------------------------------------------------------------------------- #
def test_second_vacancy_inside_cooldown_is_retried_not_dropped() -> None:
    scenario = default_demo_scenario(REF, dim_after_s=30, off_after_s=120)
    scenario.cooldown_s = 60.0
    runner, clock = make_runner(scenario)

    runner.apply_presence(zs(True, since=0.0))
    assert len(runner.apply_presence(zs(False, since=1.0))) == 0  # timeline 1 bắt đầu

    clock.advance(5.0)
    runner.apply_presence(zs(True, since=5.0))       # người quay lại → huỷ timeline 1
    clock.advance(5.0)
    runner.apply_presence(zs(False, since=10.0))     # vắng lần 2, vẫn trong cooldown 60 s

    # Timeline phải được HẸN LẠI (không được im lặng bỏ).
    assert runner.status()["pending"] or runner._vacant_retry_at is not None  # noqa: SLF001

    clock.advance(61.0)                              # qua mốc cooldown
    runner.tick()
    assert runner.status()["pending"], "timeline giảm sáng/tắt đèn không được hẹn lại"

    clock.advance(30.0)
    commands = runner.tick()
    assert [c.step_label for c in commands], "bước giảm sáng không bao giờ chạy"


# --------------------------------------------------------------------------- #
# Hồi quy: khung hình KHÔNG lấy mẫu được (suy luận lỗi) không được bắn trigger
# theo số đếm (nếu không: lỗi model ⇒ "count_le 0" ⇒ tắt đèn oan).
# --------------------------------------------------------------------------- #
def test_unsampled_frame_does_not_fire_count_triggers() -> None:
    trigger = TriggerSpec(
        type="count_le",
        count=0,
        steps=[StepSpec(after_s=0, publish=PublishAction(topic="nha/den/set", payload={"state": "OFF"}))],
    )
    scenario = ScenarioConfig(id="den", name="Đèn", zone=REF, triggers=[trigger])
    runner, clock = make_runner(scenario)

    runner.apply_presence(zs(True, since=0.0))
    clock.advance(1.0)
    commands = runner.apply_presence(zs(False, since=1.0), sampled=False)
    assert commands == [], "khung hình không lấy mẫu được lại bắn lệnh theo số đếm"
    assert runner.status()["occupied"] is True


# --------------------------------------------------------------------------- #
# Trạng thái phải NÓI RÕ vì sao đang không làm gì (yêu cầu "giải thích quyết định")
# --------------------------------------------------------------------------- #
def test_status_explains_why_nothing_happens() -> None:
    scenario = default_demo_scenario(REF)
    scenario.active_schedule = ScheduleConfig.model_validate({"from": "08:00", "to": "09:00"})
    clock = FakeClock(wall_at(12, 0))
    runner = ScenarioRunner(scenario, clock, wall_clock=lambda: wall_at(12, 0))
    runner.apply_presence(zs(True, since=0.0))
    reason = runner.status()["reason"]
    assert isinstance(reason, str) and reason
    assert "khung giờ" in reason.lower()

    plain, _ = make_runner(default_demo_scenario(REF))
    unknown_reason = plain.status()["reason"]
    assert "chưa xác định" in unknown_reason.lower()
