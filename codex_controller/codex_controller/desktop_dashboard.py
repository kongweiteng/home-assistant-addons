"""Responsive Home Assistant Ingress workbench for Codex Desktop takeover."""

DESKTOP_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Codex 桌面任务</title><style>
:root{color-scheme:dark;--bg:#0b1220;--surface:#121c2e;--surface-2:#17253a;--surface-3:#0f1929;--line:#263751;--line-strong:#375174;--text:#edf4ff;--muted:#91a4bd;--blue:#4f9cff;--blue-strong:#2374e1;--green:#42d392;--amber:#f1b84b;--red:#ef6b73;--shadow:0 18px 44px rgba(0,0,0,.22)}
*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;min-width:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select,textarea{font:inherit}button,a,input,select,textarea{outline-offset:3px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--blue)}button{min-height:42px;border:1px solid var(--line-strong);border-radius:10px;padding:9px 13px;background:var(--surface-2);color:var(--text);cursor:pointer}button:hover:not(:disabled){border-color:#5477a4}button:disabled{opacity:.48;cursor:not-allowed}button.primary{background:var(--blue-strong);border-color:var(--blue-strong)}button.danger{background:#8f3037;border-color:#a7434b}button.ghost{background:transparent}a{color:#8fc0ff;text-decoration:none}.shell{max-width:1600px;margin:auto;padding:18px}.topbar{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;margin-bottom:14px}.eyebrow{color:var(--blue);font-size:12px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.topbar h1{font-size:27px;line-height:1.15;margin:4px 0 5px}.subtitle{margin:0;color:var(--muted)}.top-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.connection{display:inline-flex;align-items:center;min-height:34px;border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:var(--muted);background:var(--surface)}.connection.good{color:var(--green);border-color:#28775d}.connection.warn{color:var(--amber);border-color:#8c6723}.connection.bad{color:#ff9ca2;border-color:#8f3d44}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}.metric{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:12px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;margin-top:4px;font-size:21px;line-height:1.2;overflow-wrap:anywhere}.workspace{display:grid;grid-template-columns:minmax(210px,.7fr) minmax(280px,1fr) minmax(0,1.7fr);gap:12px;align-items:start}.panel{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}.panel-head{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:13px 14px;border-bottom:1px solid var(--line);background:var(--surface-3)}.panel-head h2,.panel-head h3{font-size:15px;margin:0}.panel-body{padding:12px}.stack{display:grid;gap:9px}.muted{color:var(--muted)}.error{color:#ff9ca2}.success{color:#69deb3}.warning{color:#f6cb72}.badge-row{display:flex;flex-wrap:wrap;gap:6px}.badge{display:inline-flex;align-items:center;min-height:25px;border:1px solid #3a4c65;border-radius:999px;padding:2px 8px;color:#c7d4e5;font-size:12px}.badge.good{border-color:#28775d;color:#69deb3}.badge.warn{border-color:#8c6723;color:#f6cb72}.badge.bad{border-color:#8f3d44;color:#ff9ca2}.field{display:grid;gap:5px}.field label{font-size:12px;color:var(--muted)}select,input,textarea{width:100%;border:1px solid var(--line-strong);border-radius:10px;background:var(--surface-2);color:var(--text);padding:9px 11px}textarea{min-height:104px;resize:vertical}.project-list,.thread-list{display:grid;gap:7px}.project-button,.thread-button{width:100%;height:auto;text-align:left;background:transparent;border-color:transparent;padding:10px}.project-button:hover,.thread-button:hover{background:var(--surface-2)}.project-button.selected,.thread-button.selected{background:#172b48;border-color:#37669c}.project-title,.thread-title{font-weight:720;overflow-wrap:anywhere}.project-meta,.thread-meta{margin-top:4px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.project-counts,.thread-flags{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px}.count{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:2px 6px;color:var(--muted)}.filters{display:grid;grid-template-columns:1fr 1fr;gap:8px}.thread-scroll{max-height:calc(100vh - 310px);overflow:auto;padding:9px}.empty{padding:26px 16px;text-align:center;color:var(--muted)}.detail-empty{min-height:460px;display:grid;place-items:center;padding:30px;text-align:center}.detail-head{padding:15px;border-bottom:1px solid var(--line);background:var(--surface-3)}.detail-title-row{display:flex;gap:12px;justify-content:space-between;align-items:flex-start}.detail-head h2{font-size:20px;margin:0;overflow-wrap:anywhere}.detail-ref{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);overflow-wrap:anywhere}.detail-meta{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}.detail-actions{display:flex;flex-wrap:wrap;gap:8px;padding:12px 15px;border-bottom:1px solid var(--line)}.notice{margin:12px 15px 0;border-left:4px solid var(--blue);border-radius:8px;background:#101c2f;padding:10px 12px;color:var(--muted)}.notice.warn{border-color:var(--amber);background:#211b10;color:#f6cb72}.notice.bad{border-color:var(--red);background:#261417;color:#ff9ca2}.tabs{display:flex;gap:6px;padding:12px 15px 0}.tab{min-height:36px;padding:6px 10px;background:transparent}.tab[aria-pressed="true"]{background:var(--surface-2);border-color:#5277a5}.timeline,.live-feed{display:grid;gap:10px;padding:12px 15px 18px;max-height:calc(100vh - 440px);overflow:auto}.turn{border:1px solid var(--line);border-radius:12px;overflow:hidden}.turn-head{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:9px 11px;background:var(--surface-3);color:var(--muted);font-size:12px}.turn-items{display:grid;gap:8px;padding:10px}.item{min-width:0;border-left:3px solid var(--line-strong);padding:8px 10px;background:#101a2a;border-radius:7px}.item.user{border-color:var(--blue)}.item.assistant{border-color:var(--green)}.item.reasoning,.item.plan{border-color:var(--amber)}.item.command,.item.file{border-color:#8b74d8}.item-label{color:var(--muted);font-size:11px;font-weight:700;margin-bottom:4px}.item-text{white-space:pre-wrap;overflow-wrap:anywhere}.code-output{max-height:220px;margin:7px 0 0;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:8px;padding:9px;background:#09111e;color:#cde8d8;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}.change-list{display:grid;gap:4px;margin-top:5px}.change{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:#cbd8e8;overflow-wrap:anywhere}.live-event{border:1px solid var(--line);border-radius:10px;padding:10px;background:#101a2a}.live-event-head{display:flex;gap:8px;justify-content:space-between;color:var(--muted);font-size:11px}.live-event-body{margin-top:5px;white-space:pre-wrap;overflow-wrap:anywhere}.composer{position:sticky;bottom:0;border-top:1px solid var(--line);background:rgba(15,25,41,.97);backdrop-filter:blur(10px);padding:12px 15px calc(12px + env(safe-area-inset-bottom))}.mode-switch{display:flex;gap:6px;margin-bottom:8px}.mode-switch button{min-height:35px;padding:6px 10px;background:transparent}.mode-switch button[aria-pressed="true"]{background:#17355c;border-color:#4f86c9}.composer-actions{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:8px}.feedback{min-height:22px;margin-right:auto;color:var(--muted);font-size:12px}.hidden{display:none!important}.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:1050px){.workspace{grid-template-columns:220px minmax(280px,.95fr) minmax(0,1.4fr)}.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:820px){.shell{padding:12px}.topbar{align-items:stretch}.top-actions{justify-content:flex-start}.workspace{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.panel{box-shadow:none}.thread-scroll,.timeline,.live-feed{max-height:none}.detail-empty{min-height:220px}.project-panel .panel-body{padding:10px}.project-list{grid-template-columns:repeat(2,minmax(0,1fr))}.composer{position:sticky}.detail-title-row{display:block}.detail-ref{margin-top:6px}}
@media(max-width:520px){.shell{padding:10px}.topbar{display:grid}.topbar h1{font-size:24px}.top-actions{display:grid;grid-template-columns:1fr 1fr}.connection{grid-column:1/-1}.metrics{gap:7px}.metric{padding:10px}.metric strong{font-size:18px}.workspace{gap:9px}.project-list{grid-template-columns:1fr 1fr}.filters{grid-template-columns:1fr}.panel-head,.detail-head,.detail-actions,.tabs,.timeline,.live-feed,.composer{padding-left:11px;padding-right:11px}.detail-actions button{flex:1 1 calc(50% - 5px)}.composer-actions{align-items:stretch;flex-direction:column}.composer-actions button{width:100%}.feedback{margin:0}.mode-switch{display:grid;grid-template-columns:1fr 1fr}.mode-switch button{height:auto}.item{padding:8px}.top-actions a,.top-actions button{text-align:center}}
#detailContent{display:flex;flex-direction:column}.composer{position:relative;bottom:auto;order:4;border-bottom:1px solid var(--line)}.tabs{order:5}.timeline,.live-feed{order:6}
</style></head><body><main class="shell">
<header class="topbar"><div><div class="eyebrow">Codex Desktop Takeover</div><h1>桌面原任务工作台</h1><p class="subtitle">手机与 Mac 操作同一个 Thread；默认使用可对账的安全调整。</p></div><div class="top-actions"><span id="connectionState" class="connection">正在连接</span><a href="../" class="connection">控制器首页</a><button id="refreshAll" type="button">刷新</button></div></header>
<section class="metrics" aria-label="桌面任务概览"><div class="metric"><span>Mac 主机</span><strong id="metricHosts">0</strong></div><div class="metric"><span>项目</span><strong id="metricProjects">0</strong></div><div class="metric"><span>Thread</span><strong id="metricThreads">0</strong></div><div class="metric"><span>活动中</span><strong id="metricActive">0</strong></div><div class="metric"><span>需处理</span><strong id="metricRecovery">0</strong></div></section>
<section class="workspace">
<aside class="panel project-panel" aria-label="主机与项目"><div class="panel-head"><h2>主机与项目</h2><span id="hostWriteState" class="badge">只读</span></div><div class="panel-body stack"><div class="field"><label for="hostSelect">Mac 主机</label><select id="hostSelect" aria-label="选择 Mac 主机"></select></div><div id="hostMeta" class="muted">等待主机快照。</div><div id="projectList" class="project-list" aria-label="项目列表"></div></div></aside>
<section class="panel" aria-label="Thread 列表"><div class="panel-head"><h2>原 Thread</h2><span id="threadCount" class="badge">0</span></div><div class="panel-body stack"><div class="filters"><div class="field"><label for="statusFilter">状态</label><select id="statusFilter"><option value="all">全部</option><option value="active">活动中</option><option value="idle">空闲</option><option value="notLoaded">未加载</option><option value="failed">失败</option><option value="recovery_required">需恢复</option><option value="protocol_degraded">协议降级</option><option value="archived">已归档</option></select></div><div class="field"><label for="threadSearch">搜索标题</label><input id="threadSearch" maxlength="120" placeholder="输入任务标题"></div></div></div><div id="threadList" class="thread-list thread-scroll" aria-live="polite"></div></section>
<section class="panel" aria-label="Thread 详情"><div id="detailEmpty" class="detail-empty"><div><h2>选择一个原 Thread</h2><p class="muted">这里会显示公开历史、活动 Turn、实时事件和可用控制。</p></div></div><div id="detailContent" class="hidden"><div class="detail-head"><div class="detail-title-row"><div><h2 id="detailTitle">-</h2><div id="detailPreview" class="muted"></div></div><div id="detailRef" class="detail-ref"></div></div><div id="detailMeta" class="detail-meta"></div></div><div id="detailNotice" class="notice hidden"></div><div class="detail-actions"><button id="interruptButton" class="danger" type="button">停止当前 Turn</button><button id="archiveButton" type="button">归档</button><button id="unarchiveButton" type="button">恢复归档</button><button id="reloadThread" type="button">刷新详情</button></div><div class="tabs"><button id="historyTab" class="tab" type="button" aria-pressed="true">任务历史</button><button id="liveTab" class="tab" type="button" aria-pressed="false">实时活动</button></div><div id="historyView" class="timeline"></div><div id="liveView" class="live-feed hidden"></div><form id="composer" class="composer"><div class="mode-switch" aria-label="调整模式"><button id="safeMode" type="button" aria-pressed="true">安全调整</button><button id="nativeMode" type="button" aria-pressed="false">原生快速调整</button></div><label class="sr-only" for="composerInput">给当前原 Thread 的新指令</label><textarea id="composerInput" maxlength="12000" placeholder="输入新的方向、补充要求或继续任务内容"></textarea><div class="composer-actions"><span id="composerFeedback" class="feedback" role="status"></span><button id="submitDirection" class="primary" type="submit">发送</button></div></form></div></section>
</section></main><script src="desktop.js"></script></body></html>"""


DESKTOP_DASHBOARD_JS = r"""
const q = id => document.getElementById(id);
const API = '../api/desktop/v1';
const STATUS_API = '../api/status';
const SHANGHAI_TIME = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
const state = {csrf: '', hosts: [], projects: [], threads: [], selectedHost: '', selectedProject: 'all', selectedThread: '', detail: null, events: [], eventCursor: 0, eventGeneration: 0, eventController: null, mode: 'safe', view: 'history', loading: false};

function requestId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}

