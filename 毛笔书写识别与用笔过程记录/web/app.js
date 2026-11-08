"use strict";
const $ = (id) => document.getElementById(id);
const state = {records: [], record: null, report: null, samples: [], selectedLabel: "一", index: 0, playing: false, busy: false, version: 0, live: false};
const liveState = {id: null, cursor: 0, timer: null, lastData: 0, started: 0, failures: 0, finishing: false};
const API = "/api/v1/brush";
const statusNames = {recognized: "已匹配", candidate: "待校准候选", uncertain: "不确定", quality_rejected: "质量未通过", needs_templates: "待建模板"};
const modeNames = {practice: "练习", template: "示范", validation: "验证", test: "测试"};
const fmt = (v, digits = 2) => Number.isFinite(v) ? v.toFixed(digits) : "—";
let toastTimer;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  const node = $("toast");
  node.textContent = message; node.className = "toast" + (error ? " error" : ""); node.hidden = false;
  toastTimer = setTimeout(() => node.hidden = true, 6500);
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = sessionStorage.getItem("brush-token");
  if (token) headers.set("Authorization", "Bearer " + token);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(API + path, {...options, headers});
  const data = await response.json();
  if (!response.ok || data.code !== 0) throw new Error(data.msg || "接口请求失败。");
  return data.data;
}
async function busy(action) {
  if (state.busy) return;
  state.busy = true; syncButtons(); document.body.style.cursor = "progress";
  try { await action(); } catch (error) { toast(error.message || String(error), true); }
  finally { state.busy = false; document.body.style.cursor = ""; syncButtons(); }
}
function syncButtons() {
  const r = state.record;
  document.body.classList.toggle("streaming", state.live);
  $("load-demo").disabled = state.busy || state.live;
  $("csv-file").disabled = state.busy || state.live;
  $("live-start").disabled = state.busy || state.live;
  $("live-simulate").disabled = state.busy || state.live;
  $("live-stop").disabled = state.busy || !state.live || liveState.finishing;
  $("recognize").disabled = state.busy || !r || r.is_template || r.status !== "completed";
  $("add-template").disabled = state.busy || state.live || !r || r.mode !== "template" || r.is_template || !r.quality?.valid;
  $("export").disabled = state.busy || !r || state.live;
  $("calibrate").disabled = state.busy || state.live;
  $("evaluate").disabled = state.busy || state.live;
  $("play").disabled = state.live || !state.samples.length;
  $("scrub").disabled = state.live || !state.samples.length;
  $("jump-peak").disabled = state.live || !state.report?.metrics?.peak;
  document.querySelectorAll(".record-item").forEach((button) => button.disabled = state.live || state.busy);
}
function scope() {
  if (state.record) return {writer_id: state.record.writer_id, mount_id: state.record.mount_id, source_kind: state.record.source_kind};
  return {writer_id: $("writer-id").value.trim() || "writer-01", mount_id: $("mount-id").value.trim() || "mount-01", source_kind: "real"};
}
function view(name) {
  document.querySelectorAll(".view").forEach((node) => node.hidden = node.id !== "view-" + name);
  document.querySelectorAll(".nav").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  if (name === "templates") refreshTemplates().catch((e) => toast(e.message, true));
  requestAnimationFrame(drawChart);
}
function renderRecords() {
  const list = $("record-list"); list.replaceChildren();
  $("record-count").textContent = state.records.length + " 条";
  if (!state.records.length) list.append(element("p", "还没有记录，导入后会保存在这里。", "empty-note"));
  state.records.forEach((r) => {
    const button = element("button", undefined, "record-item" + (r.id === state.record?.id ? " active" : ""));
    button.append(element("strong", r.title || r.filename || "一次书写"));
    button.append(element("span", (modeNames[r.mode] || r.mode) + " · " + r.sample_count + " 点 · " + fmt(r.quality?.duration_s) + " s"));
    if (r.source_kind === "synthetic") button.append(element("em", "合成"));
    button.onclick = () => busy(() => selectRecord(r.id));
    button.disabled = state.live || state.busy;
    list.append(button);
  });
}
async function refreshRecords() {
  state.records = await api("/records");
  renderRecords();
}
async function selectRecord(id) {
  state.playing = false; $("play").textContent = "▶";
  const version = ++state.version;
  const [report, samples] = await Promise.all([api("/records/" + id + "/report"), api("/records/" + id + "/samples")]);
  if (version !== state.version) return;
  state.record = report.record; state.report = report; state.samples = samples; state.index = 0;
  $("record-title").textContent = report.record.title;
  const synthetic = state.record.source_kind === "synthetic";
  $("source-badge").textContent = synthetic ? "合成示例 · 非真实书写" : (state.record.input_kind === "live-hardware" ? "硬件记录 · " : "CSV 导入 · ") + state.record.writer_id;
  $("source-badge").className = "badge" + (synthetic ? " synthetic" : " good");
  const r = state.record.recognition;
  $("glyph-title").textContent = state.record.is_template ? "模板标签" : "运动匹配候选";
  $("glyph").textContent = state.record.is_template ? state.record.label : (r?.predicted_label || r?.candidate_label || "—");
  $("match-state").textContent = state.record.is_template ? "示范记录" : (r ? statusNames[r.status] || r.status : "尚未识别");
  $("glyph-caption").textContent = r?.stale ? "模板库已变化，请重新匹配" : synthetic ? "合成信号，仅用于检查软件流程" : "在预设三类中比较书写运动";
  $("recognition-note").textContent = r?.reason || "未校准时仅返回候选；不会把距离显示为书法评分。";
  if (r?.distances) $("recognition-note").textContent += " 距离：" + Object.entries(r.distances).map(([c, d]) => c + " " + fmt(d, 3)).join(" / ") + "（越小越近）";
  const q = state.record.quality || {};
  const m = report.metrics;
  setMetric("metric-duration", q.duration_s, "s");
  setMetric("metric-rate", q.saved_hz, "Hz", 1);
  setMetric("metric-peak", m.peak?.value_dps, "°/s", 1);
  setMetric("metric-distance", m.distance?.last_mm, "mm", 1);
  $("metric-count").textContent = state.record.sample_count + " 个已保存样本";
  $("distance-note").textContent = m.distance ? "设备估计 · 物理精度未验证" : "未提供 distance_mm";
  $("quality-badge").textContent = q.valid ? "通过" : "需检查";
  $("quality-badge").className = "badge " + (q.valid ? "good" : "bad");
  const quality = $("quality-content"); quality.replaceChildren();
  quality.append(element("p", "最大采样间隔 " + fmt(q.max_gap_ms) + " ms · 中位间隔 " + fmt(q.median_interval_ms) + " ms"));
  [...(q.issues || []), ...(q.warnings || [])].forEach((text) => quality.append(element("p", text, "issue")));
  if (q.valid) quality.append(element("p", "当前记录满足单字模板的基础采样检查，识别效果仍需独立实测。"));
  $("footer-record").textContent = "RECORD / " + id.slice(0, 12);
  $("scrub").max = String(Math.max(samples.length - 1, 0)); $("scrub").value = "0";
  $("answer").textContent = "选择一个问题，从当前记录里寻找依据。";
  renderRecords(); updateFrame(); syncButtons();
}
function setMetric(id, value, unit, digits = 2) {
  $(id).replaceChildren(document.createTextNode(fmt(value, digits)), element("small", unit));
}
function updateFrame() {
  const row = state.samples[state.index];
  const last = state.samples.at(-1);
  const first = state.samples[0];
  if (row) {
    const time = (row.sample_us - first.sample_us) / 1e6;
    $("play-time").textContent = fmt(time) + " / " + fmt((last.sample_us - first.sample_us) / 1e6) + " s";
    $("scrub").value = String(state.index);
    for (const name of ["roll", "pitch", "yaw"]) $("angle-" + name).textContent = Number.isFinite(row[name + "_deg"]) ? fmt(row[name + "_deg"], 1) + "°" : "—";
    const hasAngles = Number.isFinite(row.roll_deg) && Number.isFinite(row.pitch_deg);
    $("pose-state").textContent = hasAngles ? "设备角度示意" : "未提供姿态角";
    const roll = hasAngles ? row.roll_deg : 0, pitch = hasAngles ? row.pitch_deg : 0, yaw = hasAngles ? row.yaw_deg || 0 : 0;
    $("pen-model").style.transform = "rotateZ(" + (-24 - roll) + "deg) rotateX(" + pitch + "deg) rotateY(" + yaw + "deg)";
    $("pen-model").style.opacity = hasAngles ? "1" : ".45";
  }
  drawChart();
}
function series() {
  const rows = state.samples;
  if ($("signal-mode").value === "angles") return ["roll_deg", "pitch_deg", "yaw_deg"].map((name, i) => ({
    name: ["Roll", "Pitch", "Yaw"][i], color: ["#526f58", "#a84534", "#8d8877"][i], values: rows.map((s) => s[name] ?? null), unit: "°"
  })).filter((s) => s.values.some(Number.isFinite));
  const accel = $("signal-mode").value === "accel";
  const names = accel ? ["ax_g", "ay_g", "az_g"] : ["gx_dps", "gy_dps", "gz_dps"];
  return [{name: accel ? "加速度模长" : "角速度模长", color: "#526f58", unit: accel ? "g" : "°/s",
    values: rows.map((s) => Math.hypot(...names.map((n) => s[n])))}];
}
function drawChart() {
  const canvas = $("signal-chart"), rect = canvas.getBoundingClientRect();
  if (!rect.width) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const w = rect.width, h = rect.height, left = 46, top = 25, bottom = h - 22, right = w - 13;
  ctx.font = "10px Microsoft YaHei, sans-serif"; ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = top + (bottom - top) * i / 3;
    ctx.strokeStyle = "#e9e9df"; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
  }
  if (!state.samples.length) {
    ctx.fillStyle = "#9a9d8e"; ctx.textAlign = "center"; ctx.fillText("导入记录后显示真实采样曲线", w / 2, h / 2); return;
  }
  const traces = series(), values = traces.flatMap((s) => s.values).filter(Number.isFinite);
  if (!values.length) { ctx.fillStyle = "#8a8d7f"; ctx.fillText("此记录没有所选角度字段", left + 10, 85); return; }
  let low = Math.min(...values), high = Math.max(...values);
  if ($("signal-mode").value === "speed") low = 0;
  const padding = Math.max((high - low) * .08, .01); high += padding; low -= padding;
  const rows = state.samples, start = rows[0].sample_us, span = Math.max((rows.at(-1).sample_us - start) / 1e6, .001);
  const x = (i) => left + (rows[i].sample_us - start) / 1e6 / span * (right - left);
  const y = (value) => bottom - (value - low) / (high - low) * (bottom - top);
  ctx.fillStyle = "#989b8d"; ctx.textAlign = "right";
  for (let i = 0; i <= 3; i++) {
    const value = high - (high - low) * i / 3;
    ctx.fillText(fmt(value, Math.abs(value) < 10 ? 2 : 0), left - 7, top + (bottom - top) * i / 3 + 3);
  }
  ctx.textAlign = "center";
  for (let i = 0; i <= 4; i++) ctx.fillText(fmt(span * i / 4, 1) + "s", left + (right - left) * i / 4, h - 5);
  traces.forEach((s, traceIndex) => {
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.beginPath();
    let active = false;
    s.values.forEach((value, i) => {
      if (!Number.isFinite(value)) { active = false; return; }
      if (!active || (i && rows[i].sample_us - rows[i - 1].sample_us > 100001)) ctx.moveTo(x(i), y(value));
      else ctx.lineTo(x(i), y(value));
      active = true;
    });
    ctx.stroke(); ctx.fillStyle = s.color; ctx.textAlign = "left";
    ctx.fillText(s.name + " (" + s.unit + ")", left + traceIndex * 100, 12);
  });
  const cursor = x(state.index); ctx.strokeStyle = "#a8453480"; ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(cursor, top); ctx.lineTo(cursor, bottom); ctx.stroke(); ctx.setLineDash([]);
  traces.forEach((s) => { const value = s.values[state.index]; if (!Number.isFinite(value)) return; ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(cursor, y(value), 3.2, 0, Math.PI * 2); ctx.fill(); });
}
function seek(index) { state.index = Math.max(0, Math.min(Number(index), state.samples.length - 1)); updateFrame(); }
function jumpPeak() { if (!state.live && state.report?.metrics?.peak) { state.playing = false; $("play").textContent = "▶"; seek(state.report.metrics.peak.sample_index); } }
function togglePlayback() {
  if (state.playing) { state.playing = false; $("play").textContent = "▶"; return; }
  if (!state.samples.length) return;
  if (state.index >= state.samples.length - 1) state.index = 0;
  state.playing = true; $("play").textContent = "Ⅱ";
  const started = performance.now(), offset = state.samples[state.index].sample_us;
  let lastFrame = 0;
  function frame(now) {
    if (!state.playing) return;
    if (now - lastFrame >= 33) {
      const target = offset + (now - started) * 1000;
      while (state.index < state.samples.length - 1 && state.samples[state.index + 1].sample_us <= target) state.index++;
      updateFrame(); lastFrame = now;
      if (state.index >= state.samples.length - 1) { state.playing = false; $("play").textContent = "▶"; return; }
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}
async function importFiles(files) {
  if (!files.length) return;
  if (state.live) { toast("请先结束实时记录，再导入 CSV。"); return; }
  await busy(async () => {
    let lastId, count = 0; const errors = [];
    for (const file of files) {
      const data = new FormData();
      data.append("file", file); data.append("writer_id", $("writer-id").value.trim());
      data.append("mount_id", $("mount-id").value.trim()); data.append("mode", $("purpose").value);
      data.append("label", $("purpose").value === "practice" ? "" : state.selectedLabel);
      try { const record = await api("/records/import", {method: "POST", body: data}); lastId = record.id; count++; }
      catch (error) { errors.push(file.name + "：" + error.message); }
    }
    await refreshRecords();
    if (lastId) { await selectRecord(lastId); view("workbench"); }
    toast("已导入 " + count + " 条。" + (errors.length ? " " + errors.join("；") : ""), !!errors.length);
    $("csv-file").value = "";
  });
}
async function refreshTemplates() {
  const current = scope();
  const result = await api("/templates?" + new URLSearchParams(current));
  $("template-scope").textContent = "书写者 " + current.writer_id + " / 安装 " + current.mount_id + " / " + (current.source_kind === "synthetic" ? "合成示例（不能用于真实识别）" : "真实数据");
  const counts = $("template-counts"); counts.replaceChildren();
  for (const [label, count] of Object.entries(result.counts)) {
    const card = element("article"), text = element("span", "条有效模板 · 至少 3 条");
    text.prepend(element("b", String(count))); card.append(element("strong", label), text); counts.append(card);
  }
  $("calibration-state").textContent = result.calibration ? "已校准 · 使用 " + result.calibration.validation_ids.length + " 条验证记录。未知动作拒识能力仍需负样本实测。" : "尚未校准。此时匹配只输出候选字。";
  const list = $("template-list"); list.replaceChildren();
  if (!result.records.length) list.append(element("p", "导入时选择“示范模板”，通过质量检查后加入。", "empty-note"));
  result.records.forEach((r) => {
    const row = element("div", undefined, "template-row"), info = element("span", r.label + " · " + r.title);
    info.append(element("small", r.sample_count + " 点 / " + fmt(r.quality.saved_hz, 1) + " Hz / " + r.id.slice(0, 10)));
    const button = element("button", "移出模板", "button subtle");
    button.onclick = () => busy(async () => { await api("/records/" + r.id + "/template", {method: "DELETE"}); await refreshRecords(); if (state.record) await selectRecord(state.record.id); await refreshTemplates(); });
    row.append(info, button); list.append(row);
  });
}
async function ask(question) {
  if (!state.record) { toast("请先选择一条记录。"); return; }
  if (state.live) { toast("请先结束书写，再围绕完整记录提问。"); return; }
  const id = state.record.id;
  $("answer").textContent = "正在读取本次记录…";
  try {
    const result = await api("/records/" + id + "/chat", {method: "POST", body: JSON.stringify({question})});
    if (state.record?.id !== id) return;
    $("answer").textContent = result.answer + "\n依据记录：" + id.slice(0, 12);
    if (result.event) { const button = element("button", "定位第 " + fmt(result.event.at_s, 3) + " 秒 ↗", "text-link"); button.onclick = jumpPeak; $("answer").append(button); }
  } catch (error) { $("answer").textContent = error.message; toast(error.message, true); }
}
function renderEvaluation(result) {
  const node = $("evaluation-result"); node.replaceChildren();
  const summary = element("div", undefined, "evaluation-summary");
  summary.append(element("span", "已知字 " + result.correct + "/" + result.known_count), element("span", "准确率 " + (result.accuracy == null ? "暂无" : fmt(result.accuracy * 100, 1) + "%")), element("span", "Macro-F1 " + fmt(result.macro_f1, 3)), element("span", "P95 " + fmt(result.p95_ms, 0) + " ms")); node.append(summary);
  const table = element("table", undefined, "evaluation-table"), head = element("tr");
  ["实际 / 预测", "一", "十", "口", "不确定"].forEach((c) => head.append(element("th", c))); table.append(head);
  Object.entries(result.confusion_matrix).forEach(([label, counts]) => { const row = element("tr"); row.append(element("th", label)); ["一", "十", "口", "不确定"].forEach((c) => row.append(element("td", String(counts[c])))); table.append(row); });
  node.append(table, element("p", "统计特征基线准确率：" + (result.baseline_accuracy == null ? "暂无" : fmt(result.baseline_accuracy * 100, 1) + "%") + "。" + result.baseline_definition, "disclaimer"));
  node.append(element("p", "未知动作 " + result.unknown_count + " 条，拒识率：" + (result.unknown_rejection_rate == null ? "尚无负样本" : fmt(result.unknown_rejection_rate * 100, 1) + "%") + "。", "disclaimer"), element("p", result.note, "disclaimer"));
}

function liveStatus(text, kind) {
  $("live-status-text").textContent = text;
  $("live-status").className = "live-status " + (kind || "");
}
function resetLiveValues() {
  ["ax", "ay", "az", "gx", "gy", "gz"].forEach((key) => $("live-" + key).textContent = "—");
}
async function beginLive(simulated) {
  await busy(async () => {
    const info = {writer_id: $("writer-id").value.trim() || "writer-01", mount_id: $("mount-id").value.trim() || "mount-01",
      mode: $("purpose").value, label: $("purpose").value === "practice" ? null : state.selectedLabel, title: "现场书写 · " + new Date().toLocaleTimeString("zh-CN")};
    const record = await api(simulated ? "/live/simulate" : "/live/start", {method: "POST", body: simulated ? undefined : JSON.stringify(info)});
    await followLive(record);
  });
}
async function followLive(record) {
  clearTimeout(liveState.timer);
  state.live = true; state.playing = false;
  Object.assign(liveState, {id: record.id, cursor: 0, lastData: 0, started: performance.now(), failures: 0, finishing: false});
  view("workbench"); await selectRecord(record.id);
  // 从游标 0 追赶；恢复页面时不能把已有样本再拼接一遍。
  state.samples = []; state.report.metrics = {peak: null, distance: null};
  resetLiveValues();
  $("live-record-id").textContent = "SESSION / " + record.id;
  $("live-counter").textContent = "0 个实时样本";
  $("glyph").textContent = "—"; $("match-state").textContent = "书写中 · 结束后匹配";
  $("glyph-caption").textContent = record.source_kind === "synthetic" ? "模拟实时流 · 合成信号" : "请写完整一个字，再结束记录";
  $("quality-badge").textContent = "采集中";
  $("quality-content").replaceChildren(element("p", "结束后统一检查时间戳、采样频率、缺口和序号。"));
  $("recognition-note").textContent = "正在记录过程；单字匹配在书写结束后执行。";
  $("live-hint").textContent = record.source_kind === "synthetic" ? "模拟数据正在按设备时间逐批发送，约 6 秒后自动结束；这不是现场真实书写。" : "等待 USB 串口桥（115200）或硬件 HTTP 上报。收到样本后再落笔；写完点‘结束并分析’。";
  liveStatus("等待首批数据", "waiting");
  await refreshRecords(); syncButtons();
  liveState.timer = setTimeout(pollLive, 0);
}
function renderLiveBatch(data) {
  state.record = data.record;
  if (data.samples.length) {
    liveState.lastData = performance.now();
    const startIndex = state.samples.length;
    state.samples.push(...data.samples);
    data.samples.forEach((row, offset) => {
      const speed = Math.hypot(row.gx_dps, row.gy_dps, row.gz_dps);
      if (!state.report.metrics.peak || speed > state.report.metrics.peak.value_dps) state.report.metrics.peak = {
        value_dps: speed, sample_index: startIndex + offset, at_s: (row.sample_us - state.samples[0].sample_us) / 1e6};
      if (Number.isFinite(row.distance_mm)) state.report.metrics.distance = {last_mm: row.distance_mm};
    });
    const row = state.samples.at(-1), first = state.samples[0];
    const duration = (row.sample_us - first.sample_us) / 1e6;
    setMetric("metric-duration", duration, "s");
    setMetric("metric-rate", duration > 0 ? (state.samples.length - 1) / duration : null, "Hz", 1);
    setMetric("metric-peak", state.report.metrics.peak?.value_dps, "°/s", 1);
    setMetric("metric-distance", state.report.metrics.distance?.last_mm, "mm", 1);
    $("distance-note").textContent = state.report.metrics.distance ? "设备估计 · 物理精度未验证" : "未提供 distance_mm";
    $("metric-count").textContent = state.samples.length + " 个已保存样本";
    ["ax", "ay", "az", "gx", "gy", "gz"].forEach((key, i) => $("live-" + key).textContent = fmt(row[["ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps"][i]], i < 3 ? 3 : 1));
    state.index = state.samples.length - 1; $("scrub").max = String(state.index); updateFrame();
  }
  $("live-counter").textContent = state.samples.length + " / " + data.total_count + " 个已保存样本";
  const quiet = performance.now() - (liveState.lastData || liveState.started);
  if (data.total_count > liveState.cursor) liveStatus("正在追赶已保存数据", "waiting");
  else if (!state.samples.length) liveStatus("等待设备数据 · 尚未收到样本", "waiting");
  else if (quiet > 1200) liveStatus("数据暂未更新 · 请检查发送端", "waiting");
  else liveStatus(data.record.source_kind === "synthetic" ? "模拟实时流 · 非真实书写" : "正在接收硬件数据", data.record.source_kind === "synthetic" ? "simulated" : "receiving");
  $("live-lag").textContent = liveState.lastData ? "距页面收到新样本 " + fmt(quiet / 1000, 1) + " s · 非设备端到端延迟" : "后台保存独立于页面刷新";
}
async function pollLive() {
  if (!state.live || !liveState.id) return;
  const id = liveState.id;
  try {
    const data = await api("/records/" + id + "/feed?after=" + liveState.cursor + "&limit=1000");
    if (!state.live || liveState.id !== id) return;
    liveState.failures = 0; liveState.cursor = data.next_cursor;
    renderLiveBatch(data);
    if (data.record.status === "completed" && data.next_cursor >= data.total_count) { await completeLive(id); return; }
  } catch (error) {
    liveState.failures++;
    liveStatus("页面连接中断 · 正在重连", "waiting");
    $("live-hint").textContent = error.message + "。恢复后按游标补取；未收到的数据不会伪造。";
  }
  if (state.live) liveState.timer = setTimeout(pollLive, liveState.failures ? Math.min(3000, 500 * liveState.failures) : 100);
}
async function completeLive(id) {
  liveState.finishing = true; syncButtons(); liveStatus("已结束 · 正在检查记录", "waiting");
  let note = "";
  try {
    const record = await api("/records/" + id);
    if (record.mode === "practice") await api("/records/" + id + "/recognize", {method: "POST"});
  } catch (error) { note = "匹配未完成：" + error.message; }
  // 读取完整保存结果后切换为回放；失败时保留游标，继续重连。
  await selectRecord(id); await refreshRecords();
  state.live = false; liveState.finishing = false; syncButtons();
  liveStatus("记录已保存 · 可以回放", "");
  $("live-hint").textContent = note || (state.record.live_error || (state.record.source_kind === "synthetic" ? "模拟实时流结束，当前报告对应合成数据；可重新试播或开始真实记录。" : "本次过程已保存。可回放、查看质量和匹配结果，或开始下一个字。"));
}
async function restoreLive() {
  const record = await api("/live/current");
  if (record && record.status === "recording") await followLive(record);
}
$("live-start").onclick = () => beginLive(false);
$("live-simulate").onclick = () => beginLive(true);
$("live-stop").onclick = () => busy(async () => {
  if (liveState.id) await api("/records/" + liveState.id + "/stop", {method: "POST"});
  // 不取消轮询，最后一批样本追齐后才出报告。
  liveStatus("正在结束 · 补齐保存数据", "waiting");
});
$("live-presentation").onclick = () => {
  const enabled = document.body.classList.toggle("presentation");
  $("live-presentation").textContent = enabled ? "退出演示大屏" : "演示大屏 ↗";
  view("workbench"); requestAnimationFrame(drawChart);
};

document.querySelectorAll(".nav").forEach((button) => button.onclick = () => view(button.dataset.view));
document.querySelectorAll("[data-label]").forEach((button) => button.onclick = () => {
  state.selectedLabel = button.dataset.label;
  document.querySelectorAll("[data-label]").forEach((b) => b.classList.toggle("selected", b === button));
});
$("purpose").onchange = () => {
  const practice = $("purpose").value === "practice";
  document.querySelectorAll("[data-label]").forEach((b) => b.disabled = practice);
  $("label-hint").textContent = practice ? "练习模式不使用标签" : "标签仅用于建模板或评估";
};
$("csv-file").onchange = (e) => importFiles(Array.from(e.target.files));
$("drop-zone").ondragover = (e) => { e.preventDefault(); $("drop-zone").classList.add("dragging"); };
$("drop-zone").ondragleave = () => $("drop-zone").classList.remove("dragging");
$("drop-zone").ondrop = (e) => { e.preventDefault(); $("drop-zone").classList.remove("dragging"); importFiles(Array.from(e.dataTransfer.files)); };
$("load-demo").onclick = () => busy(async () => {
  toast("正在生成明确标注的合成示例…");
  const record = await api("/demo", {method: "POST"}); await refreshRecords(); await selectRecord(record.id); view("workbench");
  toast("合成示例已载入。它只验证软件流程，不代表真实识别效果。");
});
$("recognize").onclick = () => busy(async () => { const id = state.record.id; await api("/records/" + id + "/recognize", {method: "POST"}); await refreshRecords(); await selectRecord(id); });
$("add-template").onclick = () => busy(async () => { const id = state.record.id; await api("/records/" + id + "/template", {method: "POST"}); await refreshRecords(); await selectRecord(id); toast("已加入当前书写者与安装版本的模板库。"); });
$("export").onclick = () => busy(async () => {
  const token = sessionStorage.getItem("brush-token"), headers = token ? {Authorization: "Bearer " + token} : {};
  const response = await fetch(API + "/records/" + state.record.id + "/export", {headers});
  if (!response.ok) throw new Error("导出失败，请检查连接和令牌。");
  const url = URL.createObjectURL(await response.blob()), a = element("a"); a.href = url; a.download = state.record.id + ".csv"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 2000);
});
$("refresh-templates").onclick = () => busy(refreshTemplates);
$("calibrate").onclick = () => busy(async () => { await api("/templates/calibrate", {method: "POST", body: JSON.stringify(scope())}); await refreshTemplates(); toast("拒识阈值已校准，请用独立测试集验证效果。"); });
$("evaluate").onclick = () => busy(async () => renderEvaluation(await api("/evaluate", {method: "POST", body: JSON.stringify(scope())})));
$("signal-mode").onchange = drawChart;
$("scrub").oninput = (e) => { state.playing = false; $("play").textContent = "▶"; seek(e.target.value); };
$("play").onclick = togglePlayback; $("jump-peak").onclick = jumpPeak;
document.querySelectorAll("[data-question]").forEach((button) => button.onclick = () => ask(button.dataset.question));
$("question-form").onsubmit = (e) => { e.preventDefault(); const q = $("question").value.trim(); if (q) { ask(q); $("question").value = ""; } };
$("save-settings").onclick = () => { sessionStorage.setItem("brush-token", $("api-token").value.trim()); localStorage.setItem("brush-writer", $("writer-id").value); localStorage.setItem("brush-mount", $("mount-id").value); busy(async () => { await refreshRecords(); toast("连接设置已应用。"); }); };
$("api-token").value = sessionStorage.getItem("brush-token") || "";
$("writer-id").value = localStorage.getItem("brush-writer") || "writer-01"; $("mount-id").value = localStorage.getItem("brush-mount") || "mount-01";
new ResizeObserver(drawChart).observe($("signal-chart"));
(async () => {
  try {
    const response = await fetch("/api/v1/health"), health = await response.json();
    if (!response.ok || health.code !== 0) throw new Error("服务未就绪");
    $("server-status").replaceChildren(element("i"), document.createTextNode("本地服务已连接"));
    $("server-status").classList.add("online");
    await refreshRecords();
    const firstReal = state.records.find((r) => r.source_kind === "real");
    if (firstReal) await selectRecord(firstReal.id);
    else drawChart();
    await restoreLive();
  } catch (error) { $("server-status").textContent = "连接需检查"; toast(error.message, true); }
})();
