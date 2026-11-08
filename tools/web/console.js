"use strict";

const byId = (id) => document.getElementById(id);
const placeholder = "—";
const button = byId("probe-button");
const captureStart = byId("capture-start");
const captureStop = byId("capture-stop");
const recordName = byId("record-name");
const recordStart = byId("record-start");
const recordStop = byId("record-stop");
const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
let sending = false;
let probeError = "";
let controlsState = null;
let captureSending = false;
let recordSending = false;
let captureError = "";
let recordError = "";

function show(id, value) {
  const element = byId(id);
  const text = String(value ?? placeholder);
  if (element.textContent !== text) element.textContent = text;
}

function status(text, state) {
  show("link-status", text);
  byId("link-status").dataset.state = state;
}

function drawHistory(history) {
  const line = byId("rssi-line");
  const dot = byId("rssi-dot");
  const latest = history.at(-1);
  byId("chart-empty").hidden = Boolean(latest);
  dot.toggleAttribute("hidden", !latest);
  if (latest) {
    const levels = history.map((packet) => packet.rssi_dbm);
    const high = Math.ceil((Math.max(...levels) + 2) / 5) * 5;
    const low = Math.min(high - 15, Math.floor((Math.min(...levels) - 2) / 5) * 5);
    const points = levels.map((level, index) => [
      levels.length === 1 ? 634 : 6 + 628 * index / (levels.length - 1),
      5 + 150 * (high - level) / (high - low),
    ]);
    line.setAttribute("points", points.map((point) => point.join(",")).join(" "));
    const [x, y] = points.at(-1);
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", y);
    show("rssi-high", high);
    show("rssi-low", low);
    show("first-time", timeFormat.format(history[0].received_at * 1000));
    show("last-time", timeFormat.format(latest.received_at * 1000));
  } else {
    line.setAttribute("points", "");
    for (const id of ["rssi-high", "rssi-low", "first-time", "last-time"]) show(id);
  }

  const rows = history.slice().reverse().map((packet) => {
    const row = document.createElement("tr");
    const values = [timeFormat.format(packet.received_at * 1000), packet.seq,
      `${packet.rssi_dbm} dBm`, packet.probe_ack || placeholder];
    for (const value of values) row.insertCell().textContent = value;
    return row;
  });
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = row.insertCell();
    cell.colSpan = 4;
    cell.textContent = "暂无数据";
    rows.push(row);
  }
  byId("packets").replaceChildren(...rows);
}

function renderMotion(packet, online) {
  const imu = online ? packet?.imu : null;
  const ready = imu?.state === "ready";
  const labels = {
    starting: "正在查找 IMU",
    calibrating: imu ? `静置校准 · ${imu.cal_pct}%` : "",
    ready: "IMU 在线",
    unavailable: "IMU 不可用",
    paused: "采集已停止",
  };
  show("imu-status", online ? labels[imu?.state] || "固件未发送 IMU 数据" : "等待数据");
  for (const [axis, id] of ["roll", "pitch", "yaw"].entries()) {
    show(id, ready ? `${imu.rpy_deg[axis].toFixed(1)}°` : null);
  }
  show("acceleration", ready ? `${imu.a_g.map((x) => x.toFixed(3)).join(" / ")} g` : null);
  show("gyroscope", ready ? `${imu.w_dps.map((x) => x.toFixed(1)).join(" / ")} °/s` : null);
  show("imu-temp", ready ? `${imu.temp_c.toFixed(1)} °C` : null);
  show("oled-status", online && packet?.oled != null ? (packet.oled ? "正常" : "未检测到") : null);
  const quality = !ready ? "" : imu.clipped ? "本帧触及量程，角度可信度降低。"
    : !imu.accel_used ? "本帧未使用加速度修正，角度由陀螺积分推进。"
    : imu.rest ? "检测到静止。" : "采样中。";
  show("imu-quality", quality + "角度基于传感器安装坐标，Yaw 以本次校准为起点。");
}