async function jsonFetch(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const document = await response.json();
  if (!response.ok) throw new Error(document.error?.message || document.error?.code || '请求失败');
  return document.result ?? document;
}

function delay(milliseconds) { return new Promise(resolve => setTimeout(resolve, milliseconds)); }
function text(value, fallback = '') { return typeof value === 'string' && value ? value : fallback; }
function number(value) { return Number.isFinite(Number(value)) ? Number(value) : 0; }
function formatTime(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : `${SHANGHAI_TIME.format(date)} +08:00`;
}

function badge(label, kind = '') {
  const node = document.createElement('span');
  node.className = `badge ${kind}`.trim();
  node.textContent = label;
  return node;
}

function statusText(value) {
  const labels = {active: '活动中', idle: '空闲', notLoaded: '未加载', archived: '已归档', failed: '失败', recovery_required: '需恢复', protocol_degraded: '协议降级', inProgress: '进行中', completed: '已完成', interrupted: '已中断'};
  return labels[value] || value || '未知';
}

function statusKind(value) {
  if (['active', 'idle', 'completed'].includes(value)) return 'good';
  if (['notLoaded', 'interrupted'].includes(value)) return 'warn';
  if (['failed', 'recovery_required', 'protocol_degraded'].includes(value)) return 'bad';
  return '';
}

