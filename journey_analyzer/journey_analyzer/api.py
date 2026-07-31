"""Authenticated-by-Ingress read-only HTTP API and AMap journey UI."""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .models import Journey, TrackPoint
from .runtime import RuntimeState
from .statistics import build_statistics
from .storage import JourneyStore


JOURNEY_ID_PATTERN = re.compile(r"^jrny_v1_[0-9a-f]{20}$")


def create_server(
    host: str,
    port: int,
    db_path: str,
    config: AppConfig,
    runtime_state: RuntimeState,
) -> ThreadingHTTPServer:
    handler = create_handler(db_path, config, runtime_state)
    return ThreadingHTTPServer((host, port), handler)


def create_handler(db_path: str, config: AppConfig, runtime_state: RuntimeState):
    class JourneyRequestHandler(BaseHTTPRequestHandler):
        server_version = "JourneyAnalyzer/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path in {"/", ""}:
                    self._send_html(render_dashboard(config))
                    return
                if parsed.path == "/health":
                    with JourneyStore(db_path) as store:
                        schema_version = store.schema_version()
                    self._send_json(
                        200,
                        {
                            "service": "journey_analyzer",
                            "version": "0.1.0",
                            "schema_version": schema_version,
                            **runtime_state.snapshot(),
                        },
                    )
                    return
                if parsed.path == "/api/v1/journeys":
                    self._list_journeys(parse_qs(parsed.query))
                    return
                if parsed.path.startswith("/api/v1/journeys/"):
                    self._get_journey(parsed.path.rsplit("/", 1)[-1])
                    return
                if parsed.path == "/api/v1/stats":
                    self._get_stats(parse_qs(parsed.query))
                    return
                self._send_json(404, {"error": "not_found"})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
            except Exception:
                self._send_json(500, {"error": "internal_error"})

        def log_message(self, format: str, *args) -> None:
            return

        def _list_journeys(self, query: dict[str, list[str]]) -> None:
            entity_id = _optional_entity(query, config)
            start = _optional_datetime(query, "start")
            end = _optional_datetime(query, "end")
            limit = _bounded_query_int(query, "limit", 50, 1, 100)
            offset = _bounded_query_int(query, "offset", 0, 0, 10_000)
            with JourneyStore(db_path) as store:
                journeys = store.list_journeys(
                    entity_id=entity_id,
                    start=start,
                    end=end,
                    limit=limit,
                    offset=offset,
                )
            visible = [
                journey_summary(item)
                for item in journeys
                if item.entity_id in config.entity_ids
            ]
            self._send_json(
                200,
                {
                    "items": visible,
                    "limit": limit,
                    "offset": offset,
                    "next_offset": None if len(visible) < limit else offset + limit,
                },
            )

        def _get_journey(self, journey_id: str) -> None:
            if not JOURNEY_ID_PATTERN.fullmatch(journey_id):
                raise ValueError("invalid_journey_id")
            with JourneyStore(db_path) as store:
                journey = store.get_journey(journey_id, point_limit=2000)
            if journey is None or journey.entity_id not in config.entity_ids:
                self._send_json(404, {"error": "not_found"})
                return
            self._send_json(200, journey_detail(journey))

        def _get_stats(self, query: dict[str, list[str]]) -> None:
            entity_id = _optional_entity(query, config)
            period = _bounded_query_int(query, "period", 30, 1, 30)
            if period not in {1, 7, 30}:
                raise ValueError("period must be 1, 7 or 30")
            selected = config.entity_ids if entity_id is None else (entity_id,)
            with JourneyStore(db_path) as store:
                snapshot = build_statistics(
                    store,
                    selected,
                    timezone_name=config.timezone,
                    stale_after_s=config.stale_after_s,
                )
            distance_key = {
                1: "today_distance_km",
                7: "distance_7d_km",
                30: "distance_30d_km",
            }[period]
            self._send_json(
                200,
                {
                    **snapshot,
                    "period_days": period,
                    "period_distance_km": snapshot[distance_key],
                },
            )

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
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
            self.end_headers()
            self.wfile.write(payload)

    return JourneyRequestHandler


