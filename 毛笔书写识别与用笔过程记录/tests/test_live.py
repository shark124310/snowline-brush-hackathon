import time

import pytest
from fastapi.testclient import TestClient

from brush_backend.app import create_app
from serial_bridge import LineParser


def rows(n=10):
    return [dict(sample_us=1000000 + 10000 * i, seq=i + 1, ax_g=.01 * i,
                 ay_g=0, az_g=1, gx_dps=i, gy_dps=0, gz_dps=0) for i in range(n)]


def test_live_cursor_catches_up_without_duplicate_samples_and_restores(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/v1/brush/live/current").json()["data"] is None
        record = client.post("/api/v1/brush/live/start", json={"title": "现场测试"}).json()["data"]
        record_id = record["id"]
        assert record["source_kind"] == "real" and record["sample_count"] == 0
        assert client.post("/api/v1/brush/live/start", json={}).status_code == 409
        assert client.post("/api/v1/brush/live/simulate").status_code == 409
        path = "/api/v1/brush/records/" + record_id
        client.post(path + "/samples", json={"samples": rows()})
        first = client.get(path + "/feed?after=0&limit=4").json()["data"]
        second = client.get(path + "/feed?after=4&limit=1000").json()["data"]
        assert first["next_cursor"] == 4 and second["next_cursor"] == 10
        assert first["samples"] + second["samples"] == rows()
        assert client.get(path + "/feed?after=10").json()["data"]["samples"] == []
        assert client.get(path + "/feed?after=11").status_code == 400
        assert client.get(path + "/feed?after=-1").status_code == 422
        assert client.get(path + "/feed?limit=2001").status_code == 422
    # 服务重新启动后仍能发现未结束会话，浏览器从游标 0 补齐。
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/v1/brush/live/current").json()["data"]["id"] == record_id
        assert client.post(path + "/stop").json()["data"]["sample_count"] == 10
        final = client.get(path + "/feed?after=4").json()["data"]
        assert final["record"]["status"] == "completed" and len(final["samples"]) == 6
        assert client.post(path + "/samples", json={"samples": rows()}).status_code == 409
        assert client.post("/api/v1/brush/live/start", json={}).status_code == 200


def test_live_feed_and_control_require_configured_token(tmp_path):
    with TestClient(create_app(tmp_path, api_token="test-only")) as client:
        for method, path in (("GET", "/live/current"), ("POST", "/live/start"), ("POST", "/live/simulate"),
                             ("GET", "/records/missing/feed")):
            assert client.request(method, "/api/v1/brush" + path, json={} if method == "POST" else None).status_code == 401


def test_simulation_streams_gradually_and_stops_without_late_append(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        record = client.post("/api/v1/brush/live/simulate").json()["data"]
        assert record["source_kind"] == "synthetic" and record["sample_count"] == 0
        path = "/api/v1/brush/records/" + record["id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            feed = client.get(path + "/feed").json()["data"]
            if feed["total_count"]:
                break
            time.sleep(.025)
        assert 0 < feed["total_count"] < 321
        stopped = client.post(path + "/stop").json()["data"]
        worker = client.app.state.live.worker
        worker.join(timeout=2)
        assert not worker.is_alive()
        final = client.get(path + "/feed").json()["data"]
        assert final["total_count"] == stopped["sample_count"]
        assert final["record"]["status"] == "completed"
        # 模拟数据永远不能变成真实硬件分组。
        assert final["record"]["writer_id"] == "demo-writer"


def test_serial_protocol_parsers_validate_units_columns_and_nonfinite_values():
    parser = LineParser("csv")
    assert parser.parse("# firmware log") is None
    assert parser.parse("sample_us,seq,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps,roll_deg") is None
    parsed = parser.parse("1000000,1,.01,.02,1,2,3,4,15")
    assert parsed["sample_us"] == 1000000 and parsed["roll_deg"] == 15
    with pytest.raises(ValueError):
        parser.parse("1000000,1,.01")
    json_parser = LineParser()
    with pytest.raises(ValueError):
        json_parser.parse('{"sample_us":1,"ax_g":NaN}')
    with pytest.raises(ValueError):
        json_parser.parse("boot log")


def test_simulation_finishes_automatically_with_complete_quality_checked_record(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        record = client.post("/api/v1/brush/live/simulate").json()["data"]
        client.app.state.live.worker.join(timeout=10)
        assert not client.app.state.live.worker.is_alive()
        path = "/api/v1/brush/records/" + record["id"]
        feed = client.get(path + "/feed").json()["data"]
        assert feed["record"]["status"] == "completed" and feed["record"]["quality"]["valid"]
        assert feed["total_count"] == len(feed["samples"]) == 321
        assert feed["record"]["quality"]["saved_hz"] == pytest.approx(50)
        result = client.post(path + "/recognize").json()["data"]
        assert result["candidate_label"] == "十" and result["source_kind"] == "synthetic"