function renderControls(data) {
  controlsState = data;
  const latest = data?.history.at(-1);
  const online = data?.online;
  const imuState = latest?.imu?.state;
  const paused = imuState === "paused";
  const supported = latest?.capture_ack != null && imuState != null;
  const command = data?.capture;
  const pending = captureSending || ["queued", "sent"].includes(command?.state);
  captureStart.disabled = !online || !supported || !paused || pending;
  captureStop.disabled = !online || !supported || paused || pending;
  const captureModes = { ready: "正在采集", paused: "已停止", starting: "正在启动",
    calibrating: "正在校准", unavailable: "IMU 不可用" };
  show("capture-mode", !online ? "等待开发板"
    : supported ? captureModes[imuState] : "当前固件不支持启停");
  show("capture-status", captureError || (pending ? "等待开发板确认…"
    : command?.state === "timeout" ? "指令超时，请根据当前板端状态重试。"
    : command?.state === "ok" ? (command.enabled ? "板端已启动。启动后请静置完成校准。"
      : "板端已停止采集，Wi-Fi 保持在线。")
    : "停止采集后仍保持 Wi-Fi 连接。"));

  const recording = data?.recording;
  const active = recording?.active;
  recordName.disabled = !recording || Boolean(active) || recordSending;
  recordStart.disabled = !recording || !online || !imuState || Boolean(active)
    || !recordName.value.trim() || recordSending;
  recordStop.disabled = !active || recordSending;
  show("record-mode", active ? "正在保存" : "未保存");
  show("record-status", recordError || (active
    ? `${active.name} · 已写入 ${active.packets} 包${online && imuState === "ready"
      ? "" : " · 等待 IMU 数据"}`
    : recording?.message || "填写名称后保存本次收到的 IMU 数据。"));
  const download = byId("record-download");
  download.hidden = !recording?.last_file;
  if (recording?.last_file) {
    download.href = `/api/recordings/${encodeURIComponent(recording.last_file)}`;
    show("record-download", `下载 ${recording.last_file}`);
  }
}

function render(data) {
  const latest = data.history.at(-1);
  status(data.online ? "开发板在线" : latest ? "开发板离线" : "等待开发板",
    data.online ? "online" : latest ? "offline" : "waiting");
  show("sequence", latest?.seq);
  show("rssi", latest?.rssi_dbm);
  show("received", data.received);
  show("missed", data.missed);
  show("peer", data.peer);
  show("boot", data.boot_id);
  show("uptime", latest?.uptime_ms == null ? null : `${Math.floor(latest.uptime_ms / 1000)} s`);
  renderMotion(latest, data.online);
  renderControls(data);
  drawHistory(data.history);

  const supported = latest?.probe_ack != null;
  const phase = data.probe?.state;
  const busy = phase === "queued" || phase === "sent";
  button.disabled = sending || !data.online || !supported || busy;
  const descriptions = {
    queued: "已排队，等待板端下一次上传。",
    sent: "已发给开发板，等待板端回执。",
    ok: data.probe ? `双向通信通过 · ${data.probe.elapsed_ms} ms` : "",
    timeout: "未在 8 秒内收到回执，可重新探测。",
  };
  const message = !data.online ? "开发板在线后可发送探测。"
    : !supported ? "当前固件可收包，刷入新版后可主动探测。"
    : descriptions[phase] || "探测会随下一次 HTTP 应答下发。";
  show("probe-status", sending ? "正在发起探测…" : probeError || message);
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { signal: AbortSignal.timeout(2500) });
    if (!response.ok) throw new Error();
    render(await response.json());
  } catch {
    status("上位机服务断开", "offline");
    renderMotion(null, false);
    renderControls(null);
    show("probe-status", "等待网页重新连接上位机服务。");
    button.disabled = true;
  } finally {
    setTimeout(refresh, 400);
  }
}

button.addEventListener("click", async () => {
  sending = true;
  probeError = "";
  button.disabled = true;
  show("probe-status", "正在发起探测…");
  try {
    const response = await fetch("/api/probe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: "{}", signal: AbortSignal.timeout(2500),
    });
    const result = await response.json();
    if (!response.ok) probeError = result.error || "探测请求失败。";
    show("probe-status", probeError || "已排队，等待板端回执。");
  } catch {
    probeError = "探测请求失败，请检查上位机连接。";
    show("probe-status", probeError);
  } finally {
    sending = false;
  }
});

async function postAction(path, payload) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload), signal: AbortSignal.timeout(2500),
  });
  const result = await response.json();
  return response.ok ? "" : result.error || "操作失败";
}

async function changeCapture(enabled) {
  captureSending = true;
  captureError = "";
  renderControls(controlsState);
  try {
    captureError = await postAction("/api/capture", { enabled });
  } catch {
    captureError = "请求失败，请确认上位机连接。";
  } finally {
    captureSending = false;
  }
}

async function changeRecording(payload) {
  recordSending = true;
  recordError = "";
  renderControls(controlsState);
  try {
    recordError = await postAction("/api/recording", payload);
  } catch {
    recordError = "请求失败，请确认上位机连接。";
  } finally {
    recordSending = false;
  }
}

captureStart.addEventListener("click", () => changeCapture(true));
captureStop.addEventListener("click", () => changeCapture(false));
recordName.addEventListener("input", () => {
  recordError = "";
  renderControls(controlsState);
});
byId("record-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!recordStart.disabled) changeRecording({ action: "start", name: recordName.value });
});
recordStop.addEventListener("click", () => {
  changeRecording({ action: "stop", file: controlsState.recording.active.file });
});

refresh();
