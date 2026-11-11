"""Tests for polygon geometry, anchors and presence stabilisation."""

from __future__ import annotations

import pytest

from app.config import ZoneConfig
from app.detection import Box, Detection
from app.zones import (
    PresenceTracker,
    ZoneCounts,
    ZoneEngine,
    box_polygon_overlap_area,
    clip_polygon_to_box,
    normalised_to_pixels,
    point_in_polygon,
    polygon_area,
    polygon_bbox,
)

SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def det(x1: float, y1: float, x2: float, y2: float, name: str = "person", conf: float = 0.9) -> Detection:
    return Detection(class_id=0, class_name=name, confidence=conf, box=Box(x1, y1, x2, y2))


def counts(*detections: Detection) -> ZoneCounts:
    result = ZoneCounts()
    for detection in detections:
        result.add(detection)
    return result


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("px", "py", "expected"),
    [
        (0.5, 0.5, True),
        (0.01, 0.01, True),
        (1.01, 0.5, False),
        (-0.1, 0.5, False),
        (0.5, 1.2, False),
    ],
)
def test_point_in_polygon_square(px: float, py: float, expected: bool) -> None:
    assert point_in_polygon(px, py, SQUARE) is expected


def test_point_on_edge_is_inside() -> None:
    assert point_in_polygon(0.5, 0.0, SQUARE) is True
    assert point_in_polygon(1.0, 0.5, SQUARE) is True


def test_point_in_concave_polygon() -> None:
    # L shape: the notch (0.6..1.0 x 0.0..0.4) is outside
    l_shape = [(0.0, 0.0), (0.6, 0.0), (0.6, 0.4), (1.0, 0.4), (1.0, 1.0), (0.0, 1.0)]
    assert point_in_polygon(0.3, 0.2, l_shape) is True
    assert point_in_polygon(0.8, 0.2, l_shape) is False
    assert point_in_polygon(0.8, 0.6, l_shape) is True


def test_polygon_area_and_bbox() -> None:
    square = [(2.0, 2.0), (6.0, 2.0), (6.0, 6.0), (2.0, 6.0)]
    assert polygon_area(square) == pytest.approx(16.0)
    assert polygon_bbox(square) == (2.0, 2.0, 6.0, 6.0)


def test_clip_polygon_to_box_exact() -> None:
    polygon = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    box = Box(5.0, 5.0, 10.0, 10.0)
    clipped = clip_polygon_to_box(polygon, box)
    assert polygon_area(clipped) == pytest.approx(25.0)


def test_overlap_area_partial_and_none() -> None:
    polygon = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert box_polygon_overlap_area(Box(0.0, 0.0, 4.0, 4.0), polygon) == pytest.approx(16.0)
    assert box_polygon_overlap_area(Box(20.0, 20.0, 30.0, 30.0), polygon) == 0.0
    assert box_polygon_overlap_area(Box(8.0, 0.0, 20.0, 10.0), polygon) == pytest.approx(20.0)


def test_normalised_to_pixels() -> None:
    assert normalised_to_pixels([(0.5, 0.25)], 1920, 1080) == [(960.0, 270.0)]


# --------------------------------------------------------------------------- #
# anchors through ZoneEngine
# --------------------------------------------------------------------------- #
def make_zone(**kwargs) -> ZoneConfig:
    base = {
        "id": "z1",
        "name": "Zone 1",
        "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.8], [0.1, 0.8]],
        "classes": ["person"],
        "enter_confirm": {"frames": 1, "of": 1},
        "exit_grace_s": 0,
    }
    base.update(kwargs)
    return ZoneConfig.model_validate(base)


def engine_for(zone: ZoneConfig, min_confidence: float = 0.45) -> ZoneEngine:
    return ZoneEngine("cam", [zone], min_confidence)


def test_bottom_center_anchor() -> None:
    engine = engine_for(make_zone())
    inside = det(200, 300, 260, 700)  # feet ~ (230, 692) -> inside 100..400 x 200..800
    outside = det(900, 300, 960, 700)
    states = engine.evaluate(0.0, [inside, outside], 1000, 1000)
    assert states[0].count == 1
    assert states[0].class_counts == {"person": 1}


def test_bottom_center_excludes_body_that_hangs_outside() -> None:
    zone = make_zone(polygon=[[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]])
    engine = engine_for(zone)
    hanging = det(210, 100, 270, 700)  # centre y=400 inside, feet y=688 outside
    states = engine.evaluate(0.0, [hanging], 1000, 1000)
    assert states[0].count == 0


