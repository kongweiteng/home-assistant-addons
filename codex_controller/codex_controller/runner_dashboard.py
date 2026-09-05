"""Runner Center browser logic kept separate from the HTTP handler."""

DASHBOARD_JS = r"""
const q = id => document.getElementById(id);
let csrf = '';
let catalog = null;
let statusDoc = null;
let runnerDoc = null;
let installationState = null;
let installationTimer = null;
let credentialState = null;
let statusStream = null;
let statusReconnectTimer = null;
let statusReconnectDelay = 1000;
let statusLastMessageAt = 0;
let csrfRefreshTimer = null;
const viewNames = new Set(['overview', 'tools', 'runners']);

function selectedView() {
  const candidate = window.location.hash.replace(/^#/, '');
  return viewNames.has(candidate) ? candidate : 'overview';
}

function activateView({scroll = true} = {}) {
  const view = selectedView();
  for (const element of document.querySelectorAll('[data-view]')) {
    element.classList.toggle('active-view', element.dataset.view === view);
  }
  for (const link of document.querySelectorAll('[data-view-link]')) {
    const active = link.dataset.viewLink === view;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  if (scroll) window.scrollTo(0, 0);
}

function requestId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}

async function jsonFetch(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const document = await response.json();
  if (!response.ok) {
    throw new Error(document.error?.message || document.error?.code || '请求失败');
  }
  return document;
}

async function call(path) {
  const document = await jsonFetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: '{}',
  });
  return document.result;
}

function setFeedback(element, message, kind = 'muted') {
  element.className = kind;
  element.textContent = message;
}

function showDeviceCode(pending) {
  const box = q('loginInfo');
  box.replaceChildren();
  if (!pending) {
    box.textContent = '尚未生成设备码。';
    return;
  }
  let url;
  try {
    url = new URL(pending.verificationUrl);
  } catch (_) {
    box.textContent = '设备码响应无效。';
    return;
  }
  if (url.protocol !== 'https:') {
    box.textContent = '设备码验证地址不是 HTTPS。';
    return;
  }
  const link = document.createElement('a');
  link.target = '_blank';
  link.rel = 'noreferrer';
  link.href = url.href;
  link.textContent = url.href;
  const code = document.createElement('code');
  code.textContent = pending.userCode;
  box.append('打开 ', link, document.createElement('br'), '用户码：', code);
}

function showApiKey(status) {
  const endpoint = status.api_base_mode === 'custom' ? '自定义 Responses API' : 'OpenAI 官方 API';
  q('loginInfo').textContent = status.api_key_configured
    ? `API Key 已通过 Add-on options 私密配置；API 端点为${endpoint}，页面不会显示 URL 或 Key 内容。`
    : `尚未在 Add-on options 配置 API Key；API 端点为${endpoint}。`;
}

function badge(text, kind = '') {
  const span = document.createElement('span');
  span.className = `badge ${kind}`.trim();
  span.textContent = text;
  return span;
}

function riskText(value) {
  return value === 'read_only' ? '只读' : value === 'write' ? '写入' : '受控操作';
}

function serviceText(value) {
  return value === 'renovation_hub' ? 'Renovation Hub' : 'Operations Broker';
}

function renderTools() {
  const body = q('toolRows');
  body.replaceChildren();
  if (!catalog) return;
  const serviceFilter = q('serviceFilter').value;
  const riskFilter = q('riskFilter').value;
  for (const tool of catalog.tools) {
    if (serviceFilter !== 'all' && tool.service !== serviceFilter) continue;
    if (riskFilter !== 'all' && tool.risk_type !== riskFilter) continue;
    const row = document.createElement('tr');
    const name = document.createElement('td');
    const title = document.createElement('div');
    const technical = document.createElement('div');
    title.className = 'tool-name';
    title.textContent = tool.display_name;
    technical.className = 'technical';
    technical.textContent = tool.name;
    name.append(title, technical);
    const type = document.createElement('td');
    type.append(serviceText(tool.service), document.createElement('br'), riskText(tool.risk_type));
    const states = document.createElement('td');
    const badges = document.createElement('div');
    badges.className = 'badges';
    badges.append(
      badge(tool.configured ? '服务已配置' : '服务未配置', tool.configured ? 'good' : 'bad'),
      badge(tool.enabled ? '策略开启' : '策略关闭', tool.enabled ? 'good' : 'bad'),
      badge(
        tool.mcp_published ? 'MCP 已发布' : tool.waiting_for_mcp_refresh ? '等待 MCP 刷新' : 'MCP 未发布',
        tool.mcp_published ? 'good' : tool.waiting_for_mcp_refresh ? 'warn' : 'bad',
      ),
      badge(tool.callable ? '可调用' : '不可调用', tool.callable ? 'good' : 'bad'),
    );
    states.append(badges);
    const intent = document.createElement('td');
    intent.className = 'intent';
    intent.textContent = tool.intent_examples.join('；');
    const recent = document.createElement('td');
    recent.className = 'muted';
    recent.textContent = tool.last_invocation
      ? `${tool.last_invocation.outcome} · ${tool.last_invocation.error_code || '无错误'} · ${tool.last_invocation.duration_ms}ms`
      : '暂无';
    const action = document.createElement('td');
    const toggle = document.createElement('button');
    toggle.className = `toggle ${tool.enabled ? 'on' : ''}`;
    toggle.textContent = tool.enabled ? '已开启' : '已关闭';
    toggle.disabled = catalog.policy_error !== null;
    toggle.onclick = () => setTool(tool, !tool.enabled, toggle);
    action.append(toggle);
    row.append(name, type, states, intent, recent, action);
    body.append(row);
  }
  if (!body.children.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 6;
    cell.className = 'muted';
    cell.textContent = '当前筛选条件下没有工具。';
    row.append(cell);
    body.append(row);
  }
}

async function setTool(tool, enabled, button) {
  button.disabled = true;
  setFeedback(q('toolFeedback'), '正在保存策略…');
  try {
    const document = await jsonFetch(`api/tools/${encodeURIComponent(tool.name)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({enabled, revision: catalog.revision, request_id: requestId()}),
    });
    setFeedback(
      q('toolFeedback'),
      `${tool.display_name}已${enabled ? '开启' : '关闭'}，目录 revision ${document.result.revision}`,
      'success',
    );
    await refreshTools();
  } catch (error) {
    setFeedback(q('toolFeedback'), error.message, 'error');
    await refreshTools();
  } finally {
    button.disabled = false;
  }
}

async function refreshTools() {
  const document = await jsonFetch('api/tools');
  catalog = document.result;
  q('published').textContent = `${catalog.summary.published}/${catalog.summary.known}`;
  renderTools();
}

function runnerStateKind(value) {
  if (['online', 'idle', 'enabled', 'claimed'].includes(value)) return 'good';
  if (['stale', 'draining', 'pending'].includes(value)) return 'warn';
  if (['offline', 'recovery_required', 'error', 'revoked', 'expired'].includes(value)) return 'bad';
  return '';
}

function runnerStateText(value) {
  const labels = {
    pending: '待启用',
    enabled: '已启用',
    draining: '排空中',
    disabled: '已停用',
    revoked: '已吊销',
    online: '在线',
    stale: '过期',
    offline: '离线',
    idle: '空闲',
    busy: '忙碌',
    recovery_required: '需恢复',
    error: '错误',
    claimed: '已注册',
    expired: '注册已过期',
  };
  return labels[value] || value;
}

function enrollmentText(value) {
  const labels = {
    pending: '注册待领取',
    claimed: '已注册',
    expired: '注册已过期',
    revoked: '注册已撤销',
  };
  return labels[value] || '无注册材料';
}

function runnerPlatformText(value) {
  return value === 'macos' ? 'macOS' : value === 'linux' ? 'Linux' : value;
}

function runnerButton(label, handler, kind = 'secondary', disabled = false) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.className = kind;
  button.disabled = disabled;
  button.onclick = handler;
  return button;
}

async function runnerMutation(method, path, body) {
  const document = await jsonFetch(path, {
    method,
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: JSON.stringify(body),
  });
  return document.result;
}

function currentRunner(runnerId) {
  return runnerDoc?.runners.find(runner => runner.runner_id === runnerId) || null;
}

async function runnerAction(runner, action) {
  setFeedback(q('runnerFeedback'), '正在执行受控管理操作…');
  try {
    let result;
    if (action === 'enable') {
      result = await runnerMutation('PATCH', `api/runners/${encodeURIComponent(runner.runner_id)}`, {
        admin_state: 'enabled', revision: runner.revision, request_id: requestId(),
      });
    } else if (action === 'disable') {
      result = await runnerMutation('PATCH', `api/runners/${encodeURIComponent(runner.runner_id)}`, {
        admin_state: 'disabled', revision: runner.revision, request_id: requestId(),
      });
    } else if (action === 'drain') {
      result = await runnerMutation('POST', `api/runners/${encodeURIComponent(runner.runner_id)}/drain`, {
        revision: runner.revision, request_id: requestId(),
      });
    } else if (action === 'emergency-disable') {
      if (!confirm(`紧急停用 ${runner.display_name}？无法确认的运行任务会进入 recovery_required，且不会自动转移。`)) return;
      result = await runnerMutation('POST', `api/runners/${encodeURIComponent(runner.runner_id)}/emergency-disable`, {
        revision: runner.revision, request_id: requestId(),
      });
    } else if (action === 'self-check') {
      result = await runnerMutation('POST', `api/runners/${encodeURIComponent(runner.runner_id)}/self-check`, {
        revision: runner.revision, request_id: requestId(),
      });
    } else if (action === 'rotate') {
      if (!confirm(`轮换 ${runner.display_name} 的凭据？旧凭据会立即吊销。`)) return;
      result = await runnerMutation('POST', `api/runners/${encodeURIComponent(runner.runner_id)}/credential-rotation`, {
        revision: runner.revision, request_id: requestId(),
      });
      showCredentialRotation(result.credential, runner.runner_id);
    } else if (action === 'delete') {
      if (!confirm(`删除 ${runner.display_name} 的管理记录？只会吊销凭据并归档，不会删除服务器、worktree、分支或 Session。`)) return;
      result = await runnerMutation('DELETE', `api/runners/${encodeURIComponent(runner.runner_id)}`, {
        revision: runner.revision, request_id: requestId(),
      });
    }
    setFeedback(q('runnerFeedback'), `${runner.display_name}：操作已记录`, 'success');
    await refreshRunners();
  } catch (error) {
    setFeedback(q('runnerFeedback'), error.message, 'error');
    await refreshRunners();
  }
}

async function revokeEnrollment(runner) {
  if (!confirm(`撤销 ${runner.display_name} 当前的一次性注册命令？撤销后旧命令会立即失效。`)) return;
  setFeedback(q('runnerFeedback'), '正在撤销一次性注册…');
  try {
    const result = await runnerMutation(
      'POST',
      `api/runners/${encodeURIComponent(runner.runner_id)}/enrollment-revocation`,
      {revision: runner.revision, request_id: requestId()},
    );
    if (installationState?.runnerId === runner.runner_id) {
      installationState.revision = result.runner.revision;
      installationState.state = 'revoked';
      installationState.command = '';
      installationState.link = '';
      renderInstallationState();
      setFeedback(q('runnerInstallFeedback'), '注册已撤销，旧命令不可再使用。', 'success');
    }
    setFeedback(q('runnerFeedback'), `${runner.display_name}：一次性注册已撤销`, 'success');
    await refreshRunners();
  } catch (error) {
    setFeedback(q('runnerFeedback'), error.message, 'error');
    setFeedback(q('runnerInstallFeedback'), error.message, 'error');
    await refreshRunners();
  }
}

async function regenerateEnrollment(runner) {
  if (!confirm(`重新生成 ${runner.display_name} 的安装命令？任何尚未领取的旧命令都会立即失效。`)) return;
  setFeedback(q('runnerFeedback'), '正在重新生成安装命令…');
  try {
    const latest = currentRunner(runner.runner_id) || runner;
    const result = await runnerMutation(
      'POST',
      `api/runners/${encodeURIComponent(runner.runner_id)}/enrollment-regeneration`,
      {revision: latest.revision, request_id: requestId()},
    );
    if (!showInstallation(result)) {
      throw new Error('新的安装命令不可再次显示，请刷新 Runner 状态后重试。');
    }
    setFeedback(q('runnerFeedback'), `${runner.display_name}：新的安装命令已生成`, 'success');
    await refreshRunners();
  } catch (error) {
    setFeedback(q('runnerFeedback'), error.message, 'error');
    setFeedback(q('runnerInstallFeedback'), error.message, 'error');
    await refreshRunners();
  }
}

async function showRunnerDetail(runner) {
  try {
    const document = await jsonFetch(`api/runners/${encodeURIComponent(runner.runner_id)}`);
    const detail = document.result;
    q('runnerDetail').classList.remove('hidden');
    q('runnerDetailTitle').textContent = `${detail.display_name} · ${detail.runner_id}`;
    const check = detail.self_check?.ok === true
      ? '通过'
      : detail.self_check?.ok === false
        ? `失败 ${detail.self_check.error_code || ''}`
        : '未上报';
    const enrollment = enrollmentText(detail.enrollment?.state);
    const events = (detail.events || []).slice(0, 8)
      .map(event => `${event.created_at} ${event.event_type}`)
      .join('；');
    q('runnerDetailBody').textContent = `协议 ${detail.protocol_version} · Agent ${detail.agent_version || '未知'} · Codex ${detail.codex_version || '未知'} · policy ${detail.policy_revision} · ${enrollment} · 自检 ${check} · 最近审计 ${events || '无'}`;
  } catch (error) {
    setFeedback(q('runnerFeedback'), error.message, 'error');
  }
}

function renderRunners() {
  const body = q('runnerRows');
  body.replaceChildren();
  if (!runnerDoc) return;
  const stateFilter = q('runnerStateFilter').value;
  const platformFilter = q('runnerPlatformFilter').value;
  for (const runner of runnerDoc.runners) {
    if (stateFilter !== 'all' && runner.admin_state !== stateFilter) continue;
    if (platformFilter !== 'all' && runner.os !== platformFilter) continue;
    const row = document.createElement('tr');
    const name = document.createElement('td');
    const title = document.createElement('div');
    const id = document.createElement('div');
    title.className = 'tool-name';
    title.textContent = runner.display_name;
    id.className = 'technical';
    id.textContent = runner.runner_id;
    name.append(title, id);
    const platform = document.createElement('td');
    platform.textContent = `${runnerPlatformText(runner.os)} / ${runner.arch}\nAgent ${runner.agent_version || '未注册'}`;
    const state = document.createElement('td');
    const states = document.createElement('div');
    states.className = 'badges';
    for (const value of [runner.admin_state, runner.connectivity_state, runner.work_state]) {
      states.append(badge(runnerStateText(value), runnerStateKind(value)));
    }
    if (runner.enrollment?.state) {
      states.append(badge(enrollmentText(runner.enrollment.state), runnerStateKind(runner.enrollment.state)));
    }
    state.append(states);
    const policy = document.createElement('td');
    policy.textContent = `${runner.allowed_projects.join('、') || '无项目'}\n${runner.labels.join('、') || '无标签'}`;
    const activity = document.createElement('td');
    activity.textContent = `${runner.current_task_id || '无活动任务'}\n${runner.last_heartbeat_at || '从未心跳'}`;
    const actions = document.createElement('td');
    const group = document.createElement('div');
    const registered = runner.enrollment?.state === 'claimed' || Boolean(runner.agent_version);
    group.className = 'runner-actions';
    group.append(runnerButton('详情', () => showRunnerDetail(runner)));
    if (registered && ['pending', 'disabled'].includes(runner.admin_state)) {
      group.append(runnerButton('启用', () => runnerAction(runner, 'enable'), 'toggle'));
    }
    if (!registered && runner.admin_state === 'pending') {
      group.append(runnerButton('停用', () => runnerAction(runner, 'disable')));
    }
    if (runner.admin_state === 'enabled') {
      group.append(runnerButton('排空停用', () => runnerAction(runner, 'drain')));
    }
    if (['enabled', 'draining'].includes(runner.admin_state) || runner.work_state === 'busy') {
      group.append(runnerButton('紧急停用', () => runnerAction(runner, 'emergency-disable'), 'danger'));
    }
    if (runner.enrollment?.state === 'pending') {
      group.append(
        runnerButton('撤销注册', () => revokeEnrollment(runner), 'danger'),
        runnerButton('重新生成', () => regenerateEnrollment(runner)),
      );
    } else if (
      ['expired', 'revoked'].includes(runner.enrollment?.state)
      && ['pending', 'disabled'].includes(runner.admin_state)
      && !registered
    ) {
      group.append(runnerButton('重新生成安装命令', () => regenerateEnrollment(runner)));
    }
    if (registered && runner.admin_state !== 'revoked') {
      group.append(
        runnerButton('自检', () => runnerAction(runner, 'self-check')),
        runnerButton('轮换凭据', () => runnerAction(runner, 'rotate')),
      );
    }
    if (runner.admin_state === 'disabled' && runner.work_state === 'idle' && !runner.current_task_id) {
      group.append(runnerButton('删除', () => runnerAction(runner, 'delete'), 'danger'));
    }
    actions.append(group);
    row.append(name, platform, state, policy, activity, actions);
    body.append(row);
  }
  if (!body.children.length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 6;
    cell.className = 'muted';
    cell.textContent = runnerDoc.runners.length
      ? '当前筛选条件下没有 Runner。'
      : '尚未注册 Runner；可在上方生成一次性安装命令。';
    row.append(cell);
    body.append(row);
  }
}

function formatCountdown(seconds) {
  const minutes = Math.floor(seconds / 60).toString().padStart(2, '0');
  const remainder = (seconds % 60).toString().padStart(2, '0');
  return `${minutes}:${remainder}`;
}

function renderInstallationState() {
  if (!installationState) return;
  const expiresAt = Date.parse(installationState.expiresAt);
  const remaining = Number.isFinite(expiresAt)
    ? Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000))
    : 0;
  if (installationState.state === 'pending' && remaining === 0) {
    installationState.state = 'expired';
    installationState.command = '';
    installationState.link = '';
  }
  const runner = currentRunner(installationState.runnerId);
  const unavailable = installationState.state !== 'pending';
  q('runnerSecret').classList.toggle('expired', unavailable);
  q('runnerInstallStatus').textContent = enrollmentText(installationState.state);
  q('runnerInstallCountdown').textContent = installationState.state === 'pending'
    ? formatCountdown(remaining)
    : '--:--';
  q('runnerSecretValue').textContent = installationState.command || (
    installationState.state === 'revoked'
      ? '该 enrollment 已撤销，旧安装命令不可再使用。'
      : installationState.state === 'claimed'
        ? 'Runner 已领取长期凭据，一次性安装命令已从页面内存清除。'
        : '该安装命令已过期，请重新生成。'
  );
  q('runnerInstallLink').textContent = installationState.link || (
    installationState.state === 'revoked'
      ? '该安装链接已撤销。'
      : installationState.state === 'claimed'
        ? 'Runner 已完成 enrollment，一次性链接已从页面内存清除。'
        : '该安装链接已过期，请重新生成。'
  );
  q('copyRunnerCommand').disabled = unavailable || !installationState.command;
  q('copyRunnerLink').disabled = unavailable || !installationState.link;
  q('openRunnerLink').disabled = unavailable || !installationState.link;
  q('revokeRunnerEnrollment').disabled = unavailable;
  q('regenerateRunnerEnrollment').disabled = installationState.state === 'claimed'
    || Boolean(runner && !['pending', 'disabled'].includes(runner.admin_state));
}

function showInstallation(result) {
  const installation = result?.installation;
  const runner = result?.runner;
  if (!installation?.link || !installation?.command || !runner?.runner_id || !installation.expires_at) return false;
  if (installationTimer !== null) window.clearInterval(installationTimer);
  installationState = {
    runnerId: runner.runner_id,
    revision: runner.revision,
    link: installation.link,
    command: installation.command,
    expiresAt: installation.expires_at,
    runnerVersion: installation.runner_version,
    platform: installation.platform,
    arch: installation.arch,
    state: 'pending',
  };
  q('runnerSecret').classList.remove('hidden');
  q('runnerInstallPlatform').textContent = `${runnerPlatformText(installation.platform)} / ${installation.arch}`;
  q('runnerInstallVersion').textContent = installation.runner_version;
  setFeedback(q('runnerInstallFeedback'), '安装链接和终端命令只保留在当前页面内存中。');
  renderInstallationState();
  installationTimer = window.setInterval(renderInstallationState, 1000);
  q('runnerSecret').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  return true;
}

function closeInstallation() {
  if (installationTimer !== null) window.clearInterval(installationTimer);
  installationTimer = null;
  installationState = null;
  q('runnerInstallLink').textContent = '';
  q('runnerSecretValue').textContent = '';
  q('runnerSecret').classList.add('hidden');
  q('runnerSecret').classList.remove('expired');
  setFeedback(q('runnerInstallFeedback'), '');
}

function syncInstallationFromRunnerDoc() {
  if (!installationState) return;
  const runner = currentRunner(installationState.runnerId);
  if (!runner) {
    installationState.state = 'revoked';
    installationState.command = '';
    installationState.link = '';
    renderInstallationState();
    return;
  }
  installationState.revision = runner.revision;
  const state = runner.enrollment?.state;
  if (state && state !== installationState.state) {
    installationState.state = state;
    if (['claimed', 'revoked'].includes(state)) {
      installationState.command = '';
      installationState.link = '';
    }
  }
  renderInstallationState();
}

async function copyText(value) {
  if (!value) throw new Error('没有可复制的内容');
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch (_) {
      // Home Assistant Ingress may deny the async clipboard permission; use the bounded fallback.
    }
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.opacity = '0';
  document.body.append(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('浏览器拒绝复制，请手工选择命令');
}

function showCredentialRotation(credential, runnerId) {
  if (!credential?.secret) return;
  credentialState = `runner_id=${runnerId}\ncredential=${credential.secret}`;
  q('runnerCredentialValue').textContent = credentialState;
  q('runnerCredentialSecret').classList.remove('hidden');
  setFeedback(q('runnerCredentialFeedback'), '新凭据只显示一次，请立即安全保存。');
  q('runnerCredentialSecret').scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function closeCredentialRotation() {
  credentialState = null;
  q('runnerCredentialValue').textContent = '';
  q('runnerCredentialSecret').classList.add('hidden');
  setFeedback(q('runnerCredentialFeedback'), '');
}

async function refreshRunners() {
  if (!statusDoc?.runner_manager?.enabled) return;
  try {
    const document = await jsonFetch('api/runners');
    runnerDoc = document.result;
    const summary = runnerDoc.summary;
    q('runnerTotal').textContent = summary.total;
    q('runnerEnabled').textContent = summary.enabled;
    q('runnerOnline').textContent = summary.online;
    q('runnerBusy').textContent = summary.busy;
    q('runnerRecovery').textContent = summary.recovery_required;
    renderRunners();
    syncInstallationFromRunnerDoc();
  } catch (error) {
    setFeedback(q('runnerFeedback'), error.message, 'error');
  }
}

function syncRunnerConfigurationState() {
  const manager = statusDoc?.runner_manager;
  const relayReady = Boolean(manager?.relay_configured);
  const installer = manager?.installer;
  const installerReady = Boolean(installer?.ready);
  q('runnerRelayMissing').classList.toggle('hidden', relayReady);
  q('runnerInstallerMissing').classList.toggle('hidden', installerReady);
  q('createRunner').disabled = !Boolean(manager?.enabled) || !installerReady;
  if (!installerReady) {
    const code = installer?.error_code || 'installer_not_configured';
    q('runnerInstallerHelp').textContent = `安装命令入口已关闭（${code}）。请配置摘要固定的 HTTPS manifest 与公开 WSS Relay URL。`;
  }
}

function setStatusStreamState(kind, message) {
  const element = q('statusStreamState');
  element.className = `stream-state ${kind}`;
  element.textContent = message;
}

function scheduleCsrfRefresh() {
  window.clearTimeout(csrfRefreshTimer);
  csrfRefreshTimer = window.setTimeout(() => refresh(), 12 * 60 * 1000);
}

async function refresh(providedStatus = null) {
  try {
    const status = providedStatus || await jsonFetch('api/status');
    statusDoc = status;
    if (!providedStatus) {
      csrf = status.csrf_token;
      scheduleCsrfRefresh();
    }
    const account = status.app_server.account;
    const apiKeyMode = status.configured_auth_mode === 'api_key';
    q('ready').textContent = status.ready ? '就绪' : '未就绪';
    q('auth').textContent = account.auth_mode === 'apiKey'
      ? 'API Key'
      : account.auth_mode === 'chatgpt'
        ? 'ChatGPT'
        : '需要登录';
    q('queued').textContent = status.queue.jobs.queued;
    q('threadShort').textContent = status.queue.active_job?.thread_short || '无活动';
    q('login').hidden = apiKeyMode;
    q('cancel').hidden = apiKeyMode;
    q('retryApiKey').hidden = !apiKeyMode;
    q('authHelp').textContent = apiKeyMode
      ? '当前选择 API Key。URL、模型和 Key 只能在 Add-on options 中配置，页面不接收或显示秘密值。'
      : '当前选择 ChatGPT Device Code。HAOS 使用独立会话，不复制本机凭据。';
    apiKeyMode ? showApiKey(status) : showDeviceCode(status.pending_login);
    const runnerEnabled = Boolean(status.runner_manager?.enabled);
    q('runnerCenter').classList.toggle('hidden', !runnerEnabled);
    q('runnerDisabled').classList.toggle('hidden', runnerEnabled);
    q('details').textContent = `Controller ${status.version} · Codex ${status.codex_version} · intake ${status.intake_enabled ? '已启用' : '关闭'} · Runner Center ${runnerEnabled ? '已启用' : '关闭'} · Thread ${status.queue.threads} · 已知工具 ${status.tools.known} · 已配置 ${status.tools.configured} · 策略开启 ${status.tools.enabled} · MCP 心跳 ${status.tools.mcp.observed_at || '未观测'} · 策略错误 ${status.tools.policy_error || '无'} · app-server ${status.app_server.running ? '运行' : '停止'}`;
    syncRunnerConfigurationState();
    await refreshTools();
    if (runnerEnabled) await refreshRunners();
  } catch (error) {
    q('details').textContent = error.message;
    q('createRunner').disabled = true;
  }
}

function stopStatusStream() {
  if (statusStream) statusStream.close();
  statusStream = null;
}

function scheduleStatusReconnect(immediate = false) {
  stopStatusStream();
  window.clearTimeout(statusReconnectTimer);
  if (!navigator.onLine) {
    setStatusStreamState('bad', '网络已断开');
    return;
  }
  const delay = immediate ? 0 : statusReconnectDelay;
  if (!immediate) statusReconnectDelay = Math.min(15000, Math.round(statusReconnectDelay * 1.8));
  setStatusStreamState('warn', delay ? `连接中 · ${Math.ceil(delay / 1000)}秒` : '正在连接');
  statusReconnectTimer = window.setTimeout(connectStatusStream, delay);
}

function connectStatusStream() {
  window.clearTimeout(statusReconnectTimer);
  if (!navigator.onLine || document.visibilityState === 'hidden') return;
  stopStatusStream();
  setStatusStreamState('warn', '正在连接');
  const source = new EventSource('api/stream');
  statusStream = source;
  source.onopen = () => {
    if (source !== statusStream) return;
    statusReconnectDelay = 1000;
    statusLastMessageAt = Date.now();
    setStatusStreamState('good', '实时已连接');
  };
  const onFrame = event => {
    if (source !== statusStream) return;
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_) {
      scheduleStatusReconnect();
      return;
    }
    statusLastMessageAt = Date.now();
    statusReconnectDelay = 1000;
    setStatusStreamState('good', '实时已连接');
    if (event.type === 'status' && data.version === 1 && data.status) refresh(data.status);
  };
  source.addEventListener('status', onFrame);
  source.addEventListener('heartbeat', onFrame);
  source.onerror = () => {
    if (source !== statusStream) return;
    scheduleStatusReconnect();
  };
}

q('runnerForm').onsubmit = async event => {
  event.preventDefault();
  if (!statusDoc?.runner_manager?.installer?.ready) {
    setFeedback(q('runnerFeedback'), '安装制品尚未就绪，不能生成 enrollment。', 'error');
    return;
  }
  const labels = q('runnerLabels').value.split(',').map(value => value.trim()).filter(Boolean);
  const projects = q('runnerProjects').value.split(',').map(value => value.trim()).filter(Boolean);
  setFeedback(q('runnerFeedback'), '正在生成 15 分钟有效的安装命令…');
  q('createRunner').disabled = true;
  try {
    const result = await runnerMutation('POST', 'api/runner-enrollments', {
      display_name: q('runnerName').value.trim(),
      os: q('runnerOs').value,
      arch: q('runnerArch').value,
      labels,
      allowed_projects: projects,
      max_concurrency: 1,
      request_id: requestId(),
    });
    if (!showInstallation(result)) {
      throw new Error('安装命令不可再次显示，请使用 Runner 列表中的“重新生成”。');
    }
    setFeedback(q('runnerFeedback'), 'Runner 已创建为 pending；完成注册和自检后仍需人工启用。', 'success');
    await refreshRunners();
  } catch (error) {
    setFeedback(q('runnerFeedback'), error.message, 'error');
  } finally {
    syncRunnerConfigurationState();
  }
};

q('copyRunnerCommand').onclick = async () => {
  if (!installationState || installationState.state !== 'pending') return;
  try {
    await copyText(installationState.command);
    setFeedback(q('runnerInstallFeedback'), '安装命令已复制。', 'success');
  } catch (error) {
    setFeedback(q('runnerInstallFeedback'), error.message, 'error');
  }
};
q('copyRunnerLink').onclick = async () => {
  if (!installationState || installationState.state !== 'pending') return;
  try {
    await copyText(installationState.link);
    setFeedback(q('runnerInstallFeedback'), '一次性安装链接已复制。', 'success');
  } catch (error) {
    setFeedback(q('runnerInstallFeedback'), error.message, 'error');
  }
};
q('openRunnerLink').onclick = () => {
  if (!installationState || installationState.state !== 'pending') return;
  const opened = window.open(installationState.link, '_blank', 'noopener,noreferrer');
  if (opened) opened.opener = null;
  setFeedback(
    q('runnerInstallFeedback'),
    opened ? '已在新标签页打开安装脚本。' : '浏览器阻止了新窗口，请复制链接后打开。',
    opened ? 'success' : 'error',
  );
};
q('revokeRunnerEnrollment').onclick = () => {
  const runner = installationState ? currentRunner(installationState.runnerId) : null;
  if (runner) revokeEnrollment(runner);
};
q('regenerateRunnerEnrollment').onclick = () => {
  const runner = installationState ? currentRunner(installationState.runnerId) : null;
  if (runner) regenerateEnrollment(runner);
};
q('closeRunnerSecret').onclick = closeInstallation;
q('copyRunnerCredential').onclick = async () => {
  try {
    await copyText(credentialState);
    setFeedback(q('runnerCredentialFeedback'), '新凭据已复制。', 'success');
  } catch (error) {
    setFeedback(q('runnerCredentialFeedback'), error.message, 'error');
  }
};
q('closeRunnerCredential').onclick = closeCredentialRotation;
q('runnerStateFilter').onchange = renderRunners;
q('runnerPlatformFilter').onchange = renderRunners;
q('reloadRunners').onclick = refreshRunners;
q('serviceFilter').onchange = renderTools;
q('riskFilter').onchange = renderTools;
q('reloadTools').onclick = refresh;
q('login').onclick = async () => {
  try { await call('api/auth/device/start'); await refresh(); } catch (error) { alert(error.message); }
};
q('cancel').onclick = async () => {
  try { await call('api/auth/device/cancel'); await refresh(); } catch (error) { alert(error.message); }
};
q('retryApiKey').onclick = async () => {
  try { await call('api/auth/api-key/retry'); await refresh(); } catch (error) { alert(error.message); }
};
q('logout').onclick = async () => {
  if (!confirm('确认退出 HAOS Controller 的独立 Codex 会话？')) return;
  try { await call('api/auth/logout'); await refresh(); } catch (error) { alert(error.message); }
};

activateView({scroll: false});
refresh().finally(() => scheduleStatusReconnect(true));
window.addEventListener('hashchange', () => activateView());
window.setInterval(() => {
  if (statusStream && statusLastMessageAt && Date.now() - statusLastMessageAt > 16000) {
    scheduleStatusReconnect();
  }
}, 2000);
window.addEventListener('offline', () => scheduleStatusReconnect());
window.addEventListener('online', () => {
  refresh();
  scheduleStatusReconnect(true);
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    stopStatusStream();
    setStatusStreamState('warn', '页面在后台');
  } else {
    refresh();
    scheduleStatusReconnect(true);
  }
});
"""


__all__ = ["DASHBOARD_JS"]
