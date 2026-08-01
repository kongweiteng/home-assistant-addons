"""Ingress-only bounded JSON API and responsive multi-account dashboard."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import unquote, urlparse

from .runtime import WaterService


def create_server(host: str, port: int, service: WaterService) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), create_handler(service))


def create_handler(service: WaterService):
    class HuaxinRequestHandler(BaseHTTPRequestHandler):
        server_version = "HuaxinWater/0.3"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            try:
                if path in {"", "/"}:
                    self._send_html(DASHBOARD_HTML)
                    return
                if path == "/health":
                    self._send_json(200, service.health())
                    return
                if path == "/api/v1/accounts":
                    self._send_json(200, service.accounts_snapshot())
                    return
                prefix = "/api/v1/accounts/"
                if path.startswith(prefix) and "/" not in path[len(prefix) :]:
                    account_id = unquote(path[len(prefix) :])
                    snapshot = service.account_snapshot(account_id)
                    if snapshot is None:
                        self._send_json(404, {"error": "not_found"})
                    else:
                        self._send_json(200, snapshot)
                    return
                self._send_json(404, {"error": "not_found"})
            except Exception:
                self._send_json(500, {"error": "internal_error"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            prefix = "/api/v1/accounts/"
            suffix = "/refresh"
            if self.headers.get("X-Huaxin-Action") != "refresh":
                self._send_json(403, {"error": "action_header_required"})
                return
            if not path.startswith(prefix) or not path.endswith(suffix):
                self._send_json(404, {"error": "not_found"})
                return
            account_id = unquote(path[len(prefix) : -len(suffix)]).strip("/")
            if not account_id or "/" in account_id:
                self._send_json(404, {"error": "not_found"})
                return
            accepted, reason, retry_after = service.request_refresh(account_id)
            if accepted:
                self._send_json(202, {"status": "accepted"})
            elif reason == "not_found":
                self._send_json(404, {"error": reason})
            else:
                self._send_json(
                    429,
                    {"error": reason, "retry_after_seconds": retry_after},
                )

        def log_message(self, format: str, *args) -> None:
            return

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'self'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, body: dict) -> None:
            payload = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(payload)

    return HuaxinRequestHandler


DASHBOARD_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="icon" href="data:,">
  <title>华新水务</title>
  <style>
    :root{color-scheme:light dark;--bg:#f4f8fb;--panel:#fff;--text:#183044;--muted:#6c8090;--line:#dbe6ed;--accent:#087e8b;--accent2:#0aa6b5;--good:#18864b;--warn:#b26a00;--bad:#bd2c3a;--shadow:0 8px 28px rgba(29,72,93,.08)}
    @media(prefers-color-scheme:dark){:root{--bg:#0d171e;--panel:#14232c;--text:#e7f1f5;--muted:#9ab0ba;--line:#29404b;--accent:#41c8d2;--accent2:#59d8e1;--good:#5cdb91;--warn:#ffc267;--bad:#ff7e88;--shadow:none}}
    *{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,var(--bg),color-mix(in srgb,var(--bg) 82%,var(--accent) 18%));color:var(--text);font:14px/1.5 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;min-height:100vh}
    header{padding:22px clamp(16px,4vw,42px) 16px;display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap}h1{margin:0;font-size:23px;letter-spacing:.02em}.sub{color:var(--muted);font-size:12px;margin-top:3px}
    .controls{display:flex;gap:9px;align-items:center}select,button{font:inherit;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--text);padding:9px 12px}button{cursor:pointer;background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}button:disabled{opacity:.55;cursor:wait}
    main{padding:0 clamp(16px,4vw,42px) 42px;max-width:1280px;margin:auto}.notice{display:none;border:1px solid color-mix(in srgb,var(--warn) 55%,var(--line));background:color-mix(in srgb,var(--warn) 10%,var(--panel));padding:10px 13px;border-radius:10px;margin-bottom:14px;color:var(--warn)}
    .identity{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-radius:16px;padding:19px 21px;box-shadow:var(--shadow);display:flex;gap:20px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}.identity h2{margin:0 0 3px;font-size:21px}.address{font-size:15px;max-width:760px}.pill{display:inline-flex;padding:4px 9px;border-radius:999px;background:rgba(255,255,255,.18);font-size:12px}
    .cards{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:12px;margin:14px 0}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:13px;box-shadow:var(--shadow)}.card{padding:14px}.label{color:var(--muted);font-size:12px}.metric{font-size:23px;font-weight:700;margin-top:3px}.meta{font-size:12px;color:var(--muted);margin-top:5px}
    .tabs{display:flex;gap:6px;overflow:auto;padding:4px 0 10px}.tab{background:transparent;color:var(--muted);border-color:transparent;white-space:nowrap}.tab.active{color:var(--accent);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--panel))}.panel{padding:16px;min-height:220px;overflow:auto}h3{margin:0 0 12px;font-size:16px}
    table{border-collapse:collapse;width:100%;min-width:620px}th,td{text-align:left;padding:10px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:600}.empty{padding:32px 12px;text-align:center;color:var(--muted)}.kv{display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:10px}.kv>div{padding:12px;border:1px solid var(--line);border-radius:10px}.endpoint{display:grid;grid-template-columns:minmax(130px,1fr) auto;gap:8px;padding:10px 0;border-bottom:1px solid var(--line)}.status-good,.status-ok,.status-empty{color:var(--good)}.status-degraded,.status-stale,.status-cached{color:var(--warn)}.status-unavailable,.status-error,.status-auth_required{color:var(--bad)}
    .stats-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.stats-head h3{margin:0}.quality{padding:10px 12px;border-radius:10px;margin:13px 0;background:color-mix(in srgb,var(--warn) 9%,var(--panel));border:1px solid color-mix(in srgb,var(--warn) 45%,var(--line));color:var(--warn)}.stats-cards{grid-template-columns:repeat(5,minmax(150px,1fr));margin:14px 0 20px}.chart-section{margin:18px 0 23px}.chart-title{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:10px}.chart-title h4{margin:0;font-size:14px}.legend{display:flex;gap:13px;color:var(--muted);font-size:12px}.legend span{display:inline-flex;gap:5px;align-items:center}.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;background:var(--accent)}.legend .paid{background:#6d62c9}.month-chart{display:grid;grid-template-columns:repeat(12,minmax(54px,1fr));gap:7px;min-width:720px;border-bottom:1px solid var(--line);padding:6px 2px 0}.month{display:grid;grid-template-rows:auto 118px auto;gap:5px;min-width:0;text-align:center}.month-value{font-size:11px;white-space:nowrap;color:var(--muted)}.bar-space{height:118px;display:flex;align-items:flex-end;justify-content:center;gap:4px}.bar{width:min(22px,42%);min-height:2px;border-radius:5px 5px 0 0;background:linear-gradient(var(--accent2),var(--accent));position:relative}.bar.paid{background:linear-gradient(#9b91e5,#6358bd)}.bar.missing{height:0!important;min-height:0;border-top:1px dashed var(--line);width:70%;border-radius:0;background:none;align-self:flex-end}.bar.zero{opacity:.45}.month-label{font-size:12px;color:var(--muted);padding-bottom:7px}.year-table{margin-top:8px}.trend-positive{color:var(--bad)}.trend-negative{color:var(--good)}
    @media(max-width:900px){.stats-cards{grid-template-columns:repeat(3,1fr)}}
    @media(max-width:760px){.cards{grid-template-columns:1fr 1fr}.stats-cards{grid-template-columns:1fr 1fr}.kv{grid-template-columns:1fr}.identity{border-radius:12px}.controls{width:100%}.controls select{flex:1}}
  </style>
</head>
<body>
<header>
  <div><h1>华新水务</h1><div class="sub" id="pageStatus">正在读取账户…</div></div>
  <div class="controls"><select id="accountSelect" aria-label="选择户号"></select><button id="refreshButton" type="button">刷新当前户号</button></div>
</header>
<main>
  <div class="notice" id="notice"></div>
  <section class="identity">
    <div><h2 id="customerName">—</h2><div class="address" id="customerAddress">用水地址：—</div></div>
    <div><span class="pill" id="accountState">启动中</span><div class="sub" id="customerNumber" style="color:#e7fbff;margin-top:7px">—</div></div>
  </section>
  <section class="cards">
    <div class="card"><div class="label">余额</div><div class="metric" id="remaining">—</div></div>
    <div class="card"><div class="label">欠费</div><div class="metric" id="arrears">—</div></div>
    <div class="card"><div class="label">水表数量</div><div class="metric" id="meterCount">—</div></div>
    <div class="card"><div class="label">最近读数</div><div class="metric" id="latestReading">—</div></div>
  </section>
  <nav class="tabs" id="tabs">
    <button class="tab active" data-tab="overview">概览</button><button class="tab" data-tab="statistics">年月统计</button><button class="tab" data-tab="meters">水表</button><button class="tab" data-tab="water">用水记录</button><button class="tab" data-tab="payments">缴费记录</button><button class="tab" data-tab="steps">阶梯用水</button><button class="tab" data-tab="diagnostics">诊断</button>
  </nav>
  <section class="panel" id="content"><div class="empty">正在加载…</div></section>
</main>
<script>
const api=(path,options={})=>fetch(path,{credentials:'same-origin',...options}).then(async response=>{const body=await response.json();if(!response.ok){const error=new Error(body.error||'request_failed');error.body=body;throw error}return body});
const endpointNames={customer_info:'个人与水表',water_records:'用水记录',payment_records:'缴费记录',steps:'阶梯用水',payment_summary:'余额与欠费'};
const statusNames={starting:'启动中',good:'正常',degraded:'部分数据异常',unavailable:'不可用',auth_required:'需要重新认证',ok:'正常',empty:'暂无记录',stale:'显示缓存',cached:'缓存待刷新',error:'失败'};
let accounts=[],currentId=null,current=null,currentTab='overview',selectedYear=null;
const $=selector=>document.querySelector(selector);const value=(item,suffix='')=>item===null||item===undefined||item===''?'—':`${item}${suffix}`;
const localTime=item=>item?new Date(item).toLocaleString():'—';
const number=(item,digits=2)=>item===null||item===undefined?'—':new Intl.NumberFormat('zh-CN',{minimumFractionDigits:0,maximumFractionDigits:digits}).format(item);
const usage=item=>item===null||item===undefined?'—':`${number(item,3)} m³`;
const money=item=>item===null||item===undefined?'—':`¥${number(item,2)}`;
function node(tag,text,className){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item}
function setText(selector,text){$(selector).textContent=text}
async function start(){try{const listing=await api('api/v1/accounts');accounts=listing.accounts;renderAccountOptions();if(accounts.length)await selectAccount(accounts[0].id);else showError('尚未配置户号');}catch(error){showError('读取失败，请查看 Add-on 日志')}}
function renderAccountOptions(){const select=$('#accountSelect');select.textContent='';for(const account of accounts){const option=node('option',`${account.id} · ${account.masked_customer_no}`);option.value=account.id;select.append(option)}select.addEventListener('change',()=>selectAccount(select.value))}
async function selectAccount(id){currentId=id;selectedYear=null;$('#accountSelect').value=id;$('#content').replaceChildren(node('div','正在加载…','empty'));try{current=await api(`api/v1/accounts/${encodeURIComponent(id)}`);renderHeader();renderTab();}catch(error){showError('当前户号读取失败')}}
function renderHeader(){const summary=current.summary||{};setText('#pageStatus',`最后成功：${localTime(current.last_success_at)}`);setText('#customerName',value(summary.name));setText('#customerAddress',`用水地址：${value(summary.address)}`);setText('#customerNumber',current.masked_customer_no||'—');setText('#accountState',statusNames[current.status]||current.status);$('#accountState').className=`pill status-${current.status}`;setText('#remaining',value(summary.remaining,' 元'));setText('#arrears',value(summary.arrears,' 元'));setText('#meterCount',value(summary.meter_count));setText('#latestReading',value(summary.latest_reading));$('#refreshButton').disabled=Boolean(current.refreshing);const notice=$('#notice');if(current.status==='degraded'||current.status==='unavailable'||current.status==='auth_required'){notice.style.display='block';notice.textContent=current.status==='auth_required'?'上游开始要求认证，自动轮询已暂停。':'部分数据不可用；页面会明确标记并保留最后成功缓存。'}else{notice.style.display='none'}}
$('#tabs').addEventListener('click',event=>{const button=event.target.closest('[data-tab]');if(!button)return;currentTab=button.dataset.tab;document.querySelectorAll('.tab').forEach(item=>item.classList.toggle('active',item===button));renderTab()});
$('#refreshButton').addEventListener('click',async()=>{if(!currentId)return;const button=$('#refreshButton');button.disabled=true;button.textContent='刷新中…';try{await api(`api/v1/accounts/${encodeURIComponent(currentId)}/refresh`,{method:'POST',headers:{'X-Huaxin-Action':'refresh'}});await waitForRefresh();}catch(error){const seconds=error.body&&error.body.retry_after_seconds;showNotice(seconds?`刷新过于频繁，请在 ${seconds} 秒后重试。`:'刷新请求失败。')}finally{button.textContent='刷新当前户号';button.disabled=false}});
async function waitForRefresh(){for(let i=0;i<60;i++){await new Promise(resolve=>setTimeout(resolve,1000));current=await api(`api/v1/accounts/${encodeURIComponent(currentId)}`);renderHeader();renderTab();if(!current.refreshing)return}showNotice('刷新仍在进行，请稍后查看。')}
function showNotice(message){const notice=$('#notice');notice.style.display='block';notice.textContent=message}
function showError(message){setText('#pageStatus',message);$('#content').replaceChildren(node('div',message,'empty'))}
function endpoint(name){return current&&current.endpoints&&current.endpoints[name]||{status:'error',data:null,error:{kind:'not_loaded'}}}
function renderTab(){if(!current)return;({overview:renderOverview,statistics:renderStatistics,meters:renderMeters,water:renderWater,payments:renderPayments,steps:renderSteps,diagnostics:renderDiagnostics}[currentTab]||renderOverview)()}
function renderOverview(){const box=$('#content');box.textContent='';box.append(node('h3','账户概览'));const info=endpoint('customer_info').data||{};const water=info.water||{};const grid=node('div',undefined,'kv');for(const [label,item,suffix] of [['总用水量',water.total_use,''],['家庭人口',water.population,' 人'],['当前阶梯',water.step_name||water.step,''],['用水性质',water.use_kind_type,''],['最新刷新',current.last_refresh_at?localTime(current.last_refresh_at):null,''],['数据状态',statusNames[current.status]||current.status,'']]){const card=node('div');card.append(node('div',label,'label'),node('div',value(item,suffix),'metric'));grid.append(card)}box.append(grid)}
function renderStatistics(){const box=$('#content');box.textContent='';const stats=current.statistics||{};const years=Array.isArray(stats.years)?stats.years:[];if(!years.length){box.append(node('h3','年月统计'),node('div','当前用水和缴费记录中没有可识别的年月。','empty'));appendQuality(box,stats);return}if(!years.includes(selectedYear))selectedYear=stats.latest_year||years[0];const head=node('div',undefined,'stats-head');head.append(node('h3','年月统计'));const select=node('select');select.setAttribute('aria-label','选择统计年份');for(const year of years){const option=node('option',`${year} 年`);option.value=String(year);option.selected=year===selectedYear;select.append(option)}select.addEventListener('change',()=>{selectedYear=Number(select.value);renderStatistics()});head.append(select);box.append(head);appendQuality(box,stats);const summary=(stats.yearly||[]).find(item=>item.year===selectedYear)||{};const cards=node('div',undefined,'cards stats-cards');for(const [label,item,formatter,meta] of [['年度用水量',summary.usage,usage,`${summary.months_with_usage||0} 个有效月份`],['应收水费',summary.charge,money,'按计费月份'],['实缴金额',summary.payments,money,'按实际缴费月份'],['月均用量',summary.average_monthly_usage,usage,'仅计算有有效用量的月份'],['同比用量',summary.usage_year_over_year_percent,percent,`较 ${selectedYear-1} 年`]]){const card=node('div',undefined,'card');const metric=node('div',formatter(item),'metric');if(label==='同比用量'&&item>0)metric.classList.add('trend-positive');if(label==='同比用量'&&item<0)metric.classList.add('trend-negative');card.append(node('div',label,'label'),metric,node('div',meta,'meta'));cards.append(card)}box.append(cards);const months=stats.monthly_by_year&&stats.monthly_by_year[String(selectedYear)]||[];appendChart(box,'月度用水量',months,[{key:'usage',className:'',format:usage}], '各月按多块水表累加，缺失为 —');appendChart(box,'月度应收与实缴',months,[{key:'charge',className:'',format:money,label:'应收'},{key:'payments',className:'paid',format:money,label:'实缴'}], '金额独立缩放，不与用水量共用比例');appendYearTable(box,stats.yearly||[])}
function appendQuality(box,stats){const waterState=endpoint('water_records').status;const paymentState=endpoint('payment_records').status;const messages=[];if(!['ok','empty'].includes(waterState))messages.push(`用水记录：${statusNames[waterState]||waterState}`);if(!['ok','empty'].includes(paymentState))messages.push(`缴费记录：${statusNames[paymentState]||paymentState}`);if(stats.unparsed_water_records)messages.push(`${stats.unparsed_water_records} 条用水记录年月无法解析`);if(stats.unparsed_payment_records)messages.push(`${stats.unparsed_payment_records} 条缴费记录年月无法解析`);if(!messages.length)return;const quality=node('div',undefined,'quality');quality.append(node('b','数据质量提示：'),document.createTextNode(messages.join('；')));box.append(quality)}
function appendChart(box,title,months,series,description){const section=node('section',undefined,'chart-section');const titleRow=node('div',undefined,'chart-title');titleRow.append(node('h4',title),node('div',description,'meta'));section.append(titleRow);if(series.length>1){const legend=node('div',undefined,'legend');for(const item of series){const entry=node('span');entry.append(node('i',undefined,item.className),document.createTextNode(item.label));legend.append(entry)}section.append(legend)}const values=series.flatMap(item=>months.map(month=>month[item.key]).filter(item=>item!==null&&item!==undefined));const maximum=Math.max(...values,0);const chart=node('div',undefined,'month-chart');for(let index=0;index<12;index++){const month=months[index]||{month:index+1};const item=node('div',undefined,'month');const labels=series.map(entry=>entry.format(month[entry.key])).join(' / ');item.append(node('div',labels,'month-value'));const space=node('div',undefined,'bar-space');for(const entry of series){const amount=month[entry.key];const bar=node('div',undefined,`bar ${entry.className||''}`.trim());if(amount===null||amount===undefined){bar.classList.add('missing')}else{bar.style.height=`${maximum>0?Math.max(amount/maximum*100,2):2}%`;if(amount===0)bar.classList.add('zero')}bar.title=`${month.month} 月 ${entry.label||title}：${entry.format(amount)}`;space.append(bar)}item.append(space,node('div',`${month.month||index+1}月`,'month-label'));chart.append(item)}section.append(chart);box.append(section)}
function appendYearTable(box,years){box.append(node('h4','按年汇总'));const table=node('table',undefined,'year-table');const head=node('thead'),headRow=node('tr');for(const label of ['年份','用水量','应收水费','实缴金额','月均用量','有效用量月份','同比'])headRow.append(node('th',label));head.append(headRow);const body=node('tbody');for(const year of years){const row=node('tr');for(const item of [year.year,usage(year.usage),money(year.charge),money(year.payments),usage(year.average_monthly_usage),`${year.months_with_usage}/12`,percent(year.usage_year_over_year_percent)])row.append(node('td',String(item)));body.append(row)}table.append(head,body);box.append(table)}
function percent(item){if(item===null||item===undefined)return '—';return `${item>0?'+':''}${number(item,1)}%`}
function renderMeters(){const data=endpoint('customer_info').data||{};renderTable('水表信息',data.meters||[],[['登记号','registration_no'],['安装位置','location'],['最近抄表时间','latest_reading_date'],['最近读数','latest_reading']])}
function renderWater(){renderTable('用水记录',endpoint('water_records').data||[],[['计费月份','billing_month'],['抄表时间','reading_time'],['用水量','usage'],['应收金额','charge'],['水表位置','meter_location'],['登记号','registration_no']])}
function renderPayments(){renderTable('缴费记录',endpoint('payment_records').data||[],[['缴费时间','payment_time'],['金额','amount'],['缴费方式','payment_mode']])}
function renderSteps(){renderTable('阶梯用水',endpoint('steps').data||[],[['阶梯','name'],['起始值','start'],['结束值','end'],['已使用','used'],['容量','capacity']])}
function renderTable(title,items,columns){const box=$('#content');box.textContent='';box.append(node('h3',title));if(!items.length){box.append(node('div','暂无记录','empty'));return}const table=node('table');const head=node('thead'),headRow=node('tr');for(const [label] of columns)headRow.append(node('th',label));head.append(headRow);const body=node('tbody');for(const item of items){const row=node('tr');for(const [,key] of columns)row.append(node('td',value(item[key])));body.append(row)}table.append(head,body);box.append(table)}
function renderDiagnostics(){const box=$('#content');box.textContent='';box.append(node('h3','接口诊断'));for(const [name,label] of Object.entries(endpointNames)){const data=endpoint(name);const row=node('div',undefined,'endpoint');const left=node('div');left.append(node('b',label),node('div',`最后成功：${localTime(data.last_success_at)}`,'meta'));const right=node('div',statusNames[data.status]||data.status,`status-${data.status}`);if(data.error&&data.error.kind)right.title=data.error.kind;row.append(left,right);box.append(row)}}
start();
</script>
</body>
</html>'''