def test_center_anchor_counts_hanging_body() -> None:
    zone = make_zone(
        polygon=[[0.2, 0.2], [0.5, 0.2], [0.5, 0.5], [0.2, 0.5]], anchor="center"
    )
    engine = engine_for(zone)
    hanging = det(210, 100, 270, 700)
    states = engine.evaluate(0.0, [hanging], 1000, 1000)
    assert states[0].count == 1


def test_any_overlap_anchor() -> None:
    zone = make_zone(polygon=[[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6]], anchor="any_overlap")
    engine = engine_for(zone)
    touching = det(100, 100, 450, 450)  # overlaps the 400..600 corner
    states = engine.evaluate(0.0, [touching], 1000, 1000)
    assert states[0].count == 1


def test_overlap_ratio_anchor() -> None:
    zone = make_zone(polygon=[[0.5, 0.0], [1.0, 0.0], [1.0, 0.5], [0.5, 0.5]], anchor="overlap_ratio", overlap_ratio=0.6)
    engine = engine_for(zone)
    half_in = det(400, 100, 600, 300)  # 50% inside -> rejected at 0.6
    states = engine.evaluate(0.0, [half_in], 1000, 1000)
    assert states[0].count == 0
    mostly_in = det(600, 100, 700, 200)  # fully inside the 500..1000 x 0..500 region
    states = engine.evaluate(0.0, [mostly_in], 1000, 1000)
    assert states[0].count == 1


def test_zone_confidence_override() -> None:
    engine = engine_for(make_zone(min_confidence=0.8), min_confidence=0.2)
    states = engine.evaluate(0.0, [det(200, 300, 260, 700, conf=0.5)], 1000, 1000)
    assert states[0].count == 0


def test_global_confidence_used_when_zone_has_none() -> None:
    engine = engine_for(make_zone(min_confidence=None), min_confidence=0.8)
    states = engine.evaluate(0.0, [det(200, 300, 260, 700, conf=0.5)], 1000, 1000)
    assert states[0].count == 0


def test_class_filter_allows_only_listed() -> None:
    engine = engine_for(make_zone(classes=["person"]))
    states = engine.evaluate(0.0, [det(200, 300, 260, 700, name="cat")], 1000, 1000)
    assert states[0].count == 0
    states = engine.evaluate(1.0, [det(200, 300, 260, 700, name="cat"), det(210, 300, 270, 700)], 1000, 1000)
    assert states[0].count == 1


def test_disabled_zone_never_counts() -> None:
    engine = engine_for(make_zone(enabled=False))
    states = engine.evaluate(0.0, [det(200, 300, 260, 700)], 1000, 1000)
    assert states[0].count == 0
    assert states[0].occupied is None


def test_disabled_zone_does_not_freeze_stale_state() -> None:
    """BUG 2: zone bị tắt phải xoá trạng thái cũ, không được giữ số liệu "tươi"."""
    zone = make_zone(enter_confirm={"frames": 3, "of": 5}, exit_grace_s=10.0)
    engine = engine_for(zone)
    now = 0.0
    for _ in range(5):
        states = engine.evaluate(now, [det(200, 300, 260, 700)], 1000, 1000)
        now += 0.2
    assert states[0].occupied is True
    assert states[0].count == 1

    engine._zones["z1"].enabled = False  # tắt zone (mutate ZoneConfig đang giữ trong engine)
    states = engine.evaluate(now, [det(200, 300, 260, 700)], 1000, 1000)
    assert states[0].occupied is None
    assert states[0].count == 0
    assert states[0].class_counts == {}
    assert states[0].pending_exit is None
    assert states[0].updated_at == now  # vẫn báo "vừa cập nhật" nhưng trạng thái là unknown

    # bật lại: cửa sổ bỏ phiếu đã bị xoá nên một khung vắng KHÔNG được kết luận ngay
    engine._zones["z1"].enabled = True
    for _ in range(5):  # chưa đủ 'of' khung để kết luận, càng không đủ exit_grace_s
        now += 0.2
        states = engine.evaluate(now, [], 1000, 1000)
        assert states[0].occupied is not False, "cửa sổ bỏ phiếu cũ bị dùng lại"
    # mốc chờ rời phòng chỉ tính từ các khung vắng THẬT sau khi bật lại
    assert engine._trackers["z1"].pending_exit_s is not None
    states = engine.evaluate(now + 5.0, [], 1000, 1000)
    assert states[0].occupied is None  # chưa đủ 10 s vắng liên tục
    states = engine.evaluate(now + 11.0, [], 1000, 1000)
    assert states[0].occupied is False  # đủ 10 s mới rời phòng


