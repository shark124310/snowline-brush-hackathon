import csv
import hmac
import io
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, UploadFile, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import algorithms as alg
from .schemas import Batch, Question, RecordInfo, Scope
from .service import AppError, BrushService
from .storage import Store
from .live import LiveSessions
from .strokes import (StrokeRequest, StrokeSaveRequest, StrokeScoreRequest,
                      StrokeStation, parse_tip, review_points)

ROOT = Path(__file__).resolve().parent.parent


def create_app(data_dir=None, api_token=None):
    service = BrushService(Store(Path(data_dir or os.environ.get("BRUSH_DATA_DIR", ROOT / "data"))))
    live = LiveSessions(service)
    try:
        tip_mm = parse_tip(os.environ.get("BRUSH_TIP_MM", "0,0,0"))
    except ValueError:
        tip_mm = (0.0, 0.0, 0.0)
    strokes = StrokeStation(os.environ.get("BRUSH_SERIAL_PORT", "COM10"), tip_mm)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        live.close()

    app = FastAPI(title="墨序 · 毛笔书写工作台", version="0.2.0", lifespan=lifespan,
                  description="实时采集窗口、增量数据、CSV、六轴检查与 DTW 模板匹配。")
    app.state.service = service
    app.state.live = live
    token = api_token if api_token is not None else os.environ.get("BRUSH_API_TOKEN", "")

    def envelope(data, msg="ok"):
        return {"code": 0, "msg": msg, "data": data}

    def authorize(authorization: str | None = Header(default=None)):
        if token and not hmac.compare_digest(authorization or "", "Bearer " + token):
            raise AppError("接口令牌无效，请在页面连接设置中填写。", 401)

    @app.exception_handler(AppError)
    async def app_error(_request, error):
        return JSONResponse(status_code=error.status, content={"code": error.status, "msg": error.message, "data": None})

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, error):
        fields = [".".join(map(str, e["loc"])) for e in error.errors()]
        return JSONResponse(status_code=422, content={"code": 422, "msg": "参数不正确：" + "、".join(fields), "data": None})

    @app.get("/api/v1/health")
    def health():
        return envelope({"status": "ready", "version": app.version, "algorithm": alg.VERSION, "labels": alg.LABELS,
                         "auth_required": bool(token), "ai_mode": "rules",
                         "input": ["csv", "http-batch", "live-feed", "usb-stroke"]})

    api = APIRouter(prefix="/api/v1/brush", dependencies=[Depends(authorize)])

    @api.get("/stroke/meta", summary="逐笔复盘：可选字、笔画、范线与串口")
    def stroke_meta():
        return envelope(strokes.meta())

    @api.post("/stroke/capture", summary="逐笔复盘：通过 USB 采集一笔并重建轨迹")
    def stroke_capture(payload: StrokeRequest):
        job_id = strokes.start(payload.glyph, payload.stroke)
        if job_id is None:
            raise AppError("已有采集正在进行，请等它完成。", 409)
        return envelope(strokes.status(job_id))

    @api.get("/stroke/sample/{direction}", summary="逐笔复盘：读取腕墨的实测示例轨迹")
    def stroke_sample(direction: str):
        return envelope(strokes.sample(direction))

    @api.post("/stroke/score", summary="逐笔复盘：按范线或示范计算形状接近度")
    def stroke_score(payload: StrokeScoreRequest):
        return envelope(review_points(payload.as_record(), payload.glyph,
                                      payload.stroke, payload.reference))

    @api.post("/stroke/{job_id}/save", summary="把逐笔采集转存为墨序六轴记录")
    def stroke_save(job_id: str, payload: StrokeSaveRequest):
        return envelope(strokes.save(service, job_id, payload.writer_id, payload.mount_id))

    @api.get("/stroke/{job_id}", summary="逐笔复盘：查询采集任务")
    def stroke_job(job_id: str):
        job = strokes.status(job_id)
        if job is None:
            raise AppError("找不到本次采集。", 404)
        return envelope(job)

    @api.get("/live/current", summary="查询当前实时记录，供串口桥和页面恢复使用")
    def current_live():
        return envelope(live.current())

    @api.post("/live/start", summary="开始一次真实硬件记录，不会自动连接设备")
    def start_live(info: RecordInfo):
        return envelope(live.start(info))

    @api.post("/live/simulate", summary="播放隔离的合成实时流，不代表真实设备")
    def simulate_live():
        return envelope(live.start(RecordInfo(), simulated=True))

    @api.get("/records/{record_id}/feed", summary="按样本游标增量读取，页面建议每 100 ms 拉取")
    def live_feed(record_id: str, after: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=2000)):
        return envelope(live.feed(record_id, after, limit))

    @api.get("/records")
    def records():
        return envelope(service.store.list())

    @api.post("/records")
    def create(info: RecordInfo):
        return envelope(service.create(info))

    @api.post("/records/import")
    async def import_csv(file: UploadFile = File(...), writer_id: str = Form("writer-01"), mount_id: str = Form("mount-01"),
                         mode: str = Form("practice"), label: str = Form(""), title: str = Form("")):
        try:
            info = RecordInfo(writer_id=writer_id, mount_id=mount_id, mode=mode, label=label or None,
                              title=title or (file.filename or "CSV 记录"))
        except ValueError:
            raise AppError("记录用途、标签或元数据格式不正确。", 422)
        content = await file.read(5 * 1024 * 1024 + 1)
        return envelope(service.import_csv(content, info, file.filename))

    @api.get("/records/{record_id}")
    def record(record_id: str):
        return envelope(service.get(record_id))

    @api.get("/records/{record_id}/samples")
    def samples(record_id: str):
        service.get(record_id)
        return envelope(service.store.samples(record_id))

    @api.post("/records/{record_id}/samples")
    def append(record_id: str, batch: Batch):
        return envelope(service.append(record_id, [s.model_dump(exclude_none=True) for s in batch.samples]))

    @api.post("/records/{record_id}/stop")
    def stop(record_id: str):
        return envelope(live.stop(record_id))

    @api.post("/records/{record_id}/template")
    def template(record_id: str):
        return envelope(service.template(record_id))

    @api.delete("/records/{record_id}/template")
    def remove_template(record_id: str):
        return envelope(service.template(record_id, False))

    @api.post("/records/{record_id}/recognize")
    def recognize(record_id: str):
        return envelope(service.recognize(record_id))

    @api.get("/records/{record_id}/report")
    def report(record_id: str):
        return envelope(service.report(record_id))

    @api.post("/records/{record_id}/chat")
    def chat(record_id: str, question: Question):
        return envelope(service.explain(record_id, question.question))

    @api.get("/records/{record_id}/export")
    def export(record_id: str):
        service.get(record_id)
        raw = service.store.root / "raw" / (record_id + ".csv")
        if raw.is_file():
            return FileResponse(raw, media_type="text/csv", filename=record_id + ".csv")
        rows = service.store.samples(record_id)
        output = io.StringIO()
        columns = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            safe = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v) for k, v in row.items()}
            writer.writerow(safe)
        return Response(content="\ufeff" + output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{record_id}.csv"'})

    @api.get("/templates")
    def templates(writer_id: str = "writer-01", mount_id: str = "mount-01", source_kind: str = "real"):
        try:
            scope = Scope(writer_id=writer_id, mount_id=mount_id, source_kind=source_kind)
        except ValueError:
            raise AppError("数据来源只能是 real 或 synthetic。")
        bank, counts = service.bank(scope)
        return envelope({"records": bank, "counts": counts, "calibration": service.calibration(scope, bank), "scope": scope.model_dump()})

    @api.post("/templates/calibrate")
    def calibrate(scope: Scope):
        return envelope(service.calibrate(scope))

    @api.post("/evaluate")
    def evaluate(scope: Scope):
        return envelope(service.evaluation(scope))

    @api.post("/demo")
    def demo():
        with service.store.lock:
            return envelope(service.demo(), "合成示例仅用于检查软件流程，不代表真实识别性能。")

    app.include_router(api)
    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(ROOT / "web" / "index.html",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/strokes", include_in_schema=False)
    def strokes_page():
        return FileResponse(ROOT / "web" / "index.html",
                            headers={"Cache-Control": "no-cache"})

    return app