function setConnection(label, kind = '') {
  q('connectionState').className = `connection ${kind}`.trim();
  q('connectionState').textContent = label;
}

function currentHost() { return state.hosts.find(host => host.host_ref === state.selectedHost) || null; }
function currentThread() { return state.threads.find(thread => thread.thread_ref === state.selectedThread) || state.detail; }

function renderMetrics() {
  q('metricHosts').textContent = state.hosts.length;
  q('metricProjects').textContent = state.projects.length;
  q('metricThreads').textContent = state.threads.length;
  q('metricActive').textContent = state.threads.filter(thread => thread.status === 'active').length;
  q('metricRecovery').textContent = state.threads.filter(thread => ['failed', 'recovery_required', 'protocol_degraded'].includes(thread.status)).length;
}

function renderHosts() {
  const select = q('hostSelect');
  select.replaceChildren();
  if (!state.hosts.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '没有可用 Mac 主机';
    select.append(option);
    select.disabled = true;
    q('hostMeta').textContent = '尚未收到 Desktop Adapter 快照；工作台保持只读空状态。';
    q('hostWriteState').className = 'badge bad';
    q('hostWriteState').textContent = '不可用';
    return;
  }
  select.disabled = false;
  for (const host of state.hosts) {
    const option = document.createElement('option');
    option.value = host.host_ref;
    option.textContent = `${host.online ? '在线' : '离线'} · ${host.app_version || '未知 App'} · ${host.host_ref}`;
    select.append(option);
  }
  select.value = state.selectedHost;
  const host = currentHost();
  if (!host) return;
  q('hostMeta').textContent = `App ${host.app_version || '未知'} (${host.app_build || '未知 build'}) · CLI ${host.cli_version || '未知'} · 同步 ${formatTime(host.synced_at)}`;
  q('hostWriteState').className = `badge ${host.write_available ? 'good' : host.online ? 'warn' : 'bad'}`;
  q('hostWriteState').textContent = host.write_available ? '可控制' : host.online ? '只读' : '离线';
}