# --------------------------------------------------------------------------- #
# presence voting / grace
# --------------------------------------------------------------------------- #
def test_voting_requires_k_of_n() -> None:
    tracker = PresenceTracker(frames=3, of=5, exit_grace_s=3)
    occupied, changed = tracker.update(0.0, counts(det(0, 0, 10, 10)))
    assert (occupied, changed) == (None, False)
    occupied, changed = tracker.update(0.1, counts(det(0, 0, 10, 10)))
    assert (occupied, changed) == (None, False)
    occupied, changed = tracker.update(0.2, counts(det(0, 0, 10, 10)))
    assert (occupied, changed) == (True, True)


def test_occupancy_is_not_flipped_by_two_missed_frames() -> None:
    tracker = PresenceTracker(frames=3, of=5, exit_grace_s=2)
    for index in range(5):
        tracker.update(index * 0.25, counts(det(0, 0, 10, 10)))
    assert tracker.occupied is True
    # two empty frames: still 3 votes of 5 -> stays occupied, no grace timer yet
    for index in range(2):
        occupied, changed = tracker.update(2.0 + index * 0.25, counts())
        assert (occupied, changed) == (True, False)
    assert tracker.pending_exit_s is None


def test_exit_grace_delays_vacancy() -> None:
    tracker = PresenceTracker(frames=3, of=5, exit_grace_s=3)
    for index in range(5):
        tracker.update(index * 0.25, counts(det(0, 0, 10, 10)))
    assert tracker.occupied is True

    now = 10.0
    for _ in range(3):  # push enough empties to drop below 3 votes
        occupied, _ = tracker.update(now, counts())
        now += 0.25
    assert tracker.occupied is True
    assert tracker.pending_exit_s is not None

    occupied, changed = tracker.update(now + 1.0, counts())
    assert (occupied, changed) == (True, False)  # grace not over yet
    occupied, changed = tracker.update(now + 3.0, counts())
    assert (occupied, changed) == (False, True)


def test_flickering_detection_never_blinks() -> None:
    """Alternating present/absent frames must not toggle occupancy."""
    tracker = PresenceTracker(frames=3, of=5, exit_grace_s=3)
    now = 0.0
    for index in range(10):
        tracker.update(now, counts(det(0, 0, 10, 10)))
        now += 0.25
    assert tracker.occupied is True
    for index in range(20):
        tracker.update(now, counts(det(0, 0, 10, 10)) if index % 2 == 0 else counts())
        now += 0.25
    assert tracker.occupied is True  # votes stay >= 3 of 5


def test_recovery_clears_the_exit_grace_timestamp() -> None:
    """BUG 1: mốc chờ rời phòng phải được xoá khi phiếu bầu "khoẻ" trở lại.

    Nếu mốc cũ còn sót, cú tụt phiếu kế tiếp sẽ rời phòng NGAY (bỏ qua
    exit_grace_s) → đèn tắt sớm rồi bật lại = nhấp nháy.
    """
    tracker = PresenceTracker(frames=3, of=5, exit_grace_s=10.0)
    now = 0.0
    for _ in range(5):
        tracker.update(now, counts(det(0, 0, 10, 10)))
        now += 0.2
    assert tracker.occupied is True

    # 3 khung vắng mở mốc chờ rời phòng, nhưng chưa đủ 10 s
    for _ in range(3):
        occupied, changed = tracker.update(now, counts())
        now += 0.2
        assert (occupied, changed) == (True, False)
    assert tracker.pending_exit_s is not None

    # người trong phòng hiện lại → cửa sổ bỏ phiếu khoẻ lại
    for _ in range(5):
        occupied, changed = tracker.update(now, counts(det(0, 0, 10, 10)))
        now += 0.2
        assert (occupied, changed) == (True, False)
    assert tracker.pending_exit_s is None

    # bỏ qua một quãng dài rồi mới vắng lại: phải đếm lại đủ 10 s liên tục
    dip_start = now + 11.0
    dip_now = dip_start
    for _ in range(3):  # đủ để tụt dưới 3/5 phiếu
        occupied, changed = tracker.update(dip_now, counts())
        dip_now += 0.2
        assert (occupied, changed) == (True, False), "mốc chờ cũ vẫn còn → rời phòng sớm"
    occupied, changed = tracker.update(dip_start + 9.5, counts())
    assert (occupied, changed) == (True, False)  # 10 s liên tục chưa trôi hết
    occupied, changed = tracker.update(dip_start + 10.5, counts())
    assert (occupied, changed) == (False, True)  # đủ 10 s vắng liên tục mới rời phòng


