from jinja2 import Template
from sqldoc.extractor import Table
from datetime import datetime

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
        .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 40px; }
        .stat-card { background: white; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; }
        .stat-card .number { font-size: 2rem; font-weight: 700; color: #4f46e5; }
        .stat-card .label { color: #6b7280; font-size: 0.875rem; margin-top: 4px; }
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
        .col-name { font-weight: 500; font-family: monospace; color: #111827; }
        .col-type { color: #6b7280; font-family: monospace; font-size: 0.8rem; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 500; margin-right: 4px; }
        .badge-pk { background: #fef3c7; color: #92400e; }
        .badge-fk { background: #dbeafe; color: #1e40af; }
        .badge-nullable { background: #f3f4f6; color: #6b7280; }
        .col-description { color: #4b5563; font-size: 0.85rem; line-height: 1.5; }
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
            <div class="stat-card">
                <div class="number">{{ total_columns }}</div>
                <div class="label">Columns</div>
            </div>
            <div class="stat-card">
                <div class="number">{{ total_schemas }}</div>
                <div class="label">Schemas</div>
            </div>
        </div>

        {% for schema, tables in schemas.items() %}
        <div class="schema-group">
            <div class="schema-title">{{ schema }}</div>
            {% for table in tables %}
            <div class="table-card">
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
                        <tr>
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
            </div>
            {% endfor %}
        </div>
        {% endfor %}
    </div>
    <div class="footer">Generated by sqldoc</div>
</body>
</html>
"""

def render_html(database: str, tables: list[Table], output_path: str):
    schemas = {}
    for table in tables:
        if table.schema not in schemas:
            schemas[table.schema] = []
        schemas[table.schema].append(table)

    total_columns = sum(len(t.columns) for t in tables)

    template = Template(HTML_TEMPLATE)
    html = template.render(
        database=database,
        tables=tables,
        schemas=schemas,
        total_tables=len(tables),
        total_columns=total_columns,
        total_schemas=len(schemas),
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p")
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Documentation written to {output_path}")