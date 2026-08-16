"""Dependency-free Ingress UI assets for Passkey proposal confirmation."""

from __future__ import annotations


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>HA 操作审批</title>
  <link rel="stylesheet" href="static/app.css">
</head>
<body>
  <main class="shell">
    <header>
      <p class="eyebrow">HOME ASSISTANT OPERATIONS BROKER</p>
      <h1>Passkey 操作审批</h1>
      <p class="subtitle">核对不可变提案后，用已注册的 Passkey 签名。本阶段不会执行任何操作。</p>
    </header>
    <section id="status" class="status">正在读取审批上下文…</section>
    <section id="secure-window" class="card hidden">
      <h2>在安全窗口中完成 Passkey</h2>
      <p>Home Assistant Ingress 当前以 iframe 显示本页，而部分浏览器不会在该上下文中启动 WebAuthn。请在同一已登录会话的顶层安全窗口中继续；仍会要求本人使用 Touch ID 或已注册的安全密钥。</p>
      <a id="open-secure-window" class="button secondary" target="_blank" rel="noopener noreferrer">在安全窗口中打开</a>
    </section>
    <section id="proposal" class="card hidden">
      <div class="row"><span>Action ID</span><strong id="action-id"></strong></div>
      <div class="row"><span>操作</span><strong id="action-type"></strong></div>
      <div class="row"><span>目标</span><strong id="target"></strong></div>
      <div class="row"><span>风险 / 备份</span><strong id="risk"></strong></div>
      <div class="row stack"><span>参数摘要</span><pre id="parameters"></pre></div>
      <div class="row stack"><span>预期变化</span><p id="expected"></p></div>
      <div class="row stack"><span>验证计划</span><ol id="validation"></ol></div>
      <div class="row stack"><span>回滚计划</span><ol id="rollback"></ol></div>
      <div class="row"><span>到期</span><strong id="expires"></strong></div>
      <button id="authorize" class="primary" disabled>使用 Passkey 确认</button>
    </section>
    <section id="enroll" class="card hidden">
      <h2>注册当前 HA 管理员的 Passkey</h2>
      <p>输入 Add-on 私有配置中的 enrollment token。它只用于初始注册，不会保存到本页面。</p>
      <label for="enrollment-token">Enrollment token</label>
      <input id="enrollment-token" type="password" autocomplete="off" minlength="32">
      <button id="register" class="secondary">注册 Passkey</button>
    </section>
    <footer>Passkey 只生成一次性授权收据，不会在浏览器中直接执行。执行还必须通过内部接口、运行开关、动作开关和精确目标白名单。</footer>
  </main>
  <script src="static/app.js" defer></script>
</body>
</html>
"""


APP_CSS = """
:root{color-scheme:dark;--bg:#08111d;--card:#101d2c;--line:#26384d;--text:#e8f0fa;--muted:#9eb0c5;--accent:#59d0ff;--ok:#78e6a5;--bad:#ff8e8e}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#17304b 0,#08111d 45%);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{width:min(760px,calc(100% - 28px));margin:0 auto;padding:36px 0}.eyebrow{color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.14em;margin:0 0 8px}h1{font-size:clamp(30px,6vw,48px);line-height:1.08;margin:0}.subtitle{color:var(--muted);max-width:620px}.status,.card{background:rgba(16,29,44,.94);border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:18px;box-shadow:0 16px 60px rgba(0,0,0,.25)}.status.ok{border-color:#286846;color:var(--ok)}.status.error{border-color:#7b3838;color:var(--bad)}.row{display:flex;gap:20px;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--line)}.row span{color:var(--muted)}.row strong{text-align:right;overflow-wrap:anywhere}.row.stack{display:block}.row.stack p,.row.stack pre,.row.stack ol{margin:8px 0 0}pre{white-space:pre-wrap;background:#07101a;border-radius:10px;padding:12px;color:#cfe6ff}button,.button{display:block;width:100%;border:0;border-radius:12px;padding:13px 16px;margin-top:18px;font-weight:700;text-align:center;text-decoration:none;cursor:pointer}.primary{background:var(--accent);color:#04101a}.secondary{background:#28435f;color:var(--text)}button:disabled{cursor:not-allowed;opacity:.45}label{display:block;color:var(--muted);margin-top:12px}input{width:100%;margin-top:6px;padding:12px;border-radius:10px;border:1px solid var(--line);background:#07101a;color:var(--text)}footer{color:var(--muted);font-size:13px;margin:20px 4px}.hidden{display:none}code{color:var(--accent)}
"""


APP_JS = r"""
const $ = (id) => document.getElementById(id);
const prefix = window.location.pathname.endsWith("/") ? window.location.pathname : `${window.location.pathname}/`;
const query = new URLSearchParams(window.location.search);
const approvalId = query.get("approval_id") || "";
const ingressContext = /^\/api\/hassio_ingress\/[^/]+\//.test(window.location.pathname);
const passkeyRequiresTopLevel = ingressContext && query.get("passkey_context") !== "top";

function api(path, options = {}) {
  return fetch(`${prefix}${path}`, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
  }).then(async (response) => {
    const payload = await response.json();
    if (!response.ok) throw new Error(payload?.error?.message || payload?.error?.code || "请求失败");
    return payload;
  });
}