function projectSummary(project) {
  const counts = project.counts || {};
  return `${number(counts.active)} 活动 · ${number(counts.idle)} 空闲 · ${number(counts.recovery_required) + number(counts.failed)} 需处理`;
}

function renderProjects() {
  const list = q('projectList');
  list.replaceChildren();
  const all = document.createElement('button');
  all.type = 'button';
  all.className = `project-button ${state.selectedProject === 'all' ? 'selected' : ''}`;
  all.setAttribute('aria-pressed', state.selectedProject === 'all' ? 'true' : 'false');
  const allTitle = document.createElement('div');
  allTitle.className = 'project-title';
  allTitle.textContent = '全部项目';
  const allMeta = document.createElement('div');
  allMeta.className = 'project-meta';
  allMeta.textContent = `${state.threads.length} 个 Thread`;
  all.append(allTitle, allMeta);
  all.onclick = () => selectProject('all');
  list.append(all);
  for (const project of state.projects) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `project-button ${state.selectedProject === project.project_ref ? 'selected' : ''}`;
    button.setAttribute('aria-pressed', state.selectedProject === project.project_ref ? 'true' : 'false');
    const title = document.createElement('div');
    title.className = 'project-title';
    title.textContent = project.project_alias;
    const meta = document.createElement('div');
    meta.className = 'project-meta';
    meta.textContent = projectSummary(project);
    button.append(title, meta);
    button.onclick = () => selectProject(project.project_ref);
    list.append(button);
  }
}

function filteredThreads() {
  const status = q('statusFilter').value;
  const query = q('threadSearch').value.trim().toLocaleLowerCase('zh-CN');
  return state.threads.filter(thread => {
    if (state.selectedProject !== 'all' && thread.project_ref !== state.selectedProject) return false;
    if (status !== 'all' && thread.status !== status) return false;
    return !query || text(thread.title).toLocaleLowerCase('zh-CN').includes(query);
  });
}

function renderThreads() {
  const list = q('threadList');
  list.replaceChildren();
  const threads = filteredThreads();
  q('threadCount').textContent = `${threads.length}`;
  for (const thread of threads) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `thread-button ${state.selectedThread === thread.thread_ref ? 'selected' : ''}`;
    button.setAttribute('aria-pressed', state.selectedThread === thread.thread_ref ? 'true' : 'false');
    const title = document.createElement('div');
    title.className = 'thread-title';
    title.textContent = text(thread.title, '未命名任务');
    const meta = document.createElement('div');
    meta.className = 'thread-meta';
    meta.textContent = `${formatTime(thread.updated_at)} · revision ${thread.thread_revision}`;
    const flags = document.createElement('div');
    flags.className = 'thread-flags';
    flags.append(badge(statusText(thread.status), statusKind(thread.status)), badge(thread.control_state || '未知控制状态'));
    button.append(title, meta, flags);
    button.onclick = () => selectThread(thread.thread_ref);
    list.append(button);
  }
  if (!threads.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = state.threads.length ? '当前筛选条件下没有 Thread。' : '尚未同步到原 Thread。';
    list.append(empty);
  }
}

