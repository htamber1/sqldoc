from datetime import datetime
from jinja2 import Environment

from sqldoc.pii import summarize

RISK_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

PII_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ database }} — PII / Compliance Scan</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg: #0a0a0f; --card: #1e2530; --card-head: #171d26;
            --text: #e5e7eb; --text-strong: #f8fafc; --muted: #94a3b8; --faint: #64748b;
            --border: #2a3340; --border-strong: #3a4658;
            --high: #f87171; --high-bg: rgba(220,38,38,0.15); --high-bd: rgba(220,38,38,0.4);
            --med: #fbbf24; --med-bg: rgba(245,158,11,0.15); --med-bd: rgba(245,158,11,0.4);
            --low: #94a3b8; --low-bg: rgba(148,163,184,0.12); --low-bd: rgba(148,163,184,0.3);
            --blue: #60a5fa;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; }
        ::-webkit-scrollbar { width: 11px; height: 11px; }
        ::-webkit-scrollbar-track { background: #0a0e18; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 6px; border: 2px solid #0a0e18; }
        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(220,38,38,0.12), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 52px 40px 46px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--high), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
        .header h1 { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em; color: var(--text-strong); margin-bottom: 8px; }
        .header p { color: var(--muted); font-size: 0.92rem; }
        .container { max-width: 1200px; margin: 0 auto; padding: 36px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .stat-card { background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 22px; text-align: center; }
        .stat-card .number { font-size: 2.2rem; font-weight: 800; letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.78rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.07em; }
        .stat-card.high .number { color: var(--high); }
        .stat-card.med .number { color: var(--med); }
        .stat-card.low .number { color: var(--low); }
        .stat-card.info .number { color: var(--blue); }
        .regs { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 28px; }
        .reg-chip { display: inline-flex; align-items: center; gap: 8px; background: var(--card); border: 1px solid var(--border-strong); border-radius: 20px; padding: 7px 16px; font-size: 0.85rem; }
        .reg-chip b { color: var(--text-strong); }
        .reg-chip .rc { color: var(--muted); }
        .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
        .filter-btn { padding: 7px 16px; font-size: 0.85rem; font-weight: 600; color: var(--muted); background: var(--card); border: 1px solid var(--border); border-radius: 8px; cursor: pointer; transition: all 0.15s; }
        .filter-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
        .filter-btn.active { background: rgba(96,165,250,0.15); color: var(--blue); border-color: var(--blue); }
        .export-btn { margin-left: auto; padding: 7px 16px; font-size: 0.85rem; font-weight: 600; color: var(--text-strong); background: var(--card); border: 1px solid var(--border-strong); border-radius: 8px; cursor: pointer; }
        .export-btn:hover { border-color: var(--blue); color: #fff; }
        .panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); }
        td { padding: 12px 16px; font-size: 0.86rem; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        .loc { font-family: 'Consolas', monospace; color: var(--text-strong); }
        .loc .col { color: var(--blue); font-weight: 600; }
        .dtype { color: var(--muted); font-family: 'Consolas', monospace; font-size: 0.8rem; }
        .risk { display: inline-block; padding: 3px 11px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; border: 1px solid transparent; }
        .risk.HIGH { background: var(--high-bg); color: var(--high); border-color: var(--high-bd); }
        .risk.MEDIUM { background: var(--med-bg); color: var(--med); border-color: var(--med-bd); }
        .risk.LOW { background: var(--low-bg); color: var(--low); border-color: var(--low-bd); }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.7rem; font-weight: 600; margin: 1px 3px 1px 0; background: rgba(96,165,250,0.14); color: var(--blue); border: 1px solid rgba(96,165,250,0.3); }
        .conf { color: var(--muted); font-size: 0.8rem; }
        .action { color: #cbd5e1; font-size: 0.82rem; line-height: 1.5; }
        .empty { text-align: center; color: var(--faint); padding: 40px; }
        .footer { max-width: 1200px; margin: 30px auto 0; padding: 20px; color: var(--faint); font-size: 0.8rem; line-height: 1.6; border-top: 1px solid var(--border); }
        .footer strong { color: var(--muted); }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; PII / Compliance Scan</span>
        <h1>{{ database }}</h1>
        <p>Generated on {{ generated_at }} &middot; {{ 'with AI data sampling' if sampled else 'name + type analysis (no data read)' }}</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card high"><div class="number">{{ summary.by_risk.HIGH }}</div><div class="label">High risk</div></div>
            <div class="stat-card med"><div class="number">{{ summary.by_risk.MEDIUM }}</div><div class="label">Medium risk</div></div>
            <div class="stat-card low"><div class="number">{{ summary.by_risk.LOW }}</div><div class="label">Low risk</div></div>
            <div class="stat-card info"><div class="number">{{ summary.total }}</div><div class="label">Columns flagged</div></div>
            <div class="stat-card info"><div class="number">{{ summary.tables_affected }}</div><div class="label">Tables affected</div></div>
        </div>

        {% if summary.by_regulation %}
        <div class="regs">
            {% for reg, count in summary.by_regulation.items() %}
            <span class="reg-chip"><b>{{ reg }}</b><span class="rc">{{ count }} finding{{ '' if count == 1 else 's' }}</span></span>
            {% endfor %}
        </div>
        {% endif %}

        <div class="toolbar">
            <button type="button" class="filter-btn active" data-filter="all">All ({{ summary.total }})</button>
            <button type="button" class="filter-btn" data-filter="HIGH">High ({{ summary.by_risk.HIGH }})</button>
            <button type="button" class="filter-btn" data-filter="MEDIUM">Medium ({{ summary.by_risk.MEDIUM }})</button>
            <button type="button" class="filter-btn" data-filter="LOW">Low ({{ summary.by_risk.LOW }})</button>
            <button type="button" class="export-btn" id="export-csv">Export CSV</button>
        </div>

        <div class="panel">
            <table>
                <thead>
                    <tr>
                        <th>Location</th><th>Type</th><th>Data Type</th><th>Risk</th>
                        <th>Confidence</th><th>Regulations</th><th>Recommended Action</th>
                    </tr>
                </thead>
                <tbody id="findings">
                    {% for f in findings %}
                    <tr data-risk="{{ f.risk }}">
                        <td class="loc">{{ f.schema }}.{{ f.table }}.<span class="col">{{ f.column }}</span></td>
                        <td>{{ f.category }}</td>
                        <td class="dtype">{{ f.data_type }}</td>
                        <td><span class="risk {{ f.risk }}">{{ f.risk }}</span></td>
                        <td class="conf">{{ f.confidence }}</td>
                        <td>{% for r in f.regulations %}<span class="badge">{{ r }}</span>{% endfor %}</td>
                        <td class="action">{{ f.action }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% if not findings %}<div class="empty">No likely-PII columns were detected.</div>{% endif %}
            <div class="empty" id="no-match" style="display:none;">No findings at this risk level.</div>
        </div>
    </div>
    <div class="footer">
        <strong>Scope &amp; privacy.</strong> Findings are heuristic — based on column names and data types{{ ' and AI review of a small data sample' if sampled else '' }}.
        {% if sampled %}Up to 5 values per flagged column were read to help the AI confirm each finding; <strong>no sampled values are stored in this report or anywhere else</strong> — only the resulting verdict.{% else %}No table row data was read — this scan used schema metadata only.{% endif %}
        Review each finding against your data-classification policy before acting. Regulation mappings (HIPAA / GDPR / PCI-DSS) indicate likely applicability, not legal advice.
    </div>

    <script>
        var FINDINGS = {{ findings_data | tojson }};
        (function () {
            var rows = Array.prototype.slice.call(document.querySelectorAll('#findings tr'));
            var btns = Array.prototype.slice.call(document.querySelectorAll('.filter-btn'));
            var noMatch = document.getElementById('no-match');
            btns.forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var f = btn.getAttribute('data-filter');
                    btns.forEach(function (b) { b.classList.toggle('active', b === btn); });
                    var shown = 0;
                    rows.forEach(function (r) {
                        var ok = (f === 'all' || r.getAttribute('data-risk') === f);
                        r.style.display = ok ? '' : 'none';
                        if (ok) { shown++; }
                    });
                    noMatch.style.display = shown ? 'none' : 'block';
                });
            });

            document.getElementById('export-csv').addEventListener('click', function () {
                var cols = ['schema', 'table', 'column', 'data_type', 'category', 'risk', 'confidence', 'regulations', 'action'];
                var esc = function (v) { v = String(v == null ? '' : v); return '"' + v.replace(/"/g, '""') + '"'; };
                var lines = [cols.join(',')];
                FINDINGS.forEach(function (f) {
                    lines.push(cols.map(function (c) {
                        return esc(c === 'regulations' ? (f[c] || []).join('; ') : f[c]);
                    }).join(','));
                });
                var blob = new Blob([lines.join('\\n')], { type: 'text/csv' });
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'pii-findings.csv';
                document.body.appendChild(a); a.click(); document.body.removeChild(a);
            });
        })();
    </script>
</body>
</html>
"""


def render_pii_html(database, findings, output_path, sampled=False):
    findings = sorted(findings, key=lambda f: (RISK_RANK[f.risk], f.schema, f.table, f.column))
    summary = summarize(findings)
    findings_data = [{
        "schema": f.schema, "table": f.table, "column": f.column,
        "data_type": f.data_type, "category": f.category, "risk": f.risk,
        "confidence": f.confidence, "regulations": f.regulations, "action": f.action,
    } for f in findings]

    template = Environment(autoescape=True).from_string(PII_TEMPLATE)
    html = template.render(
        database=database,
        findings=findings,
        summary=summary,
        sampled=sampled,
        findings_data=findings_data,
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"PII scan report written to {output_path}")
