"""Task-first navigation and compact management views for the Controller."""

import re
from .ui_icons import icon


_NAV = '''<a href="desktop/">任务</a><a data-view-link="tools" href="?view=tools#tools">工具</a><a class="new-task-link" href="desktop/?new=1" aria-label="新建任务">新建</a><a data-view-link="runners" href="?view=runners#runners">状态</a><a data-view-link="overview" href="?view=overview#overview">设置</a>'''
for _label, _name in [('任务', 'ChatsCircle'), ('工具', 'Wrench'), ('新建', 'Plus'), ('状态', 'Gauge'), ('设置', 'GearSix')]:
    _NAV = _NAV.replace('>' + _label + '</a>', '>' + icon(_name) + _label + '</a>')

_STYLE = """
.side-rail{display:none!important}.page{max-width:1320px;margin:0 auto;padding:28px 32px 80px}.workspace-nav{position:sticky;top:0;z-index:60;display:flex;align-items:center;gap:30px;min-height:64px;padding:8px 32px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.96);backdrop-filter:blur(16px)}.workspace-brand{font-weight:700;letter-spacing:-.04em;font-size:20px;white-space:nowrap}.workspace-nav nav{display:flex;align-items:center;gap:6px;flex:1}.workspace-nav a{display:flex;align-items:center;justify-content:center;min-height:44px;padding:8px 18px;border-radius:10px;color:var(--muted);text-decoration:none}.workspace-nav a.active,.mobile-nav a.active{background:#eeeeeb;color:var(--text);font-weight:650}.workspace-nav .new-task-link{order:9;margin-left:auto;background:#222326;color:#fff}.section.app-view{margin-top:0}.topbar,.view-heading{margin-bottom:24px}.view-heading h1,.topbar h1{font-size:28px}.eyebrow{margin-bottom:7px}.tool-directory{display:grid;gap:12px}.tool-group-heading{margin:18px 0 0;font-size:14px;color:var(--muted)}.tool-card{background:#fff;border:1px solid var(--line);border-radius:14px;overflow:hidden}.tool-card summary{display:flex;align-items:center;gap:14px;min-height:80px;padding:16px 20px;cursor:pointer;list-style:none}.tool-card summary::-webkit-details-marker{display:none}.tool-card summary:after{content:'⌄';margin-left:5px;color:var(--muted)}.tool-card[open] summary:after{transform:rotate(180deg)}.tool-card .tool-heading{min-width:0;flex:1}.tool-card .technical{overflow-wrap:anywhere}.tool-card .tool-description{border-top:1px solid var(--line);padding:16px 20px;display:grid;gap:14px}.tool-card .tool-description p{margin:0;overflow-wrap:anywhere}.tool-card .tool-description .toggle{justify-self:start}.directory-footer{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 0}.tool-search{flex:1;min-width:220px}.tool-search input{width:100%}.toolbar .secondary{min-height:40px;font-size:13px}.management-disclosure{margin:18px 0;border:1px solid var(--line);border-radius:14px;background:#fff;padding:0 16px}.management-disclosure>summary{min-height:52px;display:flex;align-items:center;cursor:pointer;font-weight:600;gap:10px}.management-disclosure>summary:before{content:'＋';font-size:18px;color:var(--muted)}.management-disclosure[open]>summary:before{content:'−'}.management-disclosure>div{padding-bottom:16px}.management-disclosure .card{box-shadow:none}.notice{font-size:13px}.runner-actions{max-width:280px}.tool-empty{padding:32px;text-align:center}.stream-state{max-width:100%;font-variant-numeric:tabular-nums}button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #abc4f6;outline-offset:3px}.tool-summary-count{color:var(--muted);font-size:13px}.mobile-nav{display:none}
@media(max-width:980px){.workspace-nav{padding:8px 18px;gap:20px}.workspace-nav a{padding:8px 12px}.page{padding:24px 20px 96px}.mobile-nav{display:none}}
@media(max-width:700px){.workspace-nav{min-height:60px;padding:10px 16px;justify-content:space-between;gap:10px}.workspace-nav nav{display:none}.workspace-brand{font-size:19px}.page{padding:20px 14px 108px;min-width:0}.mobile-nav{position:fixed;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));left:0;right:0;bottom:0;height:auto;min-height:70px;padding:6px 5px calc(6px + env(safe-area-inset-bottom));border-radius:0;border:0;border-top:1px solid var(--line);box-shadow:none;background:rgba(255,255,255,.97)}.mobile-nav a{min-height:52px;font-size:13px;padding:6px 2px}.mobile-nav .new-task-link{background:#222326;color:#fff;margin:0 4px}.topbar,.view-heading{min-height:0;align-items:start;margin-bottom:20px;padding:0}.topbar h1,.view-heading h1{font-size:25px}.view-heading p,.topbar p{display:block;line-height:1.6;margin-top:6px}.top-actions{display:none}.tool-search{grid-column:1/-1;min-width:0}.tool-card summary{padding:14px 12px;gap:8px;min-height:78px}.tool-card .tool-name{font-size:14px}.tool-card .tool-description{padding:14px 12px}.tool-card .technical{font-size:11px}.toolbar{gap:10px}.toolbar label{min-width:0}.toolbar select,.toolbar input{min-width:0;max-width:100%;font-size:16px}.tool-card .badge{white-space:nowrap}.management-disclosure{padding:0 12px}.grid{margin:0 0 18px;padding:0 0 2px;gap:8px}.grid .card{flex-basis:110px}.section-head h2{font-size:17px}.form-grid input{min-width:0;width:100%;font-size:16px}.directory-footer{font-size:12px}.directory-footer button{white-space:nowrap}.runner-actions{max-width:none}}
"""


