"""HTML + JSON rendering for the `sqldoc server` instance report."""
from dataclasses import asdict
from datetime import datetime

from jinja2 import Environment

from sqldoc import __version__
from sqldoc.server import summarize

SERVER_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ server_name }} — Server Health</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg: #0a0a0f; --card: #1e2530; --card-head: #171d26;
            --text: #e5e7eb; --text-strong: #f8fafc; --muted: #94a3b8; --faint: #64748b;
            --border: #2a3340; --border-strong: #3a4658;
            --red: #f87171; --amber: #fbbf24; --green: #34d399; --blue: #60a5fa; --violet: #c084fc; --orange: #fb923c;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; }
        ::-webkit-scrollbar { width: 11px; height: 11px; }
        ::-webkit-scrollbar-track { background: #0a0e18; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 6px; border: 2px solid #0a0e18; }
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(96,165,250,0.13), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--blue), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1280px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 20px; text-align: center; }
        .stat-card .number { font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.72rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
        .c-red .number { color: var(--red); } .c-amber .number { color: var(--amber); }
        .c-blue .number { color: var(--blue); } .c-green .number { color: var(--green); }
        .c-violet .number { color: var(--violet); } .c-orange .number { color: var(--orange); }
        h2.section { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); margin: 30px 0 12px; display: flex; align-items: center; gap: 10px; }
        h2.section .n { font-size: 0.8rem; color: var(--muted); font-weight: 600; }
        .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
        @media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; overflow-x: auto; }
        .panel .phead { padding: 14px 18px; background: var(--card-head); border-bottom: 1px solid var(--border-strong); font-size: 0.82rem; font-weight: 700; color: var(--text-strong); }
        .pbody { padding: 16px 18px; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 10px 14px; text-align: left; font-size: 0.7rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
        td { padding: 9px 14px; font-size: 0.84rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        .mono { font-family: 'Consolas', monospace; }
        .num { text-align: right; font-family: 'Consolas', monospace; white-space: nowrap; }
        .sql { font-family: 'Consolas', monospace; font-size: 0.78rem; color: #cbd5e1; white-space: pre-wrap; word-break: break-word; max-width: 480px; }
        .bar-wrap { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
        .bar-label { width: 130px; font-size: 0.8rem; color: var(--muted); }
        .bar-track { flex: 1; height: 12px; background: #0a0e18; border-radius: 6px; overflow: hidden; }
        .bar-fill { height: 100%; border-radius: 6px; }
        .bar-val { width: 90px; text-align: right; font-family: 'Consolas', monospace; font-size: 0.8rem; }
        .kv { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--border); font-size: 0.86rem; }
        .kv:last-child { border-bottom: none; }
        .kv .k { color: var(--muted); }
        .kv .v { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .pill { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; border: 1px solid transparent; }
        .pill.ok { background: rgba(52,211,153,0.14); color: var(--green); border-color: rgba(52,211,153,0.35); }
        .pill.warn { background: rgba(245,158,11,0.15); color: var(--amber); border-color: rgba(245,158,11,0.4); }
        .pill.bad { background: rgba(220,38,38,0.15); color: var(--red); border-color: rgba(220,38,38,0.4); }
        .warn { background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.3); border-radius: 10px; padding: 12px 16px; margin-bottom: 20px; color: var(--amber); font-size: 0.83rem; }
        .empty { text-align: center; color: var(--faint); padding: 22px; font-size: 0.85rem; }
        .footer { max-width: 1280px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Server Health</span>
        <h1>{{ server_name }}</h1>
        <p>Generated on {{ generated_at }} &middot; instance-level metrics from server-scoped DMVs (no table row data read)</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card c-blue"><div class="number">{{ summary.cpu_sql_percent }}%</div><div class="label">SQL CPU</div></div>
            <div class="stat-card c-violet"><div class="number">{{ (summary.memory_total_mb / 1024)|round(1) }}</div><div class="label">Memory (GB)</div></div>
            <div class="stat-card c-green"><div class="number">{{ summary.sessions }}</div><div class="label">Sessions</div></div>
            <div class="stat-card {{ 'c-red' if summary.blocking_chains else 'c-green' }}"><div class="number">{{ summary.blocking_chains }}</div><div class="label">Blocking chains</div></div>
            <div class="stat-card {{ 'c-red' if summary.low_disk_volumes else 'c-blue' }}"><div class="number">{{ summary.low_disk_volumes }}</div><div class="label">Low-disk volumes</div></div>
        </div>

        {% if report.errors %}
        <div class="warn">
            Some checks were skipped (usually a missing <b>VIEW SERVER STATE</b> permission or msdb access):
            {% for section, msg in report.errors %}<div>&bull; <b>{{ section }}</b> — {{ msg }}</div>{% endfor %}
        </div>
        {% endif %}

        <div class="grid2">
            <div class="panel">
                <div class="phead">CPU</div>
                <div class="pbody">
                    {% set cpu = report.cpu %}
                    {% if cpu %}
                    <div class="bar-wrap"><div class="bar-label">SQL Server</div><div class="bar-track"><div class="bar-fill" style="width: {{ cpu.sql_process_percent }}%; background: var(--blue);"></div></div><div class="bar-val">{{ cpu.sql_process_percent }}%</div></div>
                    <div class="bar-wrap"><div class="bar-label">Other processes</div><div class="bar-track"><div class="bar-fill" style="width: {{ cpu.other_process_percent }}%; background: var(--amber);"></div></div><div class="bar-val">{{ cpu.other_process_percent }}%</div></div>
                    <div class="bar-wrap"><div class="bar-label">Idle</div><div class="bar-track"><div class="bar-fill" style="width: {{ cpu.idle_percent }}%; background: var(--green);"></div></div><div class="bar-val">{{ cpu.idle_percent }}%</div></div>
                    {% else %}<div class="empty">CPU sample unavailable.</div>{% endif %}
                </div>
            </div>
            <div class="panel">
                <div class="phead">Instance</div>
                <div class="pbody">
                    {% set info = report.info %}
                    {% if info %}
                    <div class="kv"><span class="k">Uptime</span><span class="v">{{ info.uptime_text }}</span></div>
                    <div class="kv"><span class="k">Started</span><span class="v">{{ info.sql_server_start_time }}</span></div>
                    <div class="kv"><span class="k">Logical CPUs</span><span class="v">{{ info.cpu_count }}</span></div>
                    <div class="kv"><span class="k">Schedulers</span><span class="v">{{ info.scheduler_count }}</span></div>
                    <div class="kv"><span class="k">Physical memory</span><span class="v">{{ (info.physical_memory_mb / 1024)|round(1) }} GB</span></div>
                    {% else %}<div class="empty">Instance info unavailable.</div>{% endif %}
                </div>
            </div>
        </div>

        <h2 class="section">Memory <span class="n">buffer pool / plan cache / stolen</span></h2>
        <div class="panel">
            <div class="pbody">
                {% set mem = report.memory %}
                {% if mem and mem.total_mb %}
                <div class="bar-wrap"><div class="bar-label">Buffer pool</div><div class="bar-track"><div class="bar-fill" style="width: {{ (100 * mem.buffer_pool_mb / mem.total_mb)|round|int }}%; background: var(--blue);"></div></div><div class="bar-val">{{ (mem.buffer_pool_mb/1024)|round(1) }} GB</div></div>
                <div class="bar-wrap"><div class="bar-label">Plan cache</div><div class="bar-track"><div class="bar-fill" style="width: {{ (100 * mem.plan_cache_mb / mem.total_mb)|round|int }}%; background: var(--violet);"></div></div><div class="bar-val">{{ (mem.plan_cache_mb/1024)|round(1) }} GB</div></div>
                <div class="bar-wrap"><div class="bar-label">Stolen (non-BP)</div><div class="bar-track"><div class="bar-fill" style="width: {{ (100 * mem.stolen_mb / mem.total_mb)|round|int }}%; background: var(--amber);"></div></div><div class="bar-val">{{ (mem.stolen_mb/1024)|round(1) }} GB</div></div>
                <div class="kv" style="margin-top:10px;"><span class="k">Total (memory clerks)</span><span class="v">{{ (mem.total_mb/1024)|round(1) }} GB</span></div>
                {% else %}<div class="empty">Memory clerk data unavailable.</div>{% endif %}
            </div>
        </div>

        <h2 class="section">Disk volumes <span class="n">free space + I/O latency</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Volume</th><th>Total</th><th>Free</th><th>Free %</th><th>Read lat.</th><th>Write lat.</th><th>Status</th></tr></thead>
                <tbody>
                    {% for v in report.volumes %}
                    <tr>
                        <td class="mono">{{ v.volume }}{% if v.logical_name %} <span class="muted">({{ v.logical_name }})</span>{% endif %}</td>
                        <td class="num">{{ v.total_gb }} GB</td>
                        <td class="num">{{ v.available_gb }} GB</td>
                        <td class="num">{{ v.free_percent }}%</td>
                        <td class="num">{{ v.read_latency_ms }} ms</td>
                        <td class="num">{{ v.write_latency_ms }} ms</td>
                        <td><span class="pill {{ 'bad' if v.is_low else 'ok' }}">{{ 'LOW' if v.is_low else 'OK' }}</span></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.volumes %}<div class="empty">No volume statistics available.</div>{% endif %}
        </div>

        <h2 class="section">Blocking chains <span class="n">sessions waiting on another session</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>Blocker SPID</th><th>Blocked SPID</th><th>Wait</th><th>Wait ms</th><th>Blocked query</th></tr></thead>
                <tbody>
                    {% for b in report.blocking_chains %}
                    <tr>
                        <td class="num">{{ b.blocker_session_id }}</td>
                        <td class="num">{{ b.blocked_session_id }}</td>
                        <td class="mono">{{ b.wait_type or '—' }}</td>
                        <td class="num">{{ '{:,}'.format(b.wait_time_ms) }}</td>
                        <td class="sql">{{ b.blocked_query }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.blocking_chains %}<div class="empty">No blocking detected.</div>{% endif %}
        </div>

        <h2 class="section">Top running queries <span class="n">active requests by CPU</span></h2>
        <div class="panel">
            <table>
                <thead><tr><th>SPID</th><th>Login</th><th>Database</th><th>Status</th><th>CPU ms</th><th>Elapsed ms</th><th>Query</th></tr></thead>
                <tbody>
                    {% for q in report.top_queries %}
                    <tr>
                        <td class="num">{{ q.session_id }}</td>
                        <td class="mono">{{ q.login_name }}</td>
                        <td class="mono">{{ q.database }}</td>
                        <td>{{ q.status }}{% if q.blocking_session_id %} <span class="pill bad">blocked by {{ q.blocking_session_id }}</span>{% endif %}</td>
                        <td class="num">{{ '{:,}'.format(q.cpu_ms) }}</td>
                        <td class="num">{{ '{:,}'.format(q.elapsed_ms) }}</td>
                        <td class="sql">{{ q.query_text }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not report.top_queries %}<div class="empty">No user requests running right now.</div>{% endif %}
        </div>

        <div class="grid2">
            <div class="panel">
                <div class="phead">Connections by login</div>
                <div class="pbody">
                    {% if report.connections and report.connections.by_login %}
                    {% for login, n in report.connections.by_login %}<div class="kv"><span class="k mono">{{ login }}</span><span class="v">{{ n }}</span></div>{% endfor %}
                    {% else %}<div class="empty">No session data.</div>{% endif %}
                </div>
            </div>
            <div class="panel">
                <div class="phead">Connections by database</div>
                <div class="pbody">
                    {% if report.connections and report.connections.by_database %}
                    {% for db, n in report.connections.by_database %}<div class="kv"><span class="k mono">{{ db }}</span><span class="v">{{ n }}</span></div>{% endfor %}
                    {% else %}<div class="empty">No session data.</div>{% endif %}
                </div>
            </div>
        </div>
    </div>
    <div class="footer">
        <strong>About these metrics.</strong> Figures come from server-scoped DMVs
        (<code>sys.dm_os_ring_buffers</code>, <code>sys.dm_os_memory_clerks</code>, <code>sys.dm_os_volume_stats</code>,
        <code>sys.dm_exec_sessions/requests</code>) and reflect the instance's live state. The CPU ring buffer is sampled roughly once a
        minute. No table row data was read.
    </div>
</body>
</html>
"""


def build_server_json(server_name: str, report) -> dict:
    return {
        "schema_version": 1,
        "sqldoc_version": __version__,
        "report_type": "server",
        "server": server_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(report),
        "info": asdict(report.info) if report.info else None,
        "cpu": asdict(report.cpu) if report.cpu else None,
        "memory": asdict(report.memory) if report.memory else None,
        "volumes": [{**asdict(v), "used_percent": v.used_percent,
                     "free_percent": v.free_percent, "is_low": v.is_low}
                    for v in report.volumes],
        "connections": asdict(report.connections) if report.connections else None,
        "blocking_chains": [asdict(b) for b in report.blocking_chains],
        "top_queries": [asdict(q) for q in report.top_queries],
        "agent_jobs": [{**asdict(j), "is_long_running": j.is_long_running,
                        "duration_text": j.duration_text} for j in report.agent_jobs],
        "errors": [{"section": s, "message": m} for s, m in report.errors],
    }


def render_server_html(server_name, report, output_path):
    report.server_name = server_name
    template = Environment(autoescape=True).from_string(SERVER_TEMPLATE)
    html = template.render(
        server_name=server_name,
        report=report,
        summary=summarize(report),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Server health report written to {output_path}")