function renderDetail() {
  const detail = state.detail;
  q('detailEmpty').classList.toggle('hidden', Boolean(detail));
  q('detailContent').classList.toggle('hidden', !detail);
  if (!detail) return;
  const snapshot = detail.snapshot || {};
  q('detailTitle').textContent = text(detail.title, '未命名任务');
  q('detailPreview').textContent = text(snapshot.preview, '没有公开摘要。');
  q('detailRef').textContent = `${detail.thread_ref} · revision ${detail.thread_revision}`;
  const meta = q('detailMeta');
  meta.replaceChildren(badge(statusText(detail.status), statusKind(detail.status)), badge(`控制 ${detail.control_state || '未知'}`), badge(`更新 ${formatTime(detail.updated_at)}`));
  if (detail.active_turn_ref) meta.append(badge(`活动 Turn ${detail.active_turn_ref}`, 'good'));
  if (snapshot.history_incomplete) meta.append(badge('历史可能截断', 'warn'));
  renderNotice(detail);
  renderActionState(detail);
  renderHistory(snapshot.turns || []);
  renderEvents();
  renderComposer(detail);
}

function renderNotice(detail) {
  const notice = q('detailNotice');
  notice.className = 'notice hidden';
  notice.textContent = '';
  if (detail.status === 'protocol_degraded') {
    notice.className = 'notice bad';
    notice.textContent = 'App/CLI/Schema 能力不匹配，当前只允许读取；升级兼容性重新核验前禁止控制。';
  } else if (detail.status === 'recovery_required' || detail.control_state === 'recovery_required') {
    notice.className = 'notice bad';
    notice.textContent = '控制结果或 Thread 映射需要恢复核对。请刷新 App 事实状态；系统不会自动重放命令。';
  } else if (state.mode === 'native' && detail.status === 'active') {
    notice.className = 'notice warn';
    notice.textContent = '原生快速调整保持同一个活动 Turn，但当前 App build 不强制 expected Turn。桌面同时切换方向时可能发生竞态；默认推荐安全调整。';
  } else if (detail.control_state === 'load_required') {
    notice.className = 'notice';
    notice.textContent = '该原 Thread 当前未由 App View 持有；继续时会先通过 deep link 加载同一个 threadId，再重新对账。';
  } else if (detail.latest_command && ['submitted', 'accepted', 'unknown'].includes(detail.latest_command.state)) {
    notice.className = detail.latest_command.state === 'unknown' ? 'notice bad' : 'notice warn';
    notice.textContent = detail.latest_command.state === 'unknown' ? '上一控制命令结果未知，已禁止新的写操作；请等待独立快照和收据对账。' : `控制命令 ${detail.latest_command.action} 正在等待 Runner 收据。`;
  }
}

function hostCapabilities() { return new Set(currentHost()?.capabilities || []); }
function hasCapability(value) { return hostCapabilities().has(value); }
function writeAvailable() { return currentHost()?.write_available === true; }

function renderActionState(detail) {
  const active = detail.status === 'active' && Boolean(detail.active_turn_ref);
  const blocked = !writeAvailable() || ['recovery_required', 'protocol_degraded'].includes(detail.status) || ['recovery_required', 'refresh_required'].includes(detail.control_state);
  q('interruptButton').classList.toggle('hidden', !active);
  q('interruptButton').disabled = blocked || !hasCapability('interrupt_expected_turn');
  q('archiveButton').classList.toggle('hidden', detail.status !== 'idle');
  q('archiveButton').disabled = blocked || !hasCapability('archive_control_v1');
  q('unarchiveButton').classList.toggle('hidden', detail.status !== 'archived');
  q('unarchiveButton').disabled = !writeAvailable() || !hasCapability('archive_control_v1');
}

function turnStatusLabel(turn) { return `${statusText(turn.status)} · ${formatTime(turn.started_at)}${turn.duration_ms ? ` · ${turn.duration_ms}ms` : ''}`; }

function renderHistory(turns) {
  const root = q('historyView');
  root.replaceChildren();
  for (const turn of turns) {
    const section = document.createElement('section');
    section.className = 'turn';
    const head = document.createElement('div');
    head.className = 'turn-head';
    const label = document.createElement('span');
    label.textContent = turnStatusLabel(turn);
    const ref = document.createElement('span');
    ref.textContent = turn.turn_ref || 'Turn';
    head.append(label, ref);
    const items = document.createElement('div');
    items.className = 'turn-items';
    for (const item of turn.items || []) items.append(renderItem(item));
    if (!(turn.items || []).length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = '该 Turn 没有可公开的历史项。';
      items.append(empty);
    }
    if (turn.items_incomplete) items.prepend(badge('该 Turn 历史不完整', 'warn'));
    section.append(head, items);
    root.append(section);
  }
  if (!turns.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = '尚无可公开的 Turn 历史。';
    root.append(empty);
  }
}

