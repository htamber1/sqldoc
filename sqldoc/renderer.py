import math
from jinja2 import Environment
from sqldoc.extractor import Table, View, StoredProcedure
from datetime import datetime

# Palette used to color-code schemas in the ER diagram (cycled if there are more
# schemas than colors).
SCHEMA_PALETTE = [
    "#4f46e5", "#0891b2", "#059669", "#d97706", "#dc2626",
    "#7c3aed", "#db2777", "#2563eb", "#65a30d", "#c026d3",
]


def _build_er(tables: list[Table]) -> dict:
    """Compute an SVG entity-relationship layout from the extracted tables.

    Boxes are packed into columns (masonry: each next box drops into the
    shortest column) and foreign keys become curved connector paths. All
    geometry is computed here so the template only has to emit SVG.
    """
    # Layout constants (px)
    CHAR_W = 6.6      # approx width of one monospace char at 11px
    PAD = 10
    HEADER_H = 24
    ROW_H = 17
    MIN_W = 140
    MAX_CHARS = 34    # truncate long "col : type" lines to keep boxes bounded
    GUT_X = 70
    GUT_Y = 40
    MARGIN = 30

    schemas_sorted = sorted({t.schema for t in tables})
    schema_colors = {
        s: SCHEMA_PALETTE[i % len(SCHEMA_PALETTE)]
        for i, s in enumerate(schemas_sorted)
    }

    boxes = []
    id_to_box = {}
    name_to_id = {}  # bare table name (lowercased) -> box id, for FK resolution
    for t in tables:
        cols = []
        maxlen = len(t.name)
        for c in t.columns:
            line = f"{c.name} : {c.data_type}"
            if len(line) > MAX_CHARS:
                line = line[:MAX_CHARS - 1] + "…"
            maxlen = max(maxlen, len(line))
            cols.append({
                "label": line,
                "is_pk": c.is_primary_key,
                "is_fk": c.is_foreign_key,
            })
        w = max(MIN_W, int(maxlen * CHAR_W) + PAD * 2)
        h = HEADER_H + ROW_H * max(1, len(cols))
        bid = f"{t.schema}.{t.name}"
        box = {
            "id": bid,
            "title": t.name,
            "color": schema_colors[t.schema],
            "w": w,
            "h": h,
            "columns": cols,
        }
        boxes.append(box)
        id_to_box[bid] = box
        name_to_id.setdefault(t.name.lower(), bid)

    # Masonry placement into a fixed number of equal-width columns.
    n = len(boxes)
    ncols = max(1, min(6, round(math.sqrt(n)))) if n else 1
    slot_w = max((b["w"] for b in boxes), default=MIN_W)
    col_bottoms = [MARGIN] * ncols
    for b in boxes:
        ci = col_bottoms.index(min(col_bottoms))
        b["x"] = MARGIN + ci * (slot_w + GUT_X)
        b["y"] = col_bottoms[ci]
        b["cx"] = b["x"] + b["w"] // 2
        col_bottoms[ci] += b["h"] + GUT_Y

    total_w = MARGIN * 2 + ncols * slot_w + (ncols - 1) * GUT_X
    total_h = (max(col_bottoms) if col_bottoms else MARGIN) + MARGIN

    # Foreign-key edges, deduped per (child, parent) table pair.
    edges = []
    seen = set()
    for t in tables:
        child_id = f"{t.schema}.{t.name}"
        cb = id_to_box[child_id]
        for c in t.columns:
            if not (c.is_foreign_key and c.references_table):
                continue
            pid = name_to_id.get(c.references_table.lower())
            if not pid:
                continue
            key = (child_id, pid)
            if key in seen:
                continue
            seen.add(key)
            pb = id_to_box[pid]

            if child_id == pid:
                # Self-reference: small loop off the right edge.
                x = cb["x"] + cb["w"]
                y = cb["y"] + cb["h"] // 2
                d = f"M {x} {y} C {x + 55} {y - 34}, {x + 55} {y + 34}, {x} {y}"
            else:
                if pb["cx"] >= cb["cx"]:
                    sx, sy = cb["x"] + cb["w"], cb["y"] + cb["h"] // 2
                    ex, ey = pb["x"], pb["y"] + pb["h"] // 2
                else:
                    sx, sy = cb["x"], cb["y"] + cb["h"] // 2
                    ex, ey = pb["x"] + pb["w"], pb["y"] + pb["h"] // 2
                dx = ex - sx
                c1x = int(sx + dx * 0.4)
                c2x = int(ex - dx * 0.4)
                d = f"M {sx} {sy} C {c1x} {sy}, {c2x} {ey}, {ex} {ey}"
            edges.append({"d": d})

    return {
        "boxes": boxes,
        "edges": edges,
        "width": int(total_w),
        "height": int(total_h),
        "legend": [{"schema": s, "color": schema_colors[s]} for s in schemas_sorted],
    }


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ database }} Database Documentation</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8f9fa; color: #212529; }
        .header { background: #1a1a2e; color: white; padding: 40px; }
        .header h1 { font-size: 2rem; margin-bottom: 8px; }
        .header p { color: #a0aec0; font-size: 0.95rem; }
        .container { max-width: 1200px; margin: 0 auto; padding: 40px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 20px; margin-bottom: 40px; }
        .stat-card { background: white; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; }
        .stat-card .number { font-size: 2rem; font-weight: 700; color: #4f46e5; }
        .stat-card .label { color: #6b7280; font-size: 0.875rem; margin-top: 4px; }
        .search-bar { position: sticky; top: 0; z-index: 20; background: #f8f9fa; padding: 16px 0; margin-bottom: 8px; }
        .search-bar input { width: 100%; padding: 12px 16px; font-size: 1rem; border: 1px solid #d1d5db; border-radius: 8px; outline: none; }
        .search-bar input:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,0.15); }
        .search-count { font-size: 0.8rem; color: #6b7280; margin-top: 6px; min-height: 1em; }
        .section-title { font-size: 1.5rem; font-weight: 700; margin: 24px 0 16px; color: #111827; }
        .er-panel { background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 40px; overflow: hidden; }
        .er-toolbar { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid #e5e7eb; flex-wrap: wrap; }
        .er-toolbar button { border: 1px solid #d1d5db; background: #f9fafb; border-radius: 6px; padding: 4px 12px; font-size: 0.85rem; cursor: pointer; }
        .er-toolbar button:hover { background: #eef2ff; border-color: #4f46e5; }
        .er-legend { display: flex; gap: 14px; flex-wrap: wrap; margin-left: auto; }
        .er-legend span { display: inline-flex; align-items: center; gap: 5px; font-size: 0.78rem; color: #4b5563; }
        .er-legend i { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
        .er-canvas { overflow: auto; max-height: 640px; background: #fbfbfd; }
        #er-svg { transform-origin: 0 0; transition: transform 0.1s ease-out; }
        .schema-group { margin-bottom: 40px; }
        .schema-title { font-size: 1.25rem; font-weight: 600; color: #4f46e5; border-bottom: 2px solid #4f46e5; padding-bottom: 8px; margin-bottom: 20px; }
        .table-card { background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; overflow: hidden; }
        .table-header { padding: 20px 24px; border-bottom: 1px solid #e5e7eb; display: flex; justify-content: space-between; align-items: flex-start; }
        .table-name { font-size: 1.1rem; font-weight: 600; color: #111827; }
        .table-meta { font-size: 0.8rem; color: #6b7280; margin-top: 4px; }
        .table-description { font-size: 0.9rem; color: #4b5563; margin-top: 8px; line-height: 1.6; }
        .row-count { background: #ede9fe; color: #4f46e5; padding: 4px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 500; white-space: nowrap; }
        table { width: 100%; border-collapse: collapse; }
        th { background: #f9fafb; padding: 10px 16px; text-align: left; font-size: 0.8rem; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #e5e7eb; }
        td { padding: 12px 16px; font-size: 0.875rem; border-bottom: 1px solid #f3f4f6; vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: #f9fafb; }
        tr.hl td { background: #fef9c3; }
        .col-name { font-weight: 500; font-family: monospace; color: #111827; }
        .col-type { color: #6b7280; font-family: monospace; font-size: 0.8rem; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 500; margin-right: 4px; }
        .badge-pk { background: #fef3c7; color: #92400e; }
        .badge-fk { background: #dbeafe; color: #1e40af; }
        .badge-nullable { background: #f3f4f6; color: #6b7280; }
        .col-description { color: #4b5563; font-size: 0.85rem; line-height: 1.5; }
        .badge-uq { background: #dcfce7; color: #166534; }
        .badge-out { background: #fae8ff; color: #86198f; }
        .type-badge { padding: 4px 12px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.05em; white-space: nowrap; }
        .type-badge.view { background: #cffafe; color: #0e7490; }
        .type-badge.proc { background: #ede9fe; color: #6d28d9; }
        .subsection-title { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: #6b7280; padding: 14px 16px 6px; }
        .index-section { border-top: 1px solid #f3f4f6; }
        .no-params { padding: 14px 16px; color: #9ca3af; font-size: 0.85rem; font-style: italic; }
        details.definition { border-top: 1px solid #f3f4f6; }
        details.definition summary { padding: 12px 16px; cursor: pointer; font-size: 0.8rem; font-weight: 600; color: #4f46e5; user-select: none; }
        details.definition summary:hover { background: #f9fafb; }
        details.definition pre { margin: 0; padding: 16px; background: #1e293b; color: #e2e8f0; font-size: 0.78rem; line-height: 1.5; overflow-x: auto; font-family: 'Consolas', 'Monaco', monospace; }
        .no-results { display: none; text-align: center; color: #9ca3af; padding: 40px; font-size: 0.95rem; }
        .footer { text-align: center; padding: 40px; color: #9ca3af; font-size: 0.85rem; }
    </style>
</head>
<body>
    <div class="header">
        <h1>{{ database }} Documentation</h1>
        <p>Generated on {{ generated_at }} using sqldoc</p>
    </div>
    <div class="container">
        <div class="stats">
            <div class="stat-card">
                <div class="number">{{ total_tables }}</div>
                <div class="label">Tables</div>
            </div>
            {% if total_views %}
            <div class="stat-card">
                <div class="number">{{ total_views }}</div>
                <div class="label">Views</div>
            </div>
            {% endif %}
            {% if total_procedures %}
            <div class="stat-card">
                <div class="number">{{ total_procedures }}</div>
                <div class="label">Procedures</div>
            </div>
            {% endif %}
            <div class="stat-card">
                <div class="number">{{ total_columns }}</div>
                <div class="label">Columns</div>
            </div>
            <div class="stat-card">
                <div class="number">{{ total_schemas }}</div>
                <div class="label">Schemas</div>
            </div>
        </div>

        {% if er.boxes %}
        <div class="section-title">Entity Relationship Diagram</div>
        <div class="er-panel">
            <div class="er-toolbar">
                <button type="button" onclick="erZoom(0.15)">Zoom +</button>
                <button type="button" onclick="erZoom(-0.15)">Zoom −</button>
                <button type="button" onclick="erReset()">Reset</button>
                <div class="er-legend">
                    {% for item in er.legend %}
                    <span><i style="background: {{ item.color }}"></i>{{ item.schema }}</span>
                    {% endfor %}
                </div>
            </div>
            <div class="er-canvas">
                <svg id="er-svg" width="{{ er.width }}" height="{{ er.height }}" viewBox="0 0 {{ er.width }} {{ er.height }}" xmlns="http://www.w3.org/2000/svg">
                    <defs>
                        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                            <path d="M0,0 L10,5 L0,10 z" fill="#64748b"></path>
                        </marker>
                    </defs>
                    {% for e in er.edges %}
                    <path d="{{ e.d }}" fill="none" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#arrow)" opacity="0.75"></path>
                    {% endfor %}
                    {% for box in er.boxes %}
                    <g>
                        <rect x="{{ box.x }}" y="{{ box.y }}" width="{{ box.w }}" height="{{ box.h }}" rx="6" fill="white" stroke="{{ box.color }}" stroke-width="1.5"></rect>
                        <path d="M {{ box.x }} {{ box.y + 6 }} q 0 -6 6 -6 h {{ box.w - 12 }} q 6 0 6 6 v 18 h -{{ box.w }} z" fill="{{ box.color }}"></path>
                        <text x="{{ box.cx }}" y="{{ box.y + 16 }}" text-anchor="middle" fill="white" font-size="12" font-weight="700">{{ box.title }}</text>
                        {% for col in box.columns %}
                        <text x="{{ box.x + 8 }}" y="{{ box.y + 24 + loop.index0 * 17 + 12 }}" font-size="11" font-family="monospace"
                              fill="{% if col.is_pk %}#92400e{% elif col.is_fk %}#1e40af{% else %}#374151{% endif %}"
                              font-weight="{% if col.is_pk %}700{% else %}400{% endif %}">{{ col.label }}</text>
                        {% endfor %}
                    </g>
                    {% endfor %}
                </svg>
            </div>
        </div>
        {% endif %}

        <div class="search-bar">
            <input type="text" id="search" placeholder="Search tables and columns..." autocomplete="off">
            <div class="search-count" id="search-count"></div>
        </div>

        <div id="doc-body">
        <div class="section-title">Tables</div>
        {% for schema, tables in schemas.items() %}
        <div class="schema-group">
            <div class="schema-title">{{ schema }}</div>
            {% for table in tables %}
            <div class="table-card"
                 data-name="{{ (schema ~ '.' ~ table.name)|lower }}"
                 data-search="{{ (schema ~ ' ' ~ table.name ~ ' ' ~ (table.columns|map(attribute='name')|join(' ')) ~ ' ' ~ (table.columns|map(attribute='data_type')|join(' ')))|lower }}">
                <div class="table-header">
                    <div>
                        <div class="table-name">{{ table.name }}</div>
                        <div class="table-meta">{{ table.schema }}.{{ table.name }}</div>
                        {% if table.description %}
                        <div class="table-description">{{ table.description }}</div>
                        {% endif %}
                    </div>
                    <div class="row-count">{{ "{:,}".format(table.row_count) }} rows</div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Column</th>
                            <th>Type</th>
                            <th>Attributes</th>
                            <th>Description</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for col in table.columns %}
                        <tr data-col="{{ (col.name ~ ' ' ~ col.data_type)|lower }}">
                            <td class="col-name">{{ col.name }}</td>
                            <td class="col-type">{{ col.data_type }}</td>
                            <td>
                                {% if col.is_primary_key %}<span class="badge badge-pk">PK</span>{% endif %}
                                {% if col.is_foreign_key %}<span class="badge badge-fk">FK</span>{% endif %}
                                {% if col.is_nullable %}<span class="badge badge-nullable">nullable</span>{% endif %}
                            </td>
                            <td class="col-description">
                                {{ col.description or "" }}
                                {% if col.is_foreign_key and col.references_table %}
                                <div style="color:#6b7280;font-size:0.8rem;margin-top:4px;">→ {{ col.references_table }}.{{ col.references_column }}</div>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% if table.indexes %}
                <div class="index-section">
                    <div class="subsection-title">Indexes ({{ table.indexes|length }})</div>
                    <table>
                        <thead>
                            <tr><th>Name</th><th>Type</th><th>Columns</th></tr>
                        </thead>
                        <tbody>
                            {% for idx in table.indexes %}
                            <tr>
                                <td class="col-name">{{ idx.name }}
                                    {% if idx.is_primary_key %}<span class="badge badge-pk">PK</span>{% elif idx.is_unique %}<span class="badge badge-uq">UNIQUE</span>{% endif %}
                                </td>
                                <td class="col-type">{{ idx.type_desc }}</td>
                                <td class="col-description">{{ idx.key_columns|join(', ') }}{% if idx.included_columns %} <span style="color:#6b7280;">(incl: {{ idx.included_columns|join(', ') }})</span>{% endif %}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% endfor %}

        {% if views_by_schema %}
        <div class="section-title">Views</div>
        {% for schema, views in views_by_schema.items() %}
        <div class="schema-group">
            <div class="schema-title">{{ schema }}</div>
            {% for view in views %}
            <div class="table-card"
                 data-name="{{ (schema ~ '.' ~ view.name)|lower }}"
                 data-search="{{ (schema ~ ' ' ~ view.name ~ ' view ' ~ (view.columns|map(attribute='name')|join(' ')) ~ ' ' ~ (view.columns|map(attribute='data_type')|join(' ')))|lower }}">
                <div class="table-header">
                    <div>
                        <div class="table-name">{{ view.name }}</div>
                        <div class="table-meta">{{ view.schema }}.{{ view.name }}</div>
                        {% if view.description %}
                        <div class="table-description">{{ view.description }}</div>
                        {% endif %}
                    </div>
                    <div class="type-badge view">VIEW</div>
                </div>
                {% if view.columns %}
                <table>
                    <thead>
                        <tr><th>Column</th><th>Type</th><th>Description</th></tr>
                    </thead>
                    <tbody>
                        {% for col in view.columns %}
                        <tr data-col="{{ (col.name ~ ' ' ~ col.data_type)|lower }}">
                            <td class="col-name">{{ col.name }}</td>
                            <td class="col-type">{{ col.data_type }}</td>
                            <td class="col-description">{{ col.description or "" }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% endif %}
                {% if view.definition %}
                <details class="definition">
                    <summary>Definition</summary>
                    <pre><code>{{ view.definition }}</code></pre>
                </details>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% endfor %}
        {% endif %}

        {% if procs_by_schema %}
        <div class="section-title">Stored Procedures</div>
        {% for schema, procs in procs_by_schema.items() %}
        <div class="schema-group">
            <div class="schema-title">{{ schema }}</div>
            {% for proc in procs %}
            <div class="table-card"
                 data-name="{{ (schema ~ '.' ~ proc.name)|lower }}"
                 data-search="{{ (schema ~ ' ' ~ proc.name ~ ' procedure proc ' ~ (proc.parameters|map(attribute='name')|join(' ')) ~ ' ' ~ (proc.parameters|map(attribute='data_type')|join(' ')))|lower }}">
                <div class="table-header">
                    <div>
                        <div class="table-name">{{ proc.name }}</div>
                        <div class="table-meta">{{ proc.schema }}.{{ proc.name }}</div>
                        {% if proc.description %}
                        <div class="table-description">{{ proc.description }}</div>
                        {% endif %}
                    </div>
                    <div class="type-badge proc">PROC</div>
                </div>
                {% if proc.parameters %}
                <table>
                    <thead>
                        <tr><th>Parameter</th><th>Type</th><th>Direction</th></tr>
                    </thead>
                    <tbody>
                        {% for p in proc.parameters %}
                        <tr data-col="{{ (p.name ~ ' ' ~ p.data_type)|lower }}">
                            <td class="col-name">{{ p.name }}</td>
                            <td class="col-type">{{ p.data_type }}</td>
                            <td>{% if p.is_output %}<span class="badge badge-out">OUTPUT</span>{% else %}<span class="badge badge-nullable">IN</span>{% endif %}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
                {% else %}
                <div class="no-params">No parameters</div>
                {% endif %}
                {% if proc.definition %}
                <details class="definition">
                    <summary>Definition</summary>
                    <pre><code>{{ proc.definition }}</code></pre>
                </details>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% endfor %}
        {% endif %}
        </div>
        <div class="no-results" id="no-results">No objects match your search.</div>
    </div>
    <div class="footer">Generated by sqldoc</div>

    <script>
        // --- ER diagram zoom ---
        var erScale = 1;
        function _applyErScale() {
            var svg = document.getElementById('er-svg');
            if (svg) { svg.style.transform = 'scale(' + erScale + ')'; }
        }
        function erZoom(delta) {
            erScale = Math.min(2.5, Math.max(0.2, erScale + delta));
            _applyErScale();
        }
        function erReset() { erScale = 1; _applyErScale(); }

        // --- Real-time search over tables and columns ---
        (function () {
            var input = document.getElementById('search');
            if (!input) { return; }
            var cards = Array.prototype.slice.call(document.querySelectorAll('.table-card'));
            var groups = Array.prototype.slice.call(document.querySelectorAll('.schema-group'));
            var counter = document.getElementById('search-count');
            var noResults = document.getElementById('no-results');
            var total = cards.length;

            function run() {
                var q = input.value.toLowerCase().trim();
                var shown = 0;
                cards.forEach(function (card) {
                    var rows = Array.prototype.slice.call(card.querySelectorAll('tbody tr'));
                    if (!q) {
                        card.style.display = '';
                        rows.forEach(function (r) { r.style.display = ''; r.classList.remove('hl'); });
                        shown++;
                        return;
                    }
                    var nameMatch = card.dataset.name.indexOf(q) !== -1;
                    var colMatch = card.dataset.search.indexOf(q) !== -1;
                    if (nameMatch || colMatch) {
                        card.style.display = '';
                        shown++;
                        rows.forEach(function (r) {
                            var rc = r.dataset.col || '';
                            var m = rc.indexOf(q) !== -1;
                            if (nameMatch) {
                                r.style.display = '';
                                r.classList.toggle('hl', m);
                            } else {
                                r.style.display = m ? '' : 'none';
                                r.classList.toggle('hl', m);
                            }
                        });
                    } else {
                        card.style.display = 'none';
                    }
                });
                groups.forEach(function (g) {
                    var any = Array.prototype.slice.call(g.querySelectorAll('.table-card'))
                        .some(function (c) { return c.style.display !== 'none'; });
                    g.style.display = any ? '' : 'none';
                });
                counter.textContent = q ? (shown + ' of ' + total + ' objects') : '';
                noResults.style.display = (q && shown === 0) ? 'block' : 'none';
            }

            input.addEventListener('input', run);
        })();
    </script>
</body>
</html>
"""


def _group_by_schema(objects: list) -> dict:
    grouped = {}
    for obj in objects:
        grouped.setdefault(obj.schema, []).append(obj)
    return grouped


def render_html(
    database: str,
    tables: list[Table],
    output_path: str,
    views: list[View] = None,
    procedures: list[StoredProcedure] = None,
):
    views = views or []
    procedures = procedures or []

    schemas = _group_by_schema(tables)
    views_by_schema = _group_by_schema(views)
    procs_by_schema = _group_by_schema(procedures)

    total_columns = sum(len(t.columns) for t in tables)
    # Schema count spans every documented object type, not just tables.
    all_schemas = {t.schema for t in tables} | {v.schema for v in views} | {p.schema for p in procedures}

    # autoescape=True so SQL definitions / names / descriptions containing
    # <, >, & (e.g. "@CheckDate <= ...") render as text, not broken markup.
    template = Environment(autoescape=True).from_string(HTML_TEMPLATE)
    html = template.render(
        database=database,
        tables=tables,
        schemas=schemas,
        views_by_schema=views_by_schema,
        procs_by_schema=procs_by_schema,
        er=_build_er(tables),
        total_tables=len(tables),
        total_views=len(views),
        total_procedures=len(procedures),
        total_columns=total_columns,
        total_schemas=len(all_schemas),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p")
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Documentation written to {output_path}")
