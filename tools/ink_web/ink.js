"use strict";

const glyphs = {
  "三": [
    {name: "第一横", type: "horizontal", guide: [[91, 91], [231, 87]]},
    {name: "第二横", type: "horizontal", guide: [[107, 151], [215, 149]]},
    {name: "第三横", type: "horizontal", guide: [[68, 222], [251, 219]]},
  ],
  "十": [
    {name: "横", type: "horizontal", guide: [[65, 136], [251, 132]]},
    {name: "竖", type: "vertical", guide: [[163, 62], [159, 258]]},
  ],
  "口": [
    {name: "竖", type: "vertical", guide: [[90, 91], [94, 227]]},
    {name: "横折", type: "bend", guide: [[90, 92], [228, 88], [218, 227]]},
    {name: "封口横", type: "horizontal", guide: [[94, 225], [220, 221]]},
  ],
};
const byId = (id) => document.getElementById(id);
const set = (id, value) => { byId(id).textContent = value; };
const ns = "http://www.w3.org/2000/svg";
const storageKeys = {references: "ink-coach-refs-v1", recordings: "ink-coach-strokes-v1"};

function readLocal(key) {
  try { return JSON.parse(localStorage.getItem(key)) || {}; }
  catch { return {}; }
}

function writeLocal(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); }
  catch { /* Export remains available when local storage is disabled. */ }
}

const state = {
  mode: "demo", glyph: "三", stroke: 0, demo: {}, port: "COM10",
  references: readLocal(storageKeys.references), recordings: readLocal(storageKeys.recordings),
  fraction: 1, animation: 0, busy: false, job: null,
  message: "", phase: "", fit: null, teacherFit: null, current: null,
};
const keyFor = (index = state.stroke) => `${state.glyph}:${index}`;
const currentStroke = () => glyphs[state.glyph][state.stroke];
const referenceFor = () => state.references[keyFor()];
const measurePoints = (result) => result.points.map((p) => [p[1], -p[2]]);

function resultFor(index) {
  return state.mode === "demo" ? state.demo[glyphs[state.glyph][index].type] : state.recordings[keyFor(index)];
}

function svg(name, attrs, content) {
  const el = document.createElementNS(ns, name);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  if (content) el.textContent = content;
  return el;
}

function pathData(points) {
  return points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
}

function distances(points) {
  const offsets = [0];
  for (let i = 1; i < points.length; i++) {
    offsets.push(offsets.at(-1) + Math.hypot(points[i][0] - points[i - 1][0],
      points[i][1] - points[i - 1][1]));
  }
  return offsets;
}

function resample(points, offsets) {
  const last = offsets.at(-1);
  let i = 1;
  return Array.from({length: 32}, (_, k) => {
    const d = last * k / 31;
    while (i < offsets.length - 1 && offsets[i] < d) i++;
    const span = offsets[i] - offsets[i - 1];
    const fraction = span ? (d - offsets[i - 1]) / span : 0;
    return points[i - 1].map((v, j) => v + (points[i][j] - v) * fraction);
  });
}

function fitPath(points, guide) {
  const lengths = distances(points);
  const guideLengths = distances(guide);
  const referenceLength = guideLengths.at(-1);
  if (lengths.at(-1) < 0.001 || referenceLength < 0.001) return null;
  const reference = resample(guide, guideLengths);
  const sample = resample(points, lengths);
  let dot = 0, cross = 0;
  for (let i = 0; i < sample.length; i++) {
    const [a, b] = sample[i].map((v, j) => v - sample[0][j]);
    const [x, y] = reference[i].map((v, j) => v - reference[0][j]);
    dot += a * x + b * y;
    cross += a * y - b * x;
  }
  const angle = Math.atan2(cross, dot);
  const ratio = referenceLength / lengths.at(-1);
  const cos = Math.cos(angle), sin = Math.sin(angle);
  const align = (p) => {
    const [x, y] = p.map((v, i) => v - points[0][i]);
    return [guide[0][0] + ratio * (cos * x - sin * y), guide[0][1] + ratio * (sin * x + cos * y)];
  };
  const aligned = sample.map(align);
  const errors = aligned.map((p, i) => Math.hypot(p[0] - reference[i][0], p[1] - reference[i][1]));
  const rms = Math.sqrt(errors.reduce((acc, e) => acc + e * e, 0) / errors.length);
  return {draw: points.map(align), score: Math.max(0, Math.round(100 * (1 - 3.2 * rms / referenceLength))),
    peak: errors.indexOf(Math.max(...errors)) / 31};
}

