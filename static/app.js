// ML307A 监控台前端逻辑：REST 拉取 + WebSocket 实时推送
let ws = null;
let pollTimer = null;
let lastMsgIds = new Set();

const $ = (id) => document.getElementById(id);

function badge(text, kind) {
  return `<span class="badge ${kind}">${text}</span>`;
}

function renderStatus(st) {
  if (!st) return;
  const conn = st.connected;
  $("st-connected").innerHTML = conn ? badge("已连接", "b-ok") : badge("未连接", "b-bad");

  if (st.sim_ready === true) $("st-sim").innerHTML = badge("就绪", "b-ok");
  else if (st.sim_ready === false) $("st-sim").innerHTML = badge("未就绪", "b-bad");
  else $("st-sim").innerHTML = badge("未知", "b-warn");

  if (st.registered === true) $("st-reg").innerHTML = badge("已注册", "b-ok");
  else if (st.registered === false) $("st-reg").innerHTML = badge("未注册", "b-bad");
  else $("st-reg").innerHTML = badge("未知", "b-warn");

  if (st.dialed_up === true) $("st-dial").innerHTML = badge("拨号成功", "b-ok");
  else if (st.dialed_up === false) $("st-dial").innerHTML = badge("未拨号", "b-bad");
  else $("st-dial").innerHTML = badge("未知", "b-warn");

  $("st-ip").textContent = st.ip || "—";
  $("st-ops").textContent = st.operator || "—";

  let sig = "—";
  if (st.signal_percent != null) {
    sig = `${st.signal_percent}% (RSSI ${st.signal})`;
  } else if (st.signal != null) {
    sig = `RSSI ${st.signal}`;
  }
  $("st-signal").textContent = sig;
  $("st-update").textContent = st.last_update ? st.last_update.replace("T", " ").slice(0, 19) : "—";
  $("st-error").textContent = st.error || "—";
}

function renderMessages(msgs) {
  const body = $("msg-body");
  body.innerHTML = "";
  if (!msgs || msgs.length === 0) {
    $("msg-empty").style.display = "block";
    return;
  }
  $("msg-empty").style.display = "none";
  for (const m of msgs) {
    const tr = document.createElement("tr");
    const t = (m.received_at || "").replace("T", " ").slice(0, 19) || m.timestamp;
    tr.innerHTML =
      `<td>${t}</td><td>${escapeHtml(m.sender)}</td>` +
      `<td class="msg-content">${escapeHtml(m.content)}</td>`;
    body.appendChild(tr);
  }
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderState(state) {
  renderStatus(state.status);
  renderMessages(state.messages);
  if (state.webhook_url) $("in-webhook").value = state.webhook_url;
  if (state.serial_port) $("in-port").value = state.serial_port;
}

async function loadState() {
  try {
    const r = await fetch("/api/state");
    const s = await r.json();
    renderState(s);
  } catch (e) {
    console.error(e);
  }
}

// ---------- WebSocket ----------
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    $("live-dot").className = "conn-dot dot-on";
    $("live-text").textContent = "实时已连接";
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "state") renderState(msg.data);
      else if (msg.type === "message") {
        loadState(); // 简单刷新整页状态
      }
    } catch (e) {}
  };
  ws.onclose = () => {
    $("live-dot").className = "conn-dot dot-off";
    $("live-text").textContent = "实时断开，已切换轮询";
    if (!pollTimer) pollTimer = setInterval(loadState, 5000);
  };
  ws.onerror = () => ws.close();
}

// ---------- 操作 ----------
$("btn-dial").onclick = async () => {
  $("btn-dial").textContent = "拨号中…";
  try {
    const r = await fetch("/api/dial", { method: "POST" });
    const d = await r.json();
    if (d.ok) renderStatus(d.status);
    else alert("拨号失败: " + d.error);
  } catch (e) { alert("请求失败"); }
  $("btn-dial").textContent = "重新拨号";
};

$("btn-save").onclick = async () => {
  const payload = {
    serial_port: $("in-port").value.trim(),
    webhook_url: $("in-webhook").value.trim(),
  };
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.ok) {
      $("cfg-msg").textContent = "已保存。若更改了端口，模块正在重新连接…";
      renderState(d.state);
    } else {
      $("cfg-msg").textContent = "保存失败: " + d.error;
    }
  } catch (e) { $("cfg-msg").textContent = "请求失败"; }
};

$("btn-test").onclick = async () => {
  const url = $("in-webhook").value.trim();
  if (!url) { alert("请先填写 Webhook 地址"); return; }
  try {
    const r = await fetch("/api/webhook/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ webhook_url: url }),
    });
    const d = await r.json();
    $("cfg-msg").textContent = d.ok ? "已发送测试 GET 请求，请查看接收端。" : "测试失败: " + d.error;
  } catch (e) { $("cfg-msg").textContent = "请求失败"; }
};

$("btn-clear").onclick = async () => {
  try {
    await fetch("/api/messages/clear", { method: "POST" });
    loadState();
  } catch (e) {}
};

// ---------- 启动 ----------
loadState();
connectWs();
