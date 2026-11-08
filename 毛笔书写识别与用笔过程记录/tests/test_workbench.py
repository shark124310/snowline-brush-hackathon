import csv
import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from brush_backend.algorithms import dtw, inspect, metrics
from brush_backend.app import create_app
from brush_backend.schemas import RecordInfo, Sample, Scope
from brush_backend.service import AppError


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, api_token="")) as value:
        yield value


def sample_rows(count=201, interval=10000):
    return [dict(sample_us=1000000 + i * interval, seq=i + 1, boot_id="boot1",
                 ax_g=float(.1 * np.sin(i / 20)), ay_g=.01, az_g=1.,
                 gx_dps=float(20 * np.sin(i / 10)), gy_dps=2., gz_dps=1.) for i in range(count)]


def csv_bytes(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def upload(client, rows, **data):
    return client.post("/api/v1/brush/records/import", files={"file": ("test.csv", csv_bytes(rows), "text/csv")}, data=data)


def test_sparse_csv_is_saved_and_reported_but_not_recognized(client):
    response = upload(client, sample_rows(40, 340000))
    assert response.status_code == 200
    record = response.json()["data"]
    assert record["quality"]["saved_hz"] == pytest.approx(1000 / 340)
    assert not record["quality"]["valid"]
    result = client.post("/api/v1/brush/records/" + record["id"] + "/recognize").json()["data"]
    assert result["status"] == "quality_rejected" and result["predicted_label"] is None
    assert len(client.get("/api/v1/brush/records/" + record["id"] + "/samples").json()["data"]) == 40


def test_rejects_timestamp_sequence_boot_gaps_and_clipping():
    rows = sample_rows()
    assert inspect(rows)["valid"]
    rows[100]["sample_us"] = rows[99]["sample_us"]
    assert not inspect(rows)["valid"]
    rows = sample_rows()
    rows[100]["seq"] += 1
    assert not inspect(rows)["valid"]
    rows = sample_rows()
    rows[-1]["boot_id"] = "boot2"
    assert not inspect(rows)["valid"]
    rows = sample_rows()
    rows[-1]["clipped"] = 1
    assert not inspect(rows)["valid"]


def test_invalid_headers_nan_duplicate_and_html_title(client):
    bad = client.post("/api/v1/brush/records/import", files={"file": ("bad.csv", b"x,y\n1,2\n")})
    assert bad.status_code == 400 and bad.json()["msg"]
    rows = sample_rows()
    rows[0]["ax_g"] = float("nan")
    assert upload(client, rows).status_code == 400
    first = upload(client, sample_rows(), title="<script>alert(1)</script>")
    assert first.json()["data"]["title"] == "<script>alert(1)</script>"
    assert upload(client, sample_rows(), mode="test", label="一").status_code == 409
    assert len(client.get("/api/v1/brush/records").json()["data"]) == 1


def test_batch_is_independent_of_ui_and_stop_is_idempotent(client):
    record = client.post("/api/v1/brush/records", json={"title": "HTTP collection"}).json()["data"]
    path = "/api/v1/brush/records/" + record["id"]
    rows = sample_rows()
    for batch in [rows[:80], rows[80:]]:
        assert client.post(path + "/samples", json={"samples": batch}).status_code == 200
    result = client.post(path + "/stop").json()["data"]
    assert result["sample_count"] == 201 and result["quality"]["valid"]
    assert client.post(path + "/stop").json()["data"] == result
    assert client.post(path + "/samples", json={"samples": rows[:1]}).status_code == 409
    assert client.get(path + "/export").status_code == 200


def test_test_records_cannot_become_templates(client):
    record = upload(client, sample_rows(), mode="test", label="十").json()["data"]
    response = client.post("/api/v1/brush/records/" + record["id"] + "/template")
    assert response.status_code == 400


def test_authentication_and_validation_errors(tmp_path):
    with TestClient(create_app(tmp_path, api_token="test-only-token")) as secured:
        assert secured.get("/api/v1/health").status_code == 200
        assert secured.get("/api/v1/brush/records").status_code == 401
        response = secured.get("/api/v1/brush/records", headers={"Authorization": "Bearer test-only-token"})
        assert response.status_code == 200
    with TestClient(create_app(tmp_path / "other")) as local:
        bad = local.post("/api/v1/brush/records", json={"mode": "wrong"})
        assert bad.status_code == 422 and set(bad.json()) == {"code", "msg", "data"}
        assert local.get("/api/v1/brush/records/missing").status_code == 404


def test_metrics_handle_angle_wrap_and_device_distance():
    rows = sample_rows(3)
    for row, yaw in zip(rows, [179, -179, -178]):
        row.update(yaw_deg=yaw, distance_mm=12.5)
    result = metrics(rows)
    assert result["angle_ranges"]["yaw_deg"]["range"] == pytest.approx(3)
    assert "硬件" in result["distance"]["source"]
    assert result["peak"]["sample_index"] == 2
    with pytest.raises(ValueError):
        Sample.model_validate({**rows[0], "unknown_number": float("inf")})


def test_dtw_tolerates_speed_changes():
    a = np.column_stack([np.sin(np.linspace(0, np.pi * 2, 80))] * 6)
    b = np.column_stack([np.sin(np.linspace(0, np.pi * 2, 110))] * 6)
    assert dtw(a, b) < .1
    assert dtw(a, b + 3) > 2


def test_demo_is_isolated_label_is_not_used_and_template_change_invalidates(client):
    demo = client.post("/api/v1/brush/demo").json()["data"]
    assert demo["source_kind"] == "synthetic"
    assert demo["recognition"]["predicted_label"] == "十"
    service = client.app.state.service
    samples = service.store.samples(demo["id"])
    changed = [dict(s, ax_g=s["ax_g"] + .00001) for s in samples]
    # 与原标签矛盾仍然按运动信号匹配；待识别记录标签不进入算法。
    query = service.create(RecordInfo(writer_id="demo-writer", mount_id="demo-mount", label="口"), changed, source="synthetic")
    assert service.recognize(query["id"])["predicted_label"] == "十"
    real = service.create(RecordInfo(writer_id="demo-writer", mount_id="demo-mount"), changed)
    assert service.recognize(real["id"])["status"] == "needs_templates"
    bank, _ = service.bank(Scope(writer_id="demo-writer", mount_id="demo-mount", source_kind="synthetic"))
    with pytest.raises(AppError):
        service.recognize(bank[0]["id"])
    service.template(bank[0]["id"], False)
    assert service.calibration(service.scope(demo), service.bank(service.scope(demo))[0]) is None


def test_rules_are_explicit_and_cite_current_record(client):
    record = upload(client, sample_rows()).json()["data"]
    response = client.post("/api/v1/brush/records/" + record["id"] + "/chat", json={"question": "墨色和笔锋好吗？"}).json()["data"]
    assert response["mode"] == "rules"
    assert response["sources"] == [record["id"]]
    assert "不能" in response["answer"]


def test_evaluation_uses_held_out_answers_and_counts_mistakes(client):
    demo = client.post("/api/v1/brush/demo").json()["data"]
    service = client.app.state.service
    rows = service.store.samples(demo["id"])
    for offset, answer in ((.00002, "十"), (.00003, "口")):
        samples = [dict(row, ax_g=row["ax_g"] + offset) for row in rows]
        service.create(RecordInfo(writer_id="demo-writer", mount_id="demo-mount", mode="test", label=answer),
                       samples, source="synthetic")
    result = service.evaluation(service.scope(demo))
    assert result["known_count"] == 2 and result["correct"] == 1
    assert result["accuracy"] == .5
    assert result["confusion_matrix"]["口"]["十"] == 1
    assert result["baseline_accuracy"] is not None