function updateStrokeData() {
  const reference = referenceFor();
  const guide = currentStroke().guide;
  state.current = resultFor(state.stroke) || null;
  state.teacherFit = reference ? fitPath(measurePoints(reference), guide) : null;
  state.fit = state.current ? fitPath(measurePoints(state.current), state.teacherFit?.draw || guide) : null;
}

function renderOptions() {
  const buttons = Object.keys(glyphs).map((glyph) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = glyph;
    button.disabled = state.busy;
    button.setAttribute("aria-pressed", String(glyph === state.glyph));
    button.addEventListener("click", () => {
      state.glyph = glyph;
      selectStroke(0);
    });
    return button;
  });
  byId("glyphs").replaceChildren(...buttons);
  byId("stroke-steps").replaceChildren(...glyphs[state.glyph].map((stroke, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${index + 1} · ${stroke.name}`;
    button.disabled = state.busy;
    button.classList.toggle("done", state.mode === "live" && Boolean(state.recordings[keyFor(index)]));
    button.setAttribute("aria-pressed", String(index === state.stroke));
    button.addEventListener("click", () => selectStroke(index));
    return button;
  }));
}

function drawSheet(index) {
  const guides = [], traces = [];
  glyphs[state.glyph].forEach((stroke, i) => {
    const active = i === state.stroke;
    guides.push(svg("path", {d:pathData(stroke.guide), class:`stroke-guide${active ? " active" : ""}`}));
    if (active && state.teacherFit) {
      traces.push(svg("path", {d:pathData(state.teacherFit.draw), class:"stroke-reference"}));
    }
    if (active && state.fit) {
      traces.push(svg("path", {d:pathData(state.fit.draw.slice(0, index + 1)), class:"stroke-actual"}));
    } else if (!active && state.mode === "live") {
      const previous = state.recordings[keyFor(i)];
      const fit = previous && fitPath(measurePoints(previous), stroke.guide);
      if (fit) traces.push(svg("path", {d:pathData(fit.draw), class:"stroke-actual inactive"}));
    }
  });
  byId("guides").replaceChildren(...guides);
  byId("ink-paths").replaceChildren(...traces);
  const marker = state.fit?.draw[index];
  byId("ink-marker").replaceChildren(...(marker
    ? [svg("circle", {cx:marker[0], cy:marker[1], r:4.4, class:"stroke-cursor"})] : []));
}

function drawAttitude(index) {
  const chart = byId("attitude-chart");
  if (!state.current) {
    chart.replaceChildren(svg("text", {x:420,y:62,"text-anchor":"middle",class:"chart-note"}, "本笔暂无姿态数据"));
    return;
  }
  const points = state.current.points;
  const tilts = points.map((p) => p[3]);
  const high = Math.min(90, Math.max(...tilts) + 1.2);
  const low = Math.max(0, Math.min(...tilts) - 1.2);
  const time = points.at(-1)[0] || 1;
  const xy = (point) => [42 + 778 * point[0] / time, 12 + 86 * (high - point[3]) / (high - low)];
  const [x, y] = xy(points[index]);
  chart.replaceChildren(
    svg("path", {d:"M42 12H820M42 55H820M42 98H820",class:"chart-grid"}),
    svg("text", {x:1,y:16,class:"chart-note"}, `${high.toFixed(1)}°`),
    svg("text", {x:1,y:102,class:"chart-note"}, `${low.toFixed(1)}°`),
    svg("path", {d:pathData(points.map(xy)),class:"chart-trace"}),
    svg("line", {x1:x,x2:x,y1:12,y2:98,class:"chart-cursor"}),
    svg("circle", {cx:x,cy:y,r:3.5,class:"chart-dot"}),
  );
}

function renderFrame() {
  const points = state.current?.points || [];
  const time = (state.current?.duration_s || 0) * state.fraction;
  const hit = points.findIndex((p) => p[0] >= time);
  const index = hit < 0 ? points.length - 1 : hit;
  byId("scrub").disabled = !points.length;
  byId("scrub").value = Math.round(state.fraction * 1000);
  set("time-readout", points.length ? `${points[index][0].toFixed(2)} s · ${points[index][3].toFixed(1)}°` : "—");
  drawSheet(index);
  drawAttitude(index);
}

function renderStatus() {
  const status = byId("capture-status");
  status.textContent = state.message || (state.mode === "live"
    ? "握笔放稳，点击录制后按屏幕提示写一笔。"
    : "回放使用桌面直线试验，每个字按范线摆放。");
  status.dataset.phase = state.phase;
  byId("primary-action").disabled = state.busy || (state.mode === "demo" && !state.current);
  set("primary-action", state.busy ? "采集中…" : state.mode === "live"
    ? (state.current ? "重录本笔" : "录制本笔") : state.animation ? "暂停回放" : "播放本笔");
  byId("replay-capture").hidden = state.mode !== "live";
  byId("replay-capture").disabled = state.busy || !state.current;
  byId("export-raw").hidden = state.mode !== "live";
  byId("export-raw").disabled = state.busy;
  set("replay-capture", state.animation ? "暂停" : "播放");
}

function referenceTiltDifference(a, b) {
  const atPhase = (result, phase) => {
    const t = phase * result.duration_s;
    return (result.points.find((point) => point[0] >= t) || result.points.at(-1))[3];
  };
  return Math.max(...Array.from({length: 21}, (_, index) => {
    const phase = index / 20;
    return Math.abs(atPhase(a, phase) - atPhase(b, phase));
  }));
}

function render() {
  updateStrokeData();
  renderOptions();
  byId("mode-demo").setAttribute("aria-pressed", String(state.mode === "demo"));
  byId("mode-live").setAttribute("aria-pressed", String(state.mode === "live"));
  byId("mode-demo").disabled = byId("mode-live").disabled = state.busy;
  const result = state.current;
  const ref = referenceFor();
  set("stroke-count", `第 ${state.stroke + 1} / ${glyphs[state.glyph].length} 笔`);
  set("source", result?.source || (state.mode === "demo" ? "范线示意" : "等待采集"));
  set("score", state.fit?.score ?? "—");
  set("reference-label", ref ? "对照示范 · 逐笔对齐" : "对照范线 · 逐笔对齐");
  set("duration", result ? `${result.duration_s.toFixed(2)} s` : "—");
  set("displacement", result ? `${result.summary.endpoint_xy_mm.toFixed(1)} mm` : "—");
  set("tilt-label", result ? `${result.axis_label}偏转` : "姿态偏转");
  set("tilt", result ? `${result.axis_change_deg.toFixed(1)}°` : "—");
  set("attitude-label", result ? `${result.axis_label}与竖直方向夹角` : "倾角时间线");
  set("device", `本地工作台 · USB ${state.port}`);
  set("feedback-title", state.fit ? `${currentStroke().name} · 练习提示` : "练习提示");
  let feedback = "录下这一笔，即可对照范线和姿态。";
  if (state.fit) {
    const peak = state.fit.peak < .33 ? "起笔段" : state.fit.peak < .67 ? "中段" : "收笔段";
    feedback = state.fit.score >= 90 ? "这次轮廓贴近参考，可以尝试保持同样的运笔节奏。"
      : `${peak}与参考线的偏差最明显。回放该段，下一遍尽量沿范线稳定推进。`;
    if (ref) {
      const difference = result.duration_s - ref.duration_s;
      feedback += ` 用时较示范${difference < 0 ? "短" : "长"} ${Math.abs(difference).toFixed(2)} 秒。`;
      if (result.axis_label === ref.axis_label) {
        feedback += ` 倾角最大差 ${referenceTiltDifference(result, ref).toFixed(1)}°。`;
      }
    }
    if (result.segment_count > 1) feedback += ` 共检测到 ${result.segment_count} 段，本页采用主段。`;
  } else if (state.mode === "demo" && currentStroke().type === "bend") {
    feedback = "横折先向右、再向下。这里展示范线，切换到实板采集可以录下转折。";
  }
  set("feedback-text", feedback);
  byId("next-stroke").disabled = state.busy || state.stroke >= glyphs[state.glyph].length - 1;
  byId("set-reference").disabled = state.busy || (!result && !ref);
  set("set-reference", ref ? "清除示范" : "设为示范");
  byId("export-session").disabled = state.busy || !glyphs[state.glyph].some((_, i) => resultFor(i));
  renderStatus();
  renderFrame();
}

function stopReplay() {
  if (state.animation) cancelAnimationFrame(state.animation);
  state.animation = 0;
}

function selectStroke(index) {
  stopReplay();
  state.stroke = index;
  state.fraction = 1;
  state.message = "";
  state.phase = "";
  render();
}

function switchMode(mode) {
  state.mode = mode;
  selectStroke(state.stroke);
}

function play() {
  if (state.animation) {
    stopReplay();
    renderStatus();
    return;
  }
  if (!state.current) return;
  if (state.fraction >= 1) state.fraction = 0;
  const duration = state.current.duration_s * 1000;
  const start = performance.now() - state.fraction * duration;
  function tick(now) {
    state.fraction = Math.min(1, (now - start) / duration);
    renderFrame();
    state.animation = state.fraction < 1 ? requestAnimationFrame(tick) : 0;
    if (!state.animation) renderStatus();
  }
  state.animation = requestAnimationFrame(tick);
  renderStatus();
}

async function json(url, options) {
  const response = await fetch(url, {...options, signal: AbortSignal.timeout(5000)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "服务暂时不可用");
  return data;
}

async function followCapture(id) {
  let finished = false;
  try {
    for (let i = 0; i < 100; i++) {
      const job = await json(`/api/stroke/${encodeURIComponent(id)}`);
      state.phase = job.phase;
      state.message = job.message;
      renderStatus();
      if (job.phase === "error") {
        finished = true;
        break;
      }
      if (job.phase === "done") {
        state.recordings[`${job.glyph}:${job.stroke}`] = job.result;
        writeLocal(storageKeys.recordings, state.recordings);
        state.fraction = 1;
        finished = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 450));
    }
    if (!finished) state.message = "等待采集结果超时，刷新页面可恢复正在进行的录制。";
  } catch (error) {
    state.message = `采集状态读取失败。 ${error.message}`;
  } finally {
    state.busy = false;
    render();
  }
}

async function startCapture() {
  stopReplay();
  state.busy = true;
  state.message = "正在连接 USB…";
  state.phase = "opening";
  render();
  try {
    const job = await json("/api/stroke", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({glyph:state.glyph, stroke:state.stroke})});
    await followCapture(job.id);
  } catch (error) {
    state.busy = false;
    state.message = error.message;
    render();
  }
}

function toggleReference() {
  if (referenceFor()) delete state.references[keyFor()];
  else if (state.current) state.references[keyFor()] = state.current;
  writeLocal(storageKeys.references, state.references);
  render();
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function exportSession() {
  const strokes = glyphs[state.glyph].map((stroke, index) => ({
    stroke: stroke.name, recording: resultFor(index) || null,
    reference: state.references[keyFor(index)] || null,
  }));
  const data = {character:state.glyph, source:state.mode, strokes};
  download(new Blob([JSON.stringify(data, null, 2)], {type:"application/json"}),
    `腕墨-${state.glyph}-${state.mode}.json`);
}

async function exportRaw() {
  const glyph = state.glyph;
  byId("export-raw").disabled = true;
  try {
    const response = await fetch(`/api/raw/${encodeURIComponent(glyph)}`);
    if (!response.ok) throw new Error((await response.json()).error);
    const filename = response.headers.get("Content-Disposition")?.split("''")[1];
    download(await response.blob(), filename ? decodeURIComponent(filename) : `腕墨-${glyph}-原始数据.zip`);
  } catch (error) {
    if (!state.busy && state.glyph === glyph && state.mode === "live") {
      state.phase = "error";
      state.message = error.message || "下载原始数据失败";
    }
  } finally {
    renderStatus();
  }
}

byId("mode-demo").addEventListener("click", () => switchMode("demo"));
byId("mode-live").addEventListener("click", () => switchMode("live"));
byId("primary-action").addEventListener("click", () => state.mode === "demo" ? play() : startCapture());
byId("replay-capture").addEventListener("click", play);
byId("next-stroke").addEventListener("click", () => selectStroke(state.stroke + 1));
byId("set-reference").addEventListener("click", toggleReference);
byId("export-session").addEventListener("click", exportSession);
byId("export-raw").addEventListener("click", exportRaw);
byId("scrub").addEventListener("input", (event) => {
  stopReplay();
  state.fraction = Number(event.target.value) / 1000;
  renderFrame();
  renderStatus();
});

render();
Promise.allSettled([json("/sample.json"), json("/api/station")]).then(([demo, station]) => {
  if (demo.status === "fulfilled") state.demo = demo.value;
  if (station.status === "fulfilled") {
    state.port = station.value.port;
    const job = station.value.job;
    if (job?.phase === "done") state.recordings[`${job.glyph}:${job.stroke}`] = job.result;
    if (job && !["done", "error"].includes(job.phase) && !state.busy) {
      state.mode = "live";
      state.glyph = job.glyph;
      state.stroke = job.stroke;
      state.busy = true;
      state.message = job.message;
      followCapture(job.id);
    }
  }
  if (demo.status === "rejected" && state.mode === "demo") state.message = "实测样本暂时无法读取。";
  render();
});
