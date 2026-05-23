"""Stock price tracker with digest signal overlays.

Identifies the top publicly traded companies mentioned across digest items
(via entities_json), fetches 90-day daily price history via yfinance, and
writes to <vault>/20 Projects/Investments/:

  stock-tracker.html  — self-contained Plotly.js interactive chart
  Stock Tracker.md    — companion summary table with links

Chart features:
  - One % return line per ticker, color-coded, toggle-able
  - Colored vertical dashed lines where high-signal digest events mention
    the ticker, with hover text showing the item title
  - Sortable summary table (price, 30d/90d %, mention count, event count)

Runs as stage 3h in the daily pipeline (after entities). Also available
standalone via: digest stocks
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

# ── Defaults ───────────────────────────────────────────────────────────

_DEFAULT_TICKERS = [
    "NVDA", "MSFT", "AAPL", "GOOGL", "META", "AMZN", "AMD", "TSLA",
    "AVGO", "TSM", "INTC", "QCOM", "JPM", "GS", "BAC", "BLK",
    "BX", "XOM", "CVX", "SPY", "QQQ", "PLTR", "SNOW", "CRM",
    "NOW", "CRWD", "NET", "PANW", "ORCL", "IBM",
]

_CHART_COLORS = [
    "#60a5fa", "#34d399", "#f9a8d4", "#fcd34d", "#a78bfa", "#fb923c",
    "#22d3ee", "#f87171", "#86efac", "#c4b5fd", "#fdba74", "#67e8f9",
    "#38bdf8", "#4ade80", "#e879f9", "#facc15", "#f97316", "#84cc16",
    "#06b6d4", "#8b5cf6", "#ec4899", "#10b981", "#f59e0b", "#3b82f6",
    "#ef4444", "#14b8a6", "#d946ef", "#a3e635", "#fb7185", "#818cf8",
]

_TOPIC_COLORS = {
    "fed_markets": "#f59e0b", "china": "#ef4444", "ai_thinkers": "#10b981",
    "ai_capex": "#3b82f6", "ai_semis": "#8b5cf6", "ai_business_apps": "#06b6d4",
    "data_viz": "#22c55e", "other": "#6b7280",
}


# ── Data queries ────────────────────────────────────────────────────────

def _get_top_tickers(limit: int = 50) -> list[str]:
    """Count entity mentions across kept items; return top tickers by frequency."""
    sql = """
        SELECT entities_json FROM items
        WHERE entities_json IS NOT NULL
          AND entities_json != '[]'
          AND triage_decision = 'keep'
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql).fetchall()

    counts: Counter = Counter()
    for row in rows:
        try:
            for e in json.loads(row["entities_json"] or "[]"):
                t = e.get("ticker")
                if t and e.get("type") in ("company", "index", "crypto"):
                    counts[t] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    top = [t for t, _ in counts.most_common(limit)]

    # Pad with defaults when entity data is sparse
    if len(top) < 10:
        for t in _DEFAULT_TICKERS:
            if t not in top:
                top.append(t)
        top = top[:limit]

    return top