function itemMeta(label, kind = '') {
  const item = document.createElement('article');
  item.className = `item ${kind}`.trim();
  const title = document.createElement('div');
  title.className = 'item-label';
  title.textContent = label;
  item.append(title);
  return item;
}

function appendItemText(node, value) {
  const body = document.createElement('div');
  body.className = 'item-text';
  body.textContent = text(value, '无公开内容');
  node.append(body);
}

function renderItem(item) {
  const type = item?.type || 'other';
  if (type === 'user.message') { const node = itemMeta('用户输入', 'user'); appendItemText(node, item.text); return node; }
  if (type === 'assistant.message') { const node = itemMeta('Codex 回复', 'assistant'); appendItemText(node, item.text); return node; }
  if (type === 'reasoning.summary') { const node = itemMeta('公开推理摘要', 'reasoning'); appendItemText(node, item.text); return node; }
  if (type === 'plan') { const node = itemMeta('计划更新', 'plan'); appendItemText(node, item.text); return node; }
  if (type === 'command') {
    const node = itemMeta(`命令 · ${item.status || 'unknown'} · exit ${item.exit_code ?? 'unknown'}`, 'command');
    const summary = document.createElement('div');
    summary.className = 'muted';
    summary.textContent = `${number(item.output_bytes)} bytes${item.output_truncated ? ' · 已截断' : ''}`;
    const output = document.createElement('pre');
    output.className = 'code-output';
    output.textContent = text(item.output_excerpt, '没有公开输出');
    node.append(summary, output);
    return node;
  }
  if (type === 'file.change') {
    const node = itemMeta(`文件变化 · ${item.status || 'unknown'}`, 'file');
    const list = document.createElement('div');
    list.className = 'change-list';
    for (const change of item.changes || []) { const row = document.createElement('div'); row.className = 'change'; row.textContent = `${change.kind || 'unknown'} · ${change.relative_path || 'unknown'}`; list.append(row); }
    if (!list.children.length) appendItemText(node, '没有公开文件路径'); else node.append(list);
    return node;
  }
  if (type === 'tool.call') { const node = itemMeta('工具调用', 'command'); appendItemText(node, `${item.server || 'unknown'} · ${item.tool || 'unknown'} · ${item.status || 'unknown'}`); return node; }
  if (type === 'subagent.call') { const node = itemMeta('协作任务', 'command'); appendItemText(node, `${item.tool || 'unknown'} · ${item.status || 'unknown'} · ${number(item.receiver_count)} 个接收者`); return node; }
  if (type === 'web.search') { const node = itemMeta('网页搜索', 'command'); appendItemText(node, item.query); return node; }
  const node = itemMeta('任务事件');
  appendItemText(node, item.item_kind || type);
  return node;
}

function eventLabel(value) {
  const labels = {'thread.discovered': '发现 Thread', 'thread.updated': 'Thread 更新', 'thread.archived': 'Thread 已归档', 'turn.started': 'Turn 开始', 'turn.completed': 'Turn 完成', 'turn.interrupted': 'Turn 已中断', 'turn.failed': 'Turn 失败', 'user.message': '用户输入', 'assistant.delta': 'Codex 输出', 'assistant.completed': 'Codex 完成', 'plan.updated': '计划更新', 'command.started': '命令开始', 'command.output': '命令输出', 'command.completed': '命令完成', 'file.changed': '文件变化', 'file.patch': 'Diff 摘要', 'reasoning.summary': '公开推理摘要', 'awaiting.input': '等待输入', 'recovery.required': '需要恢复', 'protocol.degraded': '协议降级'};
  return labels[value] || value;
}

function eventSummary(event) {
  const payload = event.payload || {};
  if (typeof payload.text === 'string') return payload.text;
  if (typeof payload.summary === 'string') return payload.summary;
  if (typeof payload.title === 'string') return payload.title;
  if (typeof payload.status === 'string') return statusText(payload.status);
  const keys = Object.keys(payload).slice(0, 5);
  return keys.length ? keys.map(key => `${key}: ${String(payload[key]).slice(0, 180)}`).join('\n') : '状态已更新';
}

function renderEvents() {
  const root = q('liveView');
  root.replaceChildren();
  for (const event of [...state.events].reverse()) {
    const node = document.createElement('article');
    node.className = 'live-event';
    const head = document.createElement('div');
    head.className = 'live-event-head';
    const label = document.createElement('span');
    label.textContent = eventLabel(event.event_kind);
    const time = document.createElement('span');
    time.textContent = formatTime(event.created_at);
    head.append(label, time);
    const body = document.createElement('div');
    body.className = 'live-event-body';
    body.textContent = eventSummary(event);
    node.append(head, body);
    root.append(node);
  }
  if (!state.events.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = '尚无实时事件；保持此页面打开会自动等待新事件。';
    root.append(empty);
  }
}

