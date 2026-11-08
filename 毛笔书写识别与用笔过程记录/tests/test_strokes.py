import pytest
from fastapi.testclient import TestClient

from brush_backend.app import create_app
from brush_backend.service import AppError, BrushService
from brush_backend.storage import Store
from brush_backend.strokes import (GUIDES, StrokeStation, brush_samples,
                                   review_points)
from imu_trace import Sample


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, api_token="")) as value:
        yield value


def line_points(offset=0.0):
    return [[index / 10, 91 + 14 * index + offset, -91, 5.0] for index in range(11)]


def test_review_scores_guide_and_reference():
    record = {"points": line_points(), "duration_s": 1.0,
              "summary": {"endpoint_xy_mm": 140.0}, "axis_change_deg": 2.0,
              "axis_label": "IMU Z 轴", "segment_count": 1}
    guide = review_points(record, "三", 0)
    assert guide["against"] == "guide"
    assert guide["score"] >= 95
    assert len(guide["draw"]) == 11

    reference = {"points": line_points(4), "duration_s": 1.2,
                 "axis_change_deg": 2.0, "axis_label": "IMU Z 轴"}
    both = review_points(record, "三", 0, reference)
    assert both["against"] == "reference"
    assert both["reference_draw"]
    assert "用时较示范" in both["feedback"]


def test_review_rejects_short_and_bad_points():
    with pytest.raises(AppError):
        review_points({"points": [[0, 0, 0, 0]]}, "三", 0)
    with pytest.raises(AppError):
        review_points({"points": [["x", 0, 0, 0]]}, "三", 0)
    with pytest.raises(AppError):
        review_points({"points": line_points()}, "三", 9)


def test_trace_segment_converts_to_six_axis_with_gravity():
    segment = [Sample(1, 1000000, (1, 0, 0, 0), (0, 0, 0), 0.1, False, False, (655, 0, 0)),
               Sample(2, 1010000, (1, 0, 0, 0), (0, 0, 0.0980665), 0.2, False, False, (0, 655, 0))]
    rows = brush_samples(segment)
    assert rows[0]["az_g"] == pytest.approx(1.0)
    assert rows[0]["gx_dps"] == pytest.approx(10.0)
    assert rows[1]["gz_dps"] == pytest.approx(0.0)
    assert rows[1]["gy_dps"] == pytest.approx(10.0)
    assert rows[1]["roll_deg"] == pytest.approx(0.0)


def test_stroke_station_saves_capture_as_record(tmp_path):
    station = StrokeStation(directory=tmp_path / "strokes")
    service = BrushService(Store(tmp_path / "data"))
    segment = [Sample(index, 1000000 + index * 10000, (1, 0, 0, 0),
                      (0, 0, 0), 0.1, False, False, (655, 0, 0))
               for index in range(1, 41)]
    result = {"glyph": "三", "stroke": 0, "points": line_points(),
              "review": {"score": 88}}
    station.jobs.append({"id": "job1", "glyph": "三", "stroke": 0,
                         "phase": "done", "message": "完成", "segment": segment,
                         "result": result, "recording": "20261108-test"})
    record = station.save(service, "job1", "writer-01", "mount-01")
    assert record["input_kind"] == "usb-trace-stroke"
    assert record["stroke"]["glyph"] == "三"
    assert record["stroke_review"]["score"] == 88
    assert record["sample_count"] == 40
    raw = service.store.root / "raw" / (record["id"] + ".csv")
    assert raw.is_file()


def test_stroke_api_meta_sample_and_score(client):
    meta = client.get("/api/v1/brush/stroke/meta").json()["data"]
    assert [glyph["name"] for glyph in meta["glyphs"]] == ["三", "十", "口"]
    assert meta["glyphs"][0]["strokes"][0]["guide"] == GUIDES["三"][0]

    sample = client.get("/api/v1/brush/stroke/sample/horizontal").json()["data"]
    assert sample["points"] and sample["duration_s"] > 0
    assert client.get("/api/v1/brush/stroke/sample/bend").status_code == 404

    payload = {"glyph": "十", "stroke": 0, "points": sample["points"],
               "duration_s": sample["duration_s"],
               "endpoint_xy_mm": sample["summary"]["endpoint_xy_mm"],
               "axis_change_deg": sample["axis_change_deg"],
               "axis_label": sample["axis_label"], "segment_count": 1}
    scored = client.post("/api/v1/brush/stroke/score", json=payload)
    assert scored.status_code == 200
    assert 0 <= scored.json()["data"]["score"] <= 100


def test_stroke_api_rejects_bad_job(client):
    assert client.get("/api/v1/brush/stroke/nope").status_code == 404
    response = client.post("/api/v1/brush/stroke/nope/save",
                           json={"writer_id": "writer-01", "mount_id": "mount-01"})
    assert response.status_code == 404
