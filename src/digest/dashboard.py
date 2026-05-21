"""HTML timeseries dashboard — quant KPI lines + high-signal event overlays.

Writes a self-contained HTML file using Plotly.js (CDN) to:
  <vault>/80 Digest/assets/dashboard.html

X axis: days.
Left Y: FRED / CFTC z-scores as line traces.
Right Y (secondary): signal score for high-signal event triangle markers.
Below: Yahoo Finance price % change from window start.
Bottom: sortable top-events table.

Trigger: `digest dashboard` CLI command.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digest import db

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

FRED_LABELS: dict[str, str] = {
    "T10Y2Y":       "2Y-10Y Spread",
    "DGS10":        "10Y Treasury",
    "NFCI":         "Fin. Conditions",
    "T10YIE":       "10Y Breakeven",
    "RRPONTSYD":    "Overnight RRP",
    "ICSA":         "Initial Claims",
    "CPIAUCSL":     "CPI",
    "T10Y3M":       "10Y-3M Spread",
    "SOFR":         "SOFR",
    "PCEPILFE":     "Core PCE",
    "M2SL":         "M2",
    "INDPRO":       "Indust. Production",
    "DEXCHUS":      "CNY/USD",
    "CPILFESL":     "Core CPI",
    "BAMLH0A0HYM2": "HY Spread",
}

FRED_COLORS: list[str] = [
    "#60a5fa", "#34d399", "#f9a8d4", "#fcd34d",
    "#a78bfa", "#fb923c", "#22d3ee", "#f87171",
    "#86efac", "#c4b5fd", "#fdba74", "#67e8f9",
    "#38bdf8", "#4ade80", "#e879f9", "#facc15",
]

TOPIC_COLORS: dict[str, str] = {
    "fed_markets":      "#f59e0b",
    "china":            "#ef4444",
    "ai_thinkers":      "#10b981",
    "ai_capex":         "#3b82f6",
    "ai_semis":         "#8b5cf6",
    "ai_business_apps": "#06b6d4",
    "data_viz":         "#22c55e",
    "other":            "#6b7280",
}

TOPIC_LABELS: dict[str, str] = {
    "fed_markets":      "Fed & Markets",
    "china":            "China",
    "ai_thinkers":      "AI Thinkers",
    "ai_capex":         "AI Capex",
    "ai_semis":         "AI Semis",
    "ai_business_apps": "AI Apps",
    "data_viz":         "Data Viz",
    "other":            "Other",
}

SOURCE_LABELS: dict[str, str] = {
    "fred": "FRED", "cboe": "CBOE", "cftc": "CFTC",
    "yahoo": "Yahoo", "insider": "Insider", "ftd": "FTD",
    "edgar": "EDGAR", "reddit": "Reddit", "hn": "HN",
    "rss": "RSS", "substack": "Substack", "arxiv": "arXiv",
    "gmail": "Gmail", "clipped": "Clipped", "huggingface": "HF",
}


# ── Data loading ────────────────────────────────────────────────────────

def _load_fred() -> dict[str, list[dict]]:
    sql = """
        SELECT date(ingested_at) AS day,
               json_extract(metadata_json, '$.series_id') AS sid,
               MAX(CAST(json_extract(metadata_json, '$.z_score') AS REAL)) AS z
        FROM items
        WHERE source = 'fred'
          AND triage_decision = 'keep'
          AND json_extract(metadata_json, '$.z_score') IS NOT NULL
        GROUP BY day, sid
        ORDER BY day ASC
    """
    result: dict[str, list[dict]] = defaultdict(list)
    with db.get_conn() as conn:
        for row in conn.execute(sql).fetchall():
            if row["sid"]:
                result[row["sid"]].append({"date": row["day"], "z": round(float(row["z"]), 4)})
    return dict(result)


def _load_cftc() -> dict[str, list[dict]]:
    sql = """
        SELECT date(ingested_at) AS day,
               json_extract(metadata_json, '$.contract') AS contract,
               MAX(CAST(json_extract(metadata_json, '$.z_score') AS REAL)) AS z
        FROM items
        WHERE source = 'cftc'
          AND triage_decision = 'keep'
          AND json_extract(metadata_json, '$.z_score') IS NOT NULL
        GROUP BY day, contract
        ORDER BY day ASC
    """
    result: dict[str, list[dict]] = defaultdict(list)
    with db.get_conn() as conn:
        for row in conn.execute(sql).fetchall():
            if row["contract"]:
                result[row["contract"]].append({"date": row["day"], "z": round(float(row["z"]), 4)})
    return dict(result)


def _load_yahoo() -> dict[str, list[dict]]:
    sql = """
        WITH ranked AS (
            SELECT date(ingested_at) AS day,
                   json_extract(metadata_json, '$.ticker') AS ticker,
                   CAST(json_extract(metadata_json, '$.price') AS REAL) AS price,
                   ROW_NUMBER() OVER (
                       PARTITION BY date(ingested_at), json_extract(metadata_json, '$.ticker')
                       ORDER BY ingested_at DESC
                   ) AS rn
            FROM items
            WHERE source = 'yahoo'
              AND triage_decision = 'keep'
              AND json_extract(metadata_json, '$.price') IS NOT NULL
        )
        SELECT day, ticker, price FROM ranked WHERE rn = 1 ORDER BY day ASC
    """
    raw: dict[str, list[dict]] = defaultdict(list)
    with db.get_conn() as conn:
        for row in conn.execute(sql).fetchall():
            if row["ticker"]:
                raw[row["ticker"]].append({"date": row["day"], "price": float(row["price"])})
    result: dict[str, list[dict]] = {}
    for ticker, readings in raw.items():
        if not readings:
            continue
        base = readings[0]["price"]
        if base == 0:
            continue
        result[ticker] = [
            {"date": r["date"], "price": round(r["price"], 2),
             "pct": round((r["price"] / base - 1) * 100, 3)}
            for r in readings
        ]
    return result


def _load_events(min_score: float = 0.55) -> list[dict]:
    sql = """
        SELECT source, url, title, topic,
               CAST(triage_score AS REAL) AS score,
               date(ingested_at) AS day
        FROM items
        WHERE triage_decision = 'keep'
          AND triage_score >= ?
        ORDER BY triage_score DESC
        LIMIT 300
    """
    events: list[dict] = []
    with db.get_conn() as conn:
        for row in conn.execute(sql, (min_score,)).fetchall():
            title = (row["title"] or "").strip()
            if len(title) > 100:
                title = title[:97] + "..."
            events.append({
                "date":   row["day"] or "",
                "score":  round(float(row["score"]), 4),
                "title":  title,
                "topic":  row["topic"] or "other",
                "source": row["source"] or "",
                "url":    row["url"] or "",
            })
    return events


def _build_payload() -> dict:
    fred   = _load_fred()
    cftc   = _load_cftc()
    yahoo  = _load_yahoo()
    events = _load_events()

    all_dates = (
        [p["date"] for pts in fred.values() for p in pts]
        + [p["date"] for pts in yahoo.values() for p in pts]
        + [e["date"] for e in events if e["date"]]
    )
    date_range = {
        "start": min(all_dates) if all_dates else "",
        "end":   max(all_dates) if all_dates else "",
    }

    regime_label = ""
    try:
        row = db.get_latest_regime()
        if row:
            regime_label = row["regime"]
    except Exception:
        pass

    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "date_range":    date_range,
        "regime":        regime_label,
        "fred_series":   fred,
        "fred_labels":   FRED_LABELS,
        "cftc_series":   cftc,
        "yahoo_series":  yahoo,
        "events":        events,
        "topic_colors":  TOPIC_COLORS,
        "topic_labels":  TOPIC_LABELS,
        "fred_colors":   FRED_COLORS,
        "source_labels": SOURCE_LABELS,
    }


def _safe_json(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("</", "<\\/")


# ── HTML template ───────────────────────────────────────────────────────
# Plain string — not an f-string. __DASH_DATA__ is replaced at render time.

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Macro·AI Signal Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.34.0.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d1117;--surf:#161b22;--brd:#30363d;--txt:#e6edf3;--mut:#8b949e;--acc:#58a6ff}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',ui-monospace,monospace;font-size:13px;padding:16px}
header{margin-bottom:16px;border-bottom:1px solid var(--brd);padding-bottom:12px}
h1{font-size:18px;font-weight:600;color:var(--acc);letter-spacing:.03em}
.meta{color:var(--mut);font-size:11px;margin-top:4px}
.sr{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.st{background:var(--surf);border:1px solid var(--brd);border-radius:6px;padding:4px 10px;font-size:11px}
.sl{color:var(--mut)}.sv{font-weight:600}
.card{background:var(--surf);border:1px solid var(--brd);border-radius:8px;margin-bottom:12px;padding:8px}
.ct{font-size:11px;font-weight:600;color:var(--mut);padding:2px 4px 8px;text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:6px 10px;color:var(--mut);font-weight:600;border-bottom:1px solid var(--brd);cursor:pointer;user-select:none}
th:hover{color:var(--txt)}
td{padding:5px 10px;border-bottom:1px solid #21262d;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#21262d}
.bar{display:inline-block;height:4px;border-radius:2px;vertical-align:middle;margin-left:6px}
.bdg{display:inline-block;padding:1px 6px;border-radius:10px;font-size:10px;font-weight:600}
.src{font-size:10px;color:var(--mut);background:#21262d;border-radius:4px;padding:1px 5px}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
.togrow{display:flex;gap:5px;flex-wrap:wrap;padding:0 2px 8px}
.tog{background:transparent;border:1px solid;border-radius:12px;padding:2px 9px;font-size:11px;cursor:pointer;font-family:inherit;transition:opacity .15s}
.tog.off{opacity:.3}
</style>
</head>
<body>
<header>
  <h1>Macro·AI Signal Dashboard</h1>
  <div class="meta" id="meta"></div>
  <div class="sr" id="sr"></div>
</header>
<div class="card">
  <div class="ct">FRED &amp; CFTC Z-Score &mdash; Signal Events (&#x25CF; right axis)</div>
  <div id="kpi-toggles" class="togrow"></div>
  <div id="c1" style="height:420px"></div>
</div>
<div class="card">
  <div class="ct">Yahoo Finance &mdash; Price % from Window Start</div>
  <div id="yahoo-toggles" class="togrow"></div>
  <div id="c2" style="height:260px"></div>
</div>
<div class="card">
  <div class="ct">Top Signal Events &mdash; click headers to sort</div>
  <div style="max-height:380px;overflow-y:auto">
    <table>
      <thead><tr>
        <th onclick="srt(0)">Score&#x2193;</th>
        <th onclick="srt(1)">Date</th>
        <th onclick="srt(2)">Topic</th>
        <th onclick="srt(3)">Source</th>
        <th onclick="srt(4)">Title</th>
      </tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
</div>
<script>
var D=__DASH_DATA__;
var TC=D.topic_colors,TL=D.topic_labels,FC=D.fred_colors,SL=D.source_labels;

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

var BG={paper_bgcolor:'#161b22',plot_bgcolor:'#0d1117',
  font:{color:'#e6edf3',size:11},
  hoverlabel:{bgcolor:'#21262d',bordercolor:'#30363d',font:{color:'#e6edf3',size:11}}};

// ── KPI chart: FRED + CFTC z-scores + event markers on right Y ─────────
var traces=[],fredIdx={},cftcIdx={};

function makeTog(el,lbl,col,idx,chartId){
  var b=document.createElement('button');
  b.className='tog';b.textContent=lbl;
  b.style.color=col;b.style.borderColor=col;
  b.onclick=function(){
    var isOff=b.classList.contains('off');
    Plotly.restyle(chartId,{visible:isOff?true:'legendonly'},[idx]);
    b.classList.toggle('off',!isOff);
  };
  el.appendChild(b);
}

Object.keys(D.fred_series).forEach(function(sid,i){
  var pts=D.fred_series[sid];
  fredIdx[sid]=traces.length;
  traces.push({
    x:pts.map(function(p){return p.date;}),
    y:pts.map(function(p){return p.z;}),
    type:'scatter',mode:'lines+markers',
    name:D.fred_labels[sid]||sid,showlegend:false,
    line:{color:FC[i%FC.length],width:2},marker:{size:5},
    xaxis:'x',yaxis:'y',
    hovertemplate:'<b>%{meta}</b> %{y:+.2f}σ<extra></extra>',
    meta:D.fred_labels[sid]||sid
  });
});

Object.keys(D.cftc_series).forEach(function(c){
  var pts=D.cftc_series[c];
  cftcIdx[c]=traces.length;
  traces.push({
    x:pts.map(function(p){return p.date;}),
    y:pts.map(function(p){return p.z;}),
    type:'scatter',mode:'lines+markers',
    name:'CFTC '+c,showlegend:false,
    line:{color:'#fb923c',width:2,dash:'dot'},marker:{size:5,symbol:'square'},
    xaxis:'x',yaxis:'y',
    hovertemplate:'CFTC %{meta} %{y:+.2f}σ<extra></extra>',
    meta:c
  });
});

var byTopic={};
D.events.forEach(function(ev){
  var t=ev.topic||'other';
  if(!byTopic[t])byTopic[t]=[];
  byTopic[t].push(ev);
});
Object.keys(byTopic).forEach(function(topic){
  var evs=byTopic[topic],col=TC[topic]||'#6b7280',lbl=TL[topic]||topic;
  traces.push({
    x:evs.map(function(e){return e.date;}),
    y:evs.map(function(e){return e.score;}),
    type:'scatter',mode:'markers',
    name:lbl,
    marker:{symbol:'circle',size:12,color:col,opacity:.7,
      line:{color:'#0d1117',width:1}},
    text:evs.map(function(e){return e.title+' ('+( SL[e.source]||e.source)+')';}),
    hovertemplate:'<b>%{text}</b><br>Score: %{y:.3f}<br>%{x}<extra>'+lbl+'</extra>',
    xaxis:'x',yaxis:'y2'
  });
});

var kL=Object.assign({
  height:420,margin:{l:50,r:65,t:10,b:30},
  xaxis:{type:'date',gridcolor:'#21262d',linecolor:'#30363d',
    tickfont:{color:'#8b949e'},showspikes:true,spikecolor:'#58a6ff',spikethickness:1},
  yaxis:{title:{text:'Z-Score',font:{color:'#8b949e',size:10}},
    gridcolor:'#21262d',linecolor:'#30363d',zerolinecolor:'#30363d',
    tickfont:{color:'#8b949e'},tickformat:'+.1f'},
  yaxis2:{title:{text:'Signal Score',font:{color:'#8b949e',size:10}},
    overlaying:'y',side:'right',range:[0,1.3],showgrid:false,
    tickfont:{color:'#8b949e'},tickformat:'.2f',linecolor:'#30363d'},
  shapes:[
    {type:'line',x0:0,x1:1,xref:'paper',y0:0,y1:0,yref:'y',
      line:{color:'#30363d',width:1,dash:'dash'}},
    {type:'line',x0:0,x1:1,xref:'paper',y0:2,y1:2,yref:'y',
      line:{color:'#f85149',width:1,dash:'dot',opacity:.4}},
    {type:'line',x0:0,x1:1,xref:'paper',y0:-2,y1:-2,yref:'y',
      line:{color:'#3fb950',width:1,dash:'dot',opacity:.4}}
  ],
  legend:{x:0,y:1.02,orientation:'h',font:{size:10,color:'#8b949e'},bgcolor:'transparent'},
  hovermode:'x unified'
},BG);

Plotly.newPlot('c1',traces,kL,{responsive:true,displayModeBar:false});

// KPI toggle buttons
var kt=document.getElementById('kpi-toggles');
Object.keys(fredIdx).forEach(function(sid,i){
  makeTog(kt,D.fred_labels[sid]||sid,FC[i%FC.length],fredIdx[sid],'c1');
});
Object.keys(cftcIdx).forEach(function(c){
  makeTog(kt,'CFTC '+c,'#fb923c',cftcIdx[c],'c1');
});

// ── Yahoo chart ────────────────────────────────────────────────────────
var yT=[],yahooIdx={};
var yC=['#8b5cf6','#60a5fa','#34d399','#f9a8d4','#fcd34d','#fb923c','#22d3ee','#f87171'];
Object.keys(D.yahoo_series).forEach(function(ticker,i){
  var pts=D.yahoo_series[ticker];
  yahooIdx[ticker]=i;
  yT.push({
    x:pts.map(function(p){return p.date;}),
    y:pts.map(function(p){return p.pct;}),
    type:'scatter',mode:'lines+markers',
    name:ticker,showlegend:false,
    line:{color:yC[i%yC.length],width:2},marker:{size:5},
    hovertemplate:ticker+' %{y:+.2f}%  ($%{text})<extra></extra>',
    text:pts.map(function(p){return p.price.toFixed(2);})
  });
});

var yL=Object.assign({
  height:260,margin:{l:65,r:20,t:10,b:40},
  xaxis:{type:'date',gridcolor:'#21262d',linecolor:'#30363d',
    tickfont:{color:'#8b949e'},showspikes:true,spikecolor:'#58a6ff',spikethickness:1},
  yaxis:{title:{text:'% from Start',font:{color:'#8b949e',size:10}},
    gridcolor:'#21262d',linecolor:'#30363d',zerolinecolor:'#30363d',
    tickfont:{color:'#8b949e'},tickformat:'+.1f',ticksuffix:'%'},
  shapes:[{type:'line',x0:0,x1:1,xref:'paper',y0:0,y1:0,yref:'y',
    line:{color:'#30363d',width:1,dash:'dash'}}],
  showlegend:false,
  hovermode:'x unified'
},BG);

Plotly.newPlot('c2',yT,yL,{responsive:true,displayModeBar:false});

// Yahoo toggle buttons
var yt=document.getElementById('yahoo-toggles');
Object.keys(yahooIdx).forEach(function(ticker,i){
  makeTog(yt,ticker,yC[i%yC.length],yahooIdx[ticker],'c2');
});

// ── Sync x-axis zoom between charts ────────────────────────────────────
function relayX(fromId,toId){
  document.getElementById(fromId).on('plotly_relayout',function(ed){
    var r={};
    if(ed['xaxis.range[0]']!==undefined){
      r['xaxis.range[0]']=ed['xaxis.range[0]'];
      r['xaxis.range[1]']=ed['xaxis.range[1]'];
    }else if(ed['xaxis.autorange']){r['xaxis.autorange']=true;}
    if(Object.keys(r).length)Plotly.relayout(document.getElementById(toId),r);
  });
}
relayX('c1','c2');
relayX('c2','c1');

// ── Header stats ───────────────────────────────────────────────────────
document.getElementById('meta').textContent=
  D.date_range.start+' — '+D.date_range.end+
  '  ·  Generated '+new Date(D.generated_at).toLocaleString();

var sr=document.getElementById('sr');
function addStat(lblHtml,val){
  var d=document.createElement('div');
  d.className='st';
  d.innerHTML='<span class="sl">'+lblHtml+'</span> <span class="sv">'+esc(String(val))+'</span>';
  sr.appendChild(d);
}
addStat('Events',D.events.length);
addStat('FRED series',Object.keys(D.fred_series).length);
addStat('Tickers',Object.keys(D.yahoo_series).length);
if(D.regime)addStat('Regime',D.regime);

var tc={};
D.events.forEach(function(e){var t=e.topic||'other';tc[t]=(tc[t]||0)+1;});
Object.entries(tc).sort(function(a,b){return b[1]-a[1];}).slice(0,5).forEach(function(pair){
  var t=pair[0],n=pair[1],col=TC[t]||'#6b7280';
  addStat('<span style="color:'+col+'">'+(TL[t]||esc(t))+'</span>',n);
});

// ── Events table ───────────────────────────────────────────────────────
var rows=D.events.slice(0,50),sc=0,sa=false;

function render(){
  var tb=document.getElementById('tb');
  tb.innerHTML='';
  rows.forEach(function(ev){
    var col=TC[ev.topic]||'#6b7280',lbl=TL[ev.topic]||ev.topic,src=SL[ev.source]||ev.source;
    var titleHtml=ev.url
      ?'<a href="'+esc(ev.url)+'" target="_blank" rel="noopener">'+esc(ev.title)+'</a>'
      :esc(ev.title);
    var tr=document.createElement('tr');
    tr.innerHTML=
      '<td><b>'+ev.score.toFixed(3)+'</b>'+
        '<span class="bar" style="width:'+Math.round(ev.score*48)+'px;background:'+col+'"></span></td>'+
      '<td style="color:#8b949e">'+esc(ev.date)+'</td>'+
      '<td><span class="bdg" style="background:'+col+'20;color:'+col+'">'+esc(lbl)+'</span></td>'+
      '<td><span class="src">'+esc(src)+'</span></td>'+
      '<td>'+titleHtml+'</td>';
    tb.appendChild(tr);
  });
}

function srt(col){
  var keys=['score','date','topic','source','title'];
  var k=keys[col];
  if(sc===col){sa=!sa;}else{sc=col;sa=col!==0;}
  rows=D.events.slice(0,50).slice().sort(function(a,b){
    var av=a[k]!==undefined?a[k]:'',bv=b[k]!==undefined?b[k]:'';
    if(typeof av==='number'&&typeof bv==='number')return sa?av-bv:bv-av;
    return sa?String(av).localeCompare(String(bv)):String(bv).localeCompare(String(av));
  });
  render();
}

render();
</script>
</body>
</html>"""


# ── Public entry point ─────────────────────────────────────────────────

def generate_dashboard() -> dict[str, Any]:
    """Build and write the signal dashboard HTML to the Obsidian vault.

    Returns dict: {path, events, fred_series, yahoo_series}.
    """
    from digest.obsidian import Paths

    payload   = _build_payload()
    data_json = _safe_json(payload)
    html      = _HTML.replace("__DASH_DATA__", data_json)

    paths = Paths.resolve()
    paths.ensure()
    assets_dir = paths.digest_root / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / "dashboard.html"
    target.write_text(html, encoding="utf-8")

    n_events = len(payload["events"])
    n_fred   = len(payload["fred_series"])
    n_yahoo  = len(payload["yahoo_series"])
    logger.info(
        "dashboard: wrote %s (events=%d fred=%d yahoo=%d)",
        target.name, n_events, n_fred, n_yahoo,
    )
    return {
        "path":         str(target),
        "events":       n_events,
        "fred_series":  n_fred,
        "yahoo_series": n_yahoo,
    }