function composerAction(detail) {
  if (detail.status === 'active') return 'steer';
  if (['idle', 'notLoaded', 'failed'].includes(detail.status)) return 'continue';
  return null;
}

function renderComposer(detail) {
  const action = composerAction(detail);
  const blockedCommand = detail.latest_command && ['pending', 'submitted', 'accepted', 'unknown'].includes(detail.latest_command.state);
  const capability = action === 'steer' ? (state.mode === 'native' ? hasCapability('native_steer_racy') : hasCapability('interrupt_expected_turn') && hasCapability('continue_same_thread')) : action === 'continue' ? hasCapability('continue_same_thread') : false;
  const enabled = Boolean(action && writeAvailable() && capability && !blockedCommand && !['recovery_required', 'protocol_degraded'].includes(detail.status) && ['ready', 'load_required'].includes(detail.control_state));
  q('composer').classList.toggle('hidden', !action);
  q('composerInput').disabled = !enabled;
  q('safeMode').disabled = detail.status !== 'active';
  q('nativeMode').disabled = detail.status !== 'active' || !hasCapability('native_steer_racy');
  q('submitDirection').disabled = !enabled;
  q('submitDirection').textContent = action === 'steer' ? (state.mode === 'native' ? '原生快速调整' : '安全调整方向') : detail.status === 'notLoaded' ? '加载并继续' : '继续此任务';
  q('composerInput').placeholder = action === 'steer' ? '输入立即替代当前方向的新要求' : '输入此原 Thread 的下一步要求';
}

function setMode(mode) {
  state.mode = mode;
  q('safeMode').setAttribute('aria-pressed', mode === 'safe' ? 'true' : 'false');
  q('nativeMode').setAttribute('aria-pressed', mode === 'native' ? 'true' : 'false');
  if (state.detail) renderDetail();
}

function setView(view) {
  state.view = view;
  q('historyTab').setAttribute('aria-pressed', view === 'history' ? 'true' : 'false');
  q('liveTab').setAttribute('aria-pressed', view === 'live' ? 'true' : 'false');
  q('historyView').classList.toggle('hidden', view !== 'history');
  q('liveView').classList.toggle('hidden', view !== 'live');
}

async function fetchAllThreads(hostRef) {
  const threads = [];
  let cursor = 0;
  for (let page = 0; page < 20; page += 1) {
    const document = await jsonFetch(`${API}/threads?host_ref=${encodeURIComponent(hostRef)}&cursor=${cursor}&limit=200`);
    threads.push(...(document.threads || []));
    if (!document.has_more) break;
    cursor = document.next_cursor;
  }
  return threads;
}

async function refreshOverview({preserveDetail = true} = {}) {
  if (state.loading) return;
  state.loading = true;
  setConnection('正在同步', 'warn');
  try {
    const status = await jsonFetch(STATUS_API);
    state.csrf = status.csrf_token;
    const hostsDocument = await jsonFetch(`${API}/hosts`);
    state.hosts = hostsDocument.hosts || [];
    if (!state.selectedHost || !state.hosts.some(host => host.host_ref === state.selectedHost)) state.selectedHost = state.hosts[0]?.host_ref || '';
    if (state.selectedHost) {
      const [projectsDocument, threads] = await Promise.all([jsonFetch(`${API}/projects?host_ref=${encodeURIComponent(state.selectedHost)}`), fetchAllThreads(state.selectedHost)]);
      state.projects = projectsDocument.projects || [];
      state.threads = threads;
    } else {
      state.projects = [];
      state.threads = [];
    }
    if (state.selectedProject !== 'all' && !state.projects.some(project => project.project_ref === state.selectedProject)) state.selectedProject = 'all';
    if (state.selectedThread && !state.threads.some(thread => thread.thread_ref === state.selectedThread)) {
      state.selectedThread = '';
      state.detail = null;
      stopEventStream();
    }
    renderHosts();
    renderProjects();
    renderThreads();
    renderMetrics();
    if (preserveDetail && state.selectedThread) await loadThread(state.selectedThread, {restartStream: false}); else renderDetail();
    setConnection(state.hosts.some(host => host.online) ? '实时同步' : '主机离线', state.hosts.some(host => host.online) ? 'good' : 'bad');
  } catch (error) {
    setConnection('同步失败', 'bad');
    q('hostMeta').textContent = error.message;
  } finally {
    state.loading = false;
  }
}

async function selectProject(projectRef) {
  state.selectedProject = projectRef;
  renderProjects();
  renderThreads();
}

async function selectThread(threadRef) {
  state.selectedThread = threadRef;
  state.events = [];
  state.eventCursor = 0;
  renderThreads();
  await loadThread(threadRef, {restartStream: true, initialEvents: true});
}