def build_management_html(html: str) -> str:
    """Reshape the existing audited controls without duplicating their DOM IDs."""
    html = html.replace("</style>", _STYLE + ".workspace-nav a{gap:6px}.tool-card summary:after{content:'详情';font-size:11px}.tool-card[open] summary:after{content:'收起';transform:none}.management-disclosure>summary:before{content:none}@media(max-width:700px){.mobile-nav a{flex-direction:column;gap:3px;font-size:11px}}" + "</style>", 1)
    html = re.sub(r'<aside class="side-rail">.*?</aside>',
                  '<header class="workspace-nav"><span class="workspace-brand">Codex</span><nav aria-label="应用导航">'
                  + _NAV + '</nav><span id="statusStreamState" role="status" class="stream-state warn">实时连接中</span></header>', html, count=1)
    html = html.replace('<span id="statusStreamState" class="stream-state warn">实时连接中</span>', '')
    html = html.replace('<span class="stream-state good">状态自动更新</span>', '')
    html = re.sub(r'<nav class="mobile-nav".*?</nav>', '<nav class="mobile-nav" aria-label="移动端导航">' + _NAV + '</nav>', html, count=1)
    html = html.replace('控制器总览</div><h1>Codex 控制器</h1><p>跨 Mac 任务、工具与运行节点的远程工作空间',
                        '偏好与账户</div><h1>设置</h1><p>管理 Controller 独立会话、认证与服务状态')
    html = html.replace('<h2>正式认证</h2>', '<h2>Controller 账户</h2>')
    html = html.replace('<h1>Runner</h1><p>管理远程执行器、注册与恢复状态', '<h1>连接与设备</h1><p>查看在线设备，处理需要恢复的连接')
    html = html.replace('<div class="toolbar"><label>服务 ', '<div class="toolbar"><label class="tool-search">搜索工具<input id="toolSearch" type="search" placeholder="工具名称、用途或服务" autocomplete="off"></label><label>服务 ', 1)
    html = html.replace('<button id="reloadTools">刷新工具状态</button>', '<button id="reloadTools" class="secondary">重新同步</button>')
    html = html.replace('<button id="reloadRunners">刷新 Runner</button>', '<button id="reloadRunners" class="secondary">重新同步</button>')
    html = re.sub(r'<div class="table-wrap"><table><thead>.*?</thead><tbody id="toolRows"></tbody></table></div>',
                  '<div id="toolRows" class="tool-directory"></div><div class="directory-footer"><span id="toolCount" role="status" class="tool-summary-count"></span><button id="loadMoreTools" class="secondary" hidden>加载更多工具</button></div>', html, count=1)
    html = html.replace('<div class="section-head"><div><h2>新增 Runner</h2>',
                        '<details id="runnerSetup" class="management-disclosure"><summary>添加运行设备</summary><div><div class="section-head"><div><h2>新增 Runner</h2>', 1)
    html = html.replace('<div id="runnerSecret"', '</div></details><div id="runnerSecret"', 1)
    # Keep protocol explanations available without displacing the working directory.
    html = re.sub(r'(<section class="section app-view" id="tools".*?</div>)(<div class="card notice">.*?</div>)',
                  r'\1<details class="management-disclosure"><summary>工具状态说明</summary><div>\2</div></details>', html, count=1)
    return html
