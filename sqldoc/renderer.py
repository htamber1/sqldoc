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
        /* Premium dark theme:
           bg #0a0a0f · cards #0f172a · electric blue #3b82f6 · gold #f59e0b */
        * { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --bg: #0a0a0f; --card: #1e2530; --card-head: #171d26;
            --blue: #3b82f6; --blue-soft: #60a5fa; --gold: #f59e0b; --gold-soft: #fbbf24;
            --text: #e5e7eb; --text-strong: #f8fafc; --muted: #94a3b8; --faint: #64748b;
            --border: #2a3340; --border-strong: #3a4658;
        }
        html { scroll-behavior: smooth; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; }
        ::selection { background: rgba(59,130,246,0.35); color: #fff; }
        /* Slim dark scrollbars */
        ::-webkit-scrollbar { width: 11px; height: 11px; }
        ::-webkit-scrollbar-track { background: #0a0e18; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 6px; border: 2px solid #0a0e18; }
        ::-webkit-scrollbar-thumb:hover { background: #334155; }

        .header { position: relative; background: radial-gradient(900px 300px at 88% -30%, rgba(245,158,11,0.10), transparent 55%), linear-gradient(180deg, #12161d, #0a0a0f); padding: 56px 40px 52px; border-bottom: 1px solid var(--border); }
        .header::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: linear-gradient(90deg, var(--gold), transparent 70%); }
        .header .brand { display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.22em; text-transform: uppercase; color: var(--muted); margin-bottom: 14px; }
        .header h1 { font-size: 2.4rem; font-weight: 800; letter-spacing: -0.02em; margin-bottom: 10px; color: var(--text-strong); }
        .header p { color: var(--muted); font-size: 0.95rem; }
        .container { max-width: 1200px; margin: 0 auto; padding: 40px 20px 20px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 18px; margin-bottom: 44px; }
        .stat-card { position: relative; background: linear-gradient(180deg, #242c38, var(--card)); border: 1px solid var(--border); border-radius: 14px; padding: 24px; text-align: center; overflow: hidden; transition: border-color 0.15s, transform 0.15s; }
        .stat-card::before { content: ""; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: linear-gradient(90deg, transparent, rgba(245,158,11,0.55), transparent); }
        .stat-card:hover { border-color: var(--border-strong); transform: translateY(-2px); }
        .stat-card .number { font-size: 2.3rem; font-weight: 800; color: var(--gold); letter-spacing: -0.02em; }
        .stat-card .label { color: var(--muted); font-size: 0.8rem; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.08em; }
        .controls { position: sticky; top: 0; z-index: 20; background: var(--bg); padding: 14px 0 12px; margin-bottom: 14px; border-bottom: 1px solid var(--border); }
        .search-bar input { width: 100%; padding: 13px 18px; font-size: 1rem; background: var(--card); border: 1px solid var(--border-strong); border-radius: 10px; outline: none; color: var(--text); transition: border-color 0.15s, box-shadow 0.15s; }
        .search-bar input::placeholder { color: var(--faint); }
        .search-bar input:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(59,130,246,0.25); }
        .filter-nav { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
        .filter-btn { padding: 7px 18px; font-size: 0.85rem; font-weight: 600; color: var(--muted); background: var(--card); border: 1px solid var(--border); border-radius: 8px; cursor: pointer; transition: color 0.15s, border-color 0.15s, background 0.15s; }
        .filter-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
        .filter-btn.active { background: rgba(59,130,246,0.15); color: var(--blue-soft); border-color: var(--blue); }
        .search-count { font-size: 0.8rem; color: var(--muted); margin-top: 10px; min-height: 1em; }
        .section-title { font-size: 1.55rem; font-weight: 800; letter-spacing: -0.01em; margin: 28px 0 18px; color: var(--text-strong); display: flex; align-items: center; gap: 12px; }
        .section-title::before { content: ""; width: 4px; height: 1.3em; border-radius: 3px; background: var(--border-strong); }
        .er-panel { background: var(--card); border: 1px solid var(--border); border-radius: 14px; margin-bottom: 44px; overflow: hidden; }
        .er-toolbar { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
        .er-toolbar button { border: 1px solid var(--border-strong); background: #131c31; color: var(--text); border-radius: 8px; padding: 5px 14px; font-size: 0.85rem; cursor: pointer; transition: all 0.15s; }
        .er-toolbar button:hover { background: #2a3340; border-color: var(--border-strong); color: #fff; }
        .er-legend { display: flex; gap: 14px; flex-wrap: wrap; margin-left: auto; }
        .er-legend span { display: inline-flex; align-items: center; gap: 6px; font-size: 0.78rem; color: var(--muted); }
        .er-legend i { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
        .er-canvas { overflow: auto; max-height: 640px; background: #0c0f14; }
        #er-svg { transform-origin: 0 0; transition: transform 0.1s ease-out; }
        .schema-group { margin-bottom: 44px; }
        .schema-title { font-size: 1.25rem; font-weight: 700; color: var(--blue); border-bottom: 2px solid rgba(59,130,246,0.5); padding-bottom: 10px; margin-bottom: 22px; }
        .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.35); margin-bottom: 24px; overflow: hidden; transition: border-color 0.15s; }
        .table-card:hover { border-color: var(--border-strong); }
        .table-header { padding: 20px 24px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
        .table-name { font-size: 1.15rem; font-weight: 700; color: var(--text-strong); }
        .table-meta { font-size: 0.8rem; color: var(--muted); margin-top: 4px; font-family: 'Consolas', monospace; }
        .table-description { font-size: 0.9rem; color: #cbd5e1; margin-top: 10px; line-height: 1.6; }
        .row-count { background: rgba(255,255,255,0.05); color: var(--muted); border: 1px solid var(--border-strong); padding: 4px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 600; white-space: nowrap; }
        table { width: 100%; border-collapse: collapse; }
        th { background: var(--card-head); padding: 11px 16px; text-align: left; font-size: 0.72rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid var(--border-strong); }
        td { padding: 12px 16px; font-size: 0.875rem; border-bottom: 1px solid var(--border); vertical-align: top; color: var(--text); }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(255,255,255,0.025); }
        tr.hl td { background: rgba(245,158,11,0.13); }
        .col-name { font-weight: 600; font-family: 'Consolas', monospace; color: var(--blue-soft); }
        .col-type { color: var(--muted); font-family: 'Consolas', monospace; font-size: 0.8rem; }
        .badge { display: inline-block; padding: 2px 9px; border-radius: 5px; font-size: 0.72rem; font-weight: 600; margin-right: 4px; border: 1px solid transparent; }
        .badge-pk { background: rgba(245,158,11,0.15); color: var(--gold-soft); border-color: rgba(245,158,11,0.4); }
        .badge-fk { background: rgba(59,130,246,0.15); color: var(--blue-soft); border-color: rgba(59,130,246,0.4); }
        .badge-nullable { background: rgba(148,163,184,0.1); color: var(--muted); border-color: rgba(148,163,184,0.25); }
        .col-description { color: #cbd5e1; font-size: 0.85rem; line-height: 1.5; }
        .badge-uq { background: rgba(16,185,129,0.15); color: #34d399; border-color: rgba(16,185,129,0.35); }
        .badge-out { background: rgba(217,70,239,0.15); color: #e879f9; border-color: rgba(217,70,239,0.35); }
        .type-badge { padding: 4px 13px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.06em; white-space: nowrap; border: 1px solid transparent; }
        .type-badge.view { background: rgba(6,182,212,0.15); color: #22d3ee; border-color: rgba(6,182,212,0.35); }
        .type-badge.proc { background: rgba(139,92,246,0.15); color: #a78bfa; border-color: rgba(139,92,246,0.35); }
        .subsection-title { font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); padding: 14px 16px 6px; }
        .index-section { border-top: 1px solid var(--border); }
        .no-params { padding: 14px 16px; color: var(--faint); font-size: 0.85rem; font-style: italic; }
        details.definition { border-top: 1px solid var(--border); }
        details.definition summary { padding: 12px 16px; cursor: pointer; font-size: 0.8rem; font-weight: 700; color: #cbd5e1; user-select: none; letter-spacing: 0.02em; }
        details.definition summary:hover { background: rgba(255,255,255,0.025); }
        details.definition pre { margin: 0; padding: 16px; background: #05070d; color: #cbd5e1; font-size: 0.78rem; line-height: 1.55; overflow-x: auto; font-family: 'Consolas', 'Monaco', monospace; border-top: 1px solid var(--border); }
        .no-results { display: none; text-align: center; color: var(--faint); padding: 40px; font-size: 0.95rem; }
        .footer { text-align: center; padding: 40px; color: var(--faint); font-size: 0.85rem; border-top: 1px solid var(--border); margin-top: 20px; }
    </style>
</head>
<body>
    <div class="header">
        <span class="brand">sqldoc &middot; Database Documentation</span>
        <h1>{{ database }}</h1>
        <p>Generated on {{ generated_at }}</p>
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
                            <path d="M0,0 L10,5 L0,10 z" fill="#94a3b8"></path>
                        </marker>
                    </defs>
                    {% for e in er.edges %}
                    <path d="{{ e.d }}" fill="none" stroke="#64748b" stroke-width="1.5" marker-end="url(#arrow)" opacity="0.8"></path>
                    {% endfor %}
                    {% for box in er.boxes %}
                    <g>
                        <rect x="{{ box.x }}" y="{{ box.y }}" width="{{ box.w }}" height="{{ box.h }}" rx="6" fill="#1e2530" stroke="{{ box.color }}" stroke-width="1.5"></rect>
                        <path d="M {{ box.x }} {{ box.y + 6 }} q 0 -6 6 -6 h {{ box.w - 12 }} q 6 0 6 6 v 18 h -{{ box.w }} z" fill="{{ box.color }}"></path>
                        <text x="{{ box.cx }}" y="{{ box.y + 16 }}" text-anchor="middle" fill="white" font-size="12" font-weight="700">{{ box.title }}</text>
                        {% for col in box.columns %}
                        <text x="{{ box.x + 8 }}" y="{{ box.y + 24 + loop.index0 * 17 + 12 }}" font-size="11" font-family="monospace"
                              fill="{% if col.is_pk %}#fbbf24{% elif col.is_fk %}#60a5fa{% else %}#cbd5e1{% endif %}"
                              font-weight="{% if col.is_pk %}700{% else %}400{% endif %}">{{ col.label }}</text>
                        {% endfor %}
                    </g>
                    {% endfor %}
                </svg>
            </div>
        </div>
        {% endif %}

        <div class="controls">
            <div class="search-bar">
                <input type="text" id="search" placeholder="Search tables, views, procedures, and columns..." autocomplete="off">
            </div>
            <div class="filter-nav">
                <button type="button" class="filter-btn active" data-filter="all">All</button>
                <button type="button" class="filter-btn" data-filter="tables">Tables</button>
                {% if views_by_schema %}<button type="button" class="filter-btn" data-filter="views">Views</button>{% endif %}
                {% if procs_by_schema %}<button type="button" class="filter-btn" data-filter="procedures">Procedures</button>{% endif %}
            </div>
            <div class="search-count" id="search-count"></div>
        </div>

        <div id="doc-body">
        <div class="obj-section" data-section="tables">
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
        </div>

        {% if views_by_schema %}
        <div class="obj-section" data-section="views">
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
        </div>
        {% endif %}

        {% if procs_by_schema %}
        <div class="obj-section" data-section="procedures">
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
        </div>
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

        // --- Type filter (All / Tables / Views / Procedures) + real-time search ---
        (function () {
            var input = document.getElementById('search');
            var cards = Array.prototype.slice.call(document.querySelectorAll('.table-card'));
            var groups = Array.prototype.slice.call(document.querySelectorAll('.schema-group'));
            var sections = Array.prototype.slice.call(document.querySelectorAll('.obj-section'));
            var filterBtns = Array.prototype.slice.call(document.querySelectorAll('.filter-btn'));
            var counter = document.getElementById('search-count');
            var noResults = document.getElementById('no-results');
            var total = cards.length;
            var activeFilter = 'all';

            function sectionOf(card) {
                var s = card.closest('.obj-section');
                return s ? s.getAttribute('data-section') : '';
            }

            function apply() {
                var q = (input ? input.value : '').toLowerCase().trim();
                var shown = 0;
                cards.forEach(function (card) {
                    var passFilter = (activeFilter === 'all' || sectionOf(card) === activeFilter);
                    var rows = Array.prototype.slice.call(card.querySelectorAll('tbody tr'));
                    if (!passFilter) { card.style.display = 'none'; return; }
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
                // Hide schema groups and whole sections that have no visible cards.
                groups.forEach(function (g) {
                    var any = Array.prototype.slice.call(g.querySelectorAll('.table-card'))
                        .some(function (c) { return c.style.display !== 'none'; });
                    g.style.display = any ? '' : 'none';
                });
                sections.forEach(function (s) {
                    var any = Array.prototype.slice.call(s.querySelectorAll('.table-card'))
                        .some(function (c) { return c.style.display !== 'none'; });
                    s.style.display = any ? '' : 'none';
                });
                if (!q && activeFilter === 'all') {
                    counter.textContent = '';
                } else if (!q) {
                    counter.textContent = shown + ' ' + activeFilter;
                } else {
                    counter.textContent = shown + ' match' + (shown === 1 ? '' : 'es');
                }
                noResults.style.display = (shown === 0) ? 'block' : 'none';
            }

            if (input) { input.addEventListener('input', apply); }
            filterBtns.forEach(function (btn) {
                btn.addEventListener('click', function () {
                    activeFilter = btn.getAttribute('data-filter');
                    filterBtns.forEach(function (b) { b.classList.toggle('active', b === btn); });
                    apply();
                });
            });
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
