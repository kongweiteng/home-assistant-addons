"""Authenticated Ingress dashboard and bounded read-only diagnostics API."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


def create_server(host: str, port: int, runtime_state) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ESLinkGas/0.1"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._json(200, _health(runtime_state.snapshot()))
            elif path == "/api/v1/status":
                self._json(200, runtime_state.snapshot())
            elif path in {"/", "/index.html"}:
                self._html(200, DASHBOARD_HTML)
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            self._json(405, {"error": "read_only"})

        def log_message(self, format: str, *args) -> None:
            return

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, body_text: str) -> None:
            body = body_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)


def _health(snapshot: dict) -> dict:
    accounts = snapshot.get("accounts") if isinstance(snapshot, dict) else {}
    accounts = accounts if isinstance(accounts, dict) else {}
    return {
        "ok": True,
        "account_count": len(accounts),
        "available_count": sum(
            1 for value in accounts.values() if value.get("available") is True
        ),
        "auth_required_count": sum(
            1 for value in accounts.values() if value.get("status") == "auth_required"
        ),
        "generated_at": snapshot.get("generated_at") if isinstance(snapshot, dict) else None,
    }


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>燃气账户</title>
  <style>
    :root{color-scheme:light dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    body{margin:0;background:#f4f6f8;color:#1f2937}main{max-width:980px;margin:auto;padding:20px}
    h1{margin:0 0 6px;font-size:26px}.note{color:#6b7280;margin:0 0 18px}.grid{display:grid;gap:16px}
    .card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 3px 16px #00000012}
    .row{display:flex;justify-content:space-between;gap:16px;padding:7px 0;border-bottom:1px solid #eef0f2}
    .row:last-child{border:0}.label{color:#6b7280}.value{text-align:right;overflow-wrap:anywhere}
    .balance{font-size:30px;color:#ef6c00;font-weight:700}.status{font-weight:650}.bad{color:#c62828}.good{color:#2e7d32}
    table{width:100%;border-collapse:collapse;margin-top:10px}th,td{text-align:left;padding:8px;border-bottom:1px solid #eef0f2;font-size:14px}
    @media(prefers-color-scheme:dark){body{background:#111827;color:#e5e7eb}.card{background:#1f2937}.row,th,td{border-color:#374151}.note,.label{color:#9ca3af}}
  </style>
</head>
<body><main><h1>燃气账户</h1><p class="note">非官方只读接入；不会执行充值、缴费、绑定或其他写操作。</p><div id="accounts" class="grid"></div></main>
<script>
const text=(tag,value,cls)=>{const node=document.createElement(tag);node.textContent=value??"--";if(cls)node.className=cls;return node};
const row=(label,value,cls)=>{const node=document.createElement("div");node.className="row";node.append(text("span",label,"label"),text("span",value,"value "+(cls||"")));return node};
function renderAccount(account){
  const card=document.createElement("section");card.className="card";
  card.append(text("h2",account.account_id||"账户"));
  card.append(row("数据状态",account.status,account.available?"good":"bad"));
  card.append(row("余额",account.balance==null?"未知":account.balance+" CNY","balance"));
  card.append(row("户号",account.user_no_masked));
  card.append(row("姓名",account.customer_name));
  card.append(row("地址",account.customer_address));
  card.append(row("手机号",account.customer_mobile));
  card.append(row("客户类型",account.customer_class));
  card.append(row("地址状态",account.address_status));
  card.append(row("最近成功",account.last_success_at));
  if(account.last_error)card.append(row("最近错误",account.last_error,"bad"));
  const meters=Array.isArray(account.meters)?account.meters:[];
  if(meters.length){
    const table=document.createElement("table");
    const head=document.createElement("tr");["表号","余额","状态","类型","价格类别","购气指令"].forEach(v=>head.append(text("th",v)));table.append(head);
    meters.forEach(m=>{const tr=document.createElement("tr");[m.meter_no_masked,m.balance,m.meter_status,m.meter_type,m.price_name,m.purchase_command_status].forEach(v=>tr.append(text("td",v)));table.append(tr)});card.append(table);
  }
  return card;
}
async function load(){const root=document.getElementById("accounts");try{const response=await fetch("api/v1/status",{cache:"no-store"});const data=await response.json();root.replaceChildren(...Object.values(data.accounts||{}).map(renderAccount));if(!root.children.length)root.append(text("p","等待首次采集…"));}catch(error){root.replaceChildren(text("p","状态读取失败","bad"));}}
load();setInterval(load,60000);
</script></body></html>"""