function b64urlToBytes(value) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function bytesToB64url(value) {
  if (value === null || value === undefined) return null;
  const bytes = new Uint8Array(value);
  let binary = "";
  bytes.forEach((item) => { binary += String.fromCharCode(item); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function creationOptions(options) {
  const publicKey = options.publicKey;
  publicKey.challenge = b64urlToBytes(publicKey.challenge);
  publicKey.user.id = b64urlToBytes(publicKey.user.id);
  (publicKey.excludeCredentials || []).forEach((item) => { item.id = b64urlToBytes(item.id); });
  return {publicKey};
}

function requestOptions(options) {
  const publicKey = options.publicKey;
  publicKey.challenge = b64urlToBytes(publicKey.challenge);
  (publicKey.allowCredentials || []).forEach((item) => { item.id = b64urlToBytes(item.id); });
  return {publicKey};
}

function serializeCredential(credential) {
  const response = credential.response;
  const result = {
    id: credential.id,
    rawId: bytesToB64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {clientDataJSON: bytesToB64url(response.clientDataJSON)},
  };
  if (response.attestationObject) result.response.attestationObject = bytesToB64url(response.attestationObject);
  if (response.authenticatorData) result.response.authenticatorData = bytesToB64url(response.authenticatorData);
  if (response.signature) result.response.signature = bytesToB64url(response.signature);
  if ("userHandle" in response) result.response.userHandle = bytesToB64url(response.userHandle);
  return result;
}

function showStatus(message, kind = "") {
  $("status").textContent = message;
  $("status").className = `status ${kind}`;
}

function fillList(element, values) {
  element.replaceChildren(...values.map((value) => {
    const item = document.createElement("li");
    item.textContent = value;
    return item;
  }));
}

function showSecureWindowGate(message) {
  if (!passkeyRequiresTopLevel) return false;
  $("secure-window").classList.remove("hidden");
  const secureWindowUrl = new URL(window.location.href);
  secureWindowUrl.searchParams.set("passkey_context", "top");
  $("open-secure-window").href = secureWindowUrl.toString();
  $("enroll").classList.add("hidden");
  $("authorize").disabled = true;
  showStatus(message, "error");
  return true;
}

function render(context) {
  if (!passkeyRequiresTopLevel && context.enrollment_enabled && !context.registered_for_user) $("enroll").classList.remove("hidden");
  if (!context.request) {
    if (showSecureWindowGate("当前为 Home Assistant Ingress 内嵌页面。请在安全窗口中注册或使用 Passkey。")) return;
    showStatus("当前没有指定审批请求。可以先为当前 HA 管理员注册 Passkey。", "ok");
    return;
  }
  const request = context.request;
  $("proposal").classList.remove("hidden");
  $("action-id").textContent = request.action_id;
  $("action-type").textContent = request.action_type;
  $("target").textContent = request.target;
  $("risk").textContent = `${request.risk_level} / ${request.requires_backup ? "需要备份" : "无需备份"}`;
  $("parameters").textContent = JSON.stringify(request.parameter_summary, null, 2);
  $("expected").textContent = request.expected_change;
  fillList($("validation"), request.validation_plan);
  fillList($("rollback"), request.rollback_plan);
  $("expires").textContent = request.expires_at;
  if (showSecureWindowGate("提案可以在此核对；Passkey 签名必须在顶层安全窗口中完成。")) return;
  if (request.state === "authorized") {
    showStatus("该提案已完成 Passkey 签名，但执行仍被禁用。", "ok");
  } else if (request.state === "expired") {
    showStatus("该提案已经过期，不能签名或执行。", "error");
  } else if (!context.registered_for_user) {
    showStatus("请先注册当前 HA 管理员的 Passkey。", "error");
  } else {
    $("authorize").disabled = false;
    showStatus("请完整核对提案，然后使用 Passkey 确认。", "ok");
  }
}

$("register").addEventListener("click", async () => {
  const token = $("enrollment-token").value;
  try {
    showStatus("正在创建 Passkey 注册挑战…");
    const begin = await api("api/passkeys/register/begin", {method: "POST", body: JSON.stringify({enrollment_token: token})});
    const credential = await navigator.credentials.create(creationOptions(begin.options));
    await api("api/passkeys/register/complete", {method: "POST", body: JSON.stringify({enrollment_token: token, flow_id: begin.flow_id, response: serializeCredential(credential)})});
    $("enrollment-token").value = "";
    showStatus("Passkey 注册成功。刷新后可以确认提案。", "ok");
    setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    showStatus(error.message, "error");
  }
});

$("authorize").addEventListener("click", async () => {
  try {
    showStatus("正在创建与提案绑定的 Passkey 挑战…");
    const begin = await api(`api/approvals/${encodeURIComponent(approvalId)}/begin`, {method: "POST", body: "{}"});
    const credential = await navigator.credentials.get(requestOptions(begin.options));
    await api(`api/approvals/${encodeURIComponent(approvalId)}/complete`, {method: "POST", body: JSON.stringify({flow_id: begin.flow_id, response: serializeCredential(credential)})});
    showStatus("Passkey 签名已验证；执行仍被禁用。", "ok");
    $("authorize").disabled = true;
  } catch (error) {
    showStatus(error.message, "error");
  }
});

api(`api/context${approvalId ? `?approval_id=${encodeURIComponent(approvalId)}` : ""}`)
  .then(render)
  .catch((error) => showStatus(error.message, "error"));
"""
