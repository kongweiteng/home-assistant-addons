"""Responsive Home Assistant Ingress workbench for Codex Desktop takeover."""

DESKTOP_DASHBOARD_HTML_LEGACY = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="only light"><meta name="supported-color-schemes" content="light"><meta name="theme-color" content="#f7f7f5">
<title>Codex 控制器</title><style>
:root{color-scheme:light;--bg:#f7f7f5;--surface:#fff;--surface-2:#f0f0ed;--surface-3:#fafaf8;--line:rgba(30,32,36,.11);--line-strong:rgba(30,32,36,.18);--text:#202124;--muted:#73767b;--blue:#3768e5;--blue-soft:#e9eefc;--green:#1a8b67;--green-soft:#e8f4ef;--amber:#a96e16;--amber-soft:#f8efdf;--red:#bd384d;--red-soft:#f9eaed;--shadow:0 1px 3px rgba(20,23,27,.05),0 14px 36px rgba(20,23,27,.055)}
*{box-sizing:border-box}html{background:var(--bg);scrollbar-gutter:stable}body{margin:0;min-width:320px;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased}button,input,select,textarea{font:inherit;color:inherit}button,a,input,select,textarea{outline-offset:3px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--blue)}button{min-height:44px;border:1px solid var(--line-strong);border-radius:11px;padding:9px 13px;background:var(--surface);cursor:pointer;transition:background .15s,border-color .15s,transform .15s}button:hover:not(:disabled){background:var(--surface-2)}button:active:not(:disabled){transform:scale(.985)}button:disabled{opacity:.45;cursor:not-allowed}button.primary{border-color:#222326;background:#222326;color:#fff}button.primary:hover:not(:disabled){background:#383a3e}button.danger{border-color:#efd0d7;background:var(--red-soft);color:var(--red)}button.ghost{background:transparent}a{color:var(--blue);text-decoration:none}.shell{max-width:1600px;margin:auto;padding:18px}.topbar{display:flex;gap:18px;align-items:center;justify-content:space-between;margin-bottom:14px}.brand{display:flex;align-items:center;gap:12px;min-width:0}.brand-mark{width:38px;height:38px;display:grid;place-items:center;border-radius:12px;background:#222326;color:#fff;font-weight:760}.eyebrow{color:var(--muted);font-size:11px;font-weight:620;letter-spacing:.02em}.topbar h1{font-size:27px;line-height:1.15;letter-spacing:-.035em;margin:2px 0}.subtitle{margin:0;color:var(--muted);font-size:13px}.top-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.connection{display:inline-flex;align-items:center;min-height:36px;border:1px solid var(--line);border-radius:999px;padding:6px 11px;color:var(--muted);background:var(--surface)}.connection.good{color:var(--green);background:var(--green-soft)}.connection.warn{color:var(--amber);background:var(--amber-soft)}.connection.bad{color:var(--red);background:var(--red-soft)}.runner-banner{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px;padding:13px 15px;border:1px solid var(--line);border-radius:14px;background:var(--surface);box-shadow:0 1px 2px rgba(20,23,27,.035)}.runner-banner strong{display:block}.runner-banner span{display:block;margin-top:2px;color:var(--muted);font-size:12px}.runner-banner.bad{border-color:#ead8b9}.runner-banner.bad strong{color:#5d4a2c}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}.metric{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:12px 14px}.metric span{display:block;color:var(--muted);font-size:12px}.metric strong{display:block;margin-top:4px;font-size:21px;line-height:1.2;overflow-wrap:anywhere}.workspace{display:grid;grid-template-columns:minmax(220px,.72fr) minmax(300px,1fr) minmax(0,1.85fr);gap:12px;align-items:start}.panel{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow);overflow:hidden}.panel-head{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:13px 14px;border-bottom:1px solid var(--line);background:var(--surface-3)}.panel-head h2,.panel-head h3{font-size:15px;margin:0}.project-panel .panel-head button{display:none}.panel-body{padding:12px}.stack{display:grid;gap:10px}.muted{color:var(--muted)}.error{color:var(--red)}.success{color:var(--green)}.warning{color:var(--amber)}.badge-row,.detail-meta{display:flex;flex-wrap:wrap;gap:6px}.badge{display:inline-flex;align-items:center;min-height:25px;border:0;border-radius:999px;padding:3px 8px;background:#eeefec;color:#666a70;font-size:11px}.badge.good{background:var(--green-soft);color:var(--green)}.badge.warn{background:var(--amber-soft);color:var(--amber)}.badge.bad{background:var(--red-soft);color:var(--red)}.field{display:grid;gap:5px}.field label{font-size:12px;color:var(--muted)}select,input,textarea{width:100%;border:1px solid var(--line-strong);border-radius:11px;background:var(--surface);padding:10px 11px}textarea{min-height:100px;resize:vertical}.project-list,.thread-list{display:grid}.project-button,.thread-button{width:100%;height:auto;text-align:left;background:transparent;border-color:transparent;border-radius:10px;padding:11px}.project-button:hover,.thread-button:hover{background:var(--surface-2)}.project-button.selected{background:var(--surface-2)}.thread-button{min-height:84px;border-radius:0;border-bottom:1px solid var(--line)}.thread-button.selected{background:#f6f7f4;box-shadow:inset 2px 0 0 var(--blue)}.project-title,.thread-title{font-weight:700;overflow-wrap:anywhere}.project-meta,.thread-meta{margin-top:4px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.thread-flags{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px}.filters{display:grid;grid-template-columns:1fr 1.35fr;gap:8px}.thread-scroll{max-height:calc(100vh - 322px);overflow:auto;padding:0 10px}.empty{padding:30px 16px;text-align:center;color:var(--muted)}.detail-empty{min-height:510px;display:grid;place-items:center;padding:30px;text-align:center}.detail-empty h2{margin:0 0 6px;font-size:20px}.detail-head{padding:15px 16px;border-bottom:1px solid var(--line);background:var(--surface)}.detail-title-row{display:flex;gap:12px;justify-content:space-between;align-items:flex-start}.detail-head h2{font-size:20px;line-height:1.28;letter-spacing:-.02em;margin:0;overflow-wrap:anywhere}.detail-ref{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);overflow-wrap:anywhere}.detail-meta{margin-top:9px}.detail-actions{display:flex;flex-wrap:wrap;gap:8px;padding:10px 15px;border-bottom:1px solid var(--line)}.detail-back{display:none}.notice{margin:12px 15px 0;border-left:3px solid var(--blue);border-radius:6px 11px 11px 6px;background:var(--blue-soft);padding:10px 12px;color:#4c5d8d}.notice.warn{border-color:var(--amber);background:var(--amber-soft);color:#815b20}.notice.bad{border-color:var(--red);background:var(--red-soft);color:#9e3344}.tabs{display:flex;gap:2px;padding:11px 15px 0;border-bottom:1px solid var(--line)}.tab{min-height:39px;padding:7px 11px;border:0;border-bottom:2px solid transparent;border-radius:7px 7px 0 0;background:transparent;color:var(--muted)}.tab[aria-pressed="true"]{border-bottom-color:var(--text);color:var(--text);font-weight:650}.timeline,.live-feed{display:grid;gap:10px;padding:14px 15px 18px;max-height:calc(100vh - 430px);overflow:auto}.turn{border:1px solid var(--line);border-radius:13px;overflow:hidden}.turn-head{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:9px 11px;background:var(--surface-3);color:var(--muted);font-size:12px}.turn-items{display:grid;gap:8px;padding:10px}.item{min-width:0;border-left:2px solid var(--line-strong);padding:9px 11px;background:#fafaf8;border-radius:7px}.item.user{border-color:var(--blue)}.item.assistant{border-color:var(--green)}.item.reasoning,.item.plan{border-color:var(--amber)}.item.command,.item.file{border-color:#7a6ca9}.item-label{color:var(--muted);font-size:11px;font-weight:700;margin-bottom:4px}.item-text{white-space:pre-wrap;overflow-wrap:anywhere}.code-output{max-height:220px;margin:7px 0 0;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:8px;padding:9px;background:#f0f1ee;color:#33423b;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}.change-list{display:grid;gap:4px;margin-top:5px}.change{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:#555b64;overflow-wrap:anywhere}.live-event{border:1px solid var(--line);border-radius:11px;padding:11px;background:#fafaf8}.live-event-head{display:flex;gap:8px;justify-content:space-between;color:var(--muted);font-size:11px}.live-event-body{margin-top:5px;white-space:pre-wrap;overflow-wrap:anywhere}.composer{border-top:1px solid var(--line);background:rgba(255,255,255,.97);padding:10px 15px calc(12px + env(safe-area-inset-bottom))}.mode-switch{display:flex;gap:5px;margin-bottom:8px}.mode-switch button{min-height:36px;padding:6px 10px;background:transparent}.mode-switch button[aria-pressed="true"]{border-color:#222326;background:#222326;color:#fff}.composer-actions{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:8px}.feedback{min-height:22px;margin-right:auto;color:var(--muted);font-size:12px}.model-field{margin-bottom:8px}.model-meta{min-height:18px;font-size:11px;color:var(--muted)}.mobile-nav{display:none}.sheet-backdrop{display:none}.new-task-sheet{position:fixed;z-index:90;left:50%;top:50%;width:min(520px,calc(100% - 32px));max-height:calc(100dvh - 32px);overflow:auto;transform:translate(-50%,-50%);border:1px solid var(--line);border-radius:20px;background:var(--surface);box-shadow:0 24px 80px rgba(22,25,30,.2);padding:8px 18px 20px}.sheet-handle{display:none;width:38px;height:4px;border-radius:4px;background:#c7c9cc;margin:0 auto 6px}.sheet-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.sheet-head h2{margin:9px 0;font-size:20px}.sheet-copy{margin:0 0 15px;color:var(--muted);font-size:13px}.new-task-form{display:grid;gap:13px}.new-task-form textarea{min-height:126px}.sheet-actions{display:flex;gap:8px;justify-content:flex-end}.sheet-actions .primary{min-width:130px}.modal-backdrop{position:fixed;z-index:80;inset:0;background:rgba(25,27,30,.25)}.hidden{display:none!important}.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:1050px){.workspace{grid-template-columns:210px minmax(270px,.9fr) minmax(0,1.4fr)}.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:820px){body{padding-bottom:calc(76px + env(safe-area-inset-bottom))}.shell{padding:0 14px 18px}.topbar{min-height:82px;margin:0;padding-top:env(safe-area-inset-top)}.brand-mark,.subtitle,.top-actions a{display:none}.topbar h1{font-size:25px}.top-actions{flex-wrap:nowrap}.top-actions .connection{display:none}.top-actions button{min-width:44px}.runner-banner{margin:4px 0 14px}.runner-banner span{max-width:230px}.metrics{display:none}.workspace{display:block}.panel{box-shadow:none}.project-panel{display:none;position:fixed;z-index:90;left:10px;right:10px;bottom:10px;max-height:76dvh;overflow:auto;border-radius:20px}.project-panel.open{display:block}.project-panel .panel-head button{display:inline-flex}.project-panel.open+.thread-panel{pointer-events:none}.thread-scroll{max-height:none;padding:0}.thread-panel{border:0;background:transparent}.thread-panel .panel-head{padding-left:4px;padding-right:4px;background:transparent}.thread-panel .panel-body{padding:8px 0}.thread-button{padding-left:12px;padding-right:4px}.thread-button.active-thread{box-shadow:inset 2px 0 0 var(--blue)}.detail-panel{display:none;position:fixed;z-index:40;inset:0;background:var(--bg);border:0;border-radius:0;overflow:auto}.detail-open .detail-panel{display:block}.detail-open .shell{padding:0}.detail-open .topbar,.detail-open .runner-banner,.detail-open .metrics,.detail-open .project-panel,.detail-open .thread-panel,.detail-open .mobile-nav{display:none}.detail-empty{min-height:60dvh}.detail-head{position:sticky;z-index:3;top:0;padding:calc(10px + env(safe-area-inset-top)) 14px 12px;background:rgba(247,247,245,.96);backdrop-filter:blur(18px)}.detail-title-row{display:grid;grid-template-columns:54px minmax(0,1fr);align-items:start}.detail-back{display:inline-flex;grid-row:1/3;min-width:54px;padding:0 8px;align-items:center;justify-content:center;background:transparent}.detail-ref{margin-top:4px}.detail-actions{padding:9px 14px}.tabs{position:sticky;z-index:2;top:91px;padding:8px 14px 0;background:rgba(247,247,245,.97);backdrop-filter:blur(18px)}.timeline,.live-feed{max-height:none;padding:14px 14px 260px}.composer{position:fixed;z-index:5;left:10px;right:10px;bottom:10px;border:1px solid var(--line);border-radius:20px;background:rgba(255,255,255,.97);box-shadow:0 10px 32px rgba(25,28,32,.13);backdrop-filter:blur(18px);padding:6px 8px calc(8px + env(safe-area-inset-bottom))}.composer textarea{min-height:82px;max-height:150px;border-color:transparent;background:var(--bg)}.composer .model-field{display:grid;grid-template-columns:1fr;gap:3px}.composer .model-field label{display:none}.mode-switch{overflow:auto;margin-bottom:5px}.mode-switch button{white-space:nowrap}.composer-actions{margin-top:6px}.composer-actions .primary{min-width:118px}.feedback{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.mobile-nav{position:fixed;z-index:30;left:12px;right:12px;bottom:10px;height:calc(66px + env(safe-area-inset-bottom));display:grid;grid-template-columns:repeat(5,1fr);padding:4px 7px env(safe-area-inset-bottom);border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.95);box-shadow:0 8px 28px rgba(25,28,32,.11);backdrop-filter:blur(18px)}.mobile-nav button,.mobile-nav a{min-height:56px;border:0;border-radius:12px;background:transparent;color:var(--muted);display:flex;align-items:center;justify-content:center;font-size:12px}.mobile-nav .primary-nav{margin-top:-16px;height:58px;align-self:start;background:#222326;color:#fff;box-shadow:0 6px 16px rgba(0,0,0,.18)}.new-task-sheet{left:10px;right:10px;top:auto;bottom:0;width:auto;max-height:88dvh;transform:none;border-radius:22px 22px 0 0;padding-bottom:calc(18px + env(safe-area-inset-bottom))}.sheet-handle{display:block}.sheet-actions{display:grid;grid-template-columns:1fr 1.4fr}.sheet-actions button{width:100%}}
@media(max-width:520px){.shell{padding-left:10px;padding-right:10px}.runner-banner{padding:12px}.runner-banner button{padding-left:8px;padding-right:8px}.filters{grid-template-columns:1fr 1.25fr}.panel-head,.detail-head,.detail-actions,.tabs,.timeline,.live-feed{padding-left:10px;padding-right:10px}.detail-actions button{flex:1 1 calc(50% - 5px)}.detail-ref{display:none}.turn-head{align-items:flex-start;flex-direction:column}.item{padding:8px}.new-task-sheet{left:0;right:0}.sheet-actions{grid-template-columns:1fr}}
</style></head><body><main class="shell">
<header class="topbar"><div class="brand"><span class="brand-mark">C</span><div><div class="eyebrow">远程任务中心</div><h1>Codex 控制器</h1><p class="subtitle">浏览、继续与调整 Mac 上的同一个任务</p></div></div><div class="top-actions"><span id="connectionState" class="connection">正在连接</span><a href="../" class="connection">控制器设置</a><button id="newTaskButton" class="primary" type="button">新建任务</button><button id="refreshAll" type="button">刷新</button></div></header>
<section id="runnerBanner" class="runner-banner"><div><strong id="runnerBannerTitle">正在连接 Mac Runner</strong><span id="runnerBannerText">正在读取任务与连接状态。</span></div><button id="checkConnection" type="button">检查连接</button></section>
<section class="metrics" aria-label="任务概览"><div class="metric"><span>Mac 主机</span><strong id="metricHosts">0</strong></div><div class="metric"><span>项目</span><strong id="metricProjects">0</strong></div><div class="metric"><span>任务</span><strong id="metricThreads">0</strong></div><div class="metric"><span>进行中</span><strong id="metricActive">0</strong></div><div class="metric"><span>需处理</span><strong id="metricRecovery">0</strong></div></section>
<section class="workspace">
<aside id="projectPanel" class="panel project-panel" aria-label="主机与项目"><div class="panel-head"><h2>主机与项目</h2><button id="closeProjects" class="ghost" type="button">完成</button></div><div class="panel-body stack"><div class="field"><label for="hostSelect">Mac 主机</label><select id="hostSelect" aria-label="选择 Mac 主机"></select></div><div class="badge-row"><span id="hostWriteState" class="badge">只读</span></div><div id="hostMeta" class="muted">等待主机快照。</div><div id="projectList" class="project-list" aria-label="项目列表"></div></div></aside>
<section class="panel thread-panel" aria-label="任务列表"><div class="panel-head"><h2>现在正在做</h2><span id="threadCount" class="badge">0</span></div><div class="panel-body stack"><div class="filters"><div class="field"><label for="statusFilter">状态</label><select id="statusFilter"><option value="all">全部状态</option><option value="active">活动中</option><option value="idle">空闲</option><option value="notLoaded">未加载</option><option value="failed">失败</option><option value="recovery_required">需恢复</option><option value="protocol_degraded">协议降级</option><option value="archived">已归档</option></select></div><div class="field"><label for="threadSearch">搜索任务</label><input id="threadSearch" maxlength="120" inputmode="search" placeholder="搜索标题"></div></div></div><div id="threadList" class="thread-list thread-scroll" aria-live="polite"></div></section>
<section class="panel detail-panel" aria-label="任务详情"><div id="detailEmpty" class="detail-empty"><div><h2>选择一个任务</h2><p class="muted">查看公开历史、实时活动和可用控制。</p></div></div><div id="detailContent" class="hidden"><div class="detail-head"><div class="detail-title-row"><button id="detailBack" class="detail-back" type="button" aria-label="返回任务列表">返回</button><div><div id="detailProject" class="eyebrow">当前项目</div><h2 id="detailTitle">-</h2><div id="detailPreview" class="muted"></div></div><div id="detailRef" class="detail-ref"></div></div><div id="detailMeta" class="detail-meta"></div></div><div id="detailNotice" class="notice hidden"></div><div class="detail-actions"><button id="interruptButton" class="danger" type="button">中断当前 Turn</button><button id="archiveButton" type="button">归档</button><button id="unarchiveButton" type="button">恢复归档</button><button id="reloadThread" type="button">刷新详情</button></div><div class="tabs"><button id="historyTab" class="tab" type="button" aria-pressed="true">任务历史</button><button id="liveTab" class="tab" type="button" aria-pressed="false">实时活动</button></div><div id="historyView" class="timeline"></div><div id="liveView" class="live-feed hidden"></div><form id="composer" class="composer"><div class="mode-switch" aria-label="调整模式"><button id="safeMode" type="button" aria-pressed="true">安全调整</button><button id="nativeMode" type="button" aria-pressed="false">原生快速调整</button></div><div id="modelField" class="field model-field"><label for="modelSelect">运行模型</label><select id="modelSelect" aria-describedby="modelMeta"></select><div id="modelMeta" class="model-meta"></div></div><label class="sr-only" for="composerInput">给当前任务的新指令</label><textarea id="composerInput" maxlength="12000" placeholder="调整当前方向"></textarea><div class="composer-actions"><span id="composerFeedback" class="feedback" role="status"></span><button id="submitDirection" class="primary" type="submit">调整方向</button></div></form></div></section>
</section></main>
<nav class="mobile-nav" aria-label="移动端导航"><a href="./">任务</a><button id="mobileProjects" type="button">项目</button><button id="mobileNewTask" class="primary-nav" type="button">新建</button><button id="mobileRefresh" type="button">刷新</button><a href="../">设置</a></nav>
<div id="modalBackdrop" class="modal-backdrop hidden"></div><section id="newTaskSheet" class="new-task-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="newTaskTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="newTaskTitle">新建任务</h2><button id="closeNewTask" class="ghost" type="button">关闭</button></div><p class="sheet-copy">任务会在 Mac 与手机的同一列表中出现。连接未确认时不会发送。</p><form id="newTaskForm" class="new-task-form"><div class="field"><label for="newTaskProject">项目</label><select id="newTaskProject" required></select></div><div class="field"><label for="newTaskInput">任务要求</label><textarea id="newTaskInput" maxlength="12000" required placeholder="描述希望 Codex 完成的任务"></textarea></div><div class="field"><label for="newTaskModel">模型</label><select id="newTaskModel"></select></div><div id="newTaskFeedback" class="feedback" role="status">正在检查 Runner 创建能力。</div><div class="sheet-actions"><button id="cancelNewTask" type="button">取消</button><button id="createTaskButton" class="primary" type="submit" disabled>创建并打开</button></div></form></section>
<script src="desktop.js"></script></body></html>"""


DESKTOP_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="only light"><meta name="supported-color-schemes" content="light"><meta name="theme-color" content="#f7f7f5">
<title>Codex 控制器</title><style>
:root{color-scheme:only light;--bg:#f7f7f5;--surface:#fff;--surface-2:#f1f1ee;--surface-3:#fbfbfa;--line:rgba(31,32,35,.1);--line-strong:rgba(31,32,35,.17);--text:#202124;--muted:#777a7f;--blue:#3f68da;--blue-soft:#edf1fc;--green:#218461;--green-soft:#eaf4ef;--amber:#9b681d;--amber-soft:#f8f0e3;--red:#b43d50;--red-soft:#f9ebee;--shadow:0 1px 2px rgba(20,22,26,.035),0 12px 32px rgba(20,22,26,.05)}
@media(max-width:920px){body .delivery{grid-template-columns:repeat(2,minmax(0,1fr));margin-inline:10px}body .delivery-stage{padding-inline:6px;font-size:10px}body .queue-panel{margin:0 4px 5px}body .queue-list{max-height:min(32dvh,240px)}body .queue-item{grid-template-columns:24px minmax(0,1fr)}body .queue-actions{grid-column:2;justify-content:flex-start}body .queue-actions button{min-height:44px;flex:1 1 auto}}
.queue-panel{flex:0 0 auto;margin:10px 16px 0;border:1px solid var(--line);border-radius:13px;background:var(--surface-3);overflow:hidden}.queue-head{width:100%;min-height:44px;display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;text-align:left;font-size:12px;font-weight:650}.queue-panel:not(.open) .queue-list{display:none}.queue-list{display:grid;max-height:210px;overflow:auto}.queue-item{display:grid;grid-template-columns:28px minmax(0,1fr) auto;gap:7px;align-items:center;padding:8px 9px;border-bottom:1px solid var(--line)}.queue-item:last-child{border-bottom:0}.queue-position{color:var(--muted);font-size:11px;text-align:center}.queue-text{min-height:38px;max-height:90px;padding:7px 8px;resize:vertical;font-size:12px}.queue-text[readonly]{border-color:transparent;background:transparent}.queue-actions{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}.queue-actions button{min-height:34px;padding:5px 8px;font-size:11px}.queue-empty{padding:9px 11px;color:var(--muted);font-size:12px}.queue-add-button{min-width:80px}
*{box-sizing:border-box;forced-color-adjust:none}html{height:100%;max-width:100%;overflow-x:hidden;background:#f7f7f5;scrollbar-gutter:stable;-webkit-text-size-adjust:100%;text-size-adjust:100%}body{min-width:320px;min-height:100%;max-width:100%;overflow-x:hidden;margin:0;background:#f7f7f5;color:#202124;font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased}button,input,select,textarea{font:inherit;color:inherit;background-color:#fff}button,a,input,select,textarea{outline-offset:3px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid var(--blue)}button{min-height:42px;border:1px solid var(--line-strong);border-radius:11px;padding:8px 13px;background:var(--surface);cursor:pointer;transition:background .15s,border-color .15s,transform .15s}button:hover:not(:disabled){background:var(--surface-2)}button:active:not(:disabled){transform:scale(.985)}button:disabled{opacity:.42;cursor:not-allowed}button.primary{border-color:#222326;background:#222326;color:#fff}button.primary:hover:not(:disabled){background:#37383b}button.danger{border-color:#efd2d8;background:var(--red-soft);color:var(--red)}button.ghost{border-color:transparent;background:transparent}a{color:inherit;text-decoration:none}.hidden{display:none!important}.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.muted{color:var(--muted)}.error{color:var(--red)}.success{color:var(--green)}.warning{color:var(--amber)}
.shell{max-width:1540px;margin:auto;padding:16px 18px}.topbar{height:58px;display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:12px}.brand{display:flex;align-items:center;gap:11px;min-width:0}.brand-mark{width:36px;height:36px;display:grid;place-items:center;border-radius:11px;background:#222326;color:#fff;font-weight:750}.brand h1{margin:0;font-size:21px;line-height:1.2;letter-spacing:-.025em}.subtitle{margin:2px 0 0;color:var(--muted);font-size:12px}.top-actions{display:flex;align-items:center;gap:8px}.connection{display:inline-flex;align-items:center;gap:7px;min-height:34px;border:1px solid var(--line);border-radius:999px;padding:5px 10px;background:var(--surface);color:var(--muted);font-size:12px}.connection::before{content:"";width:7px;height:7px;border-radius:999px;background:#aaa}.connection.good{color:var(--green)}.connection.good::before{background:var(--green);box-shadow:0 0 0 3px var(--green-soft)}.connection.warn{color:var(--amber)}.connection.warn::before{background:var(--amber)}.connection.bad{color:var(--red)}.connection.bad::before{background:var(--red)}.settings-link{min-height:42px;display:inline-flex;align-items:center;border:1px solid var(--line-strong);border-radius:11px;padding:8px 13px;background:var(--surface)}
.runner-banner{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px;padding:12px 14px;border:1px solid #ead8b9;border-radius:14px;background:var(--amber-soft)}.runner-banner.ready{display:none}.runner-banner strong{display:block}.runner-banner span{display:block;margin-top:2px;color:var(--muted);font-size:12px}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;margin-bottom:12px}.metric{min-width:0;border:1px solid var(--line);border-radius:12px;padding:10px 13px;background:var(--surface)}.metric span{display:block;color:var(--muted);font-size:11px}.metric strong{display:block;margin-top:2px;font-size:19px;line-height:1.25}
.workspace{height:calc(var(--app-height,100dvh) - 172px);min-height:560px;display:grid;grid-template-columns:220px 330px minmax(420px,1fr);gap:11px}.panel{min-width:0;min-height:0;border:1px solid var(--line);border-radius:15px;background:var(--surface);box-shadow:var(--shadow);overflow:hidden}.panel-head{min-height:53px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 13px;border-bottom:1px solid var(--line);background:var(--surface-3)}.panel-head h2{margin:0;font-size:15px}.panel-body{padding:11px}.stack{display:grid;gap:10px}.field{display:grid;gap:5px}.field label{font-size:12px;color:var(--muted)}select,input,textarea{width:100%;border:1px solid var(--line-strong);border-radius:11px;background:var(--surface);padding:9px 10px}textarea{resize:none}.badge-row,.detail-meta{display:flex;flex-wrap:wrap;gap:6px}.badge{display:inline-flex;align-items:center;min-height:24px;border:0;border-radius:999px;padding:3px 8px;background:#efefec;color:#65686d;font-size:11px}.badge.good{background:var(--green-soft);color:var(--green)}.badge.warn{background:var(--amber-soft);color:var(--amber)}.badge.bad{background:var(--red-soft);color:var(--red)}
.project-panel .panel-head button{display:none}.project-list,.thread-list{display:grid}.project-button,.thread-button{width:100%;height:auto;text-align:left;border-color:transparent;background:transparent}.project-button{padding:10px;border-radius:10px}.project-button:hover,.project-button.selected{background:var(--surface-2)}.project-title,.thread-title{font-weight:660;overflow-wrap:anywhere}.project-meta,.thread-meta,.thread-preview{margin-top:3px;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.filters{display:grid;grid-template-columns:1fr 1.3fr;gap:7px}.thread-scroll{height:calc(100% - 122px);overflow:auto;padding:0 9px}.thread-button{position:relative;min-height:96px;border-radius:0;border-bottom:1px solid var(--line);padding:12px 9px}.thread-button.selected{background:#f6f6f3}.thread-button.selected::before,.thread-button.active-thread::before{content:"";position:absolute;left:0;top:18px;bottom:18px;width:2px;border-radius:2px;background:var(--blue)}.thread-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}.thread-state{flex:0 0 auto;color:var(--muted);font-size:11px}.thread-state.good{color:var(--green)}.thread-state.bad{color:var(--red)}.empty{padding:34px 16px;text-align:center;color:var(--muted)}.thread-list-footer{position:sticky;bottom:0;display:grid;gap:5px;padding:9px;background:rgba(255,255,255,.97);border-top:1px solid var(--line)}.thread-list-footer button{width:100%;background:var(--surface-3)}.thread-list-status{text-align:center;color:var(--muted);font-size:11px}
.detail-panel{display:flex;flex-direction:column}.detail-empty{height:100%;display:grid;place-items:center;padding:30px;text-align:center}.detail-empty h2{margin:0 0 6px;font-size:20px}.detail-content{height:100%;min-height:0;display:flex;flex-direction:column}.detail-head{flex:0 0 auto;position:relative;padding:13px 16px 11px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.95)}.detail-title-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:start;gap:10px}.detail-heading{min-width:0}.detail-head h2{margin:0;font-size:18px;line-height:1.3;letter-spacing:-.015em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.eyebrow{color:var(--muted);font-size:11px}.detail-preview{margin-top:3px;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.detail-meta{margin-top:8px}.sync-badge{display:inline-flex;align-items:center;gap:5px}.sync-badge::before{content:"";width:6px;height:6px;border-radius:99px;background:currentColor}.detail-back{display:none}.task-menu{position:relative}.task-menu summary{width:42px;height:42px;display:grid;place-items:center;border:1px solid var(--line-strong);border-radius:11px;background:var(--surface);cursor:pointer;list-style:none;font-weight:650}.task-menu summary::-webkit-details-marker{display:none}.task-menu-popover{position:absolute;z-index:12;top:48px;right:0;width:210px;display:grid;gap:5px;padding:7px;border:1px solid var(--line);border-radius:13px;background:var(--surface);box-shadow:0 16px 44px rgba(20,22,26,.14)}.task-menu-popover button{width:100%;text-align:left}.notice{flex:0 0 auto;margin:10px 16px 0;border-left:3px solid var(--blue);border-radius:6px 11px 11px 6px;background:var(--blue-soft);padding:9px 11px;color:#4c5d8d;font-size:12px}.notice.warn{border-color:var(--amber);background:var(--amber-soft);color:#805a20}.notice.bad{border-color:var(--red);background:var(--red-soft);color:#983547}
.conversation-wrap{position:relative;flex:1;min-height:0;overflow:hidden}.conversation{height:100%;overflow:auto;padding:20px clamp(16px,4vw,54px) 26px;scroll-behavior:smooth}.conversation-inner{max-width:820px;display:grid;gap:18px;margin:0 auto}.message{min-width:0}.message-role{margin:0 0 5px;color:var(--muted);font-size:11px;font-weight:650}.message-body{white-space:pre-wrap;overflow-wrap:anywhere}.message.assistant{padding-right:9%}.message.assistant .message-body{font-size:15px;line-height:1.65}.message.user{justify-self:end;width:min(82%,680px);padding:11px 14px;border-radius:17px 17px 5px 17px;background:var(--surface-2)}.message.user .message-role{display:none}.message.activity{max-width:92%;border-left:2px solid var(--amber);border-radius:7px;background:var(--surface-3);padding:8px 11px;color:#555b63}.message.activity .message-role{color:var(--amber)}.message.activity .message-body{font-size:12px}.message.system{justify-self:center;color:var(--muted);font-size:12px;text-align:center}.message.streaming .message-role{color:var(--green)}.typing-dot{display:inline-block;width:6px;height:6px;margin-left:6px;border-radius:99px;background:var(--green);animation:pulse 1.3s infinite}@keyframes pulse{0%,100%{opacity:.25}50%{opacity:1}}.run-details{border:1px solid var(--line);border-radius:12px;background:var(--surface-3)}.run-details summary{cursor:pointer;list-style:none;padding:9px 11px;color:var(--muted);font-size:12px}.run-details summary::-webkit-details-marker{display:none}.run-details[open] summary{border-bottom:1px solid var(--line)}.run-detail-items{display:grid;gap:7px;padding:9px}.run-item{border-left:2px solid var(--line-strong);border-radius:6px;background:var(--surface);padding:8px 9px}.run-item-label{margin-bottom:3px;color:var(--muted);font-size:11px;font-weight:650}.run-item-text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.code-output{max-height:190px;margin:6px 0 0;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:8px;padding:8px;background:#f2f2ef;color:#3b4540;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}.new-reply{position:absolute;z-index:4;left:50%;bottom:13px;transform:translateX(-50%);min-height:36px;border-color:#d9dfef;border-radius:999px;background:var(--blue-soft);color:var(--blue);box-shadow:0 5px 16px rgba(30,44,80,.12)}
.composer{flex:0 0 auto;margin:0 16px 14px;border:1px solid var(--line-strong);border-radius:17px;background:var(--surface);box-shadow:0 8px 28px rgba(20,22,26,.08);padding:7px}.composer textarea{min-height:50px;max-height:160px;border:0;background:transparent;padding:9px 10px;overflow:auto}.composer-bar{display:flex;align-items:flex-end;gap:8px}.composer-tools{min-width:0;flex:1}.composer-status{min-height:20px;padding:1px 9px;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.send-button{flex:0 0 auto;min-width:70px}.advanced{margin:1px 4px 5px}.advanced summary{display:inline-flex;min-height:30px;align-items:center;cursor:pointer;color:var(--muted);font-size:11px;list-style:none}.advanced summary::-webkit-details-marker{display:none}.advanced-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;padding:5px}.mode-switch{display:flex;gap:5px}.mode-switch button{min-height:36px;padding:6px 9px;background:transparent;font-size:12px}.mode-switch button[aria-pressed="true"]{border-color:#222326;background:#222326;color:#fff}.model-meta{grid-column:1/-1;color:var(--muted);font-size:11px}
.mobile-nav{display:none}.new-task-sheet,.connection-sheet,.management-sheet,.rename-sheet{position:fixed;z-index:90;left:50%;top:50%;width:min(520px,calc(100% - 32px));max-height:calc(100dvh - 32px);overflow:auto;transform:translate(-50%,-50%);border:1px solid var(--line);border-radius:20px;background:var(--surface);box-shadow:0 24px 80px rgba(22,25,30,.2);padding:8px 18px 20px}.connection-sheet,.rename-sheet{width:min(440px,calc(100% - 32px))}.sheet-handle{display:none;width:38px;height:4px;border-radius:4px;background:#c7c9cc;margin:0 auto 6px}.sheet-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.sheet-head h2{margin:9px 0;font-size:20px}.sheet-copy{margin:0 0 15px;color:var(--muted);font-size:13px}.new-task-form,.rename-form{display:grid;gap:13px}.new-task-form textarea{min-height:126px;resize:vertical}.sheet-actions{display:flex;gap:8px;justify-content:flex-end}.sheet-actions .primary{min-width:130px}.feedback{min-height:20px;color:var(--muted);font-size:12px}.modal-backdrop{position:fixed;z-index:80;inset:0;background:rgba(25,27,30,.25)}.connection-grid,.management-grid{display:grid;gap:0;border:1px solid var(--line);border-radius:14px;overflow:hidden}.connection-row,.management-row{display:grid;grid-template-columns:110px minmax(0,1fr);gap:12px;padding:11px 12px;border-bottom:1px solid var(--line)}.connection-row:last-child,.management-row:last-child{border-bottom:0}.connection-row span,.management-row span{color:var(--muted);font-size:12px}.connection-row strong,.management-row strong{min-width:0;text-align:right;font-size:12px;overflow-wrap:anywhere}.connection-note,.management-note{margin:12px 2px 0;color:var(--muted);font-size:12px}
@media(max-width:1050px){.workspace{grid-template-columns:190px 300px minmax(360px,1fr)}.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:920px){body{padding-bottom:calc(76px + env(safe-area-inset-bottom));min-height:-webkit-fill-available}.shell{padding:0 12px 16px}.topbar{height:74px;margin:0;padding-top:env(safe-area-inset-top)}.brand-mark,.subtitle,.settings-link{display:none}.brand h1{font-size:24px}.top-actions .connection{padding-inline:9px}.top-actions .primary{padding-inline:11px}.runner-banner{margin:3px 0 11px}.metrics{display:none}.workspace{height:auto;min-height:0;display:block}.panel{box-shadow:none}.project-panel{display:none;position:fixed;z-index:90;left:10px;right:10px;bottom:10px;max-height:76dvh;overflow:auto;border-radius:20px}.project-panel.open{display:block}.project-panel .panel-head button{display:inline-flex}.project-panel.open+.thread-panel{pointer-events:none}.thread-panel{border:0;background:transparent}.thread-panel .panel-head{padding-inline:3px;background:transparent;border:0}.thread-panel .panel-body{padding:5px 0 9px}.thread-scroll{height:auto;padding:0}.thread-button{padding:13px 10px;min-height:98px}.detail-panel{display:none;position:fixed;z-index:40;inset:0;height:var(--app-height,100dvh);border:0;border-radius:0;background:var(--bg)}.detail-open .detail-panel{display:flex}.detail-open .shell{padding:0}.detail-open .topbar,.detail-open .runner-banner,.detail-open .metrics,.detail-open .project-panel,.detail-open .thread-panel,.detail-open .mobile-nav{display:none}.detail-content{height:var(--app-height,100dvh)}.detail-head{padding:calc(9px + env(safe-area-inset-top)) 10px 10px;background:rgba(247,247,245,.94);backdrop-filter:blur(18px)}.detail-title-row{grid-template-columns:48px minmax(0,1fr) 44px;align-items:center}.detail-back,.task-menu summary,.send-button{min-height:44px}.detail-back{width:48px;display:inline-flex;align-items:center;justify-content:center;border-color:transparent;background:transparent;padding:0}.task-menu summary{width:44px;height:44px}.detail-preview{display:none}.detail-meta{margin-left:58px;margin-top:4px}.conversation{padding:17px 14px 210px}.conversation-inner{gap:17px}.message.assistant{padding-right:2%}.message.user{width:min(88%,680px)}.notice{margin-inline:10px}.composer{position:fixed;z-index:8;left:8px;right:8px;bottom:max(8px,env(safe-area-inset-bottom));max-height:calc(var(--app-height,100dvh) - max(16px,env(safe-area-inset-top)) - max(16px,env(safe-area-inset-bottom)));overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;margin:0;border-radius:19px;padding:6px 7px calc(7px + env(safe-area-inset-bottom));background:rgba(255,255,255,.96);backdrop-filter:blur(18px)}.composer textarea{max-height:118px}.advanced-grid{grid-template-columns:1fr}.model-meta{grid-column:auto}.mobile-nav{position:fixed;z-index:30;left:12px;right:12px;bottom:max(10px,env(safe-area-inset-bottom));height:calc(64px + env(safe-area-inset-bottom));display:grid;grid-template-columns:repeat(4,1fr);padding:4px 7px env(safe-area-inset-bottom);border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.95);box-shadow:0 8px 28px rgba(25,28,32,.11);backdrop-filter:blur(18px)}.mobile-nav button,.mobile-nav a{min-height:54px;border:0;border-radius:12px;background:transparent;color:var(--muted);display:flex;align-items:center;justify-content:center;font-size:12px}.mobile-nav .primary-nav{margin-top:-14px;height:56px;align-self:start;background:#222326;color:#fff;box-shadow:0 6px 16px rgba(0,0,0,.18)}.new-task-sheet{left:8px;right:8px;top:auto;bottom:0;width:auto;max-height:88dvh;transform:none;border-radius:22px 22px 0 0;padding-bottom:calc(18px + env(safe-area-inset-bottom))}.sheet-handle{display:block}.sheet-actions{display:grid;grid-template-columns:1fr 1.4fr}.sheet-actions button{width:100%}}
@media(max-width:520px){.shell{padding-inline:9px}.runner-banner{padding:11px}.filters{grid-template-columns:1fr 1.25fr}.task-menu-popover{position:fixed;top:auto;left:10px;right:10px;bottom:calc(12px + env(safe-area-inset-bottom));width:auto}.advanced-grid{padding-inline:2px}.new-task-form textarea{min-height:108px}.new-task-sheet{left:0;right:0}.connection-row{grid-template-columns:96px minmax(0,1fr)}}
@media(max-width:920px){.mobile-nav{grid-template-columns:repeat(5,1fr)}}
@media(max-width:920px){.management-sheet,.rename-sheet{left:8px;right:8px;top:auto;bottom:0;width:auto;max-height:88dvh;transform:none;border-radius:22px 22px 0 0;padding-bottom:calc(18px + env(safe-area-inset-bottom))}.management-sheet .sheet-handle,.rename-sheet .sheet-handle{display:block}}
.request-tray{margin:10px 15px 0;border:1px solid #ead8b9;border-radius:14px;background:var(--amber-soft);padding:10px}.request-tray-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}.request-tray-head h3{margin:0;font-size:14px}.request-list{display:grid;gap:7px}.request-card{width:100%;min-height:64px;display:grid;gap:3px;text-align:left;background:var(--surface);border-color:#e6d5b8}.request-card strong,.request-card span{overflow-wrap:anywhere}.request-card span{color:var(--muted);font-size:12px}.request-card.unknown{border-color:#efd0d7;background:var(--red-soft)}.request-sheet{position:fixed;z-index:95;left:50%;top:50%;width:min(560px,calc(100% - 32px));max-height:calc(100dvh - 32px);overflow:auto;transform:translate(-50%,-50%);border:1px solid var(--line);border-radius:20px;background:var(--surface);box-shadow:0 24px 80px rgba(22,25,30,.2);padding:8px 18px 20px}.request-detail{display:grid;gap:13px}.request-summary{margin:0;border-radius:11px;background:var(--surface-3);padding:11px;white-space:pre-wrap;overflow-wrap:anywhere}.request-question{display:grid;gap:6px;border-top:1px solid var(--line);padding-top:12px}.request-question h3{margin:0;font-size:14px}.request-question p{margin:0;color:var(--muted);font-size:12px}.request-option{min-height:44px;display:flex;align-items:center;gap:9px}.request-option input{width:20px;height:20px}.request-answer,.request-other{min-height:44px}.request-decisions{position:sticky;bottom:0;display:flex;gap:8px;flex-wrap:wrap;padding-top:12px;background:var(--surface)}.request-decisions button{min-height:44px;flex:1 1 120px}.request-warning{color:var(--red);font-size:12px}
@media(max-width:920px){.request-tray{margin-inline:10px}.request-sheet{left:8px;right:8px;top:auto;bottom:0;width:auto;max-height:90dvh;transform:none;border-radius:22px 22px 0 0;padding-bottom:calc(18px + env(safe-area-inset-bottom))}.request-sheet .sheet-handle{display:block}.request-decisions{padding-bottom:env(safe-area-inset-bottom)}.advanced-grid .field select,.new-task-form .field select{min-height:44px}}
.delivery{flex:0 0 auto;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin:10px 16px 0}.delivery-stage{min-width:0;border:1px solid var(--line);border-radius:10px;padding:7px 9px;background:var(--surface-3);color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.delivery-stage::before{content:"";display:inline-block;width:6px;height:6px;margin-right:6px;border-radius:99px;background:#b7b9bd}.delivery-stage.done{color:var(--green);background:var(--green-soft)}.delivery-stage.done::before{background:var(--green)}.delivery-stage.current{color:var(--blue);background:var(--blue-soft)}.delivery-stage.current::before{background:var(--blue);animation:pulse 1.3s infinite}.delivery-stage.failed{color:var(--red);background:var(--red-soft)}.delivery-stage.failed::before{background:var(--red)}@media(max-width:920px){.delivery{margin-inline:10px;grid-template-columns:repeat(2,minmax(0,1fr))}}
.resource-sheet{position:fixed;z-index:94;left:50%;top:50%;width:min(760px,calc(100% - 32px));height:min(760px,calc(100dvh - 32px));display:flex;flex-direction:column;overflow:hidden;transform:translate(-50%,-50%);border:1px solid var(--line);border-radius:20px;background:var(--surface);box-shadow:0 24px 80px rgba(22,25,30,.2);padding:8px 16px 16px}.resource-tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:2px 0 10px}.resource-tabs button{min-height:44px}.resource-tabs button[aria-selected="true"]{border-color:#222326;background:#222326;color:#fff}.resource-toolbar{display:flex;align-items:center;gap:7px;margin-bottom:8px}.resource-toolbar button{min-height:44px}.resource-path{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}.resource-body{min-height:0;flex:1;overflow:auto;border:1px solid var(--line);border-radius:14px;background:var(--surface-3)}.resource-list{display:grid}.resource-entry{width:100%;min-height:52px;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:8px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;text-align:left}.resource-entry:last-child{border-bottom:0}.resource-entry-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:620}.resource-entry-meta{color:var(--muted);font-size:11px}.resource-empty{padding:28px 16px;color:var(--muted);text-align:center}.resource-more{width:calc(100% - 16px);min-height:44px;margin:8px}.resource-output{min-height:100%;margin:0;padding:14px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}.resource-summary{padding:12px 14px;border-bottom:1px solid var(--line);background:var(--surface)}.resource-summary strong,.resource-summary span{display:block}.resource-summary span{margin-top:3px;color:var(--muted);font-size:12px}.resource-preview{min-height:180px;display:grid;place-items:center;padding:12px}.resource-preview img{display:block;max-width:100%;height:auto;border-radius:10px}.resource-preview object{display:block;width:100%;height:clamp(360px,58dvh,620px);border:0;border-radius:10px;background:#fff}.resource-download{min-height:44px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line-strong);border-radius:11px;padding:8px 13px;background:var(--surface)}.diagnostic-list{display:grid;gap:8px;padding:10px}.diagnostic-list button{min-height:52px;text-align:left}.resource-footer{display:flex;align-items:center;justify-content:space-between;gap:8px;padding-top:9px}.resource-footer button{min-height:44px}.resource-status{min-width:0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.resource-status.error{color:var(--red)}
@media(max-width:920px){.resource-sheet{left:0;right:0;top:auto;bottom:0;width:auto;height:min(88dvh,760px);transform:none;border-radius:22px 22px 0 0;padding:8px 10px calc(10px + env(safe-area-inset-bottom))}.resource-sheet .sheet-handle{display:block}.resource-entry{min-height:56px}.resource-footer{padding-bottom:env(safe-area-inset-bottom)}.resource-preview{padding:8px}.resource-preview object{height:58dvh}}
</style></head><body><main class="shell">
<header class="topbar"><div class="brand"><span class="brand-mark">C</span><div><h1>Codex</h1><p class="subtitle">Mac 上的任务，随时继续对话</p></div></div><div class="top-actions"><button id="connectionState" class="connection" type="button" aria-haspopup="dialog">正在连接</button><button id="managementButton" class="settings-link" type="button">管理</button><button id="newTaskButton" class="primary" type="button">新建任务</button></div></header>
<section id="runnerBanner" class="runner-banner"><div><strong id="runnerBannerTitle">正在连接 Mac</strong><span id="runnerBannerText">任务与回复会自动同步。</span></div><button id="checkConnection" type="button">重试连接</button></section>
<section class="metrics" aria-label="任务概览"><div class="metric"><span>Mac 主机</span><strong id="metricHosts">0</strong></div><div class="metric"><span>项目</span><strong id="metricProjects">0</strong></div><div class="metric"><span>全部任务</span><strong id="metricThreads">0</strong></div><div class="metric"><span>Codex 正在工作</span><strong id="metricActive">0</strong></div><div class="metric"><span>需要处理</span><strong id="metricRecovery">0</strong></div></section>
<section class="workspace">
<aside id="projectPanel" class="panel project-panel" aria-label="主机与项目"><div class="panel-head"><h2>项目</h2><button id="closeProjects" class="ghost" type="button">完成</button></div><div class="panel-body stack"><div class="field"><label for="hostSelect">Mac</label><select id="hostSelect" aria-label="选择 Mac 主机"></select></div><div class="badge-row"><span id="hostWriteState" class="badge">正在连接</span></div><div id="hostMeta" class="muted">等待第一次自动同步。</div><div id="projectList" class="project-list" aria-label="项目列表"></div></div></aside>
<section class="panel thread-panel" aria-label="任务列表"><div class="panel-head"><h2>任务</h2><span id="threadCount" class="badge">0</span></div><div class="panel-body stack"><div class="filters"><div class="field"><label for="statusFilter">查看</label><select id="statusFilter"><option value="all">全部任务</option><option value="active">正在工作</option><option value="idle">可以继续</option><option value="notLoaded">最近任务</option><option value="failed">需要处理</option><option value="recovery_required">需要处理</option><option value="protocol_degraded">暂时只读</option><option value="archived">已归档</option></select></div><div class="field"><label for="threadSearch">搜索</label><input id="threadSearch" maxlength="120" inputmode="search" placeholder="搜索全部已同步任务"></div></div></div><div id="threadList" class="thread-list thread-scroll" aria-live="polite"></div><div class="thread-list-footer"><button id="loadMoreThreads" type="button">加载更多任务</button><div id="threadListStatus" class="thread-list-status" role="status">正在加载最近任务</div></div></section>
<section class="panel detail-panel" aria-label="任务对话"><div id="detailEmpty" class="detail-empty"><div><h2>选择一个任务</h2><p class="muted">打开后即可像 Codex App 一样查看回复并继续对话。</p></div></div><div id="detailContent" class="detail-content hidden"><header class="detail-head"><div class="detail-title-row"><button id="detailBack" class="detail-back" type="button" aria-label="返回任务列表">返回</button><div class="detail-heading"><div id="detailProject" class="eyebrow">当前项目</div><h2 id="detailTitle">-</h2><div id="detailPreview" class="detail-preview"></div></div><details id="taskMenu" class="task-menu"><summary aria-label="更多任务操作">更多</summary><div class="task-menu-popover"><button id="renameButton" type="button">重命名</button><button id="pinButton" type="button">置顶任务</button><button id="forkButton" type="button">派生新任务</button><button id="reviewButton" type="button">开始代码审查</button><button id="resourceButton" type="button">文件、Diff 与诊断</button><button id="interruptButton" class="danger" type="button">停止当前任务</button><button id="archiveButton" type="button">归档任务</button><button id="unarchiveButton" type="button">恢复归档</button></div></details></div><div id="detailMeta" class="detail-meta"></div></header><div id="detailNotice" class="notice hidden"></div><div class="conversation-wrap"><div id="conversationView" class="conversation" aria-live="polite"><div id="conversationInner" class="conversation-inner"></div></div><button id="newReplyButton" class="new-reply hidden" type="button">查看新回复</button></div><form id="composer" class="composer"><details id="advancedControls" class="advanced"><summary>模型、推理强度与发送方式</summary><div class="advanced-grid"><div class="field"><label for="modelSelect">模型</label><select id="modelSelect" aria-describedby="modelMeta"></select></div><div class="field"><label for="effortSelect">推理强度</label><select id="effortSelect" aria-describedby="modelMeta"></select></div><div class="field"><label>发送方式</label><div class="mode-switch"><button id="safeMode" type="button" aria-pressed="true">安全调整</button><button id="nativeMode" type="button" aria-pressed="false">快速调整</button></div></div><div id="modelMeta" class="model-meta"></div></div></details><label class="sr-only" for="composerInput">给 Codex 发消息</label><div class="composer-bar"><div class="composer-tools"><textarea id="composerInput" rows="1" maxlength="12000" placeholder="给 Codex 发消息"></textarea><div id="composerFeedback" class="composer-status" role="status">回复会自动出现在这里</div></div><button id="submitDirection" class="primary send-button" type="submit">发送</button></div></form></div></section>
</section></main>
<nav class="mobile-nav" aria-label="移动端导航"><a href="./">任务</a><button id="mobileProjects" type="button">项目</button><button id="mobileNewTask" class="primary-nav" type="button">新建</button><button id="mobileConnection" type="button">状态</button><button id="mobileManagement" type="button">管理</button></nav>
<div id="modalBackdrop" class="modal-backdrop hidden"></div><section id="newTaskSheet" class="new-task-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="newTaskTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="newTaskTitle">新建任务</h2><button id="closeNewTask" class="ghost" type="button">关闭</button></div><p class="sheet-copy">任务会出现在 Mac 和手机的同一列表里。连接未确认时不会发送。</p><form id="newTaskForm" class="new-task-form"><div class="field"><label for="newTaskProject">项目</label><select id="newTaskProject" required></select></div><div class="field"><label for="newTaskInput">给 Codex 的任务</label><textarea id="newTaskInput" maxlength="12000" required placeholder="描述希望 Codex 完成的任务"></textarea></div><div class="field"><label for="newTaskModel">模型</label><select id="newTaskModel"></select></div><div class="field"><label for="newTaskEffort">推理强度</label><select id="newTaskEffort"></select></div><div id="newTaskFeedback" class="feedback" role="status">正在检查是否可以创建任务。</div><div class="sheet-actions"><button id="cancelNewTask" type="button">取消</button><button id="createTaskButton" class="primary" type="submit" disabled>创建并打开</button></div></form></section><section id="connectionSheet" class="connection-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="connectionTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="connectionTitle">实时连接</h2><button id="closeConnection" class="ghost" type="button">关闭</button></div><p class="sheet-copy">页面使用持续推送连接；断线后会从最后一个事件自动续传。</p><div class="connection-grid"><div class="connection-row"><span>页面推送</span><strong id="connectionBrowser">正在连接</strong></div><div class="connection-row"><span>Mac 长连接</span><strong id="connectionRunner">等待心跳</strong></div><div class="connection-row"><span>最近推送</span><strong id="connectionEventAt">尚未收到</strong></div><div class="connection-row"><span>任务数据</span><strong id="connectionDataAt">尚未同步</strong></div><div class="connection-row"><span>自动重连</span><strong id="connectionRetry">已启用</strong></div></div><p id="connectionNote" class="connection-note">连接正常时无需任何手动操作。</p></section><section id="managementSheet" class="management-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="managementTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="managementTitle">设置与权限</h2><button id="closeManagement" class="ghost" type="button">关闭</button></div><p class="sheet-copy">只显示 Mac Runner 实际公开的设置与能力，不会在手机端伪造或修改。</p><div id="managementContent" class="management-grid"><div class="management-row"><span>目录</span><strong>正在加载</strong></div></div><p id="managementNote" class="management-note">模型目录会按需加载。</p></section><section id="renameSheet" class="rename-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="renameTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="renameTitle">重命名任务</h2><button id="closeRename" class="ghost" type="button">关闭</button></div><form id="renameForm" class="rename-form"><div class="field"><label for="renameInput">任务名称</label><input id="renameInput" maxlength="80" required></div><div id="renameFeedback" class="feedback" role="status"></div><div class="sheet-actions"><button id="cancelRename" type="button">取消</button><button id="saveRename" class="primary" type="submit">保存</button></div></form></section><section id="reviewSheet" class="rename-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="reviewTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="reviewTitle">开始代码审查</h2><button id="closeReview" class="ghost" type="button">关闭</button></div><form id="reviewForm" class="rename-form"><div class="field"><label for="reviewType">审查范围</label><select id="reviewType"><option value="uncommittedChanges">未提交更改</option><option value="baseBranch">与基准分支比较</option></select></div><div id="reviewBranchField" class="field hidden"><label for="reviewBranch">基准分支</label><input id="reviewBranch" maxlength="255" placeholder="例如 origin/main"></div><div id="reviewFeedback" class="feedback" role="status">审查会在当前任务中启动新的 Turn。</div><div class="sheet-actions"><button id="cancelReview" type="button">取消</button><button id="startReview" class="primary" type="submit">开始审查</button></div></form></section><section id="resourceSheet" class="resource-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="resourceTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="resourceTitle">项目资源</h2><button id="closeResource" class="ghost" type="button">关闭</button></div><p class="sheet-copy">首屏只加载摘要；文件和 Diff 内容均按需分块持续加载。</p><div class="resource-tabs" role="tablist"><button id="resourceFilesTab" type="button" role="tab" aria-selected="true">项目文件</button><button id="resourceDiffTab" type="button" role="tab" aria-selected="false">Git Diff</button><button id="resourceDiagnosticsTab" type="button" role="tab" aria-selected="false">固定诊断</button></div><div id="resourceToolbar" class="resource-toolbar"><button id="resourceBack" type="button">上一级</button><div id="resourcePath" class="resource-path">项目根目录</div></div><div id="resourceBody" class="resource-body" aria-live="polite"></div><div class="resource-footer"><div id="resourceStatus" class="resource-status" role="status">打开后按需读取，不会一次加载全部内容。</div><button id="resourceMore" type="button" class="hidden">继续加载</button></div></section>
<script src="desktop.js"></script></body></html>"""


from .desktop_layout import task_first_html

DESKTOP_DASHBOARD_HTML = task_first_html(DESKTOP_DASHBOARD_HTML)

DESKTOP_DASHBOARD_JS = r"""
const q = id => document.getElementById(id);
const API = '../api/desktop/v1';
const STATUS_API = '../api/status';
const SHANGHAI_TIME = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
const requestUiState = {selectedRef: '', returnFocus: null, busy: false};
const state = {csrf: '', hosts: [], projects: [], threads: [], threadsCursor: 0, threadsHasMore: false, threadsLoading: false, selectedHost: '', selectedProject: 'all', selectedThread: '', selectedModel: '', selectedEffort: '', selectedCollaborationMode: '', detail: null, threadSettings: null, events: [], eventCursor: 0, eventSource: null, eventReconnectTimer: 0, eventReconnectAttempt: 0, overviewCursor: 0, overviewSource: null, overviewReconnectTimer: 0, overviewReconnectAttempt: 0, overviewStreamState: 'connecting', detailStreamState: 'idle', refreshGeneration: 0, mode: 'safe', loading: false, drafts: {}, pendingCreate: null, createBusy: false, settingsBusy: false, queueBusy: false, queueOpen: false, queueEditing: '', queueDrafts: {}, following: true, serverTimeMs: 0, serverTimeObservedAt: 0, lastOverviewFrameAt: 0, lastDetailFrameAt: 0, lastDataEventAt: '', overviewError: '', streamFailures: new Set(), detailReloadTimer: 0, overviewReconcileTimer: 0, newTaskReturnFocus: null, connectionReturnFocus: null, taskSettingsReturnFocus: null, historyTurns: [], historyCursor: null, historyHasMore: false, historyLoading: false, historyRequestId: '', historyInitialized: false, historyError: '', searchQuery: '', searchResults: [], searchCursor: null, searchHasMore: false, searchLoading: false, searchRequestId: '', searchAppend: false, searchError: '', taskSearchTimer: 0, taskSearchGeneration: 0, management: null, managementReturnFocus: null, renameReturnFocus: null, reviewReturnFocus: null, handledForks: new Set()};
state.selectedPermission = '';
const resourceUi = {returnFocus: null, tab: 'files', path: '', entries: [], cursor: null, hasMore: false, loading: false, pending: new Map(), file: null, chunks: [], diagnostic: null, diffSummary: null, diffFile: null, diffChunks: [], error: ''};

function requestId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}

async function jsonFetch(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 20000);
  const externalSignal = options.signal;
  const abort = () => controller.abort();
  if (externalSignal) externalSignal.addEventListener('abort', abort, {once: true});
  try {
    const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin', ...options, signal: controller.signal});
    const raw = await response.text();
    let document = null;
    try { document = raw ? JSON.parse(raw) : null; } catch (_error) { document = null; }
    if (!response.ok) {
      if (response.status === 401) throw new Error('Home Assistant 会话已失效，请重新打开此页面');
      if ([502, 503, 504].includes(response.status)) throw new Error('Home Assistant 网关暂时不可用，正在等待恢复');
      const error = new Error(document?.error?.message || document?.error?.code || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    if (!document) throw new Error('Controller 返回了无法识别的数据');
    return document.result ?? document;
  } catch (error) {
    if (error.name === 'AbortError' && !externalSignal?.aborted) throw new Error('Controller 请求超时');
    throw error;
  } finally {
    window.clearTimeout(timeout);
    if (externalSignal) externalSignal.removeEventListener('abort', abort);
  }
}

function delay(milliseconds) { return new Promise(resolve => setTimeout(resolve, milliseconds)); }
function text(value, fallback = '') { return typeof value === 'string' && value ? value : fallback; }
function number(value) { return Number.isFinite(Number(value)) ? Number(value) : 0; }
function updateViewportHeight() {
  const height = window.visualViewport?.height || window.innerHeight;
  document.documentElement.style.setProperty('--app-height', `${Math.max(320, Math.round(height))}px`);
}
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
  const labels = {active: 'Codex 正在工作', idle: '等待你的消息', notLoaded: '可以继续', archived: '已归档', failed: '需要处理', recovery_required: '需要处理', protocol_degraded: '暂时只读', inProgress: '正在工作', completed: '已完成', interrupted: '已停止'};
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

function estimatedServerNow() {
  return state.serverTimeMs ? state.serverTimeMs + Math.max(0, Date.now() - state.serverTimeObservedAt) : Date.now();
}

function freshness() {
  if (!navigator.onLine) return {label: '手机网络离线', kind: 'bad', seconds: null};
  if (state.overviewStreamState !== 'open' || state.streamFailures.has('overview') || (state.selectedThread && state.streamFailures.has('detail'))) return {label: '实时流正在重连', kind: 'warn', seconds: null};
  const streamSeconds = state.lastOverviewFrameAt ? Math.floor((Date.now() - state.lastOverviewFrameAt) / 1000) : null;
  if (streamSeconds === null || streamSeconds > 30) return {label: '实时流等待恢复', kind: 'warn', seconds: streamSeconds};
  const host = currentHost();
  const connectionObservedAt = host?.connection_observed_at || host?.synced_at;
  if (!host?.online || !connectionObservedAt) return {label: 'Mac 离线', kind: 'bad', seconds: null};
  const syncedAt = new Date(connectionObservedAt).getTime();
  if (!Number.isFinite(syncedAt)) return {label: '同步时间未知', kind: 'bad', seconds: null};
  const seconds = Math.max(0, Math.floor((estimatedServerNow() - syncedAt) / 1000));
  const dataAt = new Date(host.data_synced_at || host.synced_at || '').getTime();
  const dataSeconds = Number.isFinite(dataAt) ? Math.max(0, Math.floor((estimatedServerNow() - dataAt) / 1000)) : null;
  if (dataSeconds === null) return {label: '任务数据时间未知', kind: 'bad', seconds, dataSeconds};
  if (dataSeconds > 30) return {label: `任务数据延迟 ${dataSeconds} 秒`, kind: 'bad', seconds, dataSeconds};
  if (seconds <= 10 && dataSeconds <= 10) return {label: '实时已连接', kind: 'good', seconds, dataSeconds};
  if (dataSeconds > 10) return {label: `任务数据 ${dataSeconds} 秒前`, kind: 'warn', seconds, dataSeconds};
  if (seconds <= 30) return {label: `Mac 心跳 ${seconds} 秒前`, kind: 'warn', seconds};
  return {label: `Mac 链路延迟 ${seconds} 秒`, kind: 'bad', seconds};
}

function renderFreshness() {
  const value = freshness();
  setConnection(value.label, value.kind);
  const hostMeta = q('hostMeta');
  const host = currentHost();
  if (hostMeta && host) hostMeta.textContent = state.overviewError || `${value.label} · 任务数据最近变化 ${formatTime(host.data_synced_at || host.synced_at)}`;
  const badgeNode = q('detailSyncState');
  if (badgeNode) {
    badgeNode.className = `badge sync-badge ${value.kind}`.trim();
    badgeNode.textContent = value.label;
  }
  if (q('connectionBrowser')) {
    q('connectionBrowser').textContent = state.overviewStreamState === 'open' ? 'SSE 已连接' : 'SSE 正在重连';
    q('connectionRunner').textContent = host?.online ? `已连接 · ${value.seconds ?? '-'} 秒心跳` : '未连接';
    q('connectionEventAt').textContent = state.lastOverviewFrameAt ? formatTime(new Date(state.lastOverviewFrameAt).toISOString()) : '尚未收到';
    q('connectionDataAt').textContent = formatTime(host?.data_synced_at || host?.synced_at);
    q('connectionRetry').textContent = state.overviewStreamState === 'open' ? '待命 · 保留游标' : `第 ${Math.max(1, state.overviewReconnectAttempt)} 次尝试`;
    q('connectionNote').textContent = state.overviewError || (value.kind === 'good' ? '连接正常；任务变化会主动推送，无需手动刷新。' : '草稿会保留，连接恢复后会从最后事件继续。');
  }
}

function currentHost() { return state.hosts.find(host => host.host_ref === state.selectedHost) || null; }
function currentThread() { return state.threads.find(thread => thread.thread_ref === state.selectedThread) || state.detail; }
function hostCanCreate() {
  const host = currentHost();
  return Boolean(navigator.onLine && host?.online && host?.control_enabled === true && host?.write_available && (host.capabilities || []).includes('create_thread_v1'));
}

function renderRunnerBanner() {
  const host = currentHost();
  const banner = q('runnerBanner');
  banner.className = 'runner-banner';
  if (!navigator.onLine) {
    banner.classList.add('bad');
    q('runnerBannerTitle').textContent = '手机网络当前离线';
    q('runnerBannerText').textContent = '草稿会保留，网络恢复前不会发送任何任务或方向调整。';
    return;
  }
  if (!host) {
    banner.classList.add('bad');
    q('runnerBannerTitle').textContent = 'Mac Runner 尚未连接';
    q('runnerBannerText').textContent = '可浏览已同步内容；草稿会保留，连接恢复前不会发送。';
    return;
  }
  if (!host.online || !host.write_available) {
    banner.classList.add('bad');
    q('runnerBannerTitle').textContent = 'Mac Runner 当前离线';
    q('runnerBannerText').textContent = '草稿会保留，恢复连接前不会发送任何任务或方向调整。';
    return;
  }
  banner.classList.add('ready');
  q('runnerBannerTitle').textContent = 'Mac 已连接';
  q('runnerBannerText').textContent = `任务会自动同步，最近更新于 ${formatTime(host.synced_at)}。`;
}

function populateModelOptions(select, {includeDefault = true} = {}) {
  const models = Array.isArray(currentHost()?.models) ? currentHost().models : [];
  select.replaceChildren();
  if (includeDefault) {
    const inherit = document.createElement('option');
    inherit.value = '';
    inherit.textContent = '沿用 App 默认模型';
    select.append(inherit);
  }
  for (const model of models) {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = model.display_name || model.id;
    select.append(option);
  }
}

function modelForEffort(modelId, detail = null) {
  const models = Array.isArray(currentHost()?.models) ? currentHost().models : [];
  const currentModel = detail?.snapshot?.model;
  return models.find(model => model.id === modelId)
    || models.find(model => model.id === currentModel)
    || models.find(model => model.is_default)
    || null;
}

function populateEffortOptions(select, modelId, {detail = null, value = ''} = {}) {
  const model = modelForEffort(modelId, detail);
  const efforts = Array.isArray(model?.supported_reasoning_efforts) ? model.supported_reasoning_efforts : [];
  select.replaceChildren();
  const inherit = document.createElement('option');
  inherit.value = '';
  inherit.textContent = model?.default_reasoning_effort ? `默认 · ${model.default_reasoning_effort}` : '沿用默认推理强度';
  select.append(inherit);
  for (const effort of efforts) {
    const option = document.createElement('option');
    option.value = effort.id;
    option.textContent = effort.id;
    if (effort.description) option.title = effort.description;
    select.append(option);
  }
  select.value = efforts.some(effort => effort.id === value) ? value : '';
  return efforts.length > 0;
}

function collaborationModeCatalog() {
  return Array.isArray(currentHost()?.collaboration_modes) ? currentHost().collaboration_modes : [];
}

function populateCollaborationModes(select, {value = '', includeInherit = true} = {}) {
  const modes = collaborationModeCatalog();
  select.replaceChildren();
  if (includeInherit) {
    const inherit = document.createElement('option');
    inherit.value = '';
    inherit.textContent = '沿用当前模式';
    select.append(inherit);
  }
  for (const mode of modes) {
    const option = document.createElement('option');
    option.value = mode.id;
    option.textContent = `${mode.label || mode.id}${mode.mode === 'plan' ? ' · 先规划' : ''}`;
    select.append(option);
  }
  select.value = modes.some(mode => mode.id === value) ? value : '';
  return modes.length > 0;
}

function renderNewTaskState() {
  syncCreateDraftHost();
  const projectSelect = q('newTaskProject');
  const previousProject = projectSelect.value;
  const previousModel = q('newTaskModel').value;
  const previousEffort = q('newTaskEffort').value;
  projectSelect.replaceChildren();
  for (const project of state.projects) {
    const option = document.createElement('option');
    option.value = project.project_ref;
    option.textContent = project.project_alias;
    projectSelect.append(option);
  }
  if (state.projects.some(project => project.project_ref === previousProject)) projectSelect.value = previousProject;
  else if (state.selectedProject !== 'all' && state.projects.some(project => project.project_ref === state.selectedProject)) projectSelect.value = state.selectedProject;
  populateModelOptions(q('newTaskModel'));
  if (Array.from(q('newTaskModel').options).some(option => option.value === previousModel)) q('newTaskModel').value = previousModel;
  populateEffortOptions(q('newTaskEffort'), q('newTaskModel').value, {value: previousEffort});
  const pending = state.pendingCreate?.body.host_ref === state.selectedHost ? state.pendingCreate : null;
  const otherPending = state.pendingCreate && !pending;
  if (pending) {
    projectSelect.value = pending.body.project_ref;
    q('newTaskModel').value = pending.body.model || '';
    populateEffortOptions(q('newTaskEffort'), q('newTaskModel').value, {value: pending.body.effort || ''});
    q('newTaskInput').value = pending.body.input;
  }
  const allowed = hostCanCreate() && state.projects.length > 0;
  q('createTaskButton').disabled = state.createBusy || Boolean(otherPending) || (pending ? !navigator.onLine : !allowed);
  q('createTaskButton').textContent = state.createBusy ? '正在创建…' : pending ? '继续检查' : '创建并打开';
  q('newTaskProject').disabled = Boolean(pending) || state.projects.length === 0;
  q('newTaskModel').disabled = Boolean(pending) || !hasCapability('model_override_v1');
  q('newTaskEffort').disabled = Boolean(pending) || !hasCapability('reasoning_effort_v1');
  q('newTaskInput').disabled = Boolean(pending) || state.projects.length === 0;
  q('newTaskFeedback').className = `feedback ${pending ? 'warning' : allowed ? 'muted' : 'warning'}`;
  if (state.createBusy) q('newTaskFeedback').textContent = 'Controller 已接收 · WSS 送往 Runner · 等待 Mac 确认';
  else if (pending) q('newTaskFeedback').textContent = '只会用同一 request ID 检查结果；Controller 持久幂等日志保证不会重复创建。';
  else if (allowed) q('newTaskFeedback').textContent = '创建完成后才会打开新任务；等待或未知状态不会伪装为已发送。';
  else if (!currentHost()?.online || !currentHost()?.write_available) q('newTaskFeedback').textContent = 'Runner 离线：提交已禁用，草稿只保留在当前页面内存中。';
  else if (!hasCapability('create_thread_v1')) q('newTaskFeedback').textContent = '当前 Runner 尚未提供 create_thread_v1，不能远程新建任务。';
  else q('newTaskFeedback').textContent = '当前主机没有可选项目，不能创建任务。';
  if (otherPending) q('newTaskFeedback').textContent = '另一台 Mac 的新建结果待确认，请切回原主机查看；当前草稿已保留。';
  renderPermissionSelectors();
  renderCollaborationModeSelectors();
  if (pending?.body.permission_profile_id) q('newTaskPermission').value = pending.body.permission_profile_id;
  if (pending?.body.collaboration_mode_id) q('newTaskCollaborationMode').value = pending.body.collaboration_mode_id;
  renderCreateAttachments();
}

function syncDialogBackground() {
  const modalOpen = !q('newTaskSheet').classList.contains('hidden') || !q('connectionSheet').classList.contains('hidden') || !q('managementSheet').classList.contains('hidden') || !q('taskSettingsSheet').classList.contains('hidden') || !q('renameSheet').classList.contains('hidden') || !q('reviewSheet').classList.contains('hidden') || !q('resourceSheet').classList.contains('hidden') || (q('requestSheet') && !q('requestSheet').classList.contains('hidden')) || q('projectPanel').classList.contains('open') || !q('imageDialog').classList.contains('hidden');
  document.querySelector('main.shell').inert = modalOpen;
  document.querySelector('nav.mobile-nav').inert = modalOpen;
}

function restoreFocus(element) {
  if (element && typeof element.focus === 'function') window.setTimeout(() => element.focus(), 0);
}

function setNewTaskOpen(open) {
  if (!open) saveCreateDraftFields();
  const wasOpen = !q('newTaskSheet').classList.contains('hidden');
  if (open && !wasOpen) state.newTaskReturnFocus = document.activeElement;
  q('newTaskSheet').classList.toggle('hidden', !open);
  if (open) { setConnectionOpen(false); q('managementSheet').classList.add('hidden'); q('taskSettingsSheet').classList.add('hidden'); q('renameSheet').classList.add('hidden'); if (q('requestSheet')) q('requestSheet').classList.add('hidden'); }
  q('modalBackdrop').classList.toggle('hidden', !open && !q('projectPanel').classList.contains('open') && q('connectionSheet').classList.contains('hidden'));
  syncDialogBackground();
  if (open) {
    renderNewTaskState();
    if (!state.pendingCreate) window.setTimeout(() => q('newTaskInput').focus(), 0);
  } else if (wasOpen) {
    restoreFocus(state.newTaskReturnFocus);
    state.newTaskReturnFocus = null;
  }
}

function setConnectionOpen(open) {
  const wasOpen = !q('connectionSheet').classList.contains('hidden');
  if (open && !wasOpen) state.connectionReturnFocus = document.activeElement;
  q('connectionSheet').classList.toggle('hidden', !open);
  if (open) {
    q('newTaskSheet').classList.add('hidden');
    q('managementSheet').classList.add('hidden');
    q('taskSettingsSheet').classList.add('hidden');
    q('renameSheet').classList.add('hidden');
    q('reviewSheet').classList.add('hidden');
    if (q('requestSheet')) q('requestSheet').classList.add('hidden');
    setProjectsOpen(false);
    renderFreshness();
    window.setTimeout(() => q('closeConnection').focus(), 0);
  }
  q('modalBackdrop').classList.toggle('hidden', !open && q('newTaskSheet').classList.contains('hidden') && !q('projectPanel').classList.contains('open'));
  syncDialogBackground();
  if (!open && wasOpen) {
    restoreFocus(state.connectionReturnFocus);
    state.connectionReturnFocus = null;
  }
}

async function setManagementOpen(open) {
  const wasOpen = !q('managementSheet').classList.contains('hidden');
  if (open && !wasOpen) state.managementReturnFocus = document.activeElement;
  q('managementSheet').classList.toggle('hidden', !open);
  if (open) {
    q('newTaskSheet').classList.add('hidden');
    q('connectionSheet').classList.add('hidden');
    q('taskSettingsSheet').classList.add('hidden');
    q('renameSheet').classList.add('hidden');
    if (q('requestSheet')) q('requestSheet').classList.add('hidden');
    setProjectsOpen(false);
    q('managementContent').replaceChildren();
    const loadingRow = document.createElement('div'); loadingRow.className = 'management-row';
    const loadingLabel = document.createElement('span'); loadingLabel.textContent = '目录';
    const loadingValue = document.createElement('strong'); loadingValue.textContent = '正在加载';
    loadingRow.append(loadingLabel, loadingValue); q('managementContent').append(loadingRow);
    q('managementNote').textContent = '只读取当前 Runner 已公开的数据。';
    try {
      state.management = await jsonFetch(`${API}/management?host_ref=${encodeURIComponent(state.selectedHost)}`);
      if (!q('managementSheet').classList.contains('hidden')) renderManagement();
    } catch (error) {
      q('managementContent').replaceChildren();
      const row = document.createElement('div'); row.className = 'management-row';
      const label = document.createElement('span'); label.textContent = '读取失败';
      const value = document.createElement('strong'); value.textContent = error.message;
      row.append(label, value); q('managementContent').append(row);
    }
    window.setTimeout(() => q('closeManagement').focus(), 0);
  }
  const otherOpen = !q('newTaskSheet').classList.contains('hidden') || !q('connectionSheet').classList.contains('hidden') || !q('managementSheet').classList.contains('hidden') || !q('taskSettingsSheet').classList.contains('hidden') || !q('renameSheet').classList.contains('hidden') || q('projectPanel').classList.contains('open') || !q('imageDialog').classList.contains('hidden');
  q('modalBackdrop').classList.toggle('hidden', !open && !otherOpen);
  syncDialogBackground();
  if (!open && wasOpen) { restoreFocus(state.managementReturnFocus); state.managementReturnFocus = null; }
}

function renderManagement() {
  const root = q('managementContent'); root.replaceChildren();
  const management = state.management || {};
  const add = (label, value) => { const row = document.createElement('div'); row.className = 'management-row'; const left = document.createElement('span'); left.textContent = label; const right = document.createElement('strong'); right.textContent = value; row.append(left, right); root.append(row); };
  const feature = management.features || {};
  add('任务改名', feature.rename?.available ? '可用' : 'Runner 更新后可用');
  add('任务置顶', feature.pin?.available ? '可用' : 'Runner 更新后可用');
  add('派生任务', feature.fork?.available ? '可用' : 'Runner 更新后可用');
  add('代码审查', feature.review?.available ? '可用' : 'Runner 更新后可用');
  add('Git Diff', feature.git_diff?.available ? '可用 · 按需分页' : 'Runner 更新后可用');
  add('全局搜索', feature.global_search?.available ? '已启用 · 已同步数据' : '不可用');
  add('模型目录', `${(management.models || []).length} 个模型`);
  const settings = management.settings;
  if (settings) for (const [key, value] of Object.entries(settings)) add(`设置 · ${key}`, value || '未设置');
  else add('有效设置', 'Runner 更新后可查看');
  const profiles = management.permission_profiles;
  if (profiles) for (const profile of profiles) add(`权限 · ${profile.label}`, 'Runner 已允许');
  else add('权限目录', 'Runner 更新后可查看');
  q('managementNote').textContent = '这是只读目录；缺失能力会明确显示，不会假装设置成功。';
}

function collaborationModeReason(value) {
  return {
    current_mode_unavailable: 'Mac 重启或普通同步后无法确认当前模式',
    active_turn_in_progress: '任务正在运行，请结束当前 Turn 后再修改',
    snapshot_refresh_required: '需要等待 Mac 刷新任务状态',
    runner_offline: 'Mac Runner 当前离线',
    runner_update_required: 'Runner 更新后可用',
  }[value] || '当前模式不可编辑';
}

function renderTaskSettings() {
  const settings = state.threadSettings;
  const current = settings?.current || {};
  const update = settings?.features?.update || {available: false, reason: 'runner_update_required'};
  const select = q('taskCollaborationMode');
  const selected = select.value;
  populateCollaborationModes(select, {value: selected || current.id || ''});
  const currentText = current.id ? `Mac 已确认：${current.label || current.id}` : collaborationModeReason(current.reason || 'current_mode_unavailable');
  q('taskSettingsState').textContent = currentText;
  q('taskSettingsState').className = `task-settings-state${current.id ? '' : ' warning'}`;
  select.disabled = state.settingsBusy || !update.available;
  q('saveTaskSettings').disabled = state.settingsBusy || !update.available || !select.value || select.value === current.id;
  q('taskSettingsFeedback').textContent = update.available ? '保存后等待 Mac 精确确认；结果未知时不会自动重发。' : collaborationModeReason(update.reason);
}

async function setTaskSettingsOpen(open) {
  const wasOpen = !q('taskSettingsSheet').classList.contains('hidden');
  if (open && !wasOpen) state.taskSettingsReturnFocus = document.activeElement;
  q('taskSettingsSheet').classList.toggle('hidden', !open);
  if (open) {
    q('taskMenu').open = false;
    q('newTaskSheet').classList.add('hidden');
    q('connectionSheet').classList.add('hidden');
    q('managementSheet').classList.add('hidden');
    q('renameSheet').classList.add('hidden');
    q('reviewSheet').classList.add('hidden');
    q('resourceSheet').classList.add('hidden');
    state.threadSettings = null;
    q('taskSettingsState').textContent = '正在读取当前模式。';
    q('taskSettingsFeedback').textContent = '';
    q('saveTaskSettings').disabled = true;
    try {
      state.threadSettings = await jsonFetch(`${API}/threads/${encodeURIComponent(state.selectedThread)}/settings`);
      if (!q('taskSettingsSheet').classList.contains('hidden')) renderTaskSettings();
    } catch (error) {
      q('taskSettingsState').textContent = error.message;
      q('taskSettingsState').className = 'task-settings-state warning';
    }
    window.setTimeout(() => q('closeTaskSettings').focus(), 0);
  }
  q('modalBackdrop').classList.toggle('hidden', !open);
  syncDialogBackground();
  if (!open && wasOpen) { restoreFocus(state.taskSettingsReturnFocus); state.taskSettingsReturnFocus = null; }
}

function setRenameOpen(open) {
  const wasOpen = !q('renameSheet').classList.contains('hidden');
  if (open && !wasOpen) state.renameReturnFocus = document.activeElement;
  q('renameSheet').classList.toggle('hidden', !open);
  if (open) {
    q('taskMenu').open = false;
    q('renameInput').value = state.detail?.title || '';
    q('renameFeedback').textContent = hasCapability('thread_rename_v1') ? '保存后等待 Mac 确认。' : '当前 Runner 尚未提供重命名能力。';
    q('saveRename').disabled = !writeAvailable() || !hasCapability('thread_rename_v1');
    window.setTimeout(() => { q('renameInput').focus(); q('renameInput').select(); }, 0);
  }
  q('modalBackdrop').classList.toggle('hidden', !open);
  syncDialogBackground();
  if (!open && wasOpen) { restoreFocus(state.renameReturnFocus); state.renameReturnFocus = null; }
}

function setReviewOpen(open) {
  const wasOpen = !q('reviewSheet').classList.contains('hidden');
  if (open && !wasOpen) state.reviewReturnFocus = document.activeElement;
  q('reviewSheet').classList.toggle('hidden', !open);
  if (open) {
    q('taskMenu').open = false;
    q('newTaskSheet').classList.add('hidden');
    q('connectionSheet').classList.add('hidden');
    q('managementSheet').classList.add('hidden');
    q('renameSheet').classList.add('hidden');
    q('resourceSheet').classList.add('hidden');
    if (q('requestSheet')) q('requestSheet').classList.add('hidden');
    q('reviewType').value = 'uncommittedChanges';
    q('reviewBranch').value = '';
    q('reviewBranchField').classList.add('hidden');
    q('reviewFeedback').textContent = '审查会在当前任务中启动新的 Turn。';
    q('startReview').disabled = !writeAvailable() || !hasCapability('review_inline_v1');
    window.setTimeout(() => q('reviewType').focus(), 0);
  }
  q('modalBackdrop').classList.toggle('hidden', !open);
  syncDialogBackground();
  if (!open && wasOpen) { restoreFocus(state.reviewReturnFocus); state.reviewReturnFocus = null; }
}

function resetResourceView() {
  if (resourceUi.file?.objectUrl) URL.revokeObjectURL(resourceUi.file.objectUrl);
  resourceUi.path = '';
  resourceUi.entries = [];
  resourceUi.cursor = null;
  resourceUi.hasMore = false;
  resourceUi.loading = false;
  resourceUi.pending.clear();
  resourceUi.file = null;
  resourceUi.chunks = [];
  resourceUi.diagnostic = null;
  resourceUi.diffSummary = null;
  resourceUi.diffFile = null;
  resourceUi.diffChunks = [];
  resourceUi.error = '';
}

function setResourceOpen(open) {
  const sheet = q('resourceSheet');
  const wasOpen = !sheet.classList.contains('hidden');
  if (open && !wasOpen) resourceUi.returnFocus = document.activeElement;
  sheet.classList.toggle('hidden', !open);
  if (open) {
    q('taskMenu').open = false;
    q('newTaskSheet').classList.add('hidden');
    q('connectionSheet').classList.add('hidden');
    q('managementSheet').classList.add('hidden');
    q('renameSheet').classList.add('hidden');
    q('reviewSheet').classList.add('hidden');
    if (q('requestSheet')) q('requestSheet').classList.add('hidden');
    resetResourceView();
    resourceUi.tab = hasCapability('file_browse_v1') ? 'files' : hasCapability('git_diff_v1') ? 'diff' : 'diagnostics';
    renderResources();
    if (resourceUi.tab === 'files') void requestResourceList('');
    if (resourceUi.tab === 'diff') void requestDiffSummary();
    window.setTimeout(() => q('closeResource').focus(), 0);
  }
  q('modalBackdrop').classList.toggle('hidden', !open);
  syncDialogBackground();
  if (!open && wasOpen) {
    restoreFocus(resourceUi.returnFocus);
    resourceUi.returnFocus = null;
  }
}

function resourceCapabilities() {
  return {files: hasCapability('file_browse_v1'), artifacts: hasCapability('artifact_read_v1'), diff: hasCapability('git_diff_v1'), diagnostics: hasCapability('fixed_diagnostics_v1')};
}

function setResourceTab(tab) {
  if (!['files', 'diff', 'diagnostics'].includes(tab) || resourceUi.tab === tab) return;
  resourceUi.tab = tab;
  resetResourceView();
  resourceUi.tab = tab;
  renderResources();
  if (tab === 'files' && resourceCapabilities().files) void requestResourceList('');
  if (tab === 'diff' && resourceCapabilities().diff) void requestDiffSummary();
}

async function submitResource(route, body, context) {
  if (!state.detail || resourceUi.loading) return;
  const request = requestId();
  const threadRef = state.detail.thread_ref;
  const document = {request_id: request, thread_revision: state.detail.thread_revision, ...body};
  resourceUi.pending.set(request, context);
  resourceUi.loading = true;
  resourceUi.error = '';
  renderResources();
  try {
    const result = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}/${route}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(document)});
    if (!['pending', 'submitted', 'accepted', 'confirmed'].includes(result.state)) {
      resourceUi.pending.delete(request);
      resourceUi.loading = false;
      resourceUi.error = result.state === 'unknown' ? '结果暂时无法确认；不会自动重发，请重新打开抽屉核对。' : `请求未完成：${result.error_code || result.state || 'unknown'}`;
      renderResources();
    }
  } catch (error) {
    resourceUi.pending.delete(request);
    resourceUi.loading = false;
    resourceUi.error = `${error.message}。不会自动重发。`;
    renderResources();
  }
}

function requestResourceList(path, {append = false} = {}) {
  if (!resourceCapabilities().files) return;
  if (!append) {
    if (resourceUi.file?.objectUrl) URL.revokeObjectURL(resourceUi.file.objectUrl);
    resourceUi.file = null;
    resourceUi.chunks = [];
    resourceUi.entries = [];
    resourceUi.cursor = null;
    resourceUi.hasMore = false;
  }
  void submitResource('files/list', {relative_path: path, limit: 100, ...(append && resourceUi.cursor ? {cursor: resourceUi.cursor} : {})}, {type: 'list', path, append});
}

function openResourceEntry(entry) {
  if (!state.detail || resourceUi.loading) return;
  const relativePath = resourceUi.path ? `${resourceUi.path}/${entry.name}` : entry.name;
  if (entry.kind === 'directory') {
    requestResourceList(relativePath);
    return;
  }
  if (entry.kind !== 'file' || entry.readable !== true) return;
  const kind = entry.ref_kind === 'artifact' ? 'artifact' : 'file';
  if (kind === 'artifact' && !resourceCapabilities().artifacts) return;
  void submitResource('files/open', {relative_path: relativePath, file_kind: kind}, {type: 'open', relativePath, kind});
}

function requestResourceChunk() {
  const file = resourceUi.file;
  if (!file || resourceUi.loading || file.nextOffset === null) return;
  const limit = file.kind === 'file' ? 32 * 1024 : 64 * 1024;
  void submitResource('files/read', {file_ref: file.ref, offset: file.nextOffset, limit}, {type: 'read', ref: file.ref});
}

function runDiagnostic(diagnosticId) {
  if (!resourceCapabilities().diagnostics || resourceUi.loading) return;
  resourceUi.diagnostic = null;
  void submitResource('diagnostics/run', {diagnostic_id: diagnosticId}, {type: 'diagnostic', diagnosticId});
}

function requestDiffSummary() {
  if (!resourceCapabilities().diff || resourceUi.loading) return;
  resourceUi.diffSummary = null;
  resourceUi.diffFile = null;
  resourceUi.diffChunks = [];
  void submitResource('diff/summary', {limit: 100}, {type: 'diff-summary'});
}

function openDiffEntry(entry) {
  if (!resourceCapabilities().diff || resourceUi.loading || !entry?.diff_ref) return;
  resourceUi.diffFile = {...entry, nextCursor: null, complete: false, digest: ''};
  resourceUi.diffChunks = [];
  void submitResource('diff/read', {diff_ref: entry.diff_ref, limit: 32 * 1024}, {type: 'diff-read', diffRef: entry.diff_ref});
}

function requestDiffChunk() {
  const file = resourceUi.diffFile;
  if (!file || resourceUi.loading || file.complete || !file.nextCursor) return;
  void submitResource('diff/read', {diff_ref: file.diff_ref, cursor: file.nextCursor, limit: 32 * 1024}, {type: 'diff-read', diffRef: file.diff_ref});
}

function applyResourceEvent(event) {
  const payload = event?.payload || {};
  const pending = resourceUi.pending.get(payload.request_id);
  if (!pending) return;
  resourceUi.pending.delete(payload.request_id);
  resourceUi.loading = resourceUi.pending.size > 0;
  const kind = String(event.event_kind || '');
  if (kind.endsWith('.error')) {
    resourceUi.error = `读取失败：${payload.error_code || 'unknown'}`;
    renderResources();
    return;
  }
  if (kind === 'file.list' && pending.type === 'list' && payload.relative_path === pending.path) {
    resourceUi.path = payload.relative_path || '';
    resourceUi.entries = pending.append ? resourceUi.entries.concat(payload.entries || []) : (payload.entries || []);
    resourceUi.cursor = payload.next_cursor || null;
    resourceUi.hasMore = payload.has_more === true;
  } else if (kind === 'file.opened' && pending.type === 'open') {
    if (resourceUi.file?.objectUrl) URL.revokeObjectURL(resourceUi.file.objectUrl);
    resourceUi.file = {...payload, nextOffset: 0, objectUrl: ''};
    resourceUi.chunks = [];
    window.setTimeout(requestResourceChunk, 0);
  } else if (kind === 'file.chunk' && pending.type === 'read' && resourceUi.file?.ref === payload.ref) {
    const expectedOffset = resourceUi.chunks.reduce((sum, chunk) => sum + number(chunk.chunk_bytes), 0);
    if (payload.mime_type !== resourceUi.file.mime_type || payload.offset !== expectedOffset) {
      resourceUi.error = '读取分块与当前文件不一致；已停止加载，不会自动重试。';
      renderResources();
      return;
    }
    resourceUi.chunks.push(payload);
    resourceUi.file.nextOffset = payload.next_offset;
    if (payload.next_offset === null && resourceUi.file.kind === 'artifact') buildArtifactPreview();
    else if (payload.next_offset !== null) window.setTimeout(requestResourceChunk, 0);
  } else if (kind === 'diagnostic.result' && pending.type === 'diagnostic') {
    resourceUi.diagnostic = payload;
  } else if (kind === 'diff.summary' && pending.type === 'diff-summary') {
    resourceUi.diffSummary = payload;
    resourceUi.diffFile = null;
    resourceUi.diffChunks = [];
  } else if (kind === 'diff.chunk' && pending.type === 'diff-read' && pending.diffRef === payload.diff_ref && resourceUi.diffFile?.diff_ref === payload.diff_ref) {
    if (resourceUi.diffFile.digest && resourceUi.diffFile.digest !== payload.diff_digest) {
      resourceUi.error = '工作区在读取期间发生变化；请返回并重新加载 Diff 摘要。';
      renderResources();
      return;
    }
    resourceUi.diffFile = {...resourceUi.diffFile, relative_path: payload.relative_path, status: payload.status, digest: payload.diff_digest, nextCursor: payload.next_cursor || null, complete: payload.complete === true};
    resourceUi.diffChunks.push(payload.content || '');
  }
  renderResources();
}

function bytesLabel(value) {
  const size = Number(value);
  if (!Number.isFinite(size)) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
  return `${(size / 1024 / 1024).toFixed(1)} MiB`;
}

function buildArtifactPreview() {
  const file = resourceUi.file;
  if (!file || file.kind !== 'artifact' || file.nextOffset !== null || file.objectUrl) return;
  try {
    const parts = resourceUi.chunks.map(chunk => {
      const binary = atob(chunk.data_base64 || '');
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
      return bytes;
    });
    file.objectUrl = URL.createObjectURL(new Blob(parts, {type: file.mime_type}));
  } catch (_error) {
    resourceUi.error = '产物预览生成失败；可关闭后重新打开核对。';
  }
}

function renderResources() {
  const capabilities = resourceCapabilities();
  q('resourceFilesTab').setAttribute('aria-selected', String(resourceUi.tab === 'files'));
  q('resourceDiffTab').setAttribute('aria-selected', String(resourceUi.tab === 'diff'));
  q('resourceDiagnosticsTab').setAttribute('aria-selected', String(resourceUi.tab === 'diagnostics'));
  q('resourceFilesTab').disabled = !capabilities.files;
  q('resourceDiffTab').disabled = !capabilities.diff;
  q('resourceDiagnosticsTab').disabled = !capabilities.diagnostics;
  q('resourceToolbar').classList.toggle('hidden', resourceUi.tab === 'diagnostics');
  q('resourcePath').textContent = resourceUi.tab === 'diff' ? resourceUi.diffFile?.relative_path || '工作区变更' : resourceUi.file?.relative_path || resourceUi.path || '项目根目录';
  q('resourceBack').disabled = resourceUi.loading || (resourceUi.tab === 'diff' ? !resourceUi.diffFile : !resourceUi.file && !resourceUi.path);
  const root = q('resourceBody');
  root.replaceChildren();
  q('resourceMore').classList.add('hidden');
  q('resourceMore').disabled = resourceUi.loading;
  if (resourceUi.tab === 'diff') {
    if (resourceUi.diffFile) {
      const summary = document.createElement('div'); summary.className = 'resource-summary';
      const title = document.createElement('strong'); title.textContent = resourceUi.diffFile.relative_path;
      const meta = document.createElement('span'); meta.textContent = `${resourceUi.diffFile.status || 'modified'} · ${resourceUi.diffFile.digest || '正在计算摘要'}`;
      summary.append(title, meta);
      const output = document.createElement('pre'); output.className = 'resource-output'; output.textContent = resourceUi.diffChunks.join('') || '正在读取第一块 Diff…';
      root.append(summary, output);
      if (!resourceUi.diffFile.complete && resourceUi.diffFile.nextCursor) { q('resourceMore').classList.remove('hidden'); q('resourceMore').textContent = '继续加载 Diff'; }
    } else if (resourceUi.diffSummary) {
      const summary = document.createElement('div'); summary.className = 'resource-summary';
      const title = document.createElement('strong'); title.textContent = `${number(resourceUi.diffSummary.file_count)} 个变更文件`;
      const meta = document.createElement('span'); meta.textContent = `${resourceUi.diffSummary.workspace_digest}${resourceUi.diffSummary.has_more ? ' · 仅显示前 100 个' : ''}`;
      summary.append(title, meta); root.append(summary);
      const list = document.createElement('div'); list.className = 'resource-list';
      for (const entry of resourceUi.diffSummary.files || []) { const button = document.createElement('button'); button.type = 'button'; button.className = 'resource-entry'; const copy = document.createElement('div'); const name = document.createElement('div'); name.className = 'resource-entry-title'; name.textContent = entry.relative_path; const details = document.createElement('div'); details.className = 'resource-entry-meta'; const lines = entry.binary ? '二进制文件' : `+${number(entry.added_lines)} / -${number(entry.deleted_lines)}`; details.textContent = `${entry.status} · ${lines}`; copy.append(name, details); const arrow = document.createElement('span'); arrow.textContent = '查看'; button.append(copy, arrow); button.disabled = resourceUi.loading; button.onclick = () => openDiffEntry(entry); list.append(button); }
      if (!(resourceUi.diffSummary.files || []).length) { const empty = document.createElement('div'); empty.className = 'resource-empty'; empty.textContent = '当前工作区没有可显示的变更。'; list.append(empty); }
      root.append(list);
    } else {
      const empty = document.createElement('div'); empty.className = 'resource-empty'; empty.textContent = resourceUi.loading ? '正在读取变更文件统计…' : '打开后只读取文件统计，不会预先加载 Diff 内容。'; root.append(empty);
    }
  } else if (resourceUi.tab === 'diagnostics') {
    if (resourceUi.diagnostic) {
      const summary = document.createElement('div'); summary.className = 'resource-summary';
      const title = document.createElement('strong'); title.textContent = resourceUi.diagnostic.title || resourceUi.diagnostic.diagnostic_id;
      const meta = document.createElement('span'); meta.textContent = `${statusText(resourceUi.diagnostic.status)} · ${number(resourceUi.diagnostic.duration_ms)} ms${resourceUi.diagnostic.output_truncated ? ' · 已按安全上限截断' : ''}`;
      summary.append(title, meta);
      const output = document.createElement('pre'); output.className = 'resource-output'; output.textContent = [resourceUi.diagnostic.stdout, resourceUi.diagnostic.stderr].filter(Boolean).join('\n') || '没有输出';
      root.append(summary, output);
    } else {
      const list = document.createElement('div'); list.className = 'diagnostic-list';
      const items = [['git_status', 'Git 状态', '分支、已跟踪与未跟踪文件'], ['git_diff_stat', '未提交变更统计', '只显示文件和行数统计'], ['git_diff_check', 'Diff 格式检查', '检查空白与格式错误'], ['git_recent_commits', '最近提交', '最近二十条提交摘要']];
      for (const [id, titleText, description] of items) { const button = document.createElement('button'); button.type = 'button'; const strong = document.createElement('strong'); strong.textContent = titleText; const meta = document.createElement('div'); meta.className = 'resource-entry-meta'; meta.textContent = description; button.append(strong, meta); button.disabled = resourceUi.loading; button.onclick = () => runDiagnostic(id); list.append(button); }
      root.append(list);
    }
  } else if (resourceUi.file) {
    const summary = document.createElement('div'); summary.className = 'resource-summary';
    const title = document.createElement('strong'); title.textContent = resourceUi.file.relative_path;
    const meta = document.createElement('span'); meta.textContent = `${resourceUi.file.kind === 'artifact' ? '产物' : '文本'} · ${resourceUi.file.mime_type} · ${bytesLabel(resourceUi.file.size)}`;
    summary.append(title, meta); root.append(summary);
    if (resourceUi.file.kind === 'file') {
      const output = document.createElement('pre'); output.className = 'resource-output'; output.textContent = resourceUi.chunks.map(chunk => chunk.text || '').join('') || '正在读取第一块…'; root.append(output);
    } else if (resourceUi.file.objectUrl) {
      const preview = document.createElement('div'); preview.className = 'resource-preview';
      if (resourceUi.file.mime_type.startsWith('image/')) { const image = document.createElement('img'); image.src = resourceUi.file.objectUrl; image.alt = resourceUi.file.relative_path; preview.append(image); }
      else if (resourceUi.file.mime_type === 'application/pdf') { const object = document.createElement('object'); object.data = resourceUi.file.objectUrl; object.type = 'application/pdf'; object.setAttribute('aria-label', `${resourceUi.file.relative_path} PDF 预览`); preview.append(object); }
      const download = document.createElement('a'); download.className = 'resource-download'; download.href = resourceUi.file.objectUrl; download.download = resourceUi.file.relative_path.split('/').pop() || 'artifact'; download.textContent = resourceUi.file.mime_type === 'application/pdf' || resourceUi.file.mime_type.startsWith('image/') ? '下载原文件' : '下载产物'; preview.append(download); root.append(preview);
    } else {
      const output = document.createElement('pre'); output.className = 'resource-output'; output.textContent = resourceUi.file.nextOffset === null ? `产物已按 ${resourceUi.chunks.length} 个分块读取完成。` : `正在持续加载：${resourceUi.chunks.reduce((sum, chunk) => sum + number(chunk.chunk_bytes), 0)} / ${number(resourceUi.file.size)} 字节。`; root.append(output);
    }
  } else {
    const list = document.createElement('div'); list.className = 'resource-list';
    for (const entry of resourceUi.entries) { const button = document.createElement('button'); button.type = 'button'; button.className = 'resource-entry'; const copy = document.createElement('div'); const title = document.createElement('div'); title.className = 'resource-entry-title'; title.textContent = `${entry.kind === 'directory' ? '文件夹 · ' : ''}${entry.name}`; const meta = document.createElement('div'); meta.className = 'resource-entry-meta'; meta.textContent = entry.kind === 'file' ? `${entry.ref_kind === 'artifact' ? '产物' : entry.readable ? '文本' : '不可读取'}${Number.isFinite(Number(entry.size)) ? ` · ${bytesLabel(entry.size)}` : ''}` : entry.kind === 'directory' ? '打开文件夹' : '安全策略不允许读取'; copy.append(title, meta); const arrow = document.createElement('span'); arrow.textContent = entry.readable ? '打开' : ''; button.append(copy, arrow); button.disabled = resourceUi.loading || (entry.kind !== 'directory' && entry.readable !== true); button.onclick = () => openResourceEntry(entry); list.append(button); }
    if (!resourceUi.entries.length) { const empty = document.createElement('div'); empty.className = 'resource-empty'; empty.textContent = resourceUi.loading ? '正在读取目录…' : '这个目录没有可显示的条目。'; list.append(empty); }
    root.append(list);
    if (resourceUi.hasMore) { q('resourceMore').classList.remove('hidden'); q('resourceMore').textContent = '加载更多文件'; }
  }
  q('resourceStatus').className = `resource-status${resourceUi.error ? ' error' : ''}`;
  q('resourceStatus').textContent = resourceUi.error || (resourceUi.loading ? '正在通过 SSE 接收结果…' : resourceUi.tab === 'diff' && resourceUi.diffFile && !resourceUi.diffFile.complete ? 'Diff 每次最多加载 32 KiB；点击按钮继续。' : resourceUi.file && resourceUi.file.nextOffset !== null ? '分块间隔加载中，页面会持续更新。' : '所有内容均按需分块读取；不会加载完整项目或运行任意命令。');
}

function setProjectsOpen(open) {
  if (open) state.projectReturnFocus = document.activeElement;
  q('projectPanel').classList.toggle('open', open);
  if (open) {
    q('newTaskSheet').classList.add('hidden');
    q('connectionSheet').classList.add('hidden');
  }
  q('modalBackdrop').classList.toggle('hidden', !open && q('newTaskSheet').classList.contains('hidden') && q('connectionSheet').classList.contains('hidden'));
  syncDialogBackground();
  if (open) q('closeProjects').focus();
  else if (state.projectReturnFocus) { restoreFocus(state.projectReturnFocus); state.projectReturnFocus = null; }
}

function renderMetrics() {
  q('metricHosts').textContent = state.hosts.length;
  q('metricProjects').textContent = state.projects.length;
  q('metricThreads').textContent = state.projects.reduce((sum, project) => sum + number(project.counts?.total), 0) || state.threads.length;
  q('metricActive').textContent = state.threads.filter(thread => thread.status === 'active').length;
  q('metricRecovery').textContent = state.threads.filter(thread => ['failed', 'recovery_required', 'protocol_degraded'].includes(thread.status) || thread.control_state === 'protocol_degraded').length;
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
    renderRunnerBanner();
    renderNewTaskState();
    return;
  }
  select.disabled = false;
  for (const host of state.hosts) {
    const option = document.createElement('option');
    option.value = host.host_ref;
    option.textContent = `${host.online ? '已连接' : '离线'} · ${host.app_version ? 'Codex App' : 'Mac'}`;
    select.append(option);
  }
  select.value = state.selectedHost;
  const host = currentHost();
  if (!host) return;
  q('hostMeta').textContent = `最近自动同步：${formatTime(host.synced_at)}`;
  q('hostWriteState').className = `badge ${host.write_available ? 'good' : host.online ? 'warn' : 'bad'}`;
  q('hostWriteState').textContent = host.write_available ? '可控制' : host.online ? '只读' : '离线';
  renderRunnerBanner();
  renderNewTaskState();
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
  allMeta.textContent = `${state.threads.length} 个任务`;
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
  return state.threads.filter(thread => {
    if (state.selectedProject !== 'all' && thread.project_ref !== state.selectedProject) return false;
    if (status !== 'all' && thread.status !== status) return false;
    return true;
  });
}

function renderThreads() {
  const list = q('threadList');
  list.replaceChildren();
  const rank = {active: 0, recovery_required: 1, failed: 1, protocol_degraded: 2, idle: 3, notLoaded: 4, archived: 5};
  const searching = Boolean(q('threadSearch').value.trim());
  const threads = filteredThreads().sort((left, right) => number(right.pinned) - number(left.pinned) || (rank[left.status] ?? 9) - (rank[right.status] ?? 9) || new Date(right.updated_at || 0) - new Date(left.updated_at || 0));
  const total = state.projects.reduce((sum, project) => sum + number(project.counts?.total), 0) || state.threads.length;
  q('threadCount').textContent = total > state.threads.length ? `${state.threads.length}/${total}` : `${threads.length}`;
  for (const thread of threads) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `thread-button ${thread.status === 'active' ? 'active-thread' : ''} ${state.selectedThread === thread.thread_ref ? 'selected' : ''}`.trim();
    button.setAttribute('aria-pressed', state.selectedThread === thread.thread_ref ? 'true' : 'false');
    const heading = document.createElement('div');
    heading.className = 'thread-heading';
    const title = document.createElement('div');
    title.className = 'thread-title';
    title.textContent = text(thread.title, '未命名任务');
    const status = document.createElement('span');
    status.className = `thread-state ${statusKind(thread.status)}`.trim();
    status.textContent = statusText(thread.status);
    heading.append(title, status);
    if (thread.pinned === true) heading.append(badge('已置顶'));
    const meta = document.createElement('div');
    meta.className = 'thread-meta';
    const project = state.projects.find(project => project.project_ref === thread.project_ref);
    meta.textContent = `${project?.project_alias || '当前项目'} · ${formatTime(thread.updated_at)}`;
    const preview = document.createElement('div');
    preview.className = 'thread-preview';
    preview.textContent = text(thread.preview || thread.snapshot?.preview, thread.status === 'active' ? '正在处理，回复会自动更新' : '打开继续对话');
    button.append(heading, meta, preview);
    button.onclick = () => selectThread(thread.thread_ref);
    list.append(button);
  }
  if (!threads.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = searching ? '没有找到匹配的已同步任务。' : state.threads.length ? '这里暂时没有符合条件的任务。' : '正在等待 Mac 同步任务。';
    list.append(empty);
  }
  q('loadMoreThreads').classList.toggle('hidden', !state.threadsHasMore);
  q('loadMoreThreads').disabled = state.threadsLoading;
  q('loadMoreThreads').textContent = state.threadsLoading ? '正在加载…' : '加载更多任务';
  q('threadListStatus').textContent = state.threadsLoading ? '正在异步加载下一页' : state.threadsHasMore ? `已加载 ${state.threads.length} 个，继续向下按需加载` : searching ? `已搜索 ${state.threads.length} 个匹配任务 · 范围为已同步数据` : `已加载 ${state.threads.length} 个任务`;
}

function renderDetail() {
  const detail = state.detail;
  q('detailEmpty').classList.toggle('hidden', Boolean(detail));
  q('detailContent').classList.toggle('hidden', !detail);
  if (!detail) return;
  const snapshot = detail.snapshot || {};
  q('detailTitle').textContent = text(detail.title, '未命名任务');
  q('detailProject').textContent = state.projects.find(project => project.project_ref === detail.project_ref)?.project_alias || '当前项目';
  q('detailPreview').textContent = text(snapshot.preview, detail.status === 'active' ? 'Codex 正在处理这个任务' : '可以继续发送消息');
  const meta = q('detailMeta');
  meta.replaceChildren(badge(statusText(detail.status), statusKind(detail.status)), badge(`更新于 ${formatTime(detail.updated_at)}`));
  if (detail.pinned === true || snapshot.pinned === true) meta.append(badge('已置顶'));
  const sync = badge('正在同步', 'sync-badge');
  sync.id = 'detailSyncState';
  meta.append(sync);
  if (snapshot.history_incomplete || state.historyHasMore) meta.append(badge('可继续加载较早消息', 'warn'));
  renderNotice(detail);
  renderPendingRequests();
  renderDeliveryStatus(detail);
  renderActionState(detail);
  renderHistoryControls();
  renderConversation(mergedConversationTurns());
  renderComposer(detail);
  renderQueue(detail);
  renderFreshness();
}

function renderDeliveryStatus(detail) {
  let root = q('deliveryStatus');
  if (!root) {
    root = document.createElement('div');
    root.id = 'deliveryStatus';
    root.className = 'delivery hidden';
    root.setAttribute('role', 'status');
    root.setAttribute('aria-live', 'polite');
    q('detailNotice').insertAdjacentElement('afterend', root);
  }
  const command = detail?.latest_command;
  root.replaceChildren();
  root.classList.toggle('hidden', !command);
  if (!command) return;
  const inferred = {pending: 'controller_received', submitted: 'relay_delivered', accepted: 'runner_received', confirmed: 'mac_confirmed'};
  const stage = command.delivery_stage || inferred[command.state] || 'controller_received';
  const stageIndex = {controller_received: 0, relay_delivered: 1, runner_received: 2, mac_confirmed: 3}[stage] ?? 0;
  const failed = ['failed', 'unknown', 'recovery_required', 'conflict', 'expired'].includes(command.state);
  const labels = ['Controller 已接收', 'Relay 已送达', 'Runner 已接收', 'Mac 已确认'];
  labels.forEach((label, index) => {
    const item = document.createElement('div');
    item.className = 'delivery-stage';
    item.textContent = label;
    const done = stage === 'mac_confirmed' || index < stageIndex;
    const current = !done && index === Math.min(stageIndex, labels.length - 1);
    if (done) item.classList.add('done');
    else if (failed && current) item.classList.add('failed');
    else if (current) item.classList.add('current');
    root.append(item);
  });
}

function renderNotice(detail) {
  const notice = q('detailNotice');
  notice.className = 'notice hidden';
  notice.textContent = '';
  if (detail.status === 'protocol_degraded' || detail.control_state === 'protocol_degraded') {
    notice.className = 'notice bad';
    notice.textContent = 'Mac 端版本暂时不兼容，现在只能查看消息；恢复兼容后会自动重新连接。';
  } else if (detail.status === 'recovery_required' || detail.control_state === 'recovery_required') {
    notice.className = 'notice bad';
    notice.textContent = '上一条操作结果还不能确认。系统不会重复发送，请等待自动核对。';
  } else if (state.mode === 'native' && detail.status === 'active') {
    notice.className = 'notice warn';
    notice.textContent = '快速调整会直接修改当前运行方向；如果 Mac 同时操作，可能发生冲突。';
  } else if (detail.control_state === 'load_required') {
    notice.className = 'notice';
    notice.textContent = '发送后会先在 Mac 上打开这个原任务，再继续对话。';
  } else if (detail.latest_command && ['submitted', 'accepted', 'unknown'].includes(detail.latest_command.state)) {
    notice.className = detail.latest_command.state === 'unknown' ? 'notice bad' : 'notice warn';
    notice.textContent = detail.latest_command.state === 'unknown' ? '上一条消息是否送达还不能确认，已暂停继续发送，避免重复。' : '消息已经交给 Mac，正在等待 Codex 接收。';
  }
}

function hostCapabilities() { return new Set(currentHost()?.capabilities || []); }
function hasCapability(value) { return hostCapabilities().has(value); }
function writeAvailable() { return currentHost()?.write_available === true; }

function initPermissionUi() {
  const makeField = (selectId, metaId, labelText) => {
    const field = document.createElement('div'); field.className = 'field';
    const label = document.createElement('label'); label.htmlFor = selectId; label.textContent = labelText;
    const select = document.createElement('select'); select.id = selectId; select.disabled = true; select.setAttribute('aria-describedby', metaId);
    const meta = document.createElement('div'); meta.id = metaId; meta.className = 'model-meta';
    field.append(label, select, meta); return field;
  };
  q('advancedControls').querySelector('.advanced-grid').append(makeField('permissionSelect', 'permissionMeta', '权限 / 访问级别'));
  q('advancedControls').querySelector('.advanced-grid').append(makeField('collaborationModeSelect', 'collaborationModeMeta', '计划模式'));
  q('newTaskFeedback').before(makeField('newTaskPermission', 'newTaskPermissionMeta', '权限 / 访问级别'));
  q('newTaskFeedback').before(makeField('newTaskCollaborationMode', 'newTaskCollaborationModeMeta', '计划模式'));
  renderPermissionSelectors();
  renderCollaborationModeSelectors();
}

function appendPermissionOptions(select, profiles) {
  for (const profile of profiles) {
    const option = document.createElement('option');
    option.value = profile.id;
    option.textContent = profile.label || profile.id;
    select.append(option);
  }
}

function permissionReason(value) {
  const labels = {
    permission_profile_state_missing: '当前任务没有可验证的权限状态',
    permission_profile_unsupported: '当前任务使用自定义权限',
    permission_profile_state_mismatch: '权限档位与隔离状态不一致',
    permission_profile_allowlist_missing: 'Runner 未配置可用权限档位',
  };
  return labels[value] || '当前权限状态不可编辑';
}

function renderPermissionSelectors(detail = state.detail) {
  const supported = hasCapability('permission_profile_selection_v1');
  const profile = detail?.snapshot?.permission_profile;
  const currentOptions = Array.isArray(profile?.options) ? profile.options : [];
  const currentSelect = q('permissionSelect');
  const currentMeta = q('permissionMeta');
  if (currentSelect && currentMeta) {
    if (!supported || profile?.editable !== true || !currentOptions.some(option => option.id === state.selectedPermission)) state.selectedPermission = '';
    currentSelect.replaceChildren();
    const inherit = document.createElement('option'); inherit.value = ''; inherit.textContent = profile?.label ? `保持当前：${profile.label}` : '保持 Mac 当前权限'; currentSelect.append(inherit);
    appendPermissionOptions(currentSelect, currentOptions);
    currentSelect.value = state.selectedPermission;
    const action = detail ? composerAction(detail) : null;
    const safeAction = action === 'continue' || (action === 'steer' && state.mode === 'safe');
    currentSelect.disabled = !supported || profile?.editable !== true || !safeAction || !writeAvailable();
    if (!supported) currentMeta.textContent = 'Runner 更新并启用权限档位后可选择。';
    else if (profile?.editable !== true) currentMeta.textContent = `${profile?.label || '权限状态不可用'}：${permissionReason(profile?.reason)}。`;
    else if (!safeAction) currentMeta.textContent = '快速调整不允许修改权限；切换到安全调整后可收紧。';
    else currentMeta.textContent = '仅对本次新 Turn 生效，只能保持或收紧当前权限。';
  }
  const createSelect = q('newTaskPermission');
  const createMeta = q('newTaskPermissionMeta');
  if (createSelect && createMeta) {
    const profiles = Array.isArray(currentHost()?.permission_profiles) ? currentHost().permission_profiles : [];
    const saved = imageState.createFields[state.selectedHost]?.permission || '';
    const pendingPermission = state.pendingCreate?.body.host_ref === state.selectedHost ? state.pendingCreate.body.permission_profile_id : '';
    const previous = supported ? pendingPermission || createSelect.value || saved : '';
    createSelect.replaceChildren();
    const inherit = document.createElement('option'); inherit.value = ''; inherit.textContent = '使用 Mac 默认权限'; createSelect.append(inherit);
    appendPermissionOptions(createSelect, profiles);
    createSelect.value = profiles.some(option => option.id === previous) ? previous : '';
    const locked = state.pendingCreate?.body.host_ref === state.selectedHost;
    createSelect.disabled = !supported || !profiles.length || !hostCanCreate() || Boolean(locked);
    createMeta.textContent = supported && profiles.length ? '只能选择 Runner 预先允许的权限档位。' : 'Runner 未公开可选权限档位，将使用 Mac 默认值。';
  }
}

function renderCollaborationModeSelectors(detail = state.detail) {
  const supported = hasCapability('collaboration_mode_turn_v1') && hasCapability('collaboration_mode_catalog_v1');
  const currentSelect = q('collaborationModeSelect');
  const currentMeta = q('collaborationModeMeta');
  if (currentSelect && currentMeta) {
    const action = detail ? composerAction(detail) : null;
    const safeAction = action === 'continue' || (action === 'steer' && state.mode === 'safe');
    const available = populateCollaborationModes(currentSelect, {value: state.selectedCollaborationMode});
    state.selectedCollaborationMode = currentSelect.value;
    currentSelect.disabled = !supported || !available || !safeAction || !writeAvailable();
    if (!supported || !available) currentMeta.textContent = 'Runner 更新并公开模式目录后可选择。';
    else if (!safeAction) currentMeta.textContent = '快速调整保持当前 Turn，不能切换计划模式。';
    else currentMeta.textContent = '仅对本次新 Turn 生效；不选择则沿用任务当前模式。';
  }
  const createSelect = q('newTaskCollaborationMode');
  const createMeta = q('newTaskCollaborationModeMeta');
  if (createSelect && createMeta) {
    const saved = imageState.createFields[state.selectedHost]?.collaborationMode || '';
    const pendingMode = state.pendingCreate?.body.host_ref === state.selectedHost ? state.pendingCreate.body.collaboration_mode_id : '';
    const previous = supported ? pendingMode || createSelect.value || saved : '';
    const available = populateCollaborationModes(createSelect, {value: previous});
    const locked = state.pendingCreate?.body.host_ref === state.selectedHost;
    createSelect.disabled = !supported || !available || !hostCanCreate() || Boolean(locked);
    createMeta.textContent = supported && available ? '可直接以计划模式或默认模式开始新任务。' : 'Runner 未公开计划模式目录，将使用 Mac 默认值。';
  }
}

function initRequestUi() {
  const tray = document.createElement('section'); tray.id = 'requestTray'; tray.className = 'request-tray hidden'; tray.setAttribute('aria-live', 'polite');
  q('detailNotice').insertAdjacentElement('afterend', tray);
  const sheet = document.createElement('section'); sheet.id = 'requestSheet'; sheet.className = 'request-sheet hidden'; sheet.setAttribute('role', 'dialog'); sheet.setAttribute('aria-modal', 'true'); sheet.setAttribute('aria-labelledby', 'requestSheetTitle');
  const handle = document.createElement('div'); handle.className = 'sheet-handle';
  const head = document.createElement('div'); head.className = 'sheet-head';
  const title = document.createElement('h2'); title.id = 'requestSheetTitle'; title.textContent = '待处理请求';
  const close = document.createElement('button'); close.id = 'closeRequest'; close.type = 'button'; close.className = 'ghost'; close.textContent = '关闭'; close.onclick = () => setRequestOpen();
  head.append(title, close);
  const body = document.createElement('div'); body.id = 'requestSheetBody'; body.className = 'request-detail';
  sheet.append(handle, head, body); document.body.append(sheet);
}

function pendingRequests() {
  const values = state.detail?.snapshot?.pending_requests;
  return Array.isArray(values) ? values : [];
}

function requestKindLabel(kind) {
  return {command_approval: '命令审批', file_approval: '文件更改审批', permissions_approval: '额外权限审批', user_input: '需要你的回答', mcp_elicitation: '工具提问'}[kind] || '待处理请求';
}

function requestSafeSummary(request) {
  if (request.kind === 'command_approval') return 'Codex 请求执行一项命令。手机端不展示原始命令、终端路径或隐藏参数，请结合当前任务上下文决定。';
  if (request.kind === 'permissions_approval') {
    const permission = request.permission_summary || {};
    return `${text(request.summary, '请求额外权限')}\n文件访问项：${number(permission.file_entry_count)} · 网络访问：${permission.network_requested ? '请求开启' : '未请求'}`;
  }
  if (request.kind === 'user_input') return `共有 ${(request.questions || []).length} 个问题需要回答。`;
  return text(request.summary, requestKindLabel(request.kind));
}

function pendingResponseState(requestRef) {
  const command = state.detail?.latest_command;
  if (command?.action !== 'respond_request' || command.request_ref !== requestRef) return '';
  return command.state || '';
}

function renderPendingRequests() {
  const tray = q('requestTray'); if (!tray) return;
  const requests = pendingRequests(); tray.replaceChildren(); tray.classList.toggle('hidden', !requests.length);
  if (!requests.length) { if (!q('requestSheet').classList.contains('hidden')) setRequestOpen(); return; }
  const head = document.createElement('div'); head.className = 'request-tray-head';
  const title = document.createElement('h3'); title.textContent = `需要处理 · ${requests.length}`;
  const status = badge('实时同步', 'warn'); head.append(title, status); tray.append(head);
  const list = document.createElement('div'); list.className = 'request-list';
  for (const request of requests) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'request-card';
    const commandState = pendingResponseState(request.request_ref);
    const uncertain = ['unknown', 'recovery_required'].includes(commandState);
    const waiting = ['pending', 'submitted', 'accepted'].includes(commandState);
    if (uncertain) button.classList.add('unknown');
    const name = document.createElement('strong'); name.textContent = text(request.title, requestKindLabel(request.kind));
    const summary = document.createElement('span'); summary.textContent = uncertain ? '上次响应结果未知，已禁止再次发送；等待 Mac 快照核对。' : waiting ? '响应已送往 Mac，正在等待读回确认。' : requestSafeSummary(request);
    button.append(name, summary); button.disabled = requestUiState.busy;
    button.onclick = () => setRequestOpen(request.request_ref);
    list.append(button);
  }
  tray.append(list);
}

function setRequestOpen(requestRef = '') {
  const sheet = q('requestSheet'); if (!sheet) return;
  const open = Boolean(requestRef);
  const wasOpen = !sheet.classList.contains('hidden');
  if (open && !wasOpen) requestUiState.returnFocus = document.activeElement;
  requestUiState.selectedRef = requestRef;
  sheet.classList.toggle('hidden', !open);
  if (open) {
    q('newTaskSheet').classList.add('hidden'); q('connectionSheet').classList.add('hidden'); q('managementSheet').classList.add('hidden'); q('renameSheet').classList.add('hidden');
    renderRequestSheet(); window.setTimeout(() => q('closeRequest').focus(), 0);
  }
  const otherOpen = !q('newTaskSheet').classList.contains('hidden') || !q('connectionSheet').classList.contains('hidden') || !q('managementSheet').classList.contains('hidden') || !q('renameSheet').classList.contains('hidden') || q('projectPanel').classList.contains('open') || !q('imageDialog').classList.contains('hidden');
  q('modalBackdrop').classList.toggle('hidden', !open && !otherOpen);
  syncDialogBackground();
  if (!open && wasOpen) { restoreFocus(requestUiState.returnFocus); requestUiState.returnFocus = null; }
}

function renderRequestSheet() {
  const root = q('requestSheetBody'); root.replaceChildren();
  const request = pendingRequests().find(item => item.request_ref === requestUiState.selectedRef);
  if (!request) { const message = document.createElement('p'); message.className = 'muted'; message.textContent = '这项请求已经处理或失效。'; root.append(message); return; }
  q('requestSheetTitle').textContent = text(request.title, requestKindLabel(request.kind));
  const summary = document.createElement('p'); summary.className = 'request-summary'; summary.textContent = requestSafeSummary(request); root.append(summary);
  for (const question of request.questions || []) root.append(requestQuestionField(question));
  const commandState = pendingResponseState(request.request_ref);
  if (['unknown', 'recovery_required'].includes(commandState)) {
    const warning = document.createElement('p'); warning.className = 'request-warning'; warning.textContent = '上次响应结果未知。系统不会自动或手动重放，只会等待 Mac 的后续快照确认。'; root.append(warning); return;
  }
  if (['pending', 'submitted', 'accepted'].includes(commandState)) {
    const waiting = document.createElement('p'); waiting.className = 'muted'; waiting.textContent = '响应已送往 Mac，正在等待 SSE 推送读回确认。'; root.append(waiting); return;
  }
  const actions = document.createElement('div'); actions.className = 'request-decisions';
  const labels = {accept: '允许', decline: '拒绝', cancel: '取消请求', submit: '提交答案'};
  for (const decision of request.decisions || []) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = labels[decision] || decision;
    if (decision === 'accept' || decision === 'submit') button.className = 'primary';
    if (decision === 'decline') button.className = 'danger';
    button.disabled = requestUiState.busy || !writeAvailable() || !hasCapability('owner_request_response_v1');
    button.onclick = () => void respondToPendingRequest(request, decision);
    actions.append(button);
  }
  root.append(actions);
}

function requestQuestionField(question) {
  const field = document.createElement('div'); field.className = 'request-question'; field.dataset.questionRef = question.question_ref; field.dataset.valueType = question.value_type || 'string'; field.dataset.required = question.required ? 'true' : 'false';
  const title = document.createElement('h3'); title.textContent = text(question.header, '问题'); field.append(title);
  if (question.question) { const copy = document.createElement('p'); copy.textContent = question.question; field.append(copy); }
  if (question.secret) { const warning = document.createElement('div'); warning.className = 'request-warning'; warning.textContent = '此问题可能包含敏感信息，只能回到 Mac 上回答。'; field.append(warning); return field; }
  const options = Array.isArray(question.options) ? question.options : [];
  if (question.value_type === 'array' && options.length) {
    for (const option of options) {
      const label = document.createElement('label'); label.className = 'request-option';
      const input = document.createElement('input'); input.type = 'checkbox'; input.value = option.label;
      const copy = document.createElement('span'); copy.textContent = option.description ? `${option.label} · ${option.description}` : option.label;
      label.append(input, copy); field.append(label);
    }
  } else if (question.value_type === 'boolean') {
    const select = document.createElement('select'); select.className = 'request-answer';
    for (const [value, label] of [['', '请选择'], ['true', '是'], ['false', '否']]) { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); }
    field.append(select);
  } else if (options.length) {
    const select = document.createElement('select'); select.className = 'request-answer';
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = '请选择'; select.append(empty);
    for (const option of options) { const node = document.createElement('option'); node.value = option.label; node.textContent = option.description ? `${option.label} · ${option.description}` : option.label; select.append(node); }
    field.append(select);
    if (question.allows_other) { const other = document.createElement('input'); other.className = 'request-other'; other.maxLength = 1000; other.placeholder = '或输入其他答案'; field.append(other); }
  } else {
    const input = document.createElement('input'); input.className = 'request-answer'; input.maxLength = 1000; input.inputMode = ['number', 'integer'].includes(question.value_type) ? 'decimal' : 'text'; input.placeholder = question.required ? '请输入答案' : '选填'; field.append(input);
  }
  return field;
}

function collectRequestAnswers() {
  const answers = [];
  for (const field of q('requestSheetBody').querySelectorAll('.request-question')) {
    if (field.querySelector('.request-warning')) continue;
    let values = Array.from(field.querySelectorAll('input[type="checkbox"]:checked')).map(input => input.value);
    const other = field.querySelector('.request-other')?.value.trim();
    const single = field.querySelector('.request-answer')?.value.trim();
    if (other) values = [other]; else if (!values.length && single) values = field.dataset.valueType === 'array' ? single.split(',').map(value => value.trim()).filter(Boolean) : [single];
    if (!values.length) { if (field.dataset.required === 'true') throw new Error('请完成所有必填问题'); continue; }
    answers.push({question_ref: field.dataset.questionRef, answers: values});
  }
  return answers;
}

async function respondToPendingRequest(request, decision) {
  if (requestUiState.busy) return;
  let answers;
  if (decision === 'submit') {
    try { answers = collectRequestAnswers(); } catch (error) { const warning = document.createElement('p'); warning.className = 'request-warning'; warning.textContent = error.message; q('requestSheetBody').prepend(warning); return; }
  }
  requestUiState.busy = true; renderRequestSheet(); renderPendingRequests();
  await submitAction('respond_request', {expected_turn_ref: request.turn_ref, request_ref: request.request_ref, decision, ...(answers ? {answers} : {})});
  requestUiState.busy = false;
  if (pendingRequests().some(item => item.request_ref === request.request_ref)) renderRequestSheet(); else setRequestOpen();
  renderPendingRequests();
}

function renderActionState(detail) {
  const active = detail.status === 'active' && Boolean(detail.active_turn_ref);
  const blocked = !writeAvailable() || ['recovery_required', 'protocol_degraded'].includes(detail.status) || ['recovery_required', 'refresh_required', 'protocol_degraded'].includes(detail.control_state);
  q('interruptButton').classList.toggle('hidden', !active);
  q('interruptButton').disabled = blocked || !hasCapability('interrupt_expected_turn');
  q('archiveButton').classList.toggle('hidden', detail.status !== 'idle');
  q('archiveButton').disabled = blocked || !hasCapability('archive_control_v1');
  q('unarchiveButton').classList.toggle('hidden', detail.status !== 'archived');
  q('unarchiveButton').disabled = !writeAvailable() || !hasCapability('archive_control_v1');
  q('renameButton').disabled = !writeAvailable() || !hasCapability('thread_rename_v1');
  q('renameButton').title = q('renameButton').disabled ? 'Runner 更新后可用' : '';
  q('pinButton').disabled = !writeAvailable() || !hasCapability('thread_pin_v1');
  q('pinButton').title = q('pinButton').disabled ? 'Runner 更新后可用' : '';
  q('pinButton').textContent = detail.pinned === true || detail.snapshot?.pinned === true ? '取消置顶' : '置顶任务';
  q('forkButton').disabled = blocked || !hasCapability('thread_fork_v1');
  q('forkButton').title = q('forkButton').disabled ? 'Runner 更新且任务可控制后可用' : '';
  q('reviewButton').disabled = blocked || active || !hasCapability('review_inline_v1');
  q('reviewButton').title = q('reviewButton').disabled ? '任务空闲且 Runner 支持时可用' : '';
  q('resourceButton').disabled = !currentHost()?.online || (!hasCapability('file_browse_v1') && !hasCapability('git_diff_v1') && !hasCapability('fixed_diagnostics_v1'));
  q('resourceButton').title = q('resourceButton').disabled ? 'Runner 在线并更新后可用' : '';
}

function resetHistoryState() {
  state.historyTurns = [];
  state.historyCursor = null;
  state.historyHasMore = false;
  state.historyLoading = false;
  state.historyRequestId = '';
  state.historyInitialized = false;
  state.historyError = '';
  state.searchQuery = '';
  state.searchResults = [];
  state.searchCursor = null;
  state.searchHasMore = false;
  state.searchLoading = false;
  state.searchRequestId = '';
  state.searchAppend = false;
  state.searchError = '';
  if (q('historySearchInput')) q('historySearchInput').value = '';
}

function dedupeTurns(turns) {
  const values = [];
  const positions = new Map();
  for (const turn of turns || []) {
    const ref = turn?.turn_ref;
    if (!ref) continue;
    if (positions.has(ref)) values[positions.get(ref)] = turn;
    else { positions.set(ref, values.length); values.push(turn); }
  }
  return values;
}

function mergedConversationTurns() {
  return dedupeTurns([...(state.historyTurns || []), ...(state.detail?.snapshot?.turns || [])]);
}

function historyErrorText(code) {
  const labels = {
    history_capability_unavailable: '当前 Runner 版本仅支持最近消息',
    history_search_capability_unavailable: '当前 Runner 版本不支持任务内搜索',
    history_cursor_invalid: '历史游标已失效，请重新打开任务后再试',
    history_cursor_thread_mismatch: '历史游标不属于当前任务，已停止加载',
    history_page_too_large: '这页消息过大，Runner 已安全停止加载',
    request_expired: '历史请求已过期，请手动重试',
    runner_internal_error: 'Runner 读取历史时出现内部错误，请稍后手动重试',
  };
  return labels[code] || '较早消息暂时无法读取，请稍后手动重试';
}

function renderHistoryControls() {
  if (!q('historyPanel')) return;
  const paging = state.detail?.history?.paging_available === true;
  const searching = state.detail?.history?.search_available === true;
  q('loadEarlierMessages').disabled = !paging || state.historyLoading || (state.historyInitialized && !state.historyHasMore);
  q('loadEarlierMessages').textContent = state.historyLoading ? '正在加载…' : state.historyHasMore ? '加载较早消息' : '已显示全部消息';
  q('historySearchInput').disabled = !searching || state.searchLoading;
  q('historySearchButton').disabled = !searching || state.searchLoading;
  q('historySearchButton').textContent = state.searchLoading ? '搜索中…' : '搜索';
  const status = q('historyStatus');
  status.className = `history-status ${(state.historyError || state.searchError) ? 'error' : ''}`.trim();
  if (!paging && !searching) status.textContent = '当前 Runner 版本仅支持最近消息';
  else if (state.searchLoading) status.textContent = '正在搜索当前任务的公开文本…';
  else if (state.historyLoading) status.textContent = '较早消息会通过实时连接持续返回…';
  else if (state.searchError) status.textContent = state.searchError;
  else if (state.historyError) status.textContent = state.historyError;
  else if (state.searchQuery) status.textContent = state.searchResults.length ? `找到 ${state.searchResults.length} 条匹配结果` : '没有找到匹配的公开文本';
  else if (state.historyHasMore) status.textContent = '当前先显示最近消息，可按需向前加载';
  else status.textContent = '已显示可读取的全部消息';
  const results = q('historySearchResults');
  results.replaceChildren();
  for (const occurrence of state.searchResults) {
    const item = document.createElement('article');
    item.className = 'history-result';
    const ref = document.createElement('strong');
    ref.textContent = occurrence.turn_ref || '匹配消息';
    const snippet = document.createElement('span');
    snippet.textContent = occurrence.snippet || '';
    item.append(ref, snippet);
    results.append(item);
  }
  results.classList.toggle('hidden', !state.searchQuery || !state.searchResults.length);
  q('loadMoreSearchResults').classList.toggle('hidden', !state.searchQuery || !state.searchResults.length || !state.searchHasMore);
  q('loadMoreSearchResults').disabled = state.searchLoading || !searching;
}

async function requestHistoryPage({initial = false} = {}) {
  if (!state.detail || state.historyLoading || (!initial && !state.historyHasMore)) return;
  const threadRef = state.detail.thread_ref;
  const request = requestId();
  const cursor = initial ? null : state.historyCursor;
  state.historyRequestId = request;
  state.historyLoading = true;
  state.historyError = '';
  renderHistoryControls();
  const body = {request_id: request, ...(cursor ? {cursor} : {})};
  try {
    const submitted = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}/history/page`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    if (threadRef !== state.selectedThread || request !== state.historyRequestId) return;
    if (submitted?.state !== 'submitted') {
      state.historyLoading = false;
      state.historyError = '历史请求是否送达还不能确认，请手动重试';
      renderHistoryControls();
    }
  } catch (error) {
    if (threadRef !== state.selectedThread || request !== state.historyRequestId) return;
    state.historyLoading = false;
    state.historyError = error.message || '较早消息请求失败';
    renderHistoryControls();
  }
}

async function requestHistorySearch({append = false} = {}) {
  if (!state.detail || state.searchLoading || state.detail.history?.search_available !== true) return;
  const query = append ? state.searchQuery : q('historySearchInput').value.trim();
  if (!query || query.length > 120 || Array.from(query).some(character => character.charCodeAt(0) < 32)) {
    state.searchError = '请输入 1–120 个可见字符';
    renderHistoryControls();
    return;
  }
  if (append && !state.searchHasMore) return;
  const threadRef = state.detail.thread_ref;
  const request = requestId();
  const cursor = append ? state.searchCursor : null;
  if (!append) {
    state.searchQuery = query;
    state.searchResults = [];
    state.searchCursor = null;
    state.searchHasMore = false;
  }
  state.searchRequestId = request;
  state.searchAppend = append;
  state.searchLoading = true;
  state.searchError = '';
  renderHistoryControls();
  const body = {request_id: request, query, ...(cursor ? {cursor} : {})};
  try {
    const submitted = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}/history/search`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    if (threadRef !== state.selectedThread || request !== state.searchRequestId) return;
    if (submitted?.state !== 'submitted') {
      state.searchLoading = false;
      state.searchError = '搜索请求是否送达还不能确认，请手动重试';
      renderHistoryControls();
    }
  } catch (error) {
    if (threadRef !== state.selectedThread || request !== state.searchRequestId) return;
    state.searchLoading = false;
    state.searchError = error.message || '任务内搜索失败';
    renderHistoryControls();
  }
}

function applyHistoryEvent(event) {
  const payload = event?.payload || {};
  if (event?.event_kind === 'history.error') {
    if (payload.action === 'history_page' && payload.request_id === state.historyRequestId) {
      state.historyLoading = false;
      state.historyError = historyErrorText(payload.error_code);
    }
    if (payload.action === 'history_search' && payload.request_id === state.searchRequestId) {
      state.searchLoading = false;
      state.searchError = historyErrorText(payload.error_code);
    }
    renderHistoryControls();
    return;
  }
  if (event?.event_kind === 'history.page' && payload.request_id === state.historyRequestId) {
    const viewport = q('conversationView');
    const preserveAnchor = state.historyTurns.length > 0;
    const anchor = preserveAnchor ? {height: viewport.scrollHeight, top: viewport.scrollTop} : null;
    state.historyTurns = dedupeTurns([...(payload.turns || []), ...state.historyTurns]);
    state.historyCursor = payload.next_cursor || null;
    state.historyHasMore = payload.has_more === true;
    state.historyLoading = false;
    state.historyError = '';
    renderHistoryControls();
    renderConversation(mergedConversationTurns(), {anchor});
    return;
  }
  if (event?.event_kind === 'history.search' && payload.request_id === state.searchRequestId) {
    const incoming = Array.isArray(payload.occurrences) ? payload.occurrences : [];
    const combined = state.searchAppend ? [...state.searchResults, ...incoming] : incoming;
    const seen = new Set();
    state.searchResults = combined.filter(item => {
      const key = `${item?.turn_ref || ''}\n${item?.snippet || ''}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    state.searchCursor = payload.next_cursor || null;
    state.searchHasMore = payload.has_more === true;
    state.searchLoading = false;
    state.searchError = '';
    renderHistoryControls();
  }
}

function handleHistoryStreamReady(document) {
  if (document?.resync_required) {
    if (state.historyLoading) {
      state.historyLoading = false;
      state.historyError = '实时窗口已更新，上次历史结果未能对账，请手动重试';
    }
    if (state.searchLoading) {
      state.searchLoading = false;
      state.searchError = '实时窗口已更新，上次搜索结果未能对账，请手动重试';
    }
    renderHistoryControls();
  }
  if (
    state.detail?.history?.paging_available === true
    && state.historyInitialized
    && state.historyHasMore
    && !state.historyLoading
    && !state.historyRequestId
  ) void requestHistoryPage({initial: true});
}

function isNearConversationBottom() {
  const root = q('conversationView');
  return root.scrollHeight - root.scrollTop - root.clientHeight < 96;
}

function followLatestReply() {
  const root = q('conversationView');
  state.following = true;
  q('newReplyButton').classList.add('hidden');
  requestAnimationFrame(() => { root.scrollTop = root.scrollHeight; });
}

function messageNode(role, value, {streaming = false, label = ''} = {}) {
  const node = document.createElement('article');
  node.className = `message ${role}${streaming ? ' streaming' : ''}`;
  const title = document.createElement('div');
  title.className = 'message-role';
  title.textContent = label || (role === 'assistant' ? 'Codex' : role === 'user' ? '你' : '状态');
  if (streaming) { const dot = document.createElement('span'); dot.className = 'typing-dot'; title.append(dot); }
  const body = document.createElement('div');
  body.className = 'message-body';
  body.textContent = text(value, streaming ? '正在回复…' : '');
  node.append(title, body);
  return node;
}

function itemMeta(label) {
  const item = document.createElement('article');
  item.className = 'run-item';
  const title = document.createElement('div');
  title.className = 'run-item-label';
  title.textContent = label;
  item.append(title);
  return item;
}

function appendItemText(node, value) {
  const body = document.createElement('div');
  body.className = 'run-item-text';
  body.textContent = text(value, '无公开内容');
  node.append(body);
}

function renderItem(item) {
  const type = item?.type || 'other';
  if (type === 'reasoning.summary') { const node = itemMeta('思路摘要'); appendItemText(node, item.text); return node; }
  if (type === 'plan') { const node = itemMeta('计划'); appendItemText(node, item.text); return node; }
  if (type === 'command') {
    const node = itemMeta(`命令 · ${statusText(item.status)}`);
    const output = document.createElement('pre');
    output.className = 'code-output';
    output.textContent = text(item.output_excerpt, '没有公开输出');
    node.append(output);
    return node;
  }
  if (type === 'file.change') {
    const node = itemMeta('文件变化');
    const list = document.createElement('div'); list.className = 'run-item-text';
    for (const change of item.changes || []) { const row = document.createElement('div'); row.textContent = `${change.kind || '更新'} · ${change.relative_path || '文件名已隐藏'}`; list.append(row); }
    if (!list.children.length) appendItemText(node, '没有公开文件路径'); else node.append(list);
    return node;
  }
  if (type === 'tool.call') { const node = itemMeta('使用工具'); appendItemText(node, `${item.tool || '工具'} · ${statusText(item.status)}`); return node; }
  if (type === 'subagent.call') { const node = itemMeta('协作任务'); appendItemText(node, `${item.tool || '协作'} · ${statusText(item.status)}`); return node; }
  if (type === 'web.search') { const node = itemMeta('网页搜索'); appendItemText(node, item.query); return node; }
  const node = itemMeta('任务事件');
  appendItemText(node, item.item_kind || type);
  return node;
}

function eventSummary(event) {
  const payload = event.payload || {};
  if (typeof payload.text === 'string') return payload.text;
  if (typeof payload.summary === 'string') return payload.summary;
  if (typeof payload.title === 'string') return payload.title;
  if (typeof payload.status === 'string') return statusText(payload.status);
  return '';
}

function activityLabel(kind) {
  const labels = {'reasoning.summary': '思考摘要', 'plan.updated': '计划', 'command.started': '正在运行', 'command.completed': '运行完成', 'file.changed': '文件已更新', 'file.patch': '修改文件', 'awaiting.input': '等待你的确认'};
  return labels[kind] || '实时进度';
}

function appendRunDetails(root, turn) {
  const technical = (turn.items || []).filter(item => !['user.message', 'assistant.message', 'reasoning.summary', 'plan'].includes(item?.type));
  if (!technical.length && !turn.items_incomplete) return;
  const details = document.createElement('details');
  details.className = 'run-details';
  const summary = document.createElement('summary');
  summary.textContent = `运行详情 · ${statusText(turn.status)} · ${formatTime(turn.started_at)}`;
  const items = document.createElement('div');
  items.className = 'run-detail-items';
  if (turn.items_incomplete) items.append(messageNode('system', '部分运行记录未显示'));
  for (const item of technical) items.append(renderItem(item));
  details.append(summary, items);
  root.append(details);
}

function liveAssistantText() {
  let value = '';
  for (const event of state.events) {
    if (event.event_kind === 'assistant.completed' || ['turn.completed', 'turn.failed', 'turn.interrupted'].includes(event.event_kind)) value = '';
    if (event.event_kind !== 'assistant.delta') continue;
    const next = eventSummary(event);
    if (!next) continue;
    value = next.startsWith(value) ? next : value + next;
  }
  return value;
}

function renderConversation(turns, {anchor = null} = {}) {
  const viewport = q('conversationView');
  const root = q('conversationInner');
  const shouldFollow = !anchor && (state.following || isNearConversationBottom());
  root.replaceChildren();
  const shown = new Set();
  const imageMessages = state.detail?.image_messages || [];
  const placedImages = new Set();
  for (const turn of turns) {
    const images = imageMessages.filter(message => message.receipt?.turn_ref && message.receipt.turn_ref === turn.turn_ref);
    renderImageHistory(root, images);
    for (const message of images) { placedImages.add(message.request_id); if (message.input) shown.add(`user:${message.input}`); }
    for (const item of turn.items || []) {
      if (item?.type === 'user.message') { const value = text(item.text); if (value && !images.some(message => message.input === value)) { root.append(messageNode('user', value)); shown.add(`user:${value}`); } }
      if (item?.type === 'assistant.message') { const value = text(item.text); if (value) { root.append(messageNode('assistant', value)); shown.add(`assistant:${value}`); } }
      if (item?.type === 'reasoning.summary') { const value = text(item.text); if (value) root.append(messageNode('activity', value, {label: '思考摘要'})); }
      if (item?.type === 'plan') { const value = text(item.text); if (value) root.append(messageNode('activity', value, {label: '计划'})); }
    }
    appendRunDetails(root, turn);
  }
  for (const event of state.events) {
    if (!['user.message', 'assistant.completed'].includes(event.event_kind)) continue;
    const role = event.event_kind === 'user.message' ? 'user' : 'assistant';
    const value = eventSummary(event);
    if (value && !shown.has(`${role}:${value}`)) { root.append(messageNode(role, value)); shown.add(`${role}:${value}`); }
  }
  const activityKinds = new Set(['reasoning.summary', 'plan.updated', 'command.started', 'command.completed', 'file.changed', 'file.patch', 'awaiting.input']);
  for (const event of state.events.filter(value => activityKinds.has(value.event_kind)).slice(-8)) {
    const value = eventSummary(event);
    if (value) root.append(messageNode('activity', value, {label: activityLabel(event.event_kind)}));
  }
  const live = liveAssistantText();
  renderImageHistory(root, imageMessages.filter(message => !placedImages.has(message.request_id)));
  if (state.detail?.status === 'active') root.append(messageNode('assistant', live, {streaming: true}));
  if (!root.children.length) root.append(messageNode('system', '还没有消息。你可以在下方开始对话。'));
  if (anchor) {
    state.following = false;
    q('newReplyButton').classList.remove('hidden');
    requestAnimationFrame(() => { viewport.scrollTop = anchor.top + Math.max(0, viewport.scrollHeight - anchor.height); });
  } else if (shouldFollow) {
    state.following = true;
    q('newReplyButton').classList.add('hidden');
    requestAnimationFrame(() => { viewport.scrollTop = viewport.scrollHeight; });
  } else {
    q('newReplyButton').classList.remove('hidden');
  }
}

function queuePanel() {
  let root = q('queuePanel');
  if (root) return root;
  root = document.createElement('section');
  root.id = 'queuePanel';
  root.className = 'queue-panel hidden';
  root.setAttribute('aria-label', '排队消息');
  q('composer').prepend(root);
  return root;
}

function queueButton(label, handler, {disabled = false, danger = false} = {}) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.disabled = disabled;
  if (danger) button.className = 'danger';
  button.onclick = handler;
  return button;
}

function renderQueue(detail) {
  const root = queuePanel();
  const queued = Array.isArray(detail?.snapshot?.queued_submissions) ? detail.snapshot.queued_submissions : [];
  const supported = hasCapability('thread_queue_v1');
  root.classList.toggle('hidden', !queued.length && (!supported || detail?.status !== 'active'));
  root.classList.toggle('open', state.queueOpen === true);
  root.replaceChildren();
  if (supported || queued.length) {
    const head = document.createElement('button');
    head.type = 'button';
    head.className = 'queue-head';
    const title = document.createElement('span');
    title.textContent = '排队消息';
    const count = document.createElement('span');
    count.className = 'badge';
    count.textContent = `${queued.length}`;
    head.append(title, count);
    head.onclick = () => { state.queueOpen = !state.queueOpen; renderQueue(detail); };
    head.setAttribute('aria-expanded', state.queueOpen ? 'true' : 'false');
    head.title = state.queueOpen ? '收起排队消息' : '展开排队消息';
    root.append(head);
  }
  const list = document.createElement('div');
  list.className = 'queue-list';
  root.append(list);
  if (!queued.length) {
    const empty = document.createElement('div');
    empty.className = 'queue-empty';
    empty.textContent = detail?.status === 'active' ? '可以把下一条指令加入队列，不会打断当前回复。' : '当前没有排队消息。';
    list.append(empty);
  }
  const blocked = state.queueBusy || Boolean(detail?.latest_command && ['pending', 'submitted', 'accepted', 'unknown'].includes(detail.latest_command.state));
  queued.forEach((item, index) => {
    const row = document.createElement('article');
    row.className = 'queue-item';
    const position = document.createElement('div');
    position.className = 'queue-position';
    position.textContent = `${index + 1}`;
    const input = document.createElement('textarea');
    input.className = 'queue-text';
    input.rows = 2;
    const editing = state.queueEditing === item.queue_ref && item.editable === true;
    input.readOnly = !editing;
    input.value = editing ? (state.queueDrafts[item.queue_ref] ?? item.text ?? '') : (item.text || '此消息包含手机端不支持的内容');
    input.oninput = () => { state.queueDrafts[item.queue_ref] = input.value; };
    const actions = document.createElement('div');
    actions.className = 'queue-actions';
    if (item.editable === true) {
      actions.append(queueButton(editing ? '保存' : '编辑', () => {
        if (!editing) {
          state.queueEditing = item.queue_ref;
          state.queueDrafts[item.queue_ref] = item.text || '';
          renderQueue(detail);
          requestAnimationFrame(() => q('queuePanel')?.querySelector(`textarea[data-queue-ref="${item.queue_ref}"]`)?.focus());
          return;
        }
        const value = (state.queueDrafts[item.queue_ref] || '').trim();
        if (value) void submitQueue('update', {queueRef: item.queue_ref, input: value});
      }, {disabled: blocked}));
      input.dataset.queueRef = item.queue_ref;
    }
    actions.append(
      queueButton('上移', () => moveQueued(index, -1), {disabled: blocked || index === 0}),
      queueButton('下移', () => moveQueued(index, 1), {disabled: blocked || index === queued.length - 1}),
      queueButton('立即开始', () => void submitQueue('start', {queueRef: item.queue_ref}), {disabled: blocked}),
      queueButton('删除', () => void submitQueue('delete', {queueRef: item.queue_ref}), {disabled: blocked, danger: true}),
    );
    row.append(position, input, actions);
    list.append(row);
  });
  let add = q('queueMessageButton');
  if (!add) {
    add = document.createElement('button');
    add.id = 'queueMessageButton';
    add.type = 'button';
    add.className = 'queue-add-button';
    add.textContent = '加入队列';
    q('submitDirection').insertAdjacentElement('beforebegin', add);
  }
  add.classList.toggle('hidden', !(supported && detail?.status === 'active'));
  add.disabled = blocked || !writeAvailable() || currentAttachments().length > 0;
  add.onclick = () => {
    if (currentAttachments().length) { q('composerFeedback').textContent = '队列暂不支持图片；请直接发送，附件已保留'; return; }
    const value = q('composerInput').value.trim();
    if (!value) {
      q('composerFeedback').className = 'composer-status warning';
      q('composerFeedback').textContent = '请输入要排队的消息';
      return;
    }
    void submitQueue('add', {input: value});
  };
}

function moveQueued(index, offset) {
  const queued = Array.isArray(state.detail?.snapshot?.queued_submissions) ? state.detail.snapshot.queued_submissions : [];
  const target = index + offset;
  if (target < 0 || target >= queued.length) return;
  const refs = queued.map(item => item.queue_ref);
  [refs[index], refs[target]] = [refs[target], refs[index]];
  void submitQueue('reorder', {queueRefs: refs});
}

async function submitQueue(action, {queueRef = '', input = '', queueRefs = []} = {}) {
  const detail = state.detail;
  if (!detail || state.queueBusy) return;
  if (action === 'add' && currentAttachments().length) { q('composerFeedback').textContent = '队列暂不支持图片，请直接发送'; return; }
  state.queueBusy = true;
  renderQueue(detail);
  q('composerFeedback').className = 'composer-status muted';
  q('composerFeedback').textContent = 'Controller 正在接收队列操作…';
  const base = `${API}/threads/${encodeURIComponent(detail.thread_ref)}/queue`;
  const path = action === 'add' ? base : action === 'reorder' ? `${base}/reorder` : `${base}/${encodeURIComponent(queueRef)}/${action}`;
  const body = {request_id: requestId(), thread_revision: detail.thread_revision, ...(input ? {input} : {}), ...(queueRefs.length ? {queue_refs: queueRefs} : {})};
  try {
    const result = await jsonFetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    if (detail.thread_ref !== state.selectedThread) return;
    q('composerFeedback').className = 'composer-status success';
    q('composerFeedback').textContent = result.delivery_stage === 'mac_confirmed' || result.state === 'confirmed' ? 'Mac 已确认队列操作' : 'Controller 已接收，WSS 正在送往 Runner';
    if (action === 'add') {
      if (q('composerInput').value.trim() === input) q('composerInput').value = '';
      if ((state.drafts[detail.thread_ref] || '').trim() === input) delete state.drafts[detail.thread_ref];
      resizeComposer();
    }
    if (queueRef) delete state.queueDrafts[queueRef];
    state.queueEditing = '';
    await loadThread(detail.thread_ref, {restartStream: false});
  } catch (error) {
    if (detail.thread_ref !== state.selectedThread) return;
    q('composerFeedback').className = 'composer-status error';
    q('composerFeedback').textContent = error.message;
    await loadThread(detail.thread_ref, {restartStream: false});
  } finally {
    state.queueBusy = false;
    if (state.detail) renderQueue(state.detail);
  }
}

function composerAction(detail) {
  if (detail.status === 'active') return 'steer';
  if (['idle', 'notLoaded', 'failed'].includes(detail.status)) return 'continue';
  return null;
}

function renderModelSelector(action, enabled) {
  const select = q('modelSelect');
  const models = Array.isArray(currentHost()?.models) ? currentHost().models : [];
  const modelIds = new Set(models.map(model => model.id));
  if (!modelIds.has(state.selectedModel)) state.selectedModel = '';
  select.replaceChildren();
  const inherit = document.createElement('option');
  inherit.value = '';
  inherit.textContent = '沿用原任务模型';
  select.append(inherit);
  for (const model of models) {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = `${model.display_name || model.id}${model.is_default ? ' · App 默认' : ''}${model.display_name && model.display_name !== model.id ? ` · ${model.id}` : ''}`;
    select.append(option);
  }
  const supported = hasCapability('model_override_v1') && models.length > 0;
  const allowedForAction = action === 'continue' || (action === 'steer' && state.mode === 'safe');
  select.value = state.selectedModel;
  select.disabled = !enabled || !supported || !allowedForAction;
  const effortSupported = populateEffortOptions(q('effortSelect'), state.selectedModel, {detail: state.detail, value: state.selectedEffort});
  state.selectedEffort = q('effortSelect').value;
  q('effortSelect').disabled = !enabled || !allowedForAction || !hasCapability('reasoning_effort_v1') || !effortSupported;
  const defaultModel = models.find(model => model.is_default);
  if (!supported) q('modelMeta').textContent = '当前 App 未提供可用模型目录，将沿用原任务模型。';
  else if (!allowedForAction) q('modelMeta').textContent = '原生快速调整保持同一 Turn，不允许切换模型。';
  else q('modelMeta').textContent = `仅对本次新 Turn 生效；不选择则沿用原任务模型${defaultModel ? `。App 默认：${defaultModel.display_name || defaultModel.id}` : ''}。`;
}

function renderComposer(detail) {
  const action = composerAction(detail);
  const blockedCommand = detail.latest_command && ['pending', 'submitted', 'accepted', 'unknown'].includes(detail.latest_command.state);
  const capability = action === 'steer' ? (state.mode === 'native' ? hasCapability('native_steer_racy') : hasCapability('interrupt_expected_turn') && hasCapability('continue_same_thread')) : action === 'continue' ? hasCapability('continue_same_thread') : false;
  const enabled = Boolean(action && writeAvailable() && capability && !blockedCommand && !['recovery_required', 'protocol_degraded'].includes(detail.status) && detail.control_state !== 'protocol_degraded' && ['ready', 'load_required'].includes(detail.control_state));
  q('composer').classList.toggle('hidden', !action);
  q('composerInput').disabled = !action;
  q('safeMode').disabled = detail.status !== 'active';
  q('nativeMode').disabled = detail.status !== 'active' || !hasCapability('native_steer_racy');
  q('submitDirection').disabled = !enabled || Boolean(imageState.pending[detail.thread_ref]) || Boolean(imageState.busy[detail.thread_ref]);
  renderModelSelector(action, enabled);
  renderPermissionSelectors(detail);
  renderCollaborationModeSelectors(detail);
  q('submitDirection').textContent = '发送';
  q('composerInput').placeholder = action === 'steer' ? '给 Codex 发消息，立即调整当前方向' : '给 Codex 发消息';
  renderAttachments(enabled);
}

function setMode(mode) {
  state.mode = mode;
  if (mode === 'native') { state.selectedModel = ''; state.selectedEffort = ''; state.selectedPermission = ''; state.selectedCollaborationMode = ''; }
  q('safeMode').setAttribute('aria-pressed', mode === 'safe' ? 'true' : 'false');
  q('nativeMode').setAttribute('aria-pressed', mode === 'native' ? 'true' : 'false');
  if (state.detail) renderDetail();
}

async function loadThreadPage({reset = false} = {}) {
  if ((!reset && state.threadsLoading) || !state.selectedHost || (!reset && !state.threadsHasMore)) return;
  const generation = (state.threadPageGeneration || 0) + 1;
  state.threadPageGeneration = generation;
  const taskQuery = q('threadSearch').value.trim();
  const scope = `${state.selectedHost}:${state.selectedProject}:${q('statusFilter').value}:${taskQuery}`;
  state.threadsLoading = true;
  if (reset) {
    state.threadsCursor = 0;
    state.threadsHasMore = true;
  }
  renderThreads();
  try {
    const project = state.selectedProject !== 'all' ? `&project_ref=${encodeURIComponent(state.selectedProject)}` : '';
    const status = q('statusFilter').value !== 'all' ? `&status=${encodeURIComponent(q('statusFilter').value)}` : '';
    const endpoint = taskQuery
      ? `${API}/search?host_ref=${encodeURIComponent(state.selectedHost)}${project}${status}&query=${encodeURIComponent(taskQuery)}&cursor=${state.threadsCursor}&limit=20`
      : `${API}/threads?host_ref=${encodeURIComponent(state.selectedHost)}${project}${status}&cursor=${state.threadsCursor}&limit=40&order=recent`;
    const document = await jsonFetch(endpoint);
    if (generation !== state.threadPageGeneration || scope !== `${state.selectedHost}:${state.selectedProject}:${q('statusFilter').value}:${q('threadSearch').value.trim()}`) return;
    const retained = reset && state.selectedThread ? state.threads.find(thread => thread.thread_ref === state.selectedThread) : null;
    if (reset) state.threads = [];
    const byRef = new Map(state.threads.map(thread => [thread.thread_ref, thread]));
    for (const thread of document.threads || []) {
      const current = byRef.get(thread.thread_ref);
      byRef.set(thread.thread_ref, current && number(current.thread_revision) > number(thread.thread_revision) ? current : {...current, ...thread});
    }
    if (retained && !byRef.has(retained.thread_ref)) byRef.set(retained.thread_ref, retained);
    state.threads = Array.from(byRef.values());
    state.threadsCursor = number(document.next_cursor);
    state.threadsHasMore = Boolean(document.has_more);
  } catch (error) {
    if (generation === state.threadPageGeneration) q('threadListStatus').textContent = error.message;
  } finally {
    if (generation === state.threadPageGeneration) {
      state.threadsLoading = false;
      renderThreads();
      renderMetrics();
    }
  }
}

function maybeLoadMoreThreads() {
  if (!state.threadsHasMore || state.threadsLoading || !state.selectedHost) return;
  const list = q('threadList');
  if (list.scrollTop + list.clientHeight >= list.scrollHeight - 260) void loadThreadPage();
}

async function refreshOverview({preserveDetail = true} = {}) {
  if (state.loading) return;
  const generation = ++state.refreshGeneration;
  state.loading = true;
  if (!state.hosts.length) setConnection('正在同步', 'warn');
  try {
    const status = await jsonFetch(STATUS_API);
    if (generation !== state.refreshGeneration) return;
    state.csrf = status.csrf_token;
    const hostsDocument = await jsonFetch(`${API}/hosts`);
    if (generation !== state.refreshGeneration) return;
    const serverTime = new Date(hostsDocument.server_time || '').getTime();
    if (Number.isFinite(serverTime)) { state.serverTimeMs = serverTime; state.serverTimeObservedAt = Date.now(); }
    state.hosts = hostsDocument.hosts || [];
    state.overviewError = '';
    state.streamFailures.delete('overview-fetch');
    state.streamFailures.delete('network-recovery');
    if (!state.selectedHost || !state.hosts.some(host => host.host_ref === state.selectedHost)) state.selectedHost = state.hosts[0]?.host_ref || '';
    const hostRef = state.selectedHost;
    if (hostRef) {
      const projectsDocument = await jsonFetch(`${API}/projects?host_ref=${encodeURIComponent(hostRef)}`);
      if (generation !== state.refreshGeneration || hostRef !== state.selectedHost) return;
      state.projects = projectsDocument.projects || [];
      await loadThreadPage({reset: true});
    } else {
      state.projects = [];
      state.threads = [];
      state.threadsCursor = 0;
      state.threadsHasMore = false;
    }
    if (state.selectedProject !== 'all' && !state.projects.some(project => project.project_ref === state.selectedProject)) state.selectedProject = 'all';
    renderHosts();
    renderProjects();
    renderThreads();
    renderMetrics();
    if (preserveDetail && state.selectedThread) await loadThread(state.selectedThread, {restartStream: false, throwOnError: true}); else renderDetail();
    renderFreshness();
  } catch (error) {
    if (generation !== state.refreshGeneration) return;
    state.overviewError = error.message;
    state.streamFailures.add('overview-fetch');
    renderFreshness();
    q('hostMeta').textContent = error.message;
  } finally {
    state.loading = false;
    if (state.selectedHost && !state.overviewSource && navigator.onLine) startOverviewStream();
  }
}

async function selectProject(projectRef) {
  state.selectedProject = projectRef;
  q('projectScope').textContent = state.projects.find(project => project.project_ref === projectRef)?.project_alias || '全部项目';
  renderProjects();
  await loadThreadPage({reset: true});
  setProjectsOpen(false);
}

async function selectThread(threadRef) {
  const selection = (state.selectionGeneration || 0) + 1;
  state.selectionGeneration = selection;
  if (state.selectedThread) state.drafts[state.selectedThread] = q('composerInput').value;
  stopEventStream();
  state.selectedThread = threadRef;
  state.detail = null;
  renderDetail();
  q('composerInput').value = state.drafts[threadRef] || '';
  state.selectedModel = '';
  state.selectedEffort = '';
  state.selectedPermission = '';
  state.selectedCollaborationMode = '';
  state.queueOpen = false;
  state.queueEditing = '';
  state.events = [];
  state.eventCursor = 0;
  state.following = true;
  resetHistoryState();
  renderThreads();
  const loaded = await loadThread(threadRef, {restartStream: true});
  if (selection !== state.selectionGeneration || threadRef !== state.selectedThread || !loaded) return;
  q('composerInput').value = state.drafts[threadRef] || '';
  resizeComposer();
  document.body.classList.add('detail-open');
}

async function loadThread(threadRef, {restartStream = false, throwOnError = false} = {}) {
  const selection = state.selectionGeneration;
  try {
    const detail = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}`);
    if (selection !== state.selectionGeneration || threadRef !== state.selectedThread) return false;
    if (state.detail?.thread_ref === threadRef && number(state.detail.thread_revision) > number(detail.thread_revision)) return true;
    const initializeHistory = !state.historyInitialized;
    state.detail = detail;
    const command = detail.latest_command;
    const createdRef = command?.action === 'fork' && command?.state === 'confirmed' ? command?.receipt?.created_thread_ref : '';
    if (createdRef && !state.handledForks.has(command.request_id)) {
      state.handledForks.add(command.request_id);
      await loadThreadPage({reset: true});
      await addUnknownThread(createdRef);
      if (threadRef === state.selectedThread) window.setTimeout(() => void selectThread(createdRef), 0);
    }
    if (initializeHistory) {
      state.historyInitialized = true;
      state.historyHasMore = detail.history?.paging_available === true || detail.snapshot?.history_incomplete === true;
    }
    renderDetail();
    if (restartStream) startEventStream();
    return true;
  } catch (error) {
    if (threadRef !== state.selectedThread) return false;
    q('composerFeedback').className = 'composer-status error';
    q('composerFeedback').textContent = error.message;
    if (throwOnError) throw error;
    return false;
  }
}

function stopOverviewStream() {
  if (state.overviewSource) state.overviewSource.close();
  state.overviewSource = null;
  if (state.overviewReconnectTimer) window.clearTimeout(state.overviewReconnectTimer);
  state.overviewReconnectTimer = 0;
}

function streamDocument(event) {
  try {
    const document = JSON.parse(event.data);
    if (!document || document.version !== 1 || !Number.isFinite(Number(document.cursor))) throw new Error('invalid stream frame');
    const serverTime = new Date(document.server_time || '').getTime();
    if (Number.isFinite(serverTime)) { state.serverTimeMs = serverTime; state.serverTimeObservedAt = Date.now(); }
    return document;
  } catch (_error) {
    return null;
  }
}

function scheduleOverviewReconcile() {
  if (state.overviewReconcileTimer) window.clearTimeout(state.overviewReconcileTimer);
  const hostRef = state.selectedHost;
  state.overviewReconcileTimer = window.setTimeout(async () => {
    state.overviewReconcileTimer = 0;
    if (!hostRef || hostRef !== state.selectedHost || state.loading) return;
    try {
      const projectsDocument = await jsonFetch(`${API}/projects?host_ref=${encodeURIComponent(hostRef)}`);
      if (hostRef !== state.selectedHost) return;
      state.projects = projectsDocument.projects || [];
      await loadThreadPage({reset: true});
      state.overviewError = '';
      renderProjects();
      renderNewTaskState();
    } catch (error) {
      state.overviewError = error.message;
      renderFreshness();
    }
  }, 180);
}

function applyHostFrame(document) {
  state.lastOverviewFrameAt = Date.now();
  if (Number.isFinite(Number(document.cursor))) state.overviewCursor = Number(document.cursor);
  const host = document.host;
  if (host?.host_ref === state.selectedHost) {
    const index = state.hosts.findIndex(value => value.host_ref === host.host_ref);
    if (index >= 0) state.hosts[index] = {...state.hosts[index], ...host}; else state.hosts.push(host);
    renderHosts();
    renderRunnerBanner();
    renderNewTaskState();
  }
  state.overviewStreamState = 'open';
  state.overviewReconnectAttempt = 0;
  state.streamFailures.delete('overview');
  renderFreshness();
  const created = (document.create_commands || []).find(command => command.request_id === state.pendingCreate?.body.request_id);
  if (created) void handleCreateResult(created);
}

async function addUnknownThread(threadRef) {
  if (!threadRef || state.threads.some(thread => thread.thread_ref === threadRef)) return;
  try {
    const thread = await jsonFetch(`${API}/threads/${encodeURIComponent(threadRef)}`);
    if (thread.host_ref !== state.selectedHost || state.threads.some(value => value.thread_ref === thread.thread_ref)) return;
    state.threads.unshift(thread);
    renderThreads();
    renderMetrics();
  } catch (_error) {}
}

function applyOverviewEvents(events) {
  for (const event of events || []) {
    const threadRef = event.thread_ref;
    if (!threadRef) continue;
    const thread = state.threads.find(value => value.thread_ref === threadRef);
    const payload = event.payload || {};
    if (!thread) {
      void addUnknownThread(threadRef);
      continue;
    }
    if (payload.status) thread.status = payload.status;
    if (payload.title) thread.title = payload.title;
    if (payload.latest_summary) { thread.preview = payload.latest_summary; thread.latest_summary = payload.latest_summary; }
    if (payload.updated_at) thread.updated_at = payload.updated_at;
    if (Number.isFinite(Number(event.thread_revision))) thread.thread_revision = Number(event.thread_revision);
    state.lastDataEventAt = event.created_at || new Date().toISOString();
  }
  renderThreads();
  renderMetrics();
}

function scheduleOverviewReconnect() {
  stopOverviewStream();
  state.overviewStreamState = 'reconnecting';
  state.streamFailures.add('overview');
  state.overviewReconnectAttempt += 1;
  if (state.overviewReconnectAttempt >= 3) {
    state.overviewCursor = 0;
    void recoverOverviewBaseline();
    return;
  }
  renderFreshness();
  if (!navigator.onLine || !state.selectedHost) return;
  const delayMs = Math.min(15000, 600 * (2 ** Math.min(5, state.overviewReconnectAttempt - 1)));
  state.overviewReconnectTimer = window.setTimeout(startOverviewStream, delayMs);
}

async function recoverOverviewBaseline() {
  if (state.loading) {
    window.setTimeout(() => void recoverOverviewBaseline(), 200);
    return;
  }
  state.overviewStreamState = 'reconnecting';
  state.overviewError = '实时游标需要重新对账，正在读取最近任务。';
  renderFreshness();
  await refreshOverview({preserveDetail: true});
  state.overviewReconnectAttempt = 0;
  state.overviewError = '';
  if (state.selectedHost && !state.overviewSource && navigator.onLine) startOverviewStream();
}

function startOverviewStream() {
  stopOverviewStream();
  const hostRef = state.selectedHost;
  if (!hostRef) return;
  state.overviewStreamState = 'connecting';
  const cursor = state.overviewCursor > 0 ? `&after_cursor=${state.overviewCursor}` : '';
  const source = new EventSource(`${API}/stream?host_ref=${encodeURIComponent(hostRef)}${cursor}`);
  state.overviewSource = source;
  const receive = event => {
    if (source !== state.overviewSource || hostRef !== state.selectedHost) return;
    const document = streamDocument(event);
    if (!document || document.scope?.type !== 'host' || document.scope?.ref !== hostRef) { scheduleOverviewReconnect(); return; }
    applyHostFrame(document);
    if (event.type === 'ready') {
      if (document.resync_required) state.overviewError = '事件窗口已更新，正在用最新任务快照安全对账。';
      scheduleOverviewReconcile();
    }
    if (event.type === 'desktop') {
      applyOverviewEvents(document.events);
      scheduleOverviewReconcile();
    }
  };
  source.addEventListener('ready', receive);
  source.addEventListener('heartbeat', receive);
  source.addEventListener('desktop', receive);
  source.onopen = () => { state.overviewStreamState = 'open'; state.streamFailures.delete('overview'); renderFreshness(); };
  source.onerror = () => { if (source === state.overviewSource) scheduleOverviewReconnect(); };
}

function stopEventStream() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
  if (state.eventReconnectTimer) window.clearTimeout(state.eventReconnectTimer);
  state.eventReconnectTimer = 0;
  state.detailStreamState = 'idle';
}

function scheduleDetailReload(threadRef) {
  if (state.detailReloadTimer) window.clearTimeout(state.detailReloadTimer);
  state.detailReloadTimer = window.setTimeout(() => {
    state.detailReloadTimer = 0;
    if (threadRef === state.selectedThread) void loadThread(threadRef, {restartStream: false});
  }, 80);
}

function startEventStream() {
  stopEventStream();
  const threadRef = state.selectedThread;
  if (!threadRef) return;
  state.detailStreamState = 'connecting';
  const cursor = state.eventCursor > 0 ? `?after_cursor=${state.eventCursor}` : '';
  const source = new EventSource(`${API}/threads/${encodeURIComponent(threadRef)}/stream${cursor}`);
  state.eventSource = source;
  const receive = event => {
    if (source !== state.eventSource || threadRef !== state.selectedThread) return;
    const document = streamDocument(event);
    if (!document || document.scope?.type !== 'thread' || document.scope?.ref !== threadRef) { scheduleEventReconnect(); return; }
    state.lastDetailFrameAt = Date.now();
    state.eventCursor = Number(document.cursor);
    state.detailStreamState = 'open';
    state.eventReconnectAttempt = 0;
    state.streamFailures.delete('detail');
    if (event.type === 'ready') {
      if (document.resync_required) {
        q('composerFeedback').className = 'composer-status muted';
        q('composerFeedback').textContent = '事件窗口已更新，正在读取最新任务状态';
      }
      handleHistoryStreamReady(document);
      scheduleDetailReload(threadRef);
    }
    if (event.type === 'desktop' && document.events?.length) {
      const historyEvents = document.events.filter(value => String(value?.event_kind || '').startsWith('history.'));
      const resourceEvents = document.events.filter(value => ['file.', 'diagnostic.', 'diff.'].some(prefix => String(value?.event_kind || '').startsWith(prefix)));
      const liveEvents = document.events.filter(value => !['history.', 'file.', 'diagnostic.', 'diff.'].some(prefix => String(value?.event_kind || '').startsWith(prefix)));
      for (const historyEvent of historyEvents) applyHistoryEvent(historyEvent);
      for (const resourceEvent of resourceEvents) applyResourceEvent(resourceEvent);
      state.events.push(...liveEvents);
      state.events = state.events.slice(-500);
      if (liveEvents.length) scheduleDetailReload(threadRef);
    }
    if (event.type === 'desktop' && document.changed && !document.events?.length) scheduleDetailReload(threadRef);
    renderFreshness();
  };
  source.addEventListener('ready', receive);
  source.addEventListener('heartbeat', receive);
  source.addEventListener('desktop', receive);
  source.onopen = () => {
    state.detailStreamState = 'open';
    state.streamFailures.delete('detail');
    renderFreshness();
  };
  source.onerror = () => { if (source === state.eventSource) scheduleEventReconnect(); };
}

function scheduleEventReconnect() {
  stopEventStream();
  state.detailStreamState = 'reconnecting';
  state.streamFailures.add('detail');
  state.eventReconnectAttempt += 1;
  if (state.eventReconnectAttempt >= 3) {
    state.eventCursor = 0;
    void recoverDetailBaseline();
    return;
  }
  renderFreshness();
  if (!navigator.onLine || !state.selectedThread) return;
  const delayMs = Math.min(15000, 800 * (2 ** Math.min(5, state.eventReconnectAttempt - 1)));
  state.eventReconnectTimer = window.setTimeout(startEventStream, delayMs);
}

async function recoverDetailBaseline() {
  const threadRef = state.selectedThread;
  if (!threadRef) return;
  q('composerFeedback').className = 'composer-status muted';
  q('composerFeedback').textContent = '实时游标需要重新对账，正在读取最新任务状态';
  await loadThread(threadRef, {restartStream: false});
  state.eventReconnectAttempt = 0;
  if (threadRef === state.selectedThread && navigator.onLine) startEventStream();
}

async function submitAction(action, extra = {}, retry = null) {
  const detail = state.detail;
  if (!detail || imageState.busy[detail.thread_ref] || (!retry && imageState.pending[detail.thread_ref])) return;
  const ref = detail.thread_ref;
  const body = retry?.body || {request_id: requestId(), thread_revision: detail.thread_revision, ...extra};
  const pending = retry || {body, action, extra};
  imageState.busy[ref] = true;
  imageState.pending[ref] = pending;
  const isCurrent = () => state.selectedThread === ref;
  q('composerFeedback').className = 'composer-status muted';
  q('composerFeedback').textContent = action === 'steer' || action === 'continue' ? '正在发送…' : '正在处理…';
  q('submitDirection').disabled = true;
  try {
    const actionPath = action === 'respond_request'
      ? `${API}/threads/${encodeURIComponent(detail.thread_ref)}/requests/${encodeURIComponent(body.request_ref)}/respond`
      : `${API}/threads/${encodeURIComponent(detail.thread_ref)}/${action}`;
    const result = await jsonFetch(actionPath, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    const confirmed = result.state === 'confirmed' || result.delivery_stage === 'mac_confirmed';
    const accepted = ['pending', 'submitted', 'accepted', 'confirmed'].includes(result.state);
    if (accepted || ['failed', 'conflict', 'expired', 'recovery_required'].includes(result.state)) delete imageState.pending[ref];
    if (isCurrent()) {
      q('composerFeedback').className = `composer-status ${accepted ? 'success' : 'warning'}`;
      q('composerFeedback').textContent = confirmed ? 'Mac 已确认，回复将自动同步' : accepted ? 'Controller 已接收，等待 Mac 确认' : '未完成发送，请检查回执；草稿已保留';
    }
    if (accepted && (action === 'steer' || action === 'continue')) {
      if ((state.drafts[ref] || '').trim() === body.input) delete state.drafts[ref];
      for (const item of currentAttachments(ref)) if ((body.image_refs || []).includes(item.image_ref) && item.url) URL.revokeObjectURL(item.url);
      imageState.drafts[ref] = currentAttachments(ref).filter(item => !(body.image_refs || []).includes(item.image_ref));
      if (isCurrent()) {
        if (q('composerInput').value.trim() === body.input) q('composerInput').value = '';
        resizeComposer(); state.following = true; state.selectedModel = ''; state.selectedEffort = ''; state.selectedPermission = ''; state.selectedCollaborationMode = '';
      }
    }
    if (isCurrent()) await loadThread(ref, {restartStream: false});
  } catch (error) {
    if (error.status >= 400 && error.status < 500) delete imageState.pending[ref];
    if (isCurrent()) {
      q('composerFeedback').className = 'composer-status error';
      q('composerFeedback').textContent = `${error.message}。草稿保留，不会自动重发`;
      await loadThread(ref, {restartStream: false});
    }
  } finally {
    delete imageState.busy[ref];
    if (isCurrent() && state.detail) renderComposer(state.detail);
  }
}

async function createThread(event) {
  event.preventDefault();
  if (state.createBusy) return;
  const host = currentHost();
  const existing = state.pendingCreate;
  if (existing && existing.body.host_ref !== state.selectedHost) { q('newTaskFeedback').textContent = '请切回正在确认新任务的 Mac，当前草稿已保留'; return; }
  const attachments = currentCreateAttachments();
  const input = existing?.body.input || q('newTaskInput').value.trim();
  const projectRef = existing?.body.project_ref || q('newTaskProject').value;
  const model = existing?.body.model || q('newTaskModel').value;
  const effort = existing?.body.effort || q('newTaskEffort').value;
  const permission = existing?.body.permission_profile_id || q('newTaskPermission').value;
  const collaborationMode = existing?.body.collaboration_mode_id || q('newTaskCollaborationMode').value;
  if (!existing && !hostCanCreate()) {
    q('newTaskFeedback').className = 'feedback warning';
    q('newTaskFeedback').textContent = 'Runner 当前不可创建任务；草稿仍保留，未发送。';
    return;
  }
  if (!existing && ((!input && !attachments.length) || !state.projects.some(project => project.project_ref === projectRef))) {
    q('newTaskFeedback').className = 'feedback warning';
    q('newTaskFeedback').textContent = '请选择项目，并填写任务要求或添加图片。';
    return;
  }
  if (!existing && !createImagesReady()) { renderCreateAttachments(); q('newTaskFeedback').textContent = '图片尚未就绪或已过期，请完成上传后创建'; return; }
  if (!existing) saveCreateDraftFields();
  const images = attachments.length ? {image_refs: attachments.map(item => item.image_ref)} : {};
  const body = existing?.body || {request_id: requestId(), host_ref: host.host_ref, project_ref: projectRef, input, ...images, ...(model ? {model} : {}), ...(effort ? {effort} : {}), ...(permission ? {permission_profile_id: permission} : {}), ...(collaborationMode ? {collaboration_mode_id: collaborationMode} : {})};
  state.pendingCreate = existing || {body, controllerAccepted: false};
  state.createBusy = true;
  renderNewTaskState();
  q('newTaskFeedback').className = 'feedback muted';
  q('newTaskFeedback').textContent = existing ? '正在用同一 request ID 检查收据…' : '正在提交创建请求，等待 Mac 收据…';
  let controllerAccepted = Boolean(existing?.controllerAccepted);
  try {
    const result = await jsonFetch(`${API}/threads`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    controllerAccepted = true;
    // SSE may have delivered the terminal receipt before this POST completed.
    if (state.pendingCreate?.body.request_id === body.request_id) await handleCreateResult(result);
  } catch (error) {
    if (state.pendingCreate?.body.request_id !== body.request_id) return;
    if (error.status >= 400 && error.status < 500) state.pendingCreate = null;
    else state.pendingCreate = {body, controllerAccepted};
    renderNewTaskState();
    q('newTaskFeedback').className = 'feedback error';
    if (controllerAccepted) {
      q('newTaskFeedback').textContent = `收据检查中断：${error.message}。草稿与 request ID 已保留，可稍后安全检查。`;
    } else {
      q('newTaskFeedback').textContent = `未确认 Controller 是否收到请求：${error.message}。不会自动重试；网络恢复后可用同一 request ID 安全检查。`;
    }
  } finally {
    state.createBusy = false;
    q('createTaskButton').disabled = state.pendingCreate ? !navigator.onLine : !(hostCanCreate() && state.projects.length > 0);
    q('createTaskButton').textContent = state.pendingCreate ? '继续检查' : '创建并打开';
    if (state.pendingCreate && state.pendingCreate.body.host_ref !== state.selectedHost) q('createTaskButton').disabled = true;
    renderCreateAttachments();
  }
}

async function handleCreateResult(result) {
  const pending = state.pendingCreate;
  if (!pending || result.request_id !== pending.body.request_id) return;
  pending.controllerAccepted = true;
  if (['submitted', 'accepted', 'pending'].includes(result.state)) {
    q('newTaskFeedback').textContent = '请求已登记，等待 SSE 推送 Mac 确认；无需手动刷新';
    return;
  }
  if (result.state !== 'confirmed' || result.action !== 'create' || !result.thread_ref) {
    if (!['unknown', 'recovery_required'].includes(result.state)) state.pendingCreate = null;
    renderNewTaskState();
    q('newTaskFeedback').textContent = `创建未完成（${result.state || 'unknown'}）；草稿保留，请核对结果`;
    return;
  }
  syncCreateDraftHost();
  state.pendingCreate = null;
  clearConfirmedCreateDraft(pending.body);
  renderNewTaskState();
  q('newTaskFeedback').textContent = 'Mac 已确认创建，正在打开同一个任务';
  await addUnknownThread(result.thread_ref);
  // Closing the sheet is a deliberate navigation; late receipts must not steal another task.
  if (!q('newTaskSheet').classList.contains('hidden') && state.selectedHost === pending.body.host_ref) {
    setNewTaskOpen(false);
    await selectThread(result.thread_ref);
  }
}

function leaveDetail() {
  if (state.selectedThread) state.drafts[state.selectedThread] = q('composerInput').value;
  stopEventStream();
  document.body.classList.remove('detail-open');
}

function resizeComposer() {
  const input = q('composerInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

q('hostSelect').onchange = async () => { state.selectedHost = q('hostSelect').value; state.selectedProject = 'all'; state.selectedThread = ''; state.selectedModel = ''; state.selectedEffort = ''; state.selectedPermission = ''; state.selectedCollaborationMode = ''; state.detail = null; state.overviewCursor = 0; state.eventCursor = 0; resetHistoryState(); stopEventStream(); stopOverviewStream(); await refreshOverview({preserveDetail: false}); };
q('statusFilter').onchange = () => void loadThreadPage({reset: true});
q('threadSearch').oninput = () => {
  window.clearTimeout(state.taskSearchTimer);
  state.taskSearchTimer = window.setTimeout(() => void loadThreadPage({reset: true}), 280);
};
q('loadMoreThreads').onclick = () => void loadThreadPage();
q('threadList').addEventListener('scroll', maybeLoadMoreThreads, {passive: true});
q('checkConnection').onclick = () => { state.overviewError = ''; startOverviewStream(); if (state.selectedThread) startEventStream(); };
q('mobileProjects').onclick = () => setProjectsOpen(true);
q('closeProjects').onclick = () => setProjectsOpen(false);
q('detailBack').onclick = leaveDetail;
q('newTaskButton').onclick = () => setNewTaskOpen(true);
q('mobileNewTask').onclick = () => setNewTaskOpen(true);
q('connectionState').onclick = () => setConnectionOpen(true);
q('mobileConnection').onclick = () => setConnectionOpen(true);
q('managementButton').onclick = () => void setManagementOpen(true);
if (q('mobileManagement')) q('mobileManagement').onclick = () => void setManagementOpen(true);
q('closeManagement').onclick = () => void setManagementOpen(false);
q('taskSettingsButton').onclick = () => void setTaskSettingsOpen(true);
q('closeTaskSettings').onclick = () => void setTaskSettingsOpen(false);
q('cancelTaskSettings').onclick = () => void setTaskSettingsOpen(false);
q('taskCollaborationMode').onchange = renderTaskSettings;
q('taskSettingsForm').onsubmit = async event => {
  event.preventDefault();
  const settings = state.threadSettings;
  const collaborationMode = q('taskCollaborationMode').value;
  if (!settings || state.settingsBusy || !collaborationMode || !settings.features?.update?.available) return;
  state.settingsBusy = true;
  renderTaskSettings();
  q('taskSettingsFeedback').textContent = 'Controller 已接收，正在等待 Mac 确认。';
  try {
    const result = await jsonFetch(`${API}/threads/${encodeURIComponent(settings.thread_ref)}/collaboration-mode`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify({request_id: requestId(), thread_revision: settings.thread_revision, collaboration_mode_id: collaborationMode})});
    q('taskSettingsFeedback').textContent = result.state === 'confirmed' ? 'Mac 已确认任务模式。' : result.state === 'unknown' ? '结果暂时未知；系统不会自动重发，请等待实时状态核对。' : '请求已登记，等待 Mac 精确确认。';
    await loadThread(settings.thread_ref, {restartStream: false});
  } catch (error) {
    q('taskSettingsFeedback').textContent = `${error.message}。不会自动重发。`;
  } finally {
    state.settingsBusy = false;
    try { state.threadSettings = await jsonFetch(`${API}/threads/${encodeURIComponent(settings.thread_ref)}/settings`); } catch (_error) {}
    renderTaskSettings();
  }
};
q('closeConnection').onclick = () => setConnectionOpen(false);
q('closeNewTask').onclick = () => setNewTaskOpen(false);
q('cancelNewTask').onclick = () => setNewTaskOpen(false);
q('modalBackdrop').onclick = () => { setNewTaskOpen(false); setProjectsOpen(false); setConnectionOpen(false); void setManagementOpen(false); void setTaskSettingsOpen(false); setRenameOpen(false); setReviewOpen(false); setResourceOpen(false); setRequestOpen(); };
document.addEventListener('keydown', event => {
  const dialog = !q('imageDialog').classList.contains('hidden') ? q('imageDialog') : !q('resourceSheet').classList.contains('hidden') ? q('resourceSheet') : q('requestSheet') && !q('requestSheet').classList.contains('hidden') ? q('requestSheet') : !q('reviewSheet').classList.contains('hidden') ? q('reviewSheet') : !q('renameSheet').classList.contains('hidden') ? q('renameSheet') : !q('taskSettingsSheet').classList.contains('hidden') ? q('taskSettingsSheet') : !q('managementSheet').classList.contains('hidden') ? q('managementSheet') : q('projectPanel').classList.contains('open') ? q('projectPanel') : !q('newTaskSheet').classList.contains('hidden')
    ? q('newTaskSheet')
    : !q('connectionSheet').classList.contains('hidden')
      ? q('connectionSheet')
      : null;
  if (event.key === 'Escape' && (dialog || q('projectPanel').classList.contains('open'))) {
    event.preventDefault();
    setImageOpen();
    setNewTaskOpen(false);
    setProjectsOpen(false);
    setConnectionOpen(false);
    void setManagementOpen(false);
    void setTaskSettingsOpen(false);
    setRenameOpen(false);
    setReviewOpen(false);
    setResourceOpen(false);
    setRequestOpen();
    return;
  }
  if (event.key !== 'Tab' || !dialog) return;
  const focusable = Array.from(dialog.querySelectorAll('button:not(:disabled),select:not(:disabled),textarea:not(:disabled),input:not(:disabled),a[href]'));
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
initPermissionUi();
q('newTaskForm').onsubmit = createThread;
q('safeMode').onclick = () => setMode('safe');
q('nativeMode').onclick = () => setMode('native');
q('modelSelect').onchange = () => { state.selectedModel = q('modelSelect').value; state.selectedEffort = ''; if (state.detail) renderComposer(state.detail); };
q('effortSelect').onchange = () => { state.selectedEffort = q('effortSelect').value; };
q('permissionSelect').onchange = () => { state.selectedPermission = q('permissionSelect').value; };
q('collaborationModeSelect').onchange = () => { state.selectedCollaborationMode = q('collaborationModeSelect').value; };
q('newTaskModel').onchange = () => { populateEffortOptions(q('newTaskEffort'), q('newTaskModel').value); saveCreateDraftFields(); };
q('newTaskEffort').onchange = saveCreateDraftFields;
q('newTaskPermission').onchange = saveCreateDraftFields;
q('newTaskCollaborationMode').onchange = saveCreateDraftFields;
q('newTaskProject').onchange = saveCreateDraftFields;
q('newTaskInput').oninput = saveCreateDraftFields;
q('interruptButton').onclick = () => { q('taskMenu').open = false; if (state.detail) void submitAction('interrupt', {expected_turn_ref: state.detail.active_turn_ref}); };
q('archiveButton').onclick = () => { q('taskMenu').open = false; if (state.detail && confirm(`归档“${state.detail.title}”？任务不会被删除。`)) void submitAction('archive'); };
q('unarchiveButton').onclick = () => { q('taskMenu').open = false; if (state.detail) void submitAction('unarchive'); };
q('renameButton').onclick = () => setRenameOpen(true);
q('forkButton').onclick = () => {
  q('taskMenu').open = false;
  if (state.detail && confirm(`从“${state.detail.title}”派生一个新任务？`)) void submitAction('fork');
};
q('reviewButton').onclick = () => setReviewOpen(true);
q('resourceButton').onclick = () => setResourceOpen(true);
q('closeResource').onclick = () => setResourceOpen(false);
q('resourceFilesTab').onclick = () => setResourceTab('files');
q('resourceDiffTab').onclick = () => setResourceTab('diff');
q('resourceDiagnosticsTab').onclick = () => setResourceTab('diagnostics');
q('resourceBack').onclick = () => {
  if (resourceUi.tab === 'diagnostics') { resourceUi.diagnostic = null; renderResources(); return; }
  if (resourceUi.tab === 'diff') { resourceUi.diffFile = null; resourceUi.diffChunks = []; renderResources(); return; }
  if (resourceUi.file) {
    if (resourceUi.file.objectUrl) URL.revokeObjectURL(resourceUi.file.objectUrl);
    resourceUi.file = null; resourceUi.chunks = []; renderResources(); return;
  }
  if (!resourceUi.path) return;
  const parts = resourceUi.path.split('/'); parts.pop(); requestResourceList(parts.join('/'));
};
q('resourceMore').onclick = () => { if (resourceUi.tab === 'diff') requestDiffChunk(); else if (resourceUi.file) requestResourceChunk(); else if (resourceUi.hasMore) requestResourceList(resourceUi.path, {append: true}); };
q('closeRename').onclick = () => setRenameOpen(false);
q('cancelRename').onclick = () => setRenameOpen(false);
q('closeReview').onclick = () => setReviewOpen(false);
q('cancelReview').onclick = () => setReviewOpen(false);
q('reviewType').onchange = () => {
  const branch = q('reviewType').value === 'baseBranch';
  q('reviewBranchField').classList.toggle('hidden', !branch);
  if (branch) q('reviewBranch').focus();
};
q('reviewForm').onsubmit = event => {
  event.preventDefault();
  const type = q('reviewType').value;
  const branch = q('reviewBranch').value.trim();
  if (type === 'baseBranch' && !branch) { q('reviewFeedback').textContent = '请输入基准分支名称。'; return; }
  const review_target = type === 'baseBranch' ? {type, branch} : {type: 'uncommittedChanges'};
  void submitAction('review', {review_target});
  setReviewOpen(false);
};
q('renameForm').onsubmit = event => {
  event.preventDefault();
  const title = q('renameInput').value.trim();
  if (!state.detail || !title || title.length > 80) { q('renameFeedback').textContent = '请输入 1–80 个字符的任务名称。'; return; }
  void submitAction('rename', {title});
  setRenameOpen(false);
};
q('pinButton').onclick = () => {
  q('taskMenu').open = false;
  if (state.detail) void submitAction('pin', {pinned: !(state.detail.pinned === true || state.detail.snapshot?.pinned === true)});
};
q('loadEarlierMessages').onclick = () => void requestHistoryPage();
q('historySearchForm').onsubmit = event => { event.preventDefault(); void requestHistorySearch(); };
q('loadMoreSearchResults').onclick = () => void requestHistorySearch({append: true});
q('historySearchInput').oninput = () => { state.searchError = ''; renderHistoryControls(); };
q('composer').onsubmit = event => {
  event.preventDefault();
  const detail = state.detail;
  const input = q('composerInput').value.trim();
  const action = detail ? composerAction(detail) : null;
  const attachments = currentAttachments();
  if (!detail || !action || (!input && !attachments.length)) { q('composerFeedback').className = 'composer-status warning'; q('composerFeedback').textContent = '请输入消息或添加图片'; return; }
  if (q('submitDirection').disabled) return;
  if (attachments.length && (!hasCapability('image_input_v1') || (action === 'steer' && state.mode === 'native') || attachments.some(item => !item.image_ref || item.error || Date.parse(item.expires_at) <= Date.now()))) { renderAttachments(); q('composerFeedback').textContent = '图片尚未就绪，或当前发送方式不支持图片'; return; }
  if (!navigator.onLine || !writeAvailable()) { q('composerFeedback').className = 'composer-status warning'; q('composerFeedback').textContent = 'Mac 离线，草稿已保留且没有发送'; return; }
  const model = state.selectedModel && (action === 'continue' || state.mode === 'safe') ? state.selectedModel : '';
  const effort = state.selectedEffort && (action === 'continue' || state.mode === 'safe') ? state.selectedEffort : '';
  const permission = state.selectedPermission && (action === 'continue' || state.mode === 'safe') ? state.selectedPermission : '';
  const collaborationMode = state.selectedCollaborationMode && (action === 'continue' || state.mode === 'safe') ? state.selectedCollaborationMode : '';
  const images = attachments.length ? {image_refs: attachments.map(item => item.image_ref)} : {};
  if (action === 'steer') void submitAction('steer', {expected_turn_ref: detail.active_turn_ref, input, mode: state.mode, ...images, ...(model ? {model} : {}), ...(effort ? {effort} : {}), ...(permission ? {permission_profile_id: permission} : {}), ...(collaborationMode ? {collaboration_mode_id: collaborationMode} : {})});
  else void submitAction('continue', {input, ...images, ...(model ? {model} : {}), ...(effort ? {effort} : {}), ...(permission ? {permission_profile_id: permission} : {}), ...(collaborationMode ? {collaboration_mode_id: collaborationMode} : {})});
};
q('composerInput').oninput = () => { if (state.selectedThread) state.drafts[state.selectedThread] = q('composerInput').value; resizeComposer(); };
q('composerInput').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !window.matchMedia('(pointer: coarse)').matches) { event.preventDefault(); q('composer').requestSubmit(); } };
q('conversationView').onscroll = () => { state.following = isNearConversationBottom(); if (state.following) q('newReplyButton').classList.add('hidden'); };
q('newReplyButton').onclick = followLatestReply;
window.addEventListener('online', () => { state.streamFailures.add('overview'); renderFreshness(); if (state.selectedHost) startOverviewStream(); if (state.selectedThread && document.body.classList.contains('detail-open')) { void loadThread(state.selectedThread); startEventStream(); } });
window.addEventListener('offline', () => { stopEventStream(); stopOverviewStream(); state.overviewStreamState = 'offline'; state.streamFailures.add('overview'); renderFreshness(); renderRunnerBanner(); renderNewTaskState(); });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { if (!state.overviewSource || Date.now() - state.lastOverviewFrameAt > 30000) startOverviewStream(); if (state.selectedThread && document.body.classList.contains('detail-open')) { void loadThread(state.selectedThread); if (!state.eventSource || Date.now() - state.lastDetailFrameAt > 30000) startEventStream(); } } });
window.addEventListener('beforeunload', () => { stopOverviewStream(); stopEventStream(); });
window.addEventListener('resize', updateViewportHeight);
window.addEventListener('scroll', maybeLoadMoreThreads, {passive: true});
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', updateViewportHeight);
  window.visualViewport.addEventListener('scroll', updateViewportHeight);
}

initImageUi();
initRequestUi();
updateViewportHeight();
void refreshOverview({preserveDetail: false}).then(() => {
  const requested = new URLSearchParams(location.search).get('thread_ref');
  if (requested && /^TH-[A-Z2-7]{20,52}$/.test(requested)) void selectThread(requested);
  else if (new URLSearchParams(location.search).get('new') === '1') setNewTaskOpen(true);
});
setInterval(renderFreshness, 1000);
setInterval(() => {
  if (!navigator.onLine) return;
  if (state.overviewSource && state.lastOverviewFrameAt && Date.now() - state.lastOverviewFrameAt > 35000) scheduleOverviewReconnect();
  if (state.eventSource && state.lastDetailFrameAt && Date.now() - state.lastDetailFrameAt > 35000) scheduleEventReconnect();
}, 5000);
"""


from .desktop_image_ui import IMAGE_UI_JS

DESKTOP_DASHBOARD_JS = IMAGE_UI_JS + DESKTOP_DASHBOARD_JS

__all__ = ["DESKTOP_DASHBOARD_HTML", "DESKTOP_DASHBOARD_JS"]