def test_sample_false_is_not_treated_as_nobody_here() -> None:
    """BUG 3: khung suy luận lỗi (sample=False) không được coi là "không có ai"."""
    tracker = PresenceTracker(frames=2, of=3, exit_grace_s=5.0)
    now = 0.0
    for _ in range(3):
        tracker.update(now, counts(det(0, 0, 10, 10)))
        now += 0.2
    assert tracker.occupied is True

    for _ in range(10):  # suy luận lỗi liên tục: không được đụng vào phiếu bầu
        now += 0.2
        occupied, changed = tracker.update(now, counts(), sample=False)
        assert (occupied, changed) == (True, False)
    assert tracker.pending_exit_s is None
    assert tracker.count == 0  # số đếm thô vẫn được cập nhật để UI thấy

    # cửa sổ bỏ phiếu vẫn là 3 phiếu "có người": cần 2 khung vắng thật mới mở mốc chờ
    occupied, changed = tracker.update(now + 1.0, counts())
    assert (occupied, changed) == (True, False)
    assert tracker.pending_exit_s is None
    occupied, changed = tracker.update(now + 1.2, counts())
    assert (occupied, changed) == (True, False)
    assert tracker.pending_exit_s is not None


def test_engine_transitions_sample_false_keeps_zone_occupied() -> None:
    """BUG 3 qua ZoneEngine.transitions(..., sample=False) — API cho runtime."""
    zone = make_zone(enter_confirm={"frames": 2, "of": 3}, exit_grace_s=0.0)
    engine = engine_for(zone)
    now = 0.0
    for _ in range(3):
        states = engine.evaluate(now, [det(200, 300, 260, 700)], 1000, 1000)
        now += 0.2
    assert states[0].occupied is True

    # exit_grace_s = 0: nếu sample=False bị tính là "không có ai" thì zone tắt ngay
    for _ in range(5):
        now += 0.2
        states, changes = engine.transitions(now, [], 1000, 1000, sample=False)
        assert states[0].occupied is True
        assert changes == []
    assert states[0].count == 0  # chỉ số đếm thô đổi, trạng thái thì không
    assert states[0].pending_exit is None

    # khung vắng THẬT (sample=True) mới được rời phòng
    now += 1.0
    states, changes = engine.transitions(now, [], 1000, 1000)
    assert states[0].occupied is True
    now += 1.0
    states, changes = engine.transitions(now, [], 1000, 1000)
    assert states[0].occupied is False
    assert changes and changes[0][1] is False


def test_initial_state_is_unknown_then_vacant() -> None:
    tracker = PresenceTracker(frames=3, of=5, exit_grace_s=1)
    assert tracker.occupied is None
    now = 0.0
    for _ in range(5):  # fill the voting window with "nobody here"
        occupied, changed = tracker.update(now, counts())
        now += 0.25
        assert (occupied, changed) == (None, False)
    # window full, but the exit grace has not elapsed yet
    occupied, changed = tracker.update(now, counts())
    assert (occupied, changed) == (None, False)
    occupied, changed = tracker.update(now + 1.5, counts())
    assert (occupied, changed) == (False, True)


def test_forced_state_for_simulation() -> None:
    tracker = PresenceTracker(frames=3, of=5, exit_grace_s=3)
    occupied, changed = tracker.force(5.0, True, 2, {"person": 2})
    assert (occupied, changed) == (True, True)
    assert tracker.count == 2
    occupied, changed = tracker.force(6.0, True, 2, {"person": 2})
    assert (occupied, changed) == (True, False)


def test_engine_force_zone_and_transitions() -> None:
    engine = engine_for(make_zone())
    states, changes = engine.transitions(0.0, [], 1000, 1000)
    # frames=1/of=1 and exit_grace_s=0 in make_zone(): first empty frame decides "vacant"
    assert states[0].occupied is False
    assert len(changes) == 1 and changes[0][1] is False

    engine.force_zone("z1", True, count=2)
    states, changes = engine.transitions(1.0, [], 1000, 1000)
    assert states[0].occupied is True and states[0].count == 2 and states[0].forced is True
    assert len(changes) == 1 and changes[0][1] is True

    engine.clear_force("z1")
    engine.force_zone("z1", False)
    states, changes = engine.transitions(2.0, [], 1000, 1000)
    assert states[0].occupied is False
    assert changes and changes[0][1] is False


def test_engine_multiple_zones_and_state_dict() -> None:
    left = make_zone(id="left", name="Trái", polygon=[[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]])
    right = make_zone(id="right", name="Phải", polygon=[[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]])
    engine = ZoneEngine("cam", [left, right], 0.45)
    states = engine.evaluate(0.0, [det(100, 100, 200, 900)], 1000, 1000)
    by_id = {state.zone_id: state for state in states}
    assert by_id["left"].count == 1
    assert by_id["right"].count == 0
    payload = by_id["left"].as_dict()
    assert payload["zone"] == "cam/left"
    assert payload["occupied"] is True
    assert payload["class_counts"] == {"person": 1}
