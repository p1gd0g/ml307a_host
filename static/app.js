// ML307A 监控台前端逻辑：REST 拉取 + WebSocket 实时推送
let ws = null;
let pollTimer = null;
let lastMsgIds = new Set();
let paramsDirty = false; // 用户是否已修改自定义参数但未保存

const $ = (id) => document.getElementById(id);

// 判断某容器（或其子元素）当前是否处于聚焦状态，用于避免覆盖正在输入的表单
function isFocusedWithin(id) {
  const el = $(id);
  return !!el && el.contains(document.activeElement);
}

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
      `<td class="msg-content">${renderContent(m.content)}</td>`;
    body.appendChild(tr);
  }
}

function renderContent(content) {
  // content 可能是字符串（旧格式）或结构化对象
  if (typeof content === "string") return escapeHtml(content);
  if (!content || typeof content !== "object") return escapeHtml(String(content));

  if (content.type === "wap_push") {
    const parts = [];
    parts.push('<span class="tag-push">彩信 / WAP Push 通知</span>');
    if (content.url) {
      parts.push(`<div>链接：<a href="${escapeAttr(content.url)}" target="_blank" rel="noopener">${escapeHtml(content.url)}</a></div>`);
    }
    if (content.sender_hint) {
      parts.push(`<div>发件人：${escapeHtml(content.sender_hint)}</div>`);
    }
    if (content.raw) {
      const id = "raw-" + Math.random().toString(36).slice(2);
      parts.push(
        `<details class="raw-box"><summary>原始数据</summary>` +
        `<pre>${escapeHtml(content.raw)}</pre></details>`
      );
    }
    return parts.join("");
  }

  // 普通文本
  return escapeHtml(content.text != null ? content.text : "");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderState(state) {
  renderStatus(state.status);
  renderMessages(state.messages);
  // 仅在用户未正在编辑时才回填表单，避免覆盖输入
  if (state.webhook_url && !isFocusedWithin("in-webhook")) $("in-webhook").value = state.webhook_url;
  if (state.serial_port && !isFocusedWithin("in-port")) $("in-port").value = state.serial_port;
  // 自定义参数：未处于编辑态且用户未做未保存修改时才重建
  if (!paramsDirty && !isFocusedWithin("param-list")) {
    renderParams(state.webhook_params || {});
  }
}

function renderParams(params) {
  const list = $("param-list");
  list.innerHTML = "";
  const entries = Object.entries(params);
  if (entries.length === 0) addParamRow("", "");
  else for (const [k, v] of entries) addParamRow(k, v);
}

function addParamRow(key, val) {
  const list = $("param-list");
  const row = document.createElement("div");
  row.className = "param-row";
  row.innerHTML =
    '<input type="text" class="pk" placeholder="参数名" />' +
    '<input type="text" class="pv" placeholder="参数值" />' +
    '<button type="button" class="param-del">×</button>';
  const pk = row.querySelector(".pk");
  const pv = row.querySelector(".pv");
  pk.value = key || "";
  pv.value = val == null ? "" : val;
  // 用户编辑或删除参数即标记为未保存，避免被实时刷新覆盖
  pk.addEventListener("input", () => { paramsDirty = true; });
  pv.addEventListener("input", () => { paramsDirty = true; });
  row.querySelector(".param-del").onclick = () => { row.remove(); paramsDirty = true; };
  list.appendChild(row);
}

function collectParams() {
  const params = {};
  document.querySelectorAll("#param-list .param-row").forEach((row) => {
    const k = row.querySelector(".pk").value.trim();
    const v = row.querySelector(".pv").value;
    if (k) params[k] = v;
  });
  return params;
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
    webhook_params: collectParams(),
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
      paramsDirty = false; // 保存成功后允许参数列表随服务端刷新
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
      body: JSON.stringify({ webhook_url: url, webhook_params: collectParams() }),
    });
    const d = await r.json();
    $("cfg-msg").textContent = d.ok ? "已发送测试 GET 请求，请查看接收端。" : "测试失败: " + d.error;
  } catch (e) { $("cfg-msg").textContent = "请求失败"; }
};

$("btn-add-param").onclick = () => { addParamRow("", ""); paramsDirty = true; };

$("btn-clear").onclick = async () => {
  try {
    await fetch("/api/messages/clear", { method: "POST" });
    loadState();
  } catch (e) {}
};

// ---------- 启动 ----------
loadState();
connectWs();