def _get_mention_counts(tickers: list[str]) -> dict[str, int]:
    sql = """
        SELECT entities_json FROM items
        WHERE entities_json IS NOT NULL
          AND triage_decision = 'keep'
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql).fetchall()

    ticker_set = set(tickers)
    counts: Counter = Counter()
    for row in rows:
        try:
            for e in json.loads(row["entities_json"] or "[]"):
                t = e.get("ticker")
                if t and t in ticker_set:
                    counts[t] += 1
        except (json.JSONDecodeError, TypeError):
            continue
    return dict(counts)


def _get_signal_events(tickers: list[str], days: int = 90) -> list[dict]:
    """Return high-signal items that mention at least one of the tracked tickers."""
    sql = """
        SELECT id, title, url, topic, triage_score,
               date(ingested_at) AS day, entities_json
        FROM items
        WHERE triage_decision = 'keep'
          AND triage_score >= 0.65
          AND entities_json IS NOT NULL
          AND entities_json != '[]'
          AND ingested_at >= datetime('now', ?)
        ORDER BY triage_score DESC
        LIMIT 500
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, (f"-{days} days",)).fetchall()

    ticker_set = set(tickers)
    events: list[dict] = []
    for row in rows:
        try:
            entities = json.loads(row["entities_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        item_tickers = [
            e["ticker"] for e in entities
            if e.get("ticker") and e["ticker"] in ticker_set
        ]
        if not item_tickers:
            continue
        events.append({
            "date":    row["day"] or "",
            "title":   (row["title"] or "")[:100],
            "url":     row["url"] or "",
            "topic":   row["topic"] or "other",
            "score":   round(float(row["triage_score"]), 3),
            "tickers": item_tickers,
        })

    return sorted(events, key=lambda e: e["score"], reverse=True)


# ── Price fetching ──────────────────────────────────────────────────────

def _fetch_prices(tickers: list[str], days: int = 90) -> dict[str, dict]:
    """Batch-download daily close prices from yfinance."""
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        logger.warning("stock_tracker: yfinance or pandas not available")
        return {}

    period = "3mo" if days <= 90 else "6mo"
    try:
        raw = yf.download(
            tickers,
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        logger.warning("stock_tracker: yfinance download failed: %s", exc)
        return {}

    if raw is None or raw.empty:
        return {}

    # Normalise to a DataFrame of close prices keyed by ticker
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        elif "Close" in raw.columns:
            close = raw[["Close"]].rename(columns={"Close": tickers[0]})
        else:
            close = raw
    except Exception as exc:
        logger.warning("stock_tracker: column parse failed: %s", exc)
        return {}

    result: dict[str, dict] = {}
    for ticker in tickers:
        try:
            series = (
                close[ticker] if ticker in close.columns
                else (close.iloc[:, 0] if len(tickers) == 1 else None)
            )
            if series is None:
                continue
            series = series.dropna()
            if len(series) < 5:
                continue

            dates  = [d.strftime("%Y-%m-%d") for d in series.index]
            prices = [round(float(p), 2) for p in series.values]
            base   = prices[0]
            pcts   = [round((p / base - 1) * 100, 3) for p in prices]

            current    = prices[-1]
            change_30d = round((current / prices[max(0, len(prices) - 22)] - 1) * 100, 2)
            change_90d = round((current / prices[0] - 1) * 100, 2)

            result[ticker] = {
                "ticker":     ticker,
                "dates":      dates,
                "prices":     prices,
                "pcts":       pcts,
                "current":    current,
                "change_30d": change_30d,
                "change_90d": change_90d,
                "mentions":   0,
            }
        except Exception as exc:
            logger.debug("stock_tracker: skipped %s: %s", ticker, exc)

    return result


# ── HTML template ───────────────────────────────────────────────────────

def _safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Digest Stock Tracker</title>
<script src="https://cdn.plot.ly/plotly-2.34.0.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0d1117;--surf:#161b22;--brd:#30363d;--txt:#e6edf3;--mut:#8b949e;--acc:#58a6ff}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',ui-monospace,monospace;font-size:13px;padding:16px}
header{margin-bottom:16px;border-bottom:1px solid var(--brd);padding-bottom:12px}
h1{font-size:18px;font-weight:600;color:var(--acc)}
.meta{color:var(--mut);font-size:11px;margin-top:4px}
.card{background:var(--surf);border:1px solid var(--brd);border-radius:8px;margin-bottom:12px;padding:8px}
.ct{font-size:11px;font-weight:600;color:var(--mut);padding:2px 4px 8px;text-transform:uppercase;letter-spacing:.05em}
.togrow{display:flex;gap:5px;flex-wrap:wrap;padding:0 2px 8px}
.tog{background:transparent;border:1px solid;border-radius:12px;padding:2px 9px;font-size:11px;cursor:pointer;font-family:inherit;transition:opacity .15s}
.tog.off{opacity:.25}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:6px 10px;color:var(--mut);font-weight:600;border-bottom:1px solid var(--brd);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--txt)}
td{padding:5px 10px;border-bottom:1px solid #21262d;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#21262d}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.bdg{display:inline-block;padding:1px 6px;border-radius:10px;font-size:10px;font-weight:600;margin:1px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
</style>
</head>
<body>
<header>
  <h1>Digest Stock Tracker</h1>
  <div class="meta" id="meta"></div>
</header>
<div class="card">
  <div class="ct">Price % from 90-Day Start &mdash; Signal Events = &#x25CF; marker on ticker line</div>
  <div id="toggles" class="togrow"></div>
  <div id="chart" style="height:540px"></div>
</div>
<div class="card">
  <div class="ct">Portfolio Summary &mdash; click headers to sort</div>
  <div style="max-height:420px;overflow-y:auto">
    <table>
      <thead><tr>
        <th onclick="srt(0)">Ticker&#x2193;</th>
        <th onclick="srt(1)">Price</th>
        <th onclick="srt(2)">30d %</th>
        <th onclick="srt(3)">90d %</th>
        <th onclick="srt(4)">Mentions</th>
        <th onclick="srt(5)">Signals</th>
      </tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div>
</div>
<div class="card">
  <div class="ct">High-Signal Events &mdash; digest items referencing tracked tickers</div>
  <div style="max-height:360px;overflow-y:auto">
    <table>
      <thead><tr>
        <th>Date</th><th>Tickers</th><th>Score</th><th>Event</th>
      </tr></thead>
      <tbody id="evtb"></tbody>
    </table>
  </div>
</div>
<script>
var D=__TRACKER_DATA__;
var TC={"fed_markets":"#f59e0b","china":"#ef4444","ai_thinkers":"#10b981","ai_capex":"#3b82f6","ai_semis":"#8b5cf6","ai_business_apps":"#06b6d4","data_viz":"#22c55e","other":"#6b7280"};
var CC=["#60a5fa","#34d399","#f9a8d4","#fcd34d","#a78bfa","#fb923c","#22d3ee","#f87171","#86efac","#c4b5fd","#fdba74","#67e8f9","#38bdf8","#4ade80","#e879f9","#facc15","#f97316","#84cc16","#06b6d4","#8b5cf6","#ec4899","#10b981","#f59e0b","#3b82f6","#ef4444","#14b8a6","#d946ef","#a3e635","#fb7185","#818cf8"];

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function pctHtml(v){var cls=v>0?'up':v<0?'dn':'neu';return '<span class="'+cls+'">'+(v>0?'+':'')+v.toFixed(2)+'%</span>';}

document.getElementById('meta').textContent=
  D.tickers.length+' tickers  ·  '+D.events.length+' signal events  ·  Updated '+
  new Date(D.generated_at).toLocaleString();

var BG={paper_bgcolor:'#161b22',plot_bgcolor:'#0d1117',
  font:{color:'#e6edf3',size:11},
  hoverlabel:{bgcolor:'#21262d',bordercolor:'#30363d',font:{color:'#e6edf3',size:11}}};

// ── Signal event vertical shapes ───────────────────────────────────────
var shapes=[{type:'line',x0:0,x1:1,xref:'paper',y0:0,y1:0,yref:'y',
  line:{color:'#30363d',width:1,dash:'dash'}}];
var evByDate={};
D.events.forEach(function(ev){if(ev.date){(evByDate[ev.date]=evByDate[ev.date]||[]).push(ev);}});
Object.keys(evByDate).forEach(function(dt){
  var col=TC[evByDate[dt][0].topic]||'#6b7280';
  shapes.push({type:'line',x0:dt,x1:dt,y0:0,y1:1,yref:'paper',
    line:{color:col,width:1.5,dash:'dot'},opacity:0.5});
});

// ── Ticker traces + event marker traces ────────────────────────────────
var traces=[],tickerTraceIdx={};
D.tickers.forEach(function(td,i){
  var col=CC[i%CC.length];
  tickerTraceIdx[td.ticker]=traces.length;

  traces.push({
    x:td.dates,y:td.pcts,type:'scatter',mode:'lines',
    name:td.ticker,showlegend:false,
    line:{color:col,width:1.8},
    hovertemplate:'<b>'+esc(td.ticker)+'</b> %{y:+.2f}%  $%{text}<extra></extra>',
    text:td.prices.map(function(p){return p.toFixed(2);})
  });

  // Overlay event markers on the ticker's own price line
  var exDates=[],exPcts=[],exText=[];
  D.events.forEach(function(ev){
    if(ev.tickers.indexOf(td.ticker)<0)return;
    var di=td.dates.indexOf(ev.date);
    if(di<0)return;
    exDates.push(ev.date);
    exPcts.push(td.pcts[di]);
    exText.push(ev.title);
  });
  if(exDates.length){
    traces.push({
      x:exDates,y:exPcts,type:'scatter',mode:'markers',
      name:td.ticker+':ev',showlegend:false,
      marker:{symbol:'circle',size:9,color:col,
        line:{color:'#0d1117',width:1.5},opacity:0.9},
      text:exText,
      hovertemplate:'<b>%{text}</b><br>'+esc(td.ticker)+' %{y:+.2f}%  %{x}<extra></extra>'
    });
  }
});

var layout=Object.assign({
  height:540,margin:{l:58,r:20,t:8,b:42},
  xaxis:{type:'date',gridcolor:'#21262d',linecolor:'#30363d',
    tickfont:{color:'#8b949e'},showspikes:true,spikecolor:'#58a6ff',spikethickness:1},
  yaxis:{title:{text:'% from Start',font:{color:'#8b949e',size:10}},
    gridcolor:'#21262d',linecolor:'#30363d',zerolinecolor:'#30363d',
    tickfont:{color:'#8b949e'},tickformat:'+.1f',ticksuffix:'%'},
  shapes:shapes,hovermode:'closest',showlegend:false
},BG);

Plotly.newPlot('chart',traces,layout,{responsive:true,displayModeBar:false});

// ── Toggle pills ───────────────────────────────────────────────────────
var togEl=document.getElementById('toggles');
D.tickers.forEach(function(td,i){
  var col=CC[i%CC.length];
  var b=document.createElement('button');
  b.className='tog';b.textContent=td.ticker;
  b.style.color=col;b.style.borderColor=col;
  b.onclick=function(){
    var off=b.classList.contains('off');
    var base=tickerTraceIdx[td.ticker];
    var idxs=[base];
    if(base+1<traces.length&&traces[base+1].name===td.ticker+':ev')idxs.push(base+1);
    Plotly.restyle('chart',{visible:off?true:'legendonly'},idxs);
    b.classList.toggle('off',!off);
  };
  togEl.appendChild(b);
});

// ── Summary table ──────────────────────────────────────────────────────
var trows=D.tickers.map(function(td){
  return{ticker:td.ticker,current:td.current,c30:td.change_30d,c90:td.change_90d,
         mentions:td.mentions,events:D.events.filter(function(e){return e.tickers.indexOf(td.ticker)>=0;}).length};
});
var sc=0,sa=false;
function srt(col){
  var keys=['ticker','current','c30','c90','mentions','events'];
  var k=keys[col];
  if(sc===col){sa=!sa;}else{sc=col;sa=col===0;}
  trows=trows.slice().sort(function(a,b){
    var av=a[k],bv=b[k];
    if(typeof av==='number')return sa?av-bv:bv-av;
    return sa?String(av).localeCompare(String(bv)):String(bv).localeCompare(String(av));
  });
  renderTable();
}
function renderTable(){
  var tb=document.getElementById('tb');tb.innerHTML='';
  trows.forEach(function(r,i){
    var col=CC[D.tickers.findIndex(function(t){return t.ticker===r.ticker;})%CC.length];
    var tr=document.createElement('tr');
    tr.innerHTML=
      '<td><b style="color:'+col+'">'+esc(r.ticker)+'</b></td>'+
      '<td>$'+r.current.toFixed(2)+'</td>'+
      '<td>'+pctHtml(r.c30)+'</td>'+
      '<td>'+pctHtml(r.c90)+'</td>'+
      '<td>'+r.mentions+'</td>'+
      '<td>'+r.events+'</td>';
    tb.appendChild(tr);
  });
}
renderTable();

// ── Events table ───────────────────────────────────────────────────────
var evtb=document.getElementById('evtb');
D.events.slice(0,100).forEach(function(ev){
  var tickHtml=ev.tickers.map(function(t){
    var idx=D.tickers.findIndex(function(td){return td.ticker===t;});
    var c=idx>=0?CC[idx%CC.length]:'#8b949e';
    return '<span class="bdg" style="background:'+c+'20;color:'+c+'">'+esc(t)+'</span>';
  }).join('');
  var titleHtml=ev.url
    ?'<a href="'+esc(ev.url)+'" target="_blank" rel="noopener">'+esc(ev.title)+'</a>'
    :esc(ev.title);
  var tr=document.createElement('tr');
  tr.innerHTML=
    '<td style="color:#8b949e;white-space:nowrap">'+esc(ev.date)+'</td>'+
    '<td style="white-space:nowrap">'+tickHtml+'</td>'+
    '<td><b>'+ev.score.toFixed(3)+'</b></td>'+
    '<td>'+titleHtml+'</td>';
  evtb.appendChild(tr);
});
</script>
</body>
</html>"""


# ── Markdown companion ──────────────────────────────────────────────────

def _write_md(
    ticker_list: list[dict],
    events: list[dict],
    target_dir: Path,
) -> None:
    today = date.today().isoformat()
    lines = [
        "---",
        f"updated: {today}",
        f"tickers: {len(ticker_list)}",
        f"signal_events: {len(events)}",
        "---",
        "",
        "# Stock Tracker",
        f"> {len(ticker_list)} tickers from digest entity mentions "
        f"· [[stock-tracker|Open Chart]] · {today}",
        "",
        "## Portfolio Summary",
        "",
        "| Ticker | Price | 30d | 90d | Mentions | Signals |",
        "|--------|-------|-----|-----|----------|---------|",
    ]
    for td in ticker_list:
        c30 = td["change_30d"]
        c90 = td["change_90d"]
        ev_n = sum(1 for e in events if td["ticker"] in e["tickers"])
        lines.append(
            f"| **{td['ticker']}** | ${td['current']:.2f}"
            f" | {'↑' if c30 > 0 else '↓'} {c30:+.2f}%"
            f" | {'↑' if c90 > 0 else '↓'} {c90:+.2f}%"
            f" | {td['mentions']} | {ev_n} |"
        )

    if events:
        lines += [
            "",
            "## Recent High-Signal Events",
            "",
            "| Date | Tickers | Score | Event |",
            "|------|---------|-------|-------|",
        ]
        for ev in events[:25]:
            tstr = " ".join(f"`{t}`" for t in ev["tickers"])
            title = (ev["title"] or "")[:80]
            link  = f"[{title}]({ev['url']})" if ev["url"] else title
            lines.append(f"| {ev['date']} | {tstr} | {ev['score']:.3f} | {link} |")

    (target_dir / "Stock Tracker.md").write_text("\n".join(lines), encoding="utf-8")


# ── Public entry point ──────────────────────────────────────────────────

def run_stock_tracker(ticker_limit: int = 50) -> dict[str, Any]:
    """Fetch prices, build signal overlays, write HTML + markdown."""
    vault          = Path(settings.obsidian_vault_path).expanduser()
    investments_dir = vault / "20 Projects" / "Investments"
    investments_dir.mkdir(parents=True, exist_ok=True)

    tickers = _get_top_tickers(limit=ticker_limit)
    logger.info("stock_tracker: resolving prices for %d tickers", len(tickers))

    price_data = _fetch_prices(tickers)
    if not price_data:
        logger.warning("stock_tracker: no price data — yfinance may be unavailable")
        return {"path": "", "tickers": 0, "events": 0}

    active   = list(price_data.keys())
    events   = _get_signal_events(active)
    mentions = _get_mention_counts(active)

    # Sort by mention count so most-referenced tickers appear first / on top
    ticker_list = []
    for ticker in sorted(active, key=lambda t: mentions.get(t, 0), reverse=True):
        entry = dict(price_data[ticker])
        entry["mentions"] = mentions.get(ticker, 0)
        ticker_list.append(entry)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers":      ticker_list,
        "events":       events,
    }

    html      = _HTML.replace("__TRACKER_DATA__", _safe_json(payload))
    html_path = investments_dir / "stock-tracker.html"
    html_path.write_text(html, encoding="utf-8")

    _write_md(ticker_list, events, investments_dir)

    logger.info(
        "stock_tracker: wrote %s (tickers=%d events=%d)",
        html_path.name, len(ticker_list), len(events),
    )
    return {"path": str(html_path), "tickers": len(ticker_list), "events": len(events)}