async function loadThread(threadRef, {restartStream = false, initialEvents = false} = {}) {
  try {
    state.detail = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}`);
    if (initialEvents) {
      const eventDocument = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}/events?after_cursor=0&limit=500&wait_seconds=0`);
      state.events = eventDocument.events || [];
      state.eventCursor = eventDocument.next_cursor || 0;
    }
    renderDetail();
    if (restartStream) startEventStream();
  } catch (error) {
    q('composerFeedback').className = 'feedback error';
    q('composerFeedback').textContent = error.message;
  }
}

function stopEventStream() {
  state.eventGeneration += 1;
  if (state.eventController) state.eventController.abort();
  state.eventController = null;
}

function startEventStream() {
  stopEventStream();
  const generation = state.eventGeneration;
  const threadRef = state.selectedThread;
  if (!threadRef) return;
  void eventLoop(threadRef, generation);
}

async function eventLoop(threadRef, generation) {
  let backoff = 1000;
  while (generation === state.eventGeneration && threadRef === state.selectedThread) {
    state.eventController = new AbortController();
    try {
      const document = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}/events?after_cursor=${state.eventCursor}&limit=200&wait_seconds=20`, {signal: state.eventController.signal});
      if (generation !== state.eventGeneration) return;
      if (document.events?.length) {
        state.events.push(...document.events);
        state.events = state.events.slice(-500);
        state.eventCursor = document.next_cursor;
        renderEvents();
        await loadThread(threadRef, {restartStream: false});
      }
      setConnection('实时同步', 'good');
      backoff = 1000;
    } catch (error) {
      if (error.name === 'AbortError' || generation !== state.eventGeneration) return;
      setConnection('弱网重连中', 'warn');
      await delay(backoff);
      backoff = Math.min(backoff * 2, 15000);
    }
  }
}

async function submitAction(action, extra = {}) {
  const detail = state.detail;
  if (!detail) return;
  const body = {request_id: requestId(), thread_revision: detail.thread_revision, ...extra};
  q('composerFeedback').className = 'feedback muted';
  q('composerFeedback').textContent = '正在发送受控命令…';
  try {
    const result = await jsonFetch(`${API}/threads/${encodeURIComponent(detail.thread_ref)}/${action}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    q('composerFeedback').className = 'feedback success';
    q('composerFeedback').textContent = result.state === 'submitted' ? '命令已提交，等待 Mac 收据。' : `命令状态：${result.state}`;
    q('composerInput').value = '';
    await loadThread(detail.thread_ref, {restartStream: false});
    setTimeout(() => void loadThread(detail.thread_ref, {restartStream: false}), 1200);
  } catch (error) {
    q('composerFeedback').className = 'feedback error';
    q('composerFeedback').textContent = error.message;
    await loadThread(detail.thread_ref, {restartStream: false});
  }
}

q('hostSelect').onchange = async () => { state.selectedHost = q('hostSelect').value; state.selectedProject = 'all'; state.selectedThread = ''; state.detail = null; stopEventStream(); await refreshOverview({preserveDetail: false}); };
q('statusFilter').onchange = renderThreads;
q('threadSearch').oninput = renderThreads;
q('refreshAll').onclick = () => refreshOverview();
q('reloadThread').onclick = () => state.selectedThread && loadThread(state.selectedThread, {restartStream: false});
q('safeMode').onclick = () => setMode('safe');
q('nativeMode').onclick = () => setMode('native');
q('historyTab').onclick = () => setView('history');
q('liveTab').onclick = () => setView('live');
q('interruptButton').onclick = () => state.detail && submitAction('interrupt', {expected_turn_ref: state.detail.active_turn_ref});
q('archiveButton').onclick = () => { if (state.detail && confirm(`归档“${state.detail.title}”？只会移动同一个原 Thread，不会删除或复制。`)) void submitAction('archive'); };
q('unarchiveButton').onclick = () => state.detail && submitAction('unarchive');
q('composer').onsubmit = event => {
  event.preventDefault();
  const detail = state.detail;
  const input = q('composerInput').value.trim();
  const action = detail ? composerAction(detail) : null;
  if (!detail || !action || !input) { q('composerFeedback').className = 'feedback warning'; q('composerFeedback').textContent = '请输入要发送的新方向。'; return; }
  if (action === 'steer') void submitAction('steer', {expected_turn_ref: detail.active_turn_ref, input, mode: state.mode});
  else void submitAction('continue', {input});
};
window.addEventListener('online', () => { setConnection('网络已恢复', 'good'); void refreshOverview(); });
window.addEventListener('offline', () => setConnection('手机网络离线', 'bad'));
window.addEventListener('beforeunload', stopEventStream);

setView('history');
void refreshOverview({preserveDetail: false});
setInterval(() => void refreshOverview(), 15000);
"""


__all__ = ["DESKTOP_DASHBOARD_HTML", "DESKTOP_DASHBOARD_JS"]
