"""Responsive Home Assistant Ingress workbench for Codex Desktop takeover."""

DESKTOP_DASHBOARD_HTML_LEGACY = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
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
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Codex 控制器</title><style>
:root{color-scheme:light;--bg:#f7f7f5;--surface:#fff;--surface-2:#f1f1ee;--surface-3:#fbfbfa;--line:rgba(31,32,35,.1);--line-strong:rgba(31,32,35,.17);--text:#202124;--muted:#777a7f;--blue:#3f68da;--blue-soft:#edf1fc;--green:#218461;--green-soft:#eaf4ef;--amber:#9b681d;--amber-soft:#f8f0e3;--red:#b43d50;--red-soft:#f9ebee;--shadow:0 1px 2px rgba(20,22,26,.035),0 12px 32px rgba(20,22,26,.05)}
*{box-sizing:border-box}html{height:100%;background:var(--bg);scrollbar-gutter:stable}body{min-width:320px;min-height:100%;margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue",sans-serif;-webkit-font-smoothing:antialiased}button,input,select,textarea{font:inherit;color:inherit}button,a,input,select,textarea{outline-offset:3px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid var(--blue)}button{min-height:42px;border:1px solid var(--line-strong);border-radius:11px;padding:8px 13px;background:var(--surface);cursor:pointer;transition:background .15s,border-color .15s,transform .15s}button:hover:not(:disabled){background:var(--surface-2)}button:active:not(:disabled){transform:scale(.985)}button:disabled{opacity:.42;cursor:not-allowed}button.primary{border-color:#222326;background:#222326;color:#fff}button.primary:hover:not(:disabled){background:#37383b}button.danger{border-color:#efd2d8;background:var(--red-soft);color:var(--red)}button.ghost{border-color:transparent;background:transparent}a{color:inherit;text-decoration:none}.hidden{display:none!important}.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.muted{color:var(--muted)}.error{color:var(--red)}.success{color:var(--green)}.warning{color:var(--amber)}
.shell{max-width:1540px;margin:auto;padding:16px 18px}.topbar{height:58px;display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:12px}.brand{display:flex;align-items:center;gap:11px;min-width:0}.brand-mark{width:36px;height:36px;display:grid;place-items:center;border-radius:11px;background:#222326;color:#fff;font-weight:750}.brand h1{margin:0;font-size:21px;line-height:1.2;letter-spacing:-.025em}.subtitle{margin:2px 0 0;color:var(--muted);font-size:12px}.top-actions{display:flex;align-items:center;gap:8px}.connection{display:inline-flex;align-items:center;gap:7px;min-height:34px;border:1px solid var(--line);border-radius:999px;padding:5px 10px;background:var(--surface);color:var(--muted);font-size:12px}.connection::before{content:"";width:7px;height:7px;border-radius:999px;background:#aaa}.connection.good{color:var(--green)}.connection.good::before{background:var(--green);box-shadow:0 0 0 3px var(--green-soft)}.connection.warn{color:var(--amber)}.connection.warn::before{background:var(--amber)}.connection.bad{color:var(--red)}.connection.bad::before{background:var(--red)}.settings-link{min-height:42px;display:inline-flex;align-items:center;border:1px solid var(--line-strong);border-radius:11px;padding:8px 13px;background:var(--surface)}
.runner-banner{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px;padding:12px 14px;border:1px solid #ead8b9;border-radius:14px;background:var(--amber-soft)}.runner-banner.ready{display:none}.runner-banner strong{display:block}.runner-banner span{display:block;margin-top:2px;color:var(--muted);font-size:12px}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;margin-bottom:12px}.metric{min-width:0;border:1px solid var(--line);border-radius:12px;padding:10px 13px;background:var(--surface)}.metric span{display:block;color:var(--muted);font-size:11px}.metric strong{display:block;margin-top:2px;font-size:19px;line-height:1.25}
.workspace{height:calc(100dvh - 172px);min-height:560px;display:grid;grid-template-columns:220px 330px minmax(420px,1fr);gap:11px}.panel{min-width:0;min-height:0;border:1px solid var(--line);border-radius:15px;background:var(--surface);box-shadow:var(--shadow);overflow:hidden}.panel-head{min-height:53px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 13px;border-bottom:1px solid var(--line);background:var(--surface-3)}.panel-head h2{margin:0;font-size:15px}.panel-body{padding:11px}.stack{display:grid;gap:10px}.field{display:grid;gap:5px}.field label{font-size:12px;color:var(--muted)}select,input,textarea{width:100%;border:1px solid var(--line-strong);border-radius:11px;background:var(--surface);padding:9px 10px}textarea{resize:none}.badge-row,.detail-meta{display:flex;flex-wrap:wrap;gap:6px}.badge{display:inline-flex;align-items:center;min-height:24px;border:0;border-radius:999px;padding:3px 8px;background:#efefec;color:#65686d;font-size:11px}.badge.good{background:var(--green-soft);color:var(--green)}.badge.warn{background:var(--amber-soft);color:var(--amber)}.badge.bad{background:var(--red-soft);color:var(--red)}
.project-panel .panel-head button{display:none}.project-list,.thread-list{display:grid}.project-button,.thread-button{width:100%;height:auto;text-align:left;border-color:transparent;background:transparent}.project-button{padding:10px;border-radius:10px}.project-button:hover,.project-button.selected{background:var(--surface-2)}.project-title,.thread-title{font-weight:660;overflow-wrap:anywhere}.project-meta,.thread-meta,.thread-preview{margin-top:3px;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.filters{display:grid;grid-template-columns:1fr 1.3fr;gap:7px}.thread-scroll{height:calc(100% - 122px);overflow:auto;padding:0 9px}.thread-button{position:relative;min-height:96px;border-radius:0;border-bottom:1px solid var(--line);padding:12px 9px}.thread-button.selected{background:#f6f6f3}.thread-button.selected::before,.thread-button.active-thread::before{content:"";position:absolute;left:0;top:18px;bottom:18px;width:2px;border-radius:2px;background:var(--blue)}.thread-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}.thread-state{flex:0 0 auto;color:var(--muted);font-size:11px}.thread-state.good{color:var(--green)}.thread-state.bad{color:var(--red)}.empty{padding:34px 16px;text-align:center;color:var(--muted)}
.detail-panel{display:flex;flex-direction:column}.detail-empty{height:100%;display:grid;place-items:center;padding:30px;text-align:center}.detail-empty h2{margin:0 0 6px;font-size:20px}.detail-content{height:100%;min-height:0;display:flex;flex-direction:column}.detail-head{flex:0 0 auto;position:relative;padding:13px 16px 11px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.95)}.detail-title-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:start;gap:10px}.detail-heading{min-width:0}.detail-head h2{margin:0;font-size:18px;line-height:1.3;letter-spacing:-.015em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.eyebrow{color:var(--muted);font-size:11px}.detail-preview{margin-top:3px;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.detail-meta{margin-top:8px}.detail-back{display:none}.task-menu{position:relative}.task-menu summary{width:42px;height:42px;display:grid;place-items:center;border:1px solid var(--line-strong);border-radius:11px;background:var(--surface);cursor:pointer;list-style:none;font-weight:650}.task-menu summary::-webkit-details-marker{display:none}.task-menu-popover{position:absolute;z-index:12;top:48px;right:0;width:210px;display:grid;gap:5px;padding:7px;border:1px solid var(--line);border-radius:13px;background:var(--surface);box-shadow:0 16px 44px rgba(20,22,26,.14)}.task-menu-popover button{width:100%;text-align:left}.notice{flex:0 0 auto;margin:10px 16px 0;border-left:3px solid var(--blue);border-radius:6px 11px 11px 6px;background:var(--blue-soft);padding:9px 11px;color:#4c5d8d;font-size:12px}.notice.warn{border-color:var(--amber);background:var(--amber-soft);color:#805a20}.notice.bad{border-color:var(--red);background:var(--red-soft);color:#983547}
.conversation-wrap{position:relative;flex:1;min-height:0;overflow:hidden}.conversation{height:100%;overflow:auto;padding:20px clamp(16px,4vw,54px) 26px;scroll-behavior:smooth}.conversation-inner{max-width:820px;display:grid;gap:18px;margin:0 auto}.message{min-width:0}.message-role{margin:0 0 5px;color:var(--muted);font-size:11px;font-weight:650}.message-body{white-space:pre-wrap;overflow-wrap:anywhere}.message.assistant{padding-right:9%}.message.assistant .message-body{font-size:15px;line-height:1.65}.message.user{justify-self:end;width:min(82%,680px);padding:11px 14px;border-radius:17px 17px 5px 17px;background:var(--surface-2)}.message.user .message-role{display:none}.message.system{justify-self:center;color:var(--muted);font-size:12px;text-align:center}.message.streaming .message-role{color:var(--green)}.typing-dot{display:inline-block;width:6px;height:6px;margin-left:6px;border-radius:99px;background:var(--green);animation:pulse 1.3s infinite}@keyframes pulse{0%,100%{opacity:.25}50%{opacity:1}}.run-details{border:1px solid var(--line);border-radius:12px;background:var(--surface-3)}.run-details summary{cursor:pointer;list-style:none;padding:9px 11px;color:var(--muted);font-size:12px}.run-details summary::-webkit-details-marker{display:none}.run-details[open] summary{border-bottom:1px solid var(--line)}.run-detail-items{display:grid;gap:7px;padding:9px}.run-item{border-left:2px solid var(--line-strong);border-radius:6px;background:var(--surface);padding:8px 9px}.run-item-label{margin-bottom:3px;color:var(--muted);font-size:11px;font-weight:650}.run-item-text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.code-output{max-height:190px;margin:6px 0 0;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:8px;padding:8px;background:#f2f2ef;color:#3b4540;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}.new-reply{position:absolute;z-index:4;left:50%;bottom:13px;transform:translateX(-50%);min-height:36px;border-color:#d9dfef;border-radius:999px;background:var(--blue-soft);color:var(--blue);box-shadow:0 5px 16px rgba(30,44,80,.12)}
.composer{flex:0 0 auto;margin:0 16px 14px;border:1px solid var(--line-strong);border-radius:17px;background:var(--surface);box-shadow:0 8px 28px rgba(20,22,26,.08);padding:7px}.composer textarea{min-height:50px;max-height:160px;border:0;background:transparent;padding:9px 10px;overflow:auto}.composer-bar{display:flex;align-items:flex-end;gap:8px}.composer-tools{min-width:0;flex:1}.composer-status{min-height:20px;padding:1px 9px;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.send-button{flex:0 0 auto;min-width:70px}.advanced{margin:1px 4px 5px}.advanced summary{display:inline-flex;min-height:30px;align-items:center;cursor:pointer;color:var(--muted);font-size:11px;list-style:none}.advanced summary::-webkit-details-marker{display:none}.advanced-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:5px}.mode-switch{display:flex;gap:5px}.mode-switch button{min-height:36px;padding:6px 9px;background:transparent;font-size:12px}.mode-switch button[aria-pressed="true"]{border-color:#222326;background:#222326;color:#fff}.model-meta{grid-column:1/-1;color:var(--muted);font-size:11px}
.mobile-nav{display:none}.new-task-sheet{position:fixed;z-index:90;left:50%;top:50%;width:min(520px,calc(100% - 32px));max-height:calc(100dvh - 32px);overflow:auto;transform:translate(-50%,-50%);border:1px solid var(--line);border-radius:20px;background:var(--surface);box-shadow:0 24px 80px rgba(22,25,30,.2);padding:8px 18px 20px}.sheet-handle{display:none;width:38px;height:4px;border-radius:4px;background:#c7c9cc;margin:0 auto 6px}.sheet-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.sheet-head h2{margin:9px 0;font-size:20px}.sheet-copy{margin:0 0 15px;color:var(--muted);font-size:13px}.new-task-form{display:grid;gap:13px}.new-task-form textarea{min-height:126px;resize:vertical}.sheet-actions{display:flex;gap:8px;justify-content:flex-end}.sheet-actions .primary{min-width:130px}.feedback{min-height:20px;color:var(--muted);font-size:12px}.modal-backdrop{position:fixed;z-index:80;inset:0;background:rgba(25,27,30,.25)}
@media(max-width:1050px){.workspace{grid-template-columns:190px 300px minmax(360px,1fr)}.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:820px){body{padding-bottom:calc(76px + env(safe-area-inset-bottom))}.shell{padding:0 12px 16px}.topbar{height:74px;margin:0;padding-top:env(safe-area-inset-top)}.brand-mark,.subtitle,.settings-link{display:none}.brand h1{font-size:24px}.top-actions .connection{padding-inline:9px}.top-actions .primary{padding-inline:11px}.runner-banner{margin:3px 0 11px}.metrics{display:none}.workspace{height:auto;min-height:0;display:block}.panel{box-shadow:none}.project-panel{display:none;position:fixed;z-index:90;left:10px;right:10px;bottom:10px;max-height:76dvh;overflow:auto;border-radius:20px}.project-panel.open{display:block}.project-panel .panel-head button{display:inline-flex}.project-panel.open+.thread-panel{pointer-events:none}.thread-panel{border:0;background:transparent}.thread-panel .panel-head{padding-inline:3px;background:transparent;border:0}.thread-panel .panel-body{padding:5px 0 9px}.thread-scroll{height:auto;padding:0}.thread-button{padding:13px 10px;min-height:98px}.detail-panel{display:none;position:fixed;z-index:40;inset:0;border:0;border-radius:0;background:var(--bg)}.detail-open .detail-panel{display:flex}.detail-open .shell{padding:0}.detail-open .topbar,.detail-open .runner-banner,.detail-open .metrics,.detail-open .project-panel,.detail-open .thread-panel,.detail-open .mobile-nav{display:none}.detail-content{height:100dvh}.detail-head{padding:calc(9px + env(safe-area-inset-top)) 10px 10px;background:rgba(247,247,245,.94);backdrop-filter:blur(18px)}.detail-title-row{grid-template-columns:48px minmax(0,1fr) 42px;align-items:center}.detail-back{width:48px;display:inline-flex;align-items:center;justify-content:center;border-color:transparent;background:transparent;padding:0}.detail-preview{display:none}.detail-meta{margin-left:58px;margin-top:4px}.conversation{padding:17px 14px 210px}.conversation-inner{gap:17px}.message.assistant{padding-right:2%}.message.user{width:min(88%,680px)}.notice{margin-inline:10px}.composer{position:fixed;z-index:8;left:8px;right:8px;bottom:8px;margin:0;border-radius:19px;padding:6px 7px calc(7px + env(safe-area-inset-bottom));background:rgba(255,255,255,.96);backdrop-filter:blur(18px)}.composer textarea{max-height:118px}.advanced-grid{grid-template-columns:1fr}.model-meta{grid-column:auto}.mobile-nav{position:fixed;z-index:30;left:12px;right:12px;bottom:10px;height:calc(64px + env(safe-area-inset-bottom));display:grid;grid-template-columns:repeat(4,1fr);padding:4px 7px env(safe-area-inset-bottom);border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.95);box-shadow:0 8px 28px rgba(25,28,32,.11);backdrop-filter:blur(18px)}.mobile-nav button,.mobile-nav a{min-height:54px;border:0;border-radius:12px;background:transparent;color:var(--muted);display:flex;align-items:center;justify-content:center;font-size:12px}.mobile-nav .primary-nav{margin-top:-14px;height:56px;align-self:start;background:#222326;color:#fff;box-shadow:0 6px 16px rgba(0,0,0,.18)}.new-task-sheet{left:8px;right:8px;top:auto;bottom:0;width:auto;max-height:88dvh;transform:none;border-radius:22px 22px 0 0;padding-bottom:calc(18px + env(safe-area-inset-bottom))}.sheet-handle{display:block}.sheet-actions{display:grid;grid-template-columns:1fr 1.4fr}.sheet-actions button{width:100%}}
@media(max-width:520px){.shell{padding-inline:9px}.runner-banner{padding:11px}.filters{grid-template-columns:1fr 1.25fr}.task-menu-popover{position:fixed;top:auto;left:10px;right:10px;bottom:calc(12px + env(safe-area-inset-bottom));width:auto}.advanced-grid{padding-inline:2px}.sheet-actions{grid-template-columns:1fr}.new-task-sheet{left:0;right:0}}
</style></head><body><main class="shell">
<header class="topbar"><div class="brand"><span class="brand-mark">C</span><div><h1>Codex</h1><p class="subtitle">Mac 上的任务，随时继续对话</p></div></div><div class="top-actions"><span id="connectionState" class="connection">正在连接</span><a href="../" class="settings-link">设置</a><button id="newTaskButton" class="primary" type="button">新建任务</button></div></header>
<section id="runnerBanner" class="runner-banner"><div><strong id="runnerBannerTitle">正在连接 Mac</strong><span id="runnerBannerText">任务与回复会自动同步。</span></div><button id="checkConnection" type="button">重试连接</button></section>
<section class="metrics" aria-label="任务概览"><div class="metric"><span>Mac 主机</span><strong id="metricHosts">0</strong></div><div class="metric"><span>项目</span><strong id="metricProjects">0</strong></div><div class="metric"><span>全部任务</span><strong id="metricThreads">0</strong></div><div class="metric"><span>Codex 正在工作</span><strong id="metricActive">0</strong></div><div class="metric"><span>需要处理</span><strong id="metricRecovery">0</strong></div></section>
<section class="workspace">
<aside id="projectPanel" class="panel project-panel" aria-label="主机与项目"><div class="panel-head"><h2>项目</h2><button id="closeProjects" class="ghost" type="button">完成</button></div><div class="panel-body stack"><div class="field"><label for="hostSelect">Mac</label><select id="hostSelect" aria-label="选择 Mac 主机"></select></div><div class="badge-row"><span id="hostWriteState" class="badge">正在连接</span></div><div id="hostMeta" class="muted">等待第一次自动同步。</div><div id="projectList" class="project-list" aria-label="项目列表"></div></div></aside>
<section class="panel thread-panel" aria-label="任务列表"><div class="panel-head"><h2>任务</h2><span id="threadCount" class="badge">0</span></div><div class="panel-body stack"><div class="filters"><div class="field"><label for="statusFilter">查看</label><select id="statusFilter"><option value="all">全部任务</option><option value="active">正在工作</option><option value="idle">可以继续</option><option value="notLoaded">最近任务</option><option value="failed">需要处理</option><option value="recovery_required">需要处理</option><option value="protocol_degraded">暂时只读</option><option value="archived">已归档</option></select></div><div class="field"><label for="threadSearch">搜索</label><input id="threadSearch" maxlength="120" inputmode="search" placeholder="搜索任务标题"></div></div></div><div id="threadList" class="thread-list thread-scroll" aria-live="polite"></div></section>
<section class="panel detail-panel" aria-label="任务对话"><div id="detailEmpty" class="detail-empty"><div><h2>选择一个任务</h2><p class="muted">打开后即可像 Codex App 一样查看回复并继续对话。</p></div></div><div id="detailContent" class="detail-content hidden"><header class="detail-head"><div class="detail-title-row"><button id="detailBack" class="detail-back" type="button" aria-label="返回任务列表">返回</button><div class="detail-heading"><div id="detailProject" class="eyebrow">当前项目</div><h2 id="detailTitle">-</h2><div id="detailPreview" class="detail-preview"></div></div><details id="taskMenu" class="task-menu"><summary aria-label="更多任务操作">更多</summary><div class="task-menu-popover"><button id="interruptButton" class="danger" type="button">停止当前任务</button><button id="archiveButton" type="button">归档任务</button><button id="unarchiveButton" type="button">恢复归档</button></div></details></div><div id="detailMeta" class="detail-meta"></div></header><div id="detailNotice" class="notice hidden"></div><div class="conversation-wrap"><div id="conversationView" class="conversation" aria-live="polite"><div id="conversationInner" class="conversation-inner"></div></div><button id="newReplyButton" class="new-reply hidden" type="button">查看新回复</button></div><form id="composer" class="composer"><details id="advancedControls" class="advanced"><summary>模型与发送方式</summary><div class="advanced-grid"><div class="field"><label for="modelSelect">模型</label><select id="modelSelect" aria-describedby="modelMeta"></select></div><div class="field"><label>发送方式</label><div class="mode-switch"><button id="safeMode" type="button" aria-pressed="true">安全调整</button><button id="nativeMode" type="button" aria-pressed="false">快速调整</button></div></div><div id="modelMeta" class="model-meta"></div></div></details><label class="sr-only" for="composerInput">给 Codex 发消息</label><div class="composer-bar"><div class="composer-tools"><textarea id="composerInput" rows="1" maxlength="12000" placeholder="给 Codex 发消息"></textarea><div id="composerFeedback" class="composer-status" role="status">回复会自动出现在这里</div></div><button id="submitDirection" class="primary send-button" type="submit">发送</button></div></form></div></section>
</section></main>
<nav class="mobile-nav" aria-label="移动端导航"><a href="./">任务</a><button id="mobileProjects" type="button">项目</button><button id="mobileNewTask" class="primary-nav" type="button">新建</button><a href="../">设置</a></nav>
<div id="modalBackdrop" class="modal-backdrop hidden"></div><section id="newTaskSheet" class="new-task-sheet hidden" role="dialog" aria-modal="true" aria-labelledby="newTaskTitle"><div class="sheet-handle"></div><div class="sheet-head"><h2 id="newTaskTitle">新建任务</h2><button id="closeNewTask" class="ghost" type="button">关闭</button></div><p class="sheet-copy">任务会出现在 Mac 和手机的同一任务列表里。连接未确认时不会发送。</p><form id="newTaskForm" class="new-task-form"><div class="field"><label for="newTaskProject">项目</label><select id="newTaskProject" required></select></div><div class="field"><label for="newTaskInput">给 Codex 的任务</label><textarea id="newTaskInput" maxlength="12000" required placeholder="描述希望 Codex 完成的任务"></textarea></div><div class="field"><label for="newTaskModel">模型</label><select id="newTaskModel"></select></div><div id="newTaskFeedback" class="feedback" role="status">正在检查是否可以创建任务。</div><div class="sheet-actions"><button id="cancelNewTask" type="button">取消</button><button id="createTaskButton" class="primary" type="submit" disabled>创建并打开</button></div></form></section>
<script src="desktop.js"></script></body></html>"""


DESKTOP_DASHBOARD_JS = r"""
const q = id => document.getElementById(id);
const API = '../api/desktop/v1';
const STATUS_API = '../api/status';
const SHANGHAI_TIME = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
const state = {csrf: '', hosts: [], projects: [], threads: [], selectedHost: '', selectedProject: 'all', selectedThread: '', selectedModel: '', detail: null, events: [], eventCursor: 0, eventGeneration: 0, eventController: null, mode: 'safe', loading: false, drafts: {}, pendingCreate: null, following: true};

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

function renderNewTaskState() {
  const projectSelect = q('newTaskProject');
  const previousProject = projectSelect.value;
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
  const pending = state.pendingCreate;
  if (pending) {
    projectSelect.value = pending.body.project_ref;
    q('newTaskModel').value = pending.body.model || '';
    q('newTaskInput').value = pending.body.input;
  }
  const allowed = hostCanCreate() && state.projects.length > 0;
  q('createTaskButton').disabled = pending ? !navigator.onLine : !allowed;
  q('createTaskButton').textContent = pending ? '检查创建结果' : '创建并打开';
  q('newTaskProject').disabled = Boolean(pending) || state.projects.length === 0;
  q('newTaskModel').disabled = Boolean(pending) || !hasCapability('model_override_v1');
  q('newTaskInput').disabled = Boolean(pending) || state.projects.length === 0;
  q('newTaskFeedback').className = `feedback ${pending ? 'warning' : allowed ? 'muted' : 'warning'}`;
  if (pending) q('newTaskFeedback').textContent = '只会用同一 request ID 检查结果；Controller 持久幂等日志保证不会重复创建。';
  else if (allowed) q('newTaskFeedback').textContent = '创建完成后才会打开新任务；等待或未知状态不会伪装为已发送。';
  else if (!currentHost()?.online || !currentHost()?.write_available) q('newTaskFeedback').textContent = 'Runner 离线：提交已禁用，草稿只保留在当前页面内存中。';
  else if (!hasCapability('create_thread_v1')) q('newTaskFeedback').textContent = '当前 Runner 尚未提供 create_thread_v1，不能远程新建任务。';
  else q('newTaskFeedback').textContent = '当前主机没有可选项目，不能创建任务。';
}

function setNewTaskOpen(open) {
  q('newTaskSheet').classList.toggle('hidden', !open);
  q('modalBackdrop').classList.toggle('hidden', !open);
  if (open) {
    renderNewTaskState();
    if (!state.pendingCreate) window.setTimeout(() => q('newTaskInput').focus(), 0);
  }
}

function setProjectsOpen(open) {
  q('projectPanel').classList.toggle('open', open);
  q('modalBackdrop').classList.toggle('hidden', !open);
}

function renderMetrics() {
  q('metricHosts').textContent = state.hosts.length;
  q('metricProjects').textContent = state.projects.length;
  q('metricThreads').textContent = state.threads.length;
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
  const rank = {active: 0, recovery_required: 1, failed: 1, protocol_degraded: 2, idle: 3, notLoaded: 4, archived: 5};
  const threads = filteredThreads().sort((left, right) => (rank[left.status] ?? 9) - (rank[right.status] ?? 9) || new Date(right.updated_at || 0) - new Date(left.updated_at || 0));
  q('threadCount').textContent = `${threads.length}`;
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
    empty.textContent = state.threads.length ? '这里暂时没有符合条件的任务。' : '正在等待 Mac 同步任务。';
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
  q('detailProject').textContent = state.projects.find(project => project.project_ref === detail.project_ref)?.project_alias || '当前项目';
  q('detailPreview').textContent = text(snapshot.preview, detail.status === 'active' ? 'Codex 正在处理这个任务' : '可以继续发送消息');
  const meta = q('detailMeta');
  meta.replaceChildren(badge(statusText(detail.status), statusKind(detail.status)), badge(`更新于 ${formatTime(detail.updated_at)}`));
  if (snapshot.history_incomplete) meta.append(badge('部分较早消息未显示', 'warn'));
  renderNotice(detail);
  renderActionState(detail);
  renderConversation(snapshot.turns || []);
  renderComposer(detail);
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

function renderActionState(detail) {
  const active = detail.status === 'active' && Boolean(detail.active_turn_ref);
  const blocked = !writeAvailable() || ['recovery_required', 'protocol_degraded'].includes(detail.status) || ['recovery_required', 'refresh_required', 'protocol_degraded'].includes(detail.control_state);
  q('interruptButton').classList.toggle('hidden', !active);
  q('interruptButton').disabled = blocked || !hasCapability('interrupt_expected_turn');
  q('archiveButton').classList.toggle('hidden', detail.status !== 'idle');
  q('archiveButton').disabled = blocked || !hasCapability('archive_control_v1');
  q('unarchiveButton').classList.toggle('hidden', detail.status !== 'archived');
  q('unarchiveButton').disabled = !writeAvailable() || !hasCapability('archive_control_v1');
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

function appendRunDetails(root, turn) {
  const technical = (turn.items || []).filter(item => !['user.message', 'assistant.message'].includes(item?.type));
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

function renderConversation(turns) {
  const viewport = q('conversationView');
  const root = q('conversationInner');
  const shouldFollow = state.following || isNearConversationBottom();
  root.replaceChildren();
  const shown = new Set();
  for (const turn of turns) {
    for (const item of turn.items || []) {
      if (item?.type === 'user.message') { const value = text(item.text); if (value) { root.append(messageNode('user', value)); shown.add(`user:${value}`); } }
      if (item?.type === 'assistant.message') { const value = text(item.text); if (value) { root.append(messageNode('assistant', value)); shown.add(`assistant:${value}`); } }
    }
    appendRunDetails(root, turn);
  }
  for (const event of state.events) {
    if (!['user.message', 'assistant.completed'].includes(event.event_kind)) continue;
    const role = event.event_kind === 'user.message' ? 'user' : 'assistant';
    const value = eventSummary(event);
    if (value && !shown.has(`${role}:${value}`)) { root.append(messageNode(role, value)); shown.add(`${role}:${value}`); }
  }
  const live = liveAssistantText();
  if (state.detail?.status === 'active') root.append(messageNode('assistant', live, {streaming: true}));
  if (!root.children.length) root.append(messageNode('system', '还没有消息。你可以在下方开始对话。'));
  if (shouldFollow) {
    state.following = true;
    q('newReplyButton').classList.add('hidden');
    requestAnimationFrame(() => { viewport.scrollTop = viewport.scrollHeight; });
  } else {
    q('newReplyButton').classList.remove('hidden');
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
  q('submitDirection').disabled = !enabled;
  renderModelSelector(action, enabled);
  q('submitDirection').textContent = '发送';
  q('composerInput').placeholder = action === 'steer' ? '给 Codex 发消息，立即调整当前方向' : '给 Codex 发消息';
}

function setMode(mode) {
  state.mode = mode;
  if (mode === 'native') state.selectedModel = '';
  q('safeMode').setAttribute('aria-pressed', mode === 'safe' ? 'true' : 'false');
  q('nativeMode').setAttribute('aria-pressed', mode === 'native' ? 'true' : 'false');
  if (state.detail) renderDetail();
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
      document.body.classList.remove('detail-open');
    }
    renderHosts();
    renderProjects();
    renderThreads();
    renderMetrics();
    if (preserveDetail && state.selectedThread) await loadThread(state.selectedThread, {restartStream: false}); else renderDetail();
    setConnection(currentHost()?.online ? '已连接 · 自动同步' : 'Mac 离线', currentHost()?.online ? 'good' : 'bad');
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
  setProjectsOpen(false);
}

async function selectThread(threadRef) {
  if (state.selectedThread) state.drafts[state.selectedThread] = q('composerInput').value;
  state.selectedThread = threadRef;
  state.selectedModel = '';
  state.events = [];
  state.eventCursor = 0;
  state.following = true;
  renderThreads();
  await loadThread(threadRef, {restartStream: true, initialEvents: true});
  q('composerInput').value = state.drafts[threadRef] || '';
  resizeComposer();
  document.body.classList.add('detail-open');
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
    q('composerFeedback').className = 'composer-status error';
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
        renderConversation(state.detail?.snapshot?.turns || []);
      }
      if (Number.isFinite(Number(document.next_cursor))) state.eventCursor = Number(document.next_cursor);
      await loadThread(threadRef, {restartStream: false});
      setConnection('已连接 · 自动同步', 'good');
      backoff = 1000;
    } catch (error) {
      if (error.name === 'AbortError' || generation !== state.eventGeneration) return;
      setConnection('连接中断 · 自动重连', 'warn');
      await delay(backoff);
      backoff = Math.min(backoff * 2, 15000);
    }
  }
}

async function submitAction(action, extra = {}) {
  const detail = state.detail;
  if (!detail) return;
  const body = {request_id: requestId(), thread_revision: detail.thread_revision, ...extra};
  q('composerFeedback').className = 'composer-status muted';
  q('composerFeedback').textContent = action === 'steer' || action === 'continue' ? '正在发送…' : '正在处理…';
  q('submitDirection').disabled = true;
  try {
    const result = await jsonFetch(`${API}/threads/${encodeURIComponent(detail.thread_ref)}/${action}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    q('composerFeedback').className = 'composer-status success';
    q('composerFeedback').textContent = result.state === 'submitted' ? '已发送，等待 Codex 回复' : '已送达 Mac，正在同步';
    if (action === 'steer' || action === 'continue') {
      q('composerInput').value = '';
      delete state.drafts[detail.thread_ref];
      resizeComposer();
      state.following = true;
    }
    state.selectedModel = '';
    await loadThread(detail.thread_ref, {restartStream: false});
    setTimeout(() => void loadThread(detail.thread_ref, {restartStream: false}), 1200);
  } catch (error) {
    q('composerFeedback').className = 'composer-status error';
    q('composerFeedback').textContent = error.message;
    await loadThread(detail.thread_ref, {restartStream: false});
  }
}

async function createThread(event) {
  event.preventDefault();
  const host = currentHost();
  const existing = state.pendingCreate;
  const input = existing?.body.input || q('newTaskInput').value.trim();
  const projectRef = existing?.body.project_ref || q('newTaskProject').value;
  const model = existing?.body.model || q('newTaskModel').value;
  if (!existing && !hostCanCreate()) {
    q('newTaskFeedback').className = 'feedback warning';
    q('newTaskFeedback').textContent = 'Runner 当前不可创建任务；草稿仍保留，未发送。';
    return;
  }
  if (!existing && (!input || !state.projects.some(project => project.project_ref === projectRef))) {
    q('newTaskFeedback').className = 'feedback warning';
    q('newTaskFeedback').textContent = '请选择项目并填写任务要求。';
    return;
  }
  const body = existing?.body || {request_id: requestId(), host_ref: host.host_ref, project_ref: projectRef, input, ...(model ? {model} : {})};
  state.pendingCreate = existing || {body, controllerAccepted: false};
  renderNewTaskState();
  q('newTaskFeedback').className = 'feedback muted';
  q('newTaskFeedback').textContent = existing ? '正在用同一 request ID 检查收据…' : '正在提交创建请求，等待 Mac 收据…';
  let controllerAccepted = Boolean(existing?.controllerAccepted);
  try {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const result = await jsonFetch(`${API}/threads`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
      controllerAccepted = true;
      state.pendingCreate = {body, controllerAccepted: true};
      if (['submitted', 'accepted', 'pending'].includes(result.state)) {
        q('newTaskFeedback').className = 'feedback muted';
        q('newTaskFeedback').textContent = '请求已登记，正在用同一 request ID 等待 Mac 收据…';
        if (attempt < 11) await delay(700);
        continue;
      }
      if (result.state !== 'confirmed' || result.action !== 'create' || !result.thread_ref) {
        const uncertain = result.state === 'unknown' || result.state === 'recovery_required';
        state.pendingCreate = uncertain ? {body, controllerAccepted: true} : null;
        renderNewTaskState();
        q('newTaskFeedback').className = `feedback ${uncertain ? 'error' : 'warning'}`;
        q('newTaskFeedback').textContent = uncertain
          ? '创建结果需要对账；草稿与 request ID 已保留，不会重复创建。'
          : `创建未完成（${result.state || 'unknown'}）；可以修改草稿后重新提交。`;
        return;
      }
      state.pendingCreate = null;
      q('newTaskFeedback').className = 'feedback success';
      q('newTaskFeedback').textContent = 'Mac 已确认创建，正在打开同一个任务。';
      q('newTaskInput').value = '';
      await refreshOverview({preserveDetail: false});
      const created = state.threads.find(thread => thread.thread_ref === result.thread_ref);
      if (created) {
        setNewTaskOpen(false);
        await selectThread(created.thread_ref);
      } else {
        q('newTaskFeedback').className = 'feedback warning';
        q('newTaskFeedback').textContent = 'Mac 已确认创建，任务正在自动同步，稍后会出现在列表中。';
      }
      return;
    }
    state.pendingCreate = {body, controllerAccepted: true};
    renderNewTaskState();
    q('newTaskFeedback').className = 'feedback warning';
    q('newTaskFeedback').textContent = '请求仍在等待收据；可稍后用同一 request ID 检查结果。';
  } catch (error) {
    state.pendingCreate = {body, controllerAccepted};
    renderNewTaskState();
    q('newTaskFeedback').className = 'feedback error';
    if (controllerAccepted) {
      q('newTaskFeedback').textContent = `收据检查中断：${error.message}。草稿与 request ID 已保留，可稍后安全检查。`;
    } else {
      q('newTaskFeedback').textContent = `未确认 Controller 是否收到请求：${error.message}。不会自动重试；网络恢复后可用同一 request ID 安全检查。`;
    }
  }
}

function leaveDetail() {
  if (state.selectedThread) state.drafts[state.selectedThread] = q('composerInput').value;
  document.body.classList.remove('detail-open');
}

function resizeComposer() {
  const input = q('composerInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

q('hostSelect').onchange = async () => { state.selectedHost = q('hostSelect').value; state.selectedProject = 'all'; state.selectedThread = ''; state.selectedModel = ''; state.detail = null; stopEventStream(); await refreshOverview({preserveDetail: false}); };
q('statusFilter').onchange = renderThreads;
q('threadSearch').oninput = renderThreads;
q('checkConnection').onclick = () => refreshOverview();
q('mobileProjects').onclick = () => setProjectsOpen(true);
q('closeProjects').onclick = () => setProjectsOpen(false);
q('detailBack').onclick = leaveDetail;
q('newTaskButton').onclick = () => setNewTaskOpen(true);
q('mobileNewTask').onclick = () => setNewTaskOpen(true);
q('closeNewTask').onclick = () => setNewTaskOpen(false);
q('cancelNewTask').onclick = () => setNewTaskOpen(false);
q('modalBackdrop').onclick = () => { setNewTaskOpen(false); setProjectsOpen(false); };
q('newTaskForm').onsubmit = createThread;
q('safeMode').onclick = () => setMode('safe');
q('nativeMode').onclick = () => setMode('native');
q('modelSelect').onchange = () => { state.selectedModel = q('modelSelect').value; };
q('interruptButton').onclick = () => { q('taskMenu').open = false; if (state.detail) void submitAction('interrupt', {expected_turn_ref: state.detail.active_turn_ref}); };
q('archiveButton').onclick = () => { q('taskMenu').open = false; if (state.detail && confirm(`归档“${state.detail.title}”？任务不会被删除。`)) void submitAction('archive'); };
q('unarchiveButton').onclick = () => { q('taskMenu').open = false; if (state.detail) void submitAction('unarchive'); };
q('composer').onsubmit = event => {
  event.preventDefault();
  const detail = state.detail;
  const input = q('composerInput').value.trim();
  const action = detail ? composerAction(detail) : null;
  if (!detail || !action || !input) { q('composerFeedback').className = 'composer-status warning'; q('composerFeedback').textContent = '请输入要发送的消息'; return; }
  if (!navigator.onLine || !writeAvailable()) { q('composerFeedback').className = 'composer-status warning'; q('composerFeedback').textContent = 'Mac 离线，草稿已保留且没有发送'; return; }
  const model = state.selectedModel && (action === 'continue' || state.mode === 'safe') ? state.selectedModel : '';
  if (action === 'steer') void submitAction('steer', {expected_turn_ref: detail.active_turn_ref, input, mode: state.mode, ...(model ? {model} : {})});
  else void submitAction('continue', {input, ...(model ? {model} : {})});
};
q('composerInput').oninput = () => { if (state.selectedThread) state.drafts[state.selectedThread] = q('composerInput').value; resizeComposer(); };
q('composerInput').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); q('composer').requestSubmit(); } };
q('conversationView').onscroll = () => { state.following = isNearConversationBottom(); if (state.following) q('newReplyButton').classList.add('hidden'); };
q('newReplyButton').onclick = followLatestReply;
window.addEventListener('online', () => { setConnection('网络已恢复 · 正在同步', 'good'); void refreshOverview(); if (state.selectedThread) startEventStream(); });
window.addEventListener('offline', () => { stopEventStream(); setConnection('手机网络离线', 'bad'); renderRunnerBanner(); renderNewTaskState(); });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { void refreshOverview(); if (state.selectedThread) startEventStream(); } });
window.addEventListener('beforeunload', stopEventStream);

void refreshOverview({preserveDetail: false});
setInterval(() => { if (document.visibilityState === 'visible') void refreshOverview(); }, 8000);
"""


__all__ = ["DESKTOP_DASHBOARD_HTML", "DESKTOP_DASHBOARD_JS"]
