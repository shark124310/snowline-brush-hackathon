"use strict";

/* 逐笔复盘：把腕墨的 USB 单笔采集、轨迹重建与形状评分并入墨序工作台。 */
(() => {
  const $id = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const phaseNames = {
    opening: "连接 USB", waiting: "等待静止", ready: "采样中", write: "请书写",
    hold: "请停住", analyzing: "重建轨迹", done: "已完成", error: "失败",
  };
  const phaseIndex = {
    opening: 0, waiting: 1, ready: 2, write: 2, hold: 3, analyzing: 4, done: 5,
  };
  const s = {
    meta: null, glyph: "三", stroke: 0, result: null, review: null, reference: null,
    job: null, busy: false, demo: false, fraction: 1, animation: 0, port: "COM10",
  };

  const storageKey = () => "brush-stroke-refs-v1:" +
    ($("writer-id").value.trim() || "writer-01") + "|" +
    ($("mount-id").value.trim() || "mount-01");
  const strokeKey = (index = s.stroke) => `${s.glyph}:${index}`;

  function loadRefs() {
    try { return JSON.parse(localStorage.getItem(storageKey())) || {}; }
    catch { return {}; }
  }

  function saveRefs(value) {
    try { localStorage.setItem(storageKey(), JSON.stringify(value)); }
    catch { /* 浏览器禁用本地存储时仍可评分，只是不保留示范。 */ }
  }

  function currentGlyph() {
    return s.meta?.glyphs.find((glyph) => glyph.name === s.glyph) || null;
  }

  function currentStroke() {
    return currentGlyph()?.strokes[s.stroke] || null;
  }

  function setText(id, value) {
    const node = $id(id);
    if (node) node.textContent = value;
  }

  function svg(name, attrs, text) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text) node.textContent = text;
    return node;
  }

  function pathData(points) {
    return points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");
  }

  function setPhase(text) {
    setText("stroke-phase", text);
  }

  function renderPickers() {
    if (!s.meta) return;
    const glyphBox = $id("stroke-glyphs");
    glyphBox.replaceChildren(...s.meta.glyphs.map((glyph) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "stroke-chip glyph" + (glyph.name === s.glyph ? " selected" : "");
      button.textContent = glyph.name;
      button.disabled = s.busy;
      button.setAttribute("aria-pressed", String(glyph.name === s.glyph));
      button.onclick = () => {
        if (s.busy) return;
        s.glyph = glyph.name;
        selectStroke(0);
      };
      return button;
    }));
    const strokeBox = $id("stroke-strokes");
    strokeBox.replaceChildren(...currentGlyph().strokes.map((stroke, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "stroke-chip" + (index === s.stroke ? " selected" : "");
      button.textContent = `${index + 1} · ${stroke.name}`;
      button.disabled = s.busy;
      button.setAttribute("aria-pressed", String(index === s.stroke));
      const reference = loadRefs()[`${s.glyph}:${index}`];
      if (reference) button.classList.add("done");
      button.onclick = () => {
        if (s.busy) return;
        selectStroke(index);
      };
      return button;
    }));
  }

  function selectStroke(index) {
    stopPlayback();
    s.stroke = index;
    s.result = null;
    s.review = null;
    s.job = null;
    s.demo = false;
    s.fraction = 1;
    s.reference = loadRefs()[strokeKey()] || null;
    renderAll();
  }

  function stepStroke(delta) {
    if (s.busy) return;
    const total = currentGlyph()?.strokes.length || 0;
    const index = s.stroke + delta;
    if (index < 0 || index >= total) return;
    selectStroke(index);
  }

  function renderSteps() {
    const phase = s.busy || s.job ? s.job?.phase : null;
    const index = phaseIndex[phase] ?? -1;
    document.querySelectorAll("#stroke-steps li").forEach((node, step) => {
      node.classList.toggle("done", index === 5 || (index > step && index < 5));
      node.classList.toggle("active", index === step);
    });
    const glyph = currentGlyph();
    const total = glyph?.strokes.length || 0;
    const refs = loadRefs();
    const marks = glyph
      ? glyph.strokes.filter((_, step) => refs[`${s.glyph}:${step}`]).length : 0;
    setText("stroke-progress", `第 ${s.stroke + 1} / ${total} 笔 · 已设示范 ${marks} 笔`);
    $id("stroke-prev").disabled = s.busy || s.stroke === 0;
    $id("stroke-next").disabled = s.busy || s.stroke >= total - 1;
  }

  function drawSheet() {
    const glyph = currentGlyph();
    if (!glyph) return;
    const guides = [];
    const paths = [];
    glyph.strokes.forEach((stroke, index) => {
      const active = index === s.stroke;
      guides.push(svg("path", {
        d: pathData(stroke.guide),
        class: "stroke-guide" + (active ? " active" : ""),
      }));
    });
    if (s.review?.reference_draw) {
      paths.push(svg("path", {d: pathData(s.review.reference_draw), class: "stroke-reference"}));
    }
    if (s.review?.draw?.length) {
      paths.push(svg("path", {d: pathData(s.review.draw), class: "stroke-actual"}));
    }
    $id("stroke-guides").replaceChildren(...guides);
    $id("stroke-paths").replaceChildren(...paths);
    const draw = s.review?.draw || [];
    if (draw.length) {
      const index = Math.min(draw.length - 1, Math.round(s.fraction * (draw.length - 1)));
      const marker = draw[index];
      $id("stroke-marker").replaceChildren(
        svg("circle", {cx: marker[0], cy: marker[1], r: 4.6, class: "stroke-cursor"}));
    } else {
      $id("stroke-marker").replaceChildren();
    }
  }

  function drawChart() {
    const canvas = $id("stroke-chart");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(rect.width * ratio);
    canvas.height = Math.round(rect.height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const w = rect.width, h = rect.height;
    const left = 46, top = 14, bottom = h - 21, right = w - 11;
    ctx.font = "10px Microsoft YaHei, sans-serif";
    ctx.lineWidth = 1;
    for (let index = 0; index <= 3; index++) {
      const y = top + (bottom - top) * index / 3;
      ctx.strokeStyle = "#e9e9df";
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
    }
    const points = s.result?.points || [];
    if (!points.length) {
      ctx.fillStyle = "#9a9d8e";
      ctx.textAlign = "center";
      ctx.fillText("录制或回放示例后显示本笔倾角", w / 2, h / 2);
      return;
    }
    const tilts = points.map((point) => point[3]);
    let high = Math.min(90, Math.max(...tilts) + 1.2);
    let low = Math.max(0, Math.min(...tilts) - 1.2);
    if (high - low < 1) high = low + 1;
    const duration = points.at(-1)[0] || 1;
    const x = (point) => left + (right - left) * point[0] / duration;
    const y = (tilt) => bottom - (bottom - top) * (tilt - low) / (high - low);
    ctx.fillStyle = "#989b8d";
    ctx.textAlign = "right";
    ctx.fillText(`${high.toFixed(1)}°`, left - 7, top + 3);
    ctx.fillText(`${low.toFixed(1)}°`, left - 7, bottom + 3);
    ctx.strokeStyle = "#526f58";
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    points.forEach((point, index) => {
      if (index) ctx.lineTo(x(point), y(point[3]));
      else ctx.moveTo(x(point), y(point[3]));
    });
    ctx.stroke();
    const index = Math.min(points.length - 1, Math.round(s.fraction * (points.length - 1)));
    const point = points[index];
    ctx.strokeStyle = "#a8453480";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x(point), top);
    ctx.lineTo(x(point), bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#a84534";
    ctx.beginPath();
    ctx.arc(x(point), y(point[3]), 3.4, 0, Math.PI * 2);
    ctx.fill();
  }

  function renderFrame() {
    const points = s.result?.points || [];
    const duration = Number(s.result?.duration_s) || 0;
    $id("stroke-scrub").disabled = !points.length;
    $id("stroke-scrub").value = String(Math.round(s.fraction * 1000));
    if (points.length) {
      const index = Math.min(points.length - 1, Math.round(s.fraction * (points.length - 1)));
      const point = points[index];
      setText("stroke-time-readout", `${point[0].toFixed(2)} s · ${point[3].toFixed(1)}°`);
      setText("stroke-time", `${duration.toFixed(2)} s`);
    } else {
      setText("stroke-time-readout", "—");
      setText("stroke-time", "—");
    }
    drawSheet();
    drawChart();
  }

  function renderReview() {
    const result = s.result;
    const review = s.review;
    setText("stroke-heading", `${s.glyph} · ${currentStroke()?.name || ""}`);
    setText("stroke-subheading", s.demo
      ? "示例轨迹来自腕墨的桌面实测横移/纵移，用来检查页面与评分流程。"
      : "选择字与笔画，在米字格中对照范线。");
    setText("stroke-score", review ? String(review.score) : "—");
    setText("stroke-feedback", review?.feedback ||
      "点击“录制本笔”，或先回放示例查看效果。");
    setText("stroke-duration", result ? `${Number(result.duration_s || 0).toFixed(2)} s` : "—");
    setText("stroke-displacement", result
      ? `${Number(result.summary?.endpoint_xy_mm || 0).toFixed(1)} mm` : "—");
    setText("stroke-tilt", result
      ? `${Number(result.axis_change_deg || 0).toFixed(1)}° ${result.axis_label || ""}` : "—");
    setText("stroke-segments", result ? String(result.segment_count || 1) : "—");

    const mode = s.busy ? "采集中" : s.demo ? "示例回放" : result ? "实板采集" : "等待";
    setText("stroke-mode", mode);
    setText("stroke-source", mode);
    if (!s.busy) {
      setPhase(s.job?.message || phaseNames[s.job?.phase] ||
        (result ? "已完成" : "等待录制"));
    }

    $id("stroke-capture").disabled = s.busy;
    $id("stroke-demo").disabled = s.busy;
    $id("stroke-save").disabled = s.busy || s.job?.phase !== "done";
    const referenceButton = $id("stroke-reference");
    referenceButton.disabled = s.busy || (!result && !s.reference);
    referenceButton.textContent = s.reference ? "清除示范" : "设为示范";
    $id("stroke-play").disabled = s.busy || !result?.points?.length;
    renderSteps();
    renderFrame();
  }

  function renderAll() {
    if (!s.meta) return;
    renderPickers();
    renderReview();
    setText("stroke-port", `USB ${s.port}`);
    setText("stroke-port-2", s.port);
  }

  async function reviewNow() {
    if (!s.result) return;
    s.review = await api("/stroke/score", {
      method: "POST",
      body: JSON.stringify({
        glyph: s.glyph,
        stroke: s.stroke,
        points: s.result.points,
        duration_s: s.result.duration_s || 0,
        endpoint_xy_mm: s.result.summary?.endpoint_xy_mm || 0,
        axis_change_deg: s.result.axis_change_deg,
        axis_label: s.result.axis_label,
        segment_count: s.result.segment_count || 1,
        reference: s.reference || null,
      }),
    });
  }

  async function loadDemo() {
    if (s.busy) return;
    const stroke = currentStroke();
    if (!stroke) return;
    if (stroke.type === "bend") {
      toast("横折暂无实测示例，请用实板录制。", true);
      return;
    }
    try {
      const data = await api(`/stroke/sample/${stroke.type}`);
      s.result = data;
      s.job = null;
      s.demo = true;
      s.fraction = 1;
      await reviewNow();
      renderAll();
      toast("已载入腕墨实测示例；它只用于检查逐笔复盘流程。");
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function capture() {
    if (s.busy) return;
    s.busy = true;
    s.demo = false;
    s.result = null;
    s.review = null;
    s.job = null;
    s.fraction = 1;
    setPhase("正在连接 USB…");
    renderAll();
    try {
      const job = await api("/stroke/capture", {
        method: "POST",
        body: JSON.stringify({glyph: s.glyph, stroke: s.stroke}),
      });
      s.job = job;
      poll(job.id, 0);
    } catch (error) {
      s.busy = false;
      setPhase("失败");
      renderAll();
      toast(error.message, true);
    }
  }

  async function poll(jobId, failures) {
    try {
      const job = await api(`/stroke/${jobId}`);
      s.job = job;
      setPhase(job.message || phaseNames[job.phase] || job.phase);
      renderSteps();
      if (job.phase === "done") {
        s.busy = false;
        s.result = job.result;
        s.review = job.result?.review || null;
        s.fraction = 1;
        renderAll();
        toast("本笔已重建。可以回放、设为示范，或保存到记录。");
        return;
      }
      if (job.phase === "error") {
        s.busy = false;
        renderAll();
        toast(job.message || "采集失败。", true);
        return;
      }
      setTimeout(() => poll(jobId, 0), 500);
    } catch (error) {
      if (failures < 3) {
        setTimeout(() => poll(jobId, failures + 1), 800);
        return;
      }
      s.busy = false;
      setPhase("连接中断");
      renderAll();
      toast(error.message || "采集状态读取失败。", true);
    }
  }

  async function saveRecord() {
    if (!s.job?.id || s.job.phase !== "done") return;
    try {
      const record = await api(`/stroke/${s.job.id}/save`, {
        method: "POST",
        body: JSON.stringify({
          writer_id: $("writer-id").value.trim() || "writer-01",
          mount_id: $("mount-id").value.trim() || "mount-01",
        }),
      });
      await refreshRecords();
      await selectRecord(record.id);
      view("workbench");
      toast("已转存为六轴记录，可继续做质量检查、回放与模板匹配。");
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function toggleReference() {
    if (s.busy || (!s.result && !s.reference)) return;
    const refs = loadRefs();
    if (s.reference) {
      delete refs[strokeKey()];
      s.reference = null;
      toast("已清除本笔示范。");
    } else {
      s.reference = {
        points: s.result.points,
        duration_s: s.result.duration_s,
        axis_change_deg: s.result.axis_change_deg,
        axis_label: s.result.axis_label,
        endpoint_xy_mm: s.result.summary?.endpoint_xy_mm || 0,
      };
      refs[strokeKey()] = s.reference;
      toast("已把本笔设为当前书写者与安装版本的示范。");
    }
    saveRefs(refs);
    if (s.result) await reviewNow();
    renderAll();
  }

  function stopPlayback() {
    if (s.animation) cancelAnimationFrame(s.animation);
    s.animation = 0;
    const button = $id("stroke-play");
    if (button) button.textContent = "▶";
  }

  function play() {
    if (s.animation) {
      stopPlayback();
      return;
    }
    if (!s.result?.points?.length) return;
    if (s.fraction >= 1) s.fraction = 0;
    const duration = Math.max((s.result.duration_s || 1) * 1000, 200);
    const started = performance.now() - s.fraction * duration;
    $id("stroke-play").textContent = "Ⅱ";
    function tick(now) {
      s.fraction = Math.min(1, (now - started) / duration);
      renderFrame();
      s.animation = s.fraction < 1 ? requestAnimationFrame(tick) : 0;
      if (!s.animation) $id("stroke-play").textContent = "▶";
    }
    s.animation = requestAnimationFrame(tick);
  }

  async function init() {
    if (s.meta) return;
    const meta = await api("/stroke/meta");
    s.meta = meta;
    s.port = meta.port;
    s.reference = loadRefs()[strokeKey()] || null;
    if (meta.job) {
      s.job = meta.job;
      if (meta.job.phase === "done" && meta.job.result) {
        s.glyph = meta.job.glyph;
        s.stroke = meta.job.stroke;
        s.result = meta.job.result;
        s.review = meta.job.result.review || null;
      } else if (!["done", "error"].includes(meta.job.phase)) {
        s.glyph = meta.job.glyph;
        s.stroke = meta.job.stroke;
        s.busy = true;
        s.reference = loadRefs()[strokeKey()] || null;
        setTimeout(() => poll(meta.job.id, 0), 0);
      }
    }
    renderAll();
  }

  $id("stroke-capture").addEventListener("click", () => capture());
  $id("stroke-demo").addEventListener("click", () => loadDemo());
  $id("stroke-save").addEventListener("click", () => saveRecord());
  $id("stroke-reference").addEventListener("click", () => toggleReference());
  $id("stroke-play").addEventListener("click", play);
  $id("stroke-prev").addEventListener("click", () => stepStroke(-1));
  $id("stroke-next").addEventListener("click", () => stepStroke(1));
  $id("goto-strokes").addEventListener("click", () => {
    view("strokes");
    init().catch((error) => toast(error.message || "逐笔复盘加载失败。", true));
  });
  $id("stroke-scrub").addEventListener("input", (event) => {
    stopPlayback();
    s.fraction = Number(event.target.value) / 1000;
    renderFrame();
  });
  document.querySelector('[data-view="strokes"]').addEventListener("click", () => {
    init().catch((error) => toast(error.message || "逐笔复盘加载失败。", true));
  });
  $("save-settings").addEventListener("click", () => {
    s.reference = loadRefs()[strokeKey()] || null;
    renderAll();
  });
  new ResizeObserver(drawChart).observe($id("stroke-chart"));

  if (location.pathname.replace(/\/+$/, "").toLowerCase() === "/strokes" ||
      location.hash === "#strokes") {
    view("strokes");
    init().catch((error) => toast(error.message || "逐笔复盘加载失败。", true));
  }
})();