def journey_summary(journey: Journey) -> dict:
    return {
        "journey_id": journey.journey_id,
        "entity_id": journey.entity_id,
        "started_at": _iso(journey.started_at),
        "ended_at": _iso(journey.ended_at),
        "distance_km": round(journey.distance_m / 1000.0, 3),
        "duration_min": round(journey.duration_s / 60.0, 1),
        "point_count": journey.point_count,
        "average_speed_kmh": round(journey.average_speed_mps * 3.6, 1),
        "quality": journey.quality,
        "algorithm_version": journey.algorithm_version,
    }


def journey_detail(journey: Journey) -> dict:
    return {
        **journey_summary(journey),
        "points_truncated": len(journey.points) < journey.point_count,
        "points": [point_json(point) for point in journey.points],
    }


def point_json(point: TrackPoint) -> dict:
    return {
        "observed_at": _iso(point.observed_at),
        "latitude": point.latitude,
        "longitude": point.longitude,
        "accuracy_m": point.accuracy_m,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_entity(query: dict[str, list[str]], config: AppConfig) -> str | None:
    values = query.get("entity_id")
    if not values or not values[0]:
        return None
    entity_id = values[0]
    if entity_id not in config.entity_ids:
        raise ValueError("entity_id is not configured")
    return entity_id


def _optional_datetime(query: dict[str, list[str]], name: str) -> datetime | None:
    values = query.get(name)
    if not values or not values[0]:
        return None
    try:
        value = datetime.fromisoformat(values[0].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(timezone.utc)


def _bounded_query_int(
    query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(query.get(name, [str(default)])[0])
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise ValueError(f"{name} is outside its allowed range")
    return value


def render_dashboard(config: AppConfig) -> str:
    amap_config = json.dumps(
        {
            "key": config.amap_web_key,
            "securityCode": config.amap_security_code,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return DASHBOARD_HTML.replace("__AMAP_CONFIG__", amap_config)


DASHBOARD_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Journey Analyzer</title>
  <style>
    :root{color-scheme:dark;--bg:#0b1220;--panel:#121c2e;--muted:#91a4bd;--line:#24334a;--accent:#42a5f5;--good:#42d392}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf4ff;font:14px/1.45 system-ui,-apple-system,sans-serif}
    header{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:16px 18px;border-bottom:1px solid var(--line)}
    h1{font-size:18px;margin:0}.status{color:var(--muted)}main{display:grid;grid-template-columns:minmax(280px,380px) 1fr;min-height:calc(100vh - 62px)}
    aside{padding:14px;border-right:1px solid var(--line);overflow:auto}.cards{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}
    .card,.journey,.detail{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px}.card b{display:block;font-size:19px;margin-top:4px}.label{color:var(--muted);font-size:12px}
    .journey{width:100%;color:inherit;text-align:left;margin:0 0 8px;cursor:pointer}.journey:hover,.journey.active{border-color:var(--accent)}
    .row{display:flex;justify-content:space-between;gap:8px}.small{color:var(--muted);font-size:12px}.mapwrap{position:relative;min-height:500px}#map{position:absolute;inset:0}
    .empty{display:grid;place-items:center;height:100%;padding:30px;text-align:center;color:var(--muted)}.controls{position:absolute;z-index:5;right:12px;top:12px;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
    button.action{border:1px solid var(--line);background:var(--panel);color:#fff;border-radius:7px;padding:7px 10px;cursor:pointer}.detail{position:absolute;z-index:5;left:12px;bottom:12px;max-width:min(560px,calc(100% - 24px));display:none}
    @media(max-width:800px){main{grid-template-columns:1fr;grid-template-rows:auto 58vh}aside{border-right:0;border-bottom:1px solid var(--line);max-height:46vh}.mapwrap{min-height:58vh}}
  </style>
</head>
<body>
<header><h1>Journey Analyzer</h1><div id="status" class="status">正在读取本地统计…</div></header>
<main>
  <aside>
    <div class="cards">
      <div class="card"><span class="label">今日行程</span><b id="tripCount">—</b></div>
      <div class="card"><span class="label">今日里程</span><b id="todayDistance">—</b></div>
      <div class="card"><span class="label">近 7 日</span><b id="weekDistance">—</b></div>
      <div class="card"><span class="label">定位质量</span><b id="quality">—</b></div>
    </div>
    <div id="journeys"><div class="empty">尚无已识别行程。定位恢复后会自动采集。</div></div>
  </aside>
  <section class="mapwrap">
    <div id="map"><div class="empty">高德地图尚未配置，行程统计仍可正常使用。</div></div>
    <div class="controls">
      <button class="action" id="standard">标准图</button>
      <button class="action" id="satellite">卫星图</button>
      <button class="action" id="traffic">实时路况</button>
      <button class="action" id="play">播放轨迹</button>
    </div>
    <div class="detail" id="detail"></div>
  </section>
</main>
<script>
const amapConfig=__AMAP_CONFIG__;
let map,polyline,marker,trafficLayer,satelliteLayer,roadNetLayer,currentDetail,trafficVisible=false;
const api=(path)=>fetch(path,{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error('request_failed');return r.json()});
const value=(v,suffix='')=>v===null||v===undefined?'—':`${v}${suffix}`;
async function loadData(){
  try{
    const [stats,list]=await Promise.all([api('api/v1/stats?period=30'),api('api/v1/journeys?limit=100')]);
    document.querySelector('#status').textContent=stats.status==='no_data'?'服务正常，等待位置数据':`服务状态：${stats.status}`;
    document.querySelector('#tripCount').textContent=value(stats.today_trip_count);
    document.querySelector('#todayDistance').textContent=value(stats.today_distance_km,' km');
    document.querySelector('#weekDistance').textContent=value(stats.distance_7d_km,' km');
    document.querySelector('#quality').textContent=stats.status;
    renderJourneys(list.items);
  }catch(e){document.querySelector('#status').textContent='读取失败，请查看 Add-on 日志';}
}
function renderJourneys(items){
  const box=document.querySelector('#journeys');box.textContent='';
  if(!items.length){box.innerHTML='<div class="empty">尚无已识别行程。定位恢复后会自动采集。</div>';return;}
  for(const item of items){
    const button=document.createElement('button');button.className='journey';
    const title=document.createElement('div');title.className='row';title.innerHTML=`<b>${item.distance_km} km</b><span>${item.duration_min} min</span>`;
    const meta=document.createElement('div');meta.className='small';meta.textContent=`${new Date(item.started_at).toLocaleString()} · ${item.entity_id} · ${item.quality}`;
    button.append(title,meta);button.addEventListener('click',()=>selectJourney(item.journey_id,button));box.append(button);
  }
}
async function selectJourney(id,button){
  document.querySelectorAll('.journey').forEach(x=>x.classList.remove('active'));button.classList.add('active');
  currentDetail=await api(`api/v1/journeys/${encodeURIComponent(id)}`);showDetail(currentDetail);drawJourney(currentDetail);
}
function showDetail(j){
  const box=document.querySelector('#detail');box.style.display='block';box.textContent='';
  const title=document.createElement('b');title.textContent=`${j.distance_km} km · ${j.duration_min} min`;
  const meta=document.createElement('div');meta.className='small';meta.textContent=`${j.entity_id} | ${new Date(j.started_at).toLocaleString()} → ${new Date(j.ended_at).toLocaleString()} | ${j.point_count} 点 | ${j.quality}`;
  box.append(title,meta);
}
function loadAmap(){
  if(!amapConfig.key)return;
  if(amapConfig.securityCode)window._AMapSecurityConfig={securityJsCode:amapConfig.securityCode};
  const script=document.createElement('script');
  script.src=`https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(amapConfig.key)}&plugin=AMap.MoveAnimation,AMap.Scale,AMap.ToolBar,AMap.TileLayer.Traffic`;
  script.onload=()=>{
    document.querySelector('#map').textContent='';map=new AMap.Map('map',{zoom:11,viewMode:'2D'});map.addControl(new AMap.Scale());map.addControl(new AMap.ToolBar());
    trafficLayer=new AMap.TileLayer.Traffic({zIndex:10});satelliteLayer=new AMap.TileLayer.Satellite();roadNetLayer=new AMap.TileLayer.RoadNet();
  };
  script.onerror=()=>{document.querySelector('#map').innerHTML='<div class="empty">高德地图加载失败；本地统计和 API 不受影响。</div>';};
  document.head.append(script);
}
function drawJourney(j){
  if(!map||!j.points.length)return;
  if(polyline)map.remove(polyline);if(marker)map.remove(marker);
  const path=j.points.map(p=>wgs84ToGcj02(p.longitude,p.latitude));
  polyline=new AMap.Polyline({path,strokeColor:'#42a5f5',strokeWeight:7,strokeOpacity:.9,lineJoin:'round'});
  marker=new AMap.Marker({position:path[0],anchor:'center',icon:new AMap.Icon({size:new AMap.Size(18,18),imageSize:new AMap.Size(18,18),image:'https://webapi.amap.com/theme/v1.3/markers/n/mark_b.png'})});
  map.add([polyline,marker]);map.setFitView([polyline],false,[60,60,60,60]);
}
function outOfChina(lng,lat){return lng<72.004||lng>137.8347||lat<0.8293||lat>55.8271}
function transformLat(x,y){let r=-100+2*x+3*y+.2*y*y+.1*x*y+.2*Math.sqrt(Math.abs(x));r+=(20*Math.sin(6*x*Math.PI)+20*Math.sin(2*x*Math.PI))*2/3;r+=(20*Math.sin(y*Math.PI)+40*Math.sin(y/3*Math.PI))*2/3;r+=(160*Math.sin(y/12*Math.PI)+320*Math.sin(y*Math.PI/30))*2/3;return r}
function transformLng(x,y){let r=300+x+2*y+.1*x*x+.1*x*y+.1*Math.sqrt(Math.abs(x));r+=(20*Math.sin(6*x*Math.PI)+20*Math.sin(2*x*Math.PI))*2/3;r+=(20*Math.sin(x*Math.PI)+40*Math.sin(x/3*Math.PI))*2/3;r+=(150*Math.sin(x/12*Math.PI)+300*Math.sin(x/30*Math.PI))*2/3;return r}
function wgs84ToGcj02(lng,lat){if(outOfChina(lng,lat))return[lng,lat];const a=6378245,ee=.00669342162296594323,dLat=transformLat(lng-105,lat-35),dLng=transformLng(lng-105,lat-35),rad=lat/180*Math.PI,magic=1-ee*Math.sin(rad)**2,sqrt=Math.sqrt(magic);return[lng+dLng*180/(a/sqrt*Math.cos(rad)*Math.PI),lat+dLat*180/(a*(1-ee)/(magic*sqrt)*Math.PI)]}
document.querySelector('#standard').onclick=()=>{if(map){satelliteLayer.setMap(null);roadNetLayer.setMap(null)}};
document.querySelector('#satellite').onclick=()=>{if(map){satelliteLayer.setMap(map);roadNetLayer.setMap(map)}};
document.querySelector('#traffic').onclick=()=>{if(map){trafficVisible=!trafficVisible;trafficLayer.setMap(trafficVisible?map:null)}};
document.querySelector('#play').onclick=()=>{if(marker&&currentDetail){const path=currentDetail.points.map(p=>wgs84ToGcj02(p.longitude,p.latitude));marker.stopMove();marker.setPosition(path[0]);marker.moveAlong(path,{duration:Math.min(Math.max(currentDetail.duration_min*1200,5000),120000),autoRotation:true})}};
loadAmap();loadData();setInterval(loadData,60000);
</script>
</body></html>'''
