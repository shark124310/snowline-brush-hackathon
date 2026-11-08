import csv
import hashlib
import io
import json
import math
import time
import uuid
from datetime import datetime, timezone

import numpy as np
from pydantic import ValidationError

from . import algorithms as alg
from .schemas import RecordInfo, Sample, Scope


class AppError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class BrushService:
    def __init__(self, store):
        self.store = store

    def get(self, record_id):
        try:
            return self.store.get(record_id)
        except KeyError:
            raise AppError("记录不存在。", 404)

    @staticmethod
    def fingerprint(samples):
        if not samples:
            return None
        start = samples[0]["sample_us"]
        payload = [[s["sample_us"] - start, *[s[c] for c in alg.CHANNELS]] for s in samples]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    def create(self, info: RecordInfo, samples=None, source="real", filename=None):
        samples = samples or []
        if info.mode == "template" and info.label not in alg.LABELS:
            raise AppError("示范模板需要标注一、十、口中的一个字。")
        fingerprint = self.fingerprint(samples)
        if fingerprint:
            for existing in self.store.list():
                if existing.get("fingerprint") == fingerprint and existing["source_kind"] == source:
                    raise AppError(f"相同六轴记录已经导入（{existing['id']}），请勿重复用于不同数据集。", 409)
        record = {**info.model_dump(), "id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                  "source_kind": source, "filename": filename, "status": "completed" if samples else "recording",
                  "sample_count": len(samples), "is_template": False, "fingerprint": fingerprint,
                  "quality": alg.inspect(samples) if samples else None, "recognition": None}
        self.store.create(record, samples)
        return record

    def import_csv(self, content, info, filename):
        if not content or len(content) > 5 * 1024 * 1024:
            raise AppError("CSV 不能为空，且不超过 5 MB。")
        encodings = ("utf-16",) if content.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
        text = None
        for encoding in encodings:
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise AppError("文件编码无法识别，请导出为 UTF-8 CSV。")
        try:
            dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        required = {"sample_us", *alg.CHANNELS}
        if len(set(headers)) != len(headers):
            raise AppError("CSV 存在重复列名。")
        if not required.issubset(headers):
            raise AppError("CSV 缺少列：" + "、".join(sorted(required - set(headers))))
        reader.fieldnames = headers
        samples = []
        try:
            for line, row in enumerate(reader, 2):
                if None in row:
                    raise AppError(f"第 {line} 行的列数与表头不一致。")
                if not any(value and value.strip() for value in row.values()):
                    continue
                if any(row.get(field) in (None, "") for field in required):
                    raise AppError(f"第 {line} 行缺少必要数据。")
                cleaned = {k: (v.strip() if v and v.strip() else None) for k, v in row.items()}
                sample = Sample.model_validate(cleaned).model_dump(exclude_none=True)
                samples.append(sample)
                if len(samples) > 20000:
                    raise AppError("单条记录最多 20,000 个样本，请按一个字分段导出。")
        except ValidationError as error:
            raise AppError(f"第 {line} 行存在非法数值：{error.errors()[0]['loc']}。")
        except csv.Error:
            raise AppError("CSV 格式有误，请检查分隔符与引号。")
        if not samples:
            raise AppError("CSV 没有数据行。")
        with self.store.lock:
            record = self.create(info, samples, filename=filename)
        raw = self.store.root / "raw"
        raw.mkdir(exist_ok=True)
        (raw / (record["id"] + ".csv")).write_bytes(content)
        return record

    def append(self, record_id, samples):
        with self.store.lock:
            record = self.get(record_id)
            if record["status"] != "recording":
                raise AppError("记录已结束，不能追加。", 409)
            if record["sample_count"] + len(samples) > 20000:
                raise AppError("记录样本数超过上限。")
            record["sample_count"] += len(samples)
            record["last_received_unix_s"] = time.time()
            self.store.append(record, samples)
        return record

    def stop(self, record_id):
        with self.store.lock:
            record = self.get(record_id)
            if record["status"] == "completed":
                return record
            samples = self.store.samples(record_id)
            record.update(status="completed", quality=alg.inspect(samples), fingerprint=self.fingerprint(samples))
            self.store.update(record)
        return record

    def template(self, record_id, enabled=True):
        with self.store.lock:
            record = self.get(record_id)
            if enabled and (record["mode"] != "template" or record["label"] not in alg.LABELS):
                raise AppError("只有采集用途为示范模板的记录可加入模板；验证、测试和练习记录不能改作模板。")
            if enabled and not (record.get("quality") or {}).get("valid"):
                raise AppError("记录质量未通过，不能加入模板。")
            record["is_template"] = enabled
            self.store.update(record)
        return record

    @staticmethod
    def scope(record):
        return Scope(**{key: record[key] for key in ("writer_id", "mount_id", "source_kind")})

    def scoped(self, scope):
        return [r for r in self.store.list() if all(r[key] == value for key, value in scope.model_dump().items())]

    def bank(self, scope):
        records = [r for r in self.scoped(scope) if r["is_template"] and r["quality"]["valid"]]
        counts = {label: sum(r["label"] == label for r in records) for label in alg.LABELS}
        return records, counts

    @staticmethod
    def bank_version(records):
        return hashlib.sha256("|".join(sorted(r["id"] for r in records)).encode()).hexdigest()[:16]

    def calibration_key(self, scope):
        return "calibration:" + json.dumps(scope.model_dump(), sort_keys=True)

    def calibration(self, scope, records):
        value = self.store.setting(self.calibration_key(scope))
        if value and value["template_version"] == self.bank_version(records) and value["algorithm_version"] == alg.VERSION:
            return value
        return None

    def distances(self, samples, templates):
        prepared = [alg.prepare(self.store.samples(r["id"])) for r in templates]
        mean, scale = alg.normalizer(prepared)
        query = (alg.prepare(samples) - mean) / scale
        distances = []
        for record, template in zip(templates, prepared):
            distances.append((record["label"], alg.dtw(query, (template - mean) / scale), record["id"]))
        output, references = {}, {}
        for label in alg.LABELS:
            best = sorted((d, i) for c, d, i in distances if c == label)[:3]
            output[label] = float(np.mean([d for d, _ in best]))
            references[label] = [i for _, i in best]
        return output, references

    def recognize(self, record_id, persist=True):
        started = time.perf_counter()
        record = self.get(record_id)
        result = {"status": "quality_rejected", "predicted_label": None, "candidate_label": None,
                  "algorithm_version": alg.VERSION, "source_kind": record["source_kind"]}
        if record["status"] != "completed":
            raise AppError("请先结束记录。", 409)
        if not (record.get("quality") or {}).get("valid"):
            result["reason"] = "记录质量未通过，仍可查看与回放。"
        elif record["is_template"]:
            raise AppError("不能把模板与自身匹配作为识别效果。", 409)
        else:
            scope = self.scope(record)
            templates, counts = self.bank(scope)
            result["template_counts"] = counts
            if any(count < 3 for count in counts.values()):
                result.update(status="needs_templates", reason="同一书写者、安装和数据来源下，每个字至少需要 3 条有效模板。")
            else:
                values, references = self.distances(self.store.samples(record_id), templates)
                ranking = sorted(values, key=values.__getitem__)
                best, second = ranking[:2]
                margin = (values[second] - values[best]) / max(values[second], 1e-9)
                result.update(distances=values, candidate_label=best, margin=margin, reference_ids=references[best],
                              template_version=self.bank_version(templates), preprocessing=alg.PREPROCESS)
                calibration = self.calibration(scope, templates)
                if calibration is None:
                    result.update(status="candidate", reason="仅为最近模板候选；请用独立验证记录校准拒识阈值。")
                elif values[best] > calibration["thresholds"][best] or margin < calibration["min_margin"]:
                    result.update(status="uncertain", reason="与模板差异过大或前两名过近，返回不确定。")
                else:
                    result.update(status="recognized", predicted_label=best, reason="通过当前验证集校准的距离及间隔阈值。")
        result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        if persist:
            record["recognition"] = result
            self.store.update(record)
        return result

    def calibrate(self, scope):
        templates, counts = self.bank(scope)
        if any(n < 3 for n in counts.values()):
            raise AppError("请先为三个字分别加入至少 3 条有效模板。")
        validation = [r for r in self.scoped(scope) if r["mode"] == "validation" and r["quality"]["valid"]]
        per_class = {c: [r for r in validation if r["label"] == c] for c in alg.LABELS}
        if any(len(group) < 2 for group in per_class.values()):
            raise AppError("每个字至少需要 2 条独立有效验证记录；建议按计划采集 5 条。")
        values, margins = {c: [] for c in alg.LABELS}, []
        for record in validation:
            if record["label"] not in alg.LABELS:
                continue
            scores, _ = self.distances(self.store.samples(record["id"]), templates)
            values[record["label"]].append(scores[record["label"]])
            ranked = sorted(scores, key=scores.__getitem__)
            if ranked[0] == record["label"]:
                margins.append((scores[ranked[1]] - scores[ranked[0]]) / max(scores[ranked[1]], 1e-9))
        if not margins:
            raise AppError("验证集没有正确的最近类别，请先检查安装、单位和模板。")
        calibration = {"template_version": self.bank_version(templates), "algorithm_version": alg.VERSION,
                       "thresholds": {c: max(0.05, float(np.percentile(v, 95)) * 1.2) for c, v in values.items()},
                       "min_margin": min(0.4, max(0.03, float(np.percentile(margins, 10)) * 0.5)),
                       "validation_ids": [r["id"] for r in validation if r["label"] in alg.LABELS],
                       "unknown_validated": False,
                       "note": "阈值来自已知字验证集；未知动作拒识能力需另用负样本实测。"}
        return self.store.setting(self.calibration_key(scope), calibration)

    def report(self, record_id):
        record = self.get(record_id)
        recognition = record.get("recognition")
        if recognition and recognition.get("template_version"):
            templates, _ = self.bank(self.scope(record))
            if recognition["template_version"] != self.bank_version(templates):
                recognition = {**recognition, "stale": True, "reason": "模板库已改变，请重新识别。"}
        return {"record": {**record, "recognition": recognition}, "metrics": alg.metrics(self.store.samples(record_id)),
                "definitions": {"angles": "设备欧拉角的解环绕变化，不是笔尖位置。", "distance": "距离由硬件算法估计，未核验物理精度。",
                                "recognition": "少量字的运动模式匹配，不代表字形或书法水平评分。"}}

    def explain(self, record_id, question):
        report = self.report(record_id)
        record, metrics = report["record"], report["metrics"]
        quality = record.get("quality") or {}
        peak = metrics["peak"]
        if any(word in question for word in ("力度", "笔锋", "墨", "好看", "压力", "结构")):
            answer = "当前记录只有 IMU 运动数据，缺少纸面图像和压力观测，不能据此评价笔锋、墨迹、字形结构或书法质量。"
        elif any(word in question for word in ("采样", "质量", "频率")):
            answer = f"本次保存 {record['sample_count']} 个样本，平均 {quality.get('saved_hz', 0):.2f} Hz，最大设备时间间隔 {quality.get('max_gap_ms', 0):.2f} ms。" + ("质量检查通过。" if quality.get("valid") else "质量未通过：" + "；".join(quality.get("issues", [])))
        elif any(word in question for word in ("识别", "什么字", "匹配")):
            result = record.get("recognition") or {}
            answer = f"候选字为 {result.get('candidate_label') or '暂无'}。{result.get('reason', '请先建立模板并执行识别。')} 依据是六轴运动序列，与纸面墨迹无关。"
        elif peak:
            answer = f"这条记录持续 {quality.get('duration_s', 0):.2f} 秒。已保存样本中的最大角速度模长为 {peak['value_dps']:.2f} °/s，位于第 {peak['at_s']:.3f} 秒。可以定位到该采样点回放。"
        else:
            answer = "当前记录还没有可解读的样本。"
        return {"mode": "rules", "answer": answer, "record_id": record_id,
                "source_kind": record["source_kind"], "event": peak,
                "notice": "离线规则解读，尚未调用大模型。", "sources": [record_id]}

    def evaluation(self, scope):
        templates, _ = self.bank(scope)
        if self.calibration(scope, templates) is None:
            raise AppError("请先使用验证集完成拒识校准。")
        records = [r for r in self.scoped(scope) if r["mode"] == "test"]
        if not records:
            raise AppError("当前没有独立测试记录。")
        matrix = {c: {p: 0 for p in (*alg.LABELS, "不确定")} for c in alg.LABELS}
        correct = known = unknown = rejected_unknown = 0
        baseline_correct = 0
        feature_matrix = np.stack([alg.summary_features(alg.prepare(self.store.samples(r["id"]))) for r in templates])
        feature_mean = feature_matrix.mean(axis=0)
        feature_scale = np.maximum(feature_matrix.std(axis=0), 1e-6)
        centers = {c: np.mean([(f - feature_mean) / feature_scale for r, f in zip(templates, feature_matrix) if r["label"] == c], axis=0)
                   for c in alg.LABELS}
        elapsed, details = [], []
        for record in records:
            result = self.recognize(record["id"], persist=False)
            prediction = result.get("predicted_label")
            elapsed.append(result["elapsed_ms"])
            if record["label"] in alg.LABELS:
                known += 1
                correct += prediction == record["label"]
                matrix[record["label"]][prediction or "不确定"] += 1
                if record["quality"]["valid"]:
                    features = (alg.summary_features(alg.prepare(self.store.samples(record["id"]))) - feature_mean) / feature_scale
                    baseline = min(centers, key=lambda c: np.linalg.norm(features - centers[c]))
                    baseline_correct += baseline == record["label"]
            else:
                unknown += 1
                rejected_unknown += prediction is None
            details.append({"id": record["id"], "label": record["label"], "prediction": prediction, "status": result["status"]})
        f1 = []
        for c in alg.LABELS:
            tp = matrix[c][c]
            fn = sum(matrix[c].values()) - tp
            fp = sum(matrix[other][c] for other in alg.LABELS if other != c)
            f1.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0)
        return {"known_count": known, "correct": correct, "accuracy": correct / known if known else None,
                "baseline_accuracy": baseline_correct / known if known else None,
                "baseline_definition": "模板集统计特征＋最近类别中心；仅比较已知类别，不含未知拒识。",
                "macro_f1": float(np.mean(f1)), "confusion_matrix": matrix, "unknown_count": unknown,
                "unknown_rejection_rate": rejected_unknown / unknown if unknown else None,
                "p95_ms": float(np.percentile(elapsed, 95)), "details": details, "scope": scope.model_dump(),
                "note": "仅对应当前书写者、安装、数据来源和留出样本；合成数据结果不是硬件准确率。"}

    def demo(self):
        existing = [r for r in self.store.list() if r["source_kind"] == "synthetic" and r["mode"] == "practice"
                    and not r.get("input_kind") and r["sample_count"] > 0]
        if existing:
            return existing[0]
        rng = np.random.default_rng(20261108)
        for mode, count in (("template", 3), ("validation", 2), ("practice", 1)):
            for label in (alg.LABELS if mode != "practice" else ("十",)):
                for trial in range(count):
                    n = 321 + trial * 7
                    t = np.linspace(0, 3.2 + trial * 0.07, n)
                    u = t / t[-1]
                    pulse = np.sin(np.pi * u) ** 2
                    signals = {"一": (35 * pulse, 8 * np.sin(2 * np.pi * u), 4 * pulse),
                               "十": (20 * np.sin(2 * np.pi * u), 42 * np.sin(3 * np.pi * u), 10 * pulse),
                               "口": (25 * np.sin(4 * np.pi * u), 22 * np.cos(4 * np.pi * u) * pulse, 32 * np.sin(2 * np.pi * u))}[label]
                    gyro = np.column_stack(signals) + rng.normal(0, 0.65, (n, 3))
                    samples = []
                    for i in range(n):
                        samples.append({"sample_us": 1000000 + int(t[i] * 1e6), "seq": i + 1, "boot_id": "synthetic",
                                        "ax_g": float(gyro[i, 0] / 400 + rng.normal(0, .003)),
                                        "ay_g": float(gyro[i, 1] / 400 + rng.normal(0, .003)), "az_g": float(1 + rng.normal(0, .003)),
                                        "gx_dps": float(gyro[i, 0]), "gy_dps": float(gyro[i, 1]), "gz_dps": float(gyro[i, 2]),
                                        "roll_deg": float(12 * np.sin(2 * np.pi * u[i])), "pitch_deg": float(-30 + 15 * pulse[i]),
                                        "yaw_deg": float(10 * np.sin(np.pi * u[i]))})
                    info = RecordInfo(writer_id="demo-writer", mount_id="demo-mount", mode=mode, label=label,
                                      title=f"合成示例 · {label} · {mode} {trial + 1}")
                    record = self.create(info, samples, source="synthetic")
                    if mode == "template":
                        self.template(record["id"])
                    if mode == "practice":
                        query = record
        self.calibrate(self.scope(query))
        self.recognize(query["id"])
        return self.get(query["id"])
