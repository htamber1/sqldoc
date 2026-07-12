import click
import os
import yaml
from dotenv import load_dotenv
import re
from sqldoc import __version__
from sqldoc.extractor import build_connection_string
from sqldoc.adapters import get_adapter, detect_dialect, UnsupportedDialectError, DIALECT_CHOICES
from sqldoc.ai import enrich_tables, enrich_views, enrich_procedures, load_cache, save_cache
from sqldoc.renderer import render_html
from sqldoc.markdown_renderer import render_markdown
from sqldoc.json_renderer import render_json
from sqldoc.snapshot import build_snapshot, load_snapshot, save_snapshot, diff_snapshots, iter_diff_lines
from sqldoc.pii import (scan_tables, confirm_with_sampling, summarize,
                        findings_snapshot, diff_findings, iter_findings_diff_lines,
                        load_custom_categories, findings_json,
                        apply_allowlist, filter_by_confidence)
from sqldoc.pii_renderer import render_pii_html
from sqldoc.sarif import render_sarif
from sqldoc.health import collect_health, summarize as health_summarize
from sqldoc.health_renderer import render_health_html, build_health_json
from sqldoc.quality import collect_quality, summarize as quality_summarize
from sqldoc.quality_renderer import render_quality_html, build_quality_json
from sqldoc.intel import collect_intel, summarize as intel_summarize
from sqldoc.intel_renderer import render_intel_html, build_intel_json
from sqldoc.insights import collect_insights, summarize as insights_summarize
from sqldoc.insights_renderer import render_insights_html, build_insights_json
from sqldoc.comply import (collect_compliance, summarize as comply_summarize,
                           extract_permissions as comply_extract_permissions,
                           extract_role_members as comply_extract_role_members)
from sqldoc.comply_renderer import render_comply_html, build_comply_json
from sqldoc.comply_multi import (collect_database_access, build_cross_db, summarize_multi,
                                 DatabaseAccess)
from sqldoc.comply_multi_renderer import render_multi_comply_html, build_multi_comply_json
from sqldoc.offline import verify_file, blocking_refs
from sqldoc.dbt import find_dbt_project, parse_dbt_project, merge as dbt_merge, summarize as dbt_summarize
from sqldoc.dbt_renderer import render_dbt_html, build_dbt_json
from sqldoc.server import collect_server, summarize as server_summarize
from sqldoc.server_renderer import render_server_html, build_server_json

load_dotenv()

# Config keys that .sqldoc.yml may set; each maps to the same-named CLI option.
CONFIG_KEYS = {
    'server', 'database', 'username', 'password', 'connection_string', 'dialect', 'output',
    'mode', 'model', 'schemas', 'no_ai', 'concurrency', 'format',
    'include_definitions',
    'snapshot', 'no_snapshot', 'cache', 'no_cache', 'sample',
    'baseline', 'no_baseline', 'sarif', 'json', 'pii_patterns', 'pii_allowlist',
    'confidence_threshold', 'fail_on', 'yes',
    'top', 'min_fragmentation', 'min_pages',
    'top_values', 'no_duplicates', 'no_glossary',
    'verify_offline',
    'project_dir', 'no_db',
    'databases', 'all_databases',
    'agent',
}


def extract_metadata(adapter):
    """Extraction seam: real runs delegate to the resolved DatabaseAdapter (so
    --dialect drives the right catalog queries); tests monkeypatch these to
    inject fixture schema without a live database."""
    return adapter.extract_metadata()


def extract_views(adapter):
    return adapter.extract_views()


def extract_procedures(adapter):
    return adapter.extract_procedures()


def _parse_database(connection_string: str):
    """Best-effort extraction of the database name from a connection string,
    for labeling output and naming snapshot/cache files."""
    m = re.search(r'(?:DATABASE|Initial\s+Catalog)\s*=\s*([^;]+)', connection_string, re.IGNORECASE)
    return m.group(1).strip() if m else None

_DIFF_COLORS = {'add': 'green', 'remove': 'red', 'change': 'yellow'}
# For PII drift the semantics invert vs. code diffs: a NEW exposure is bad (red),
# a RESOLVED finding is good (green).
_PII_DIFF_COLORS = {'new': 'red', 'resolved': 'green', 'escalate': 'red', 'deescalate': 'green'}


def print_pii_diff(diff, path):
    """Render a PII-drift diff to the terminal."""
    click.echo(f"\nPII drift since last scan  ({path}):")
    for kind, text in iter_findings_diff_lines(diff):
        if kind == 'summary':
            click.echo(click.style(text, bold=True))
        elif kind == 'none':
            click.echo(click.style(text, dim=True))
        else:
            click.echo(click.style(text, fg=_PII_DIFF_COLORS.get(kind)))


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def print_diff(diff, path):
    """Render a snapshot diff to the terminal, git-diff style."""
    click.echo(f"\nSchema changes since last run  ({path}):")
    for kind, text in iter_diff_lines(diff):
        if kind == 'summary':
            click.echo(click.style(text, bold=True))
        elif kind == 'none':
            click.echo(click.style(text, dim=True))
        else:
            click.echo(click.style(text, fg=_DIFF_COLORS.get(kind)))

# Output file extension -> format, used when --format is not given explicitly.
_EXT_FORMAT = {
    '.html': 'html', '.htm': 'html',
    '.md': 'markdown', '.markdown': 'markdown',
    '.pdf': 'pdf',
    '.json': 'json',
}


def resolve_format(fmt, output):
    """Pick the output format: explicit --format wins, else infer from the
    output file extension, else default to html."""
    if fmt:
        return fmt
    return _EXT_FORMAT.get(os.path.splitext(output)[1].lower(), 'html')


def _make_resolver(ctx, cfg):
    """Return a resolve(name, value, param) that prefers an explicit CLI flag,
    then the config value, then the built-in default."""
    def resolve(name, value, param=None):
        if ctx.get_parameter_source(param or name).name == 'COMMANDLINE':
            return value
        return cfg.get(name, value)
    return resolve


def open_adapter(resolve, conn_str, dialect):
    """Resolve --dialect (explicit flag > config > auto-detect from the
    connection string) and return the concrete DatabaseAdapter. Extraction
    (doc/scan/intel/insights) flows through the returned adapter, so pointing
    --dialect at postgres/mysql actually drives the right catalog queries. An
    unsupported dialect raises a clean UsageError."""
    dialect = resolve('dialect', dialect)
    try:
        return get_adapter(conn_str, dialect)
    except UnsupportedDialectError as e:
        raise click.UsageError(str(e))


def _adapter_from_db_entry(entry):
    """Build (name, adapter) from one `.sqldoc.yml` `databases:` entry.

    An entry is a mapping with a `name` plus either a `connection_string` or the
    discrete `server`/`database`/`username`/`password`, and an optional
    `dialect`. Returns (name, adapter, conn_str)."""
    if not isinstance(entry, dict):
        raise click.UsageError("Each item under 'databases:' must be a mapping.")
    name = entry.get('name') or entry.get('database') or 'database'
    dialect = entry.get('dialect')
    conn_str = entry.get('connection_string')
    if not conn_str:
        server = entry.get('server')
        database = entry.get('database')
        username = entry.get('username')
        password = entry.get('password')
        missing = [n for n, v in (('server', server), ('database', database),
                                  ('username', username), ('password', password)) if not v]
        if missing:
            raise click.UsageError(
                f"Database '{name}' is missing connection settings: {', '.join(missing)} "
                f"(or provide a connection_string).")
        conn_str = build_connection_string(server, database, username, password)
    try:
        adapter = get_adapter(conn_str, dialect)
    except UnsupportedDialectError as e:
        raise click.UsageError(str(e))
    return name, adapter, conn_str


def _require_capability(adapter, flag, command):
    """Guard a command whose dialect-specific SQL is only implemented for some
    dialects (health/quality DMV+aggregate SQL, comply access audit). Renders a
    clean error rather than running SQL Server SQL against another engine."""
    if not getattr(adapter.capabilities, flag, False):
        raise click.UsageError(
            f"'sqldoc {command}' is not available on dialect '{adapter.dialect}' "
            f"({adapter.display_name}). It is currently supported on SQL Server / "
            f"Azure SQL only."
        )


def _verify_offline(output, enabled):
    """When --verify-offline is set, scan the rendered HTML report for any
    external resource references (CDN scripts, web fonts, remote images) that
    would break on an air-gapped network, and print the result. Non-HTML output
    is skipped. Warns (does not fail) if anything is found."""
    if not enabled or not output:
        return
    if not str(output).lower().endswith((".html", ".htm")):
        click.echo("  offline check skipped: report is not HTML.")
        return
    try:
        refs = verify_file(output)
    except OSError as e:
        click.echo(click.style(f"  offline check could not read {output}: {e}", fg='yellow'), err=True)
        return
    blocking = blocking_refs(refs)
    if blocking:
        click.echo(click.style(
            f"  ! offline check: {len(blocking)} external resource reference(s) found "
            f"- this report is NOT air-gap safe:", fg='yellow'), err=True)
        for r in blocking[:20]:
            click.echo(click.style(f"      [{r.kind}] {r.url}", fg='yellow'), err=True)
    else:
        note = ""
        links = [r for r in refs if not r.is_blocking]
        if links:
            note = f" ({len(links)} external hyperlink(s), which do not auto-load)"
        click.echo(click.style(
            f"  offline check: OK - fully self-contained, no external resources{note}.", fg='green'))


def _resolve_connection(resolve, server, database, username, password, connection_string,
                        dialect=None):
    """Merge connection settings and return (conn_str, database, server).
    A --connection-string takes precedence over the discrete parts."""
    server = resolve('server', server)
    database = resolve('database', database)
    username = resolve('username', username)
    password = resolve('password', password)
    connection_string = resolve('connection_string', connection_string)
    if connection_string:
        conn_str, database = connection_string, (database or _parse_database(connection_string) or 'database')
    else:
        missing = [n for n, v in (('server', server), ('database', database),
                                  ('username', username), ('password', password)) if not v]
        if missing:
            raise click.UsageError(
                "Missing connection settings: " + ", ".join(missing) +
                ". Provide --server/--database/--username/--password, a "
                "--connection-string, or a .sqldoc.yml config file."
            )
        conn_str = build_connection_string(server, database, username, password)
    return conn_str, database, server


def load_config(path: str, explicit: bool) -> dict:
    """Load .sqldoc.yml into a dict keyed by CLI option name.

    Missing file is fine unless the path was passed explicitly with --config.
    Hyphenated keys (e.g. no-ai) are normalized to underscores; unknown keys
    warn so typos surface instead of being silently ignored.
    """
    if not os.path.exists(path):
        if explicit:
            raise click.UsageError(f"Config file not found: {path}")
        return {}
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise click.UsageError(f"Config file {path} must contain a YAML mapping (key: value pairs).")
    config = {}
    for key, value in data.items():
        # YAML 1.1 coerces a bare `yes:` key to the boolean True; map it back so
        # the --yes option is settable from config without quoting the key.
        if key is True:
            norm = 'yes'
        else:
            norm = str(key).replace('-', '_')
        if norm not in CONFIG_KEYS:
            click.echo(f"Warning: ignoring unknown config key '{key}' in {path}", err=True)
            continue
        config[norm] = value
    return config


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to document')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to --server/--database/--username/--password)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string; postgres/mysql planned for v1.5.0)')
@click.option('--output', default='documentation.html', help='Output file path')
@click.option('--format', 'output_format', default=None, type=click.Choice(['html', 'markdown', 'pdf', 'json']), help='Output format (default: inferred from --output extension, else html)')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI mode: local (Ollama) or cloud (Anthropic)')
@click.option('--model', default=None, help='Model to use (default: llama3.1:8b for local, claude-haiku-4-5 for cloud)')
@click.option('--schemas', default=None, help='Comma-separated list of schemas to include (default: all)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip AI descriptions, output schema only')
@click.option('--concurrency', default=8, type=click.IntRange(1, 64), help='Parallel AI calls during enrichment (default: 8)')
@click.option('--include-definitions', 'include_definitions', is_flag=True, default=False,
              help='Send view/proc/trigger SQL bodies to the AI for richer descriptions (off by default; widens the data boundary — in cloud mode the SQL is sent to Anthropic)')
@click.option('--snapshot', default=None, help='JSON schema-snapshot path for change detection (default: .sqldoc-snapshots/<database>.json)')
@click.option('--no-snapshot', is_flag=True, default=False, help='Disable schema snapshot + change detection for this run')
@click.option('--cache', default=None, help='AI description cache path (default: .sqldoc-cache/<database>.json)')
@click.option('--no-cache', is_flag=True, default=False, help='Disable the AI description cache (always regenerate)')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt (for non-interactive use)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external CDN/font/image references) for air-gapped use')
def main(config, server, database, username, password, connection_string, dialect, output, output_format, mode, model, schemas, no_ai, concurrency, include_definitions, snapshot, no_snapshot, cache, no_cache, yes, verify_offline):
    """sqldoc — Automated SQL Server database documentation generator."""

    # Merge config file under CLI flags: an explicit CLI flag always wins, then
    # a .sqldoc.yml value, then the built-in default.
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')

    def resolve(name, value, param=None):
        # `name` is the config key; `param` is the Click parameter name when it
        # differs (e.g. config key 'format' vs. option dest 'output_format').
        if ctx.get_parameter_source(param or name).name == 'COMMANDLINE':
            return value
        return cfg.get(name, value)

    server = resolve('server', server)
    database = resolve('database', database)
    username = resolve('username', username)
    password = resolve('password', password)
    connection_string = resolve('connection_string', connection_string)
    output = resolve('output', output)
    output_format = resolve('format', output_format, param='output_format')
    mode = resolve('mode', mode)
    model = resolve('model', model)
    schemas = resolve('schemas', schemas)
    no_ai = resolve('no_ai', no_ai)
    concurrency = resolve('concurrency', concurrency)
    include_definitions = resolve('include_definitions', include_definitions)
    snapshot = resolve('snapshot', snapshot)
    no_snapshot = resolve('no_snapshot', no_snapshot)
    cache = resolve('cache', cache)
    no_cache = resolve('no_cache', no_cache)
    yes = resolve('yes', yes)

    # Resolve how we connect: a full connection string takes precedence over the
    # individual --server/--database/--username/--password parts.
    if connection_string:
        conn_str = connection_string
        # database is only used for labeling + snapshot/cache filenames.
        database = database or _parse_database(connection_string) or 'database'
    else:
        missing = [n for n, v in (('server', server), ('database', database),
                                  ('username', username), ('password', password)) if not v]
        if missing:
            raise click.UsageError(
                "Missing connection settings: " + ", ".join(missing) +
                ". Provide --server/--database/--username/--password, a "
                "--connection-string, or a .sqldoc.yml config file."
            )
        conn_str = build_connection_string(server, database, username, password)

    # Resolve the dialect and open the matching adapter (rejects unsupported).
    adapter = open_adapter(resolve, conn_str, dialect)

    if mode not in ('local', 'cloud'):
        raise click.UsageError(f"Invalid mode '{mode}' (must be 'local' or 'cloud').")
    if not isinstance(concurrency, int) or not (1 <= concurrency <= 64):
        raise click.UsageError(f"Invalid concurrency '{concurrency}' (must be an integer 1-64).")
    if output_format not in (None, 'html', 'markdown', 'pdf', 'json'):
        raise click.UsageError(f"Invalid format '{output_format}' (must be html, markdown, pdf, or json).")

    output_format = resolve_format(output_format, output)

    # Resolve the model per backend when not explicitly set, so --model works
    # for both without a local default (llama tag) leaking into cloud calls.
    if model is None:
        model = 'llama3.1:8b' if mode == 'local' else 'claude-haiku-4-5'

    # Describe the data-egress posture for the chosen mode. --include-definitions
    # widens what is sent to the AI beyond schema metadata to the actual SQL
    # bodies of views/procedures/triggers.
    payload = "schema metadata + SQL definitions" if include_definitions else "schema metadata"
    if no_ai:
        privacy = "No AI - schema only, nothing leaves this machine"
    elif mode == "local":
        privacy = f"local (Ollama) - {payload}, no data leaves this network"
    else:
        privacy = f"cloud (Anthropic) - {payload} sent off-network"

    click.echo(f"\nsqldoc v{__version__}")
    click.echo(f"{'='*40}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Mode:     {'No AI' if no_ai else mode}")
    click.echo(f"Privacy:  {privacy}")
    click.echo(f"Output:   {output} ({output_format})")
    click.echo(f"{'='*40}\n")

    # Guard: cloud mode sends schema metadata off the client's network. Require
    # explicit confirmation so it can never happen by accident. Row data is never
    # read or transmitted — only table/column names, types, keys, and row counts.
    if not no_ai and mode == "cloud":
        click.echo(
            "WARNING: Cloud mode sends schema metadata (table names, column names,\n"
            "         data types, keys, and row counts) to Anthropic's API. No table\n"
            "         row data is ever read or sent. Use --mode local to keep\n"
            "         everything on this network."
        )
        if include_definitions:
            click.echo(
                "         --include-definitions ALSO sends the SQL bodies of your\n"
                "         views, stored procedures, and triggers to Anthropic. These\n"
                "         definitions can embed literals, comments, or business logic —\n"
                "         review them before enabling this in cloud mode."
            )
        if yes:
            click.echo("Proceeding with cloud mode (confirmed via --yes).")
        elif not click.confirm("Proceed with cloud mode?", default=False):
            click.echo("Aborted. Re-run with --mode local to stay on-network.")
            raise click.Abort()

    # Extract metadata
    click.echo(f"Connecting to {adapter.display_name}...")
    try:
        tables = extract_metadata(adapter)
        views = extract_views(adapter)
        procedures = extract_procedures(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    all_schemas = {o.schema for o in tables + views + procedures}
    click.echo(
        f"Found {len(tables)} tables, {len(views)} views, {len(procedures)} procedures "
        f"across {len(all_schemas)} schemas"
    )

    # Filter schemas if specified (applies to every object type)
    if schemas:
        schema_list = [s.strip() for s in schemas.split(',')]
        tables = [t for t in tables if t.schema in schema_list]
        views = [v for v in views if v.schema in schema_list]
        procedures = [p for p in procedures if p.schema in schema_list]
        click.echo(
            f"Filtered to {len(tables)} tables, {len(views)} views, {len(procedures)} procedures "
            f"in schemas: {', '.join(schema_list)}"
        )

    # Schema change detection: diff this run's structure against the previous
    # snapshot, then overwrite it. Runs on the extracted (schema-filtered)
    # structure and is independent of AI descriptions, so it works with --no-ai.
    if not no_snapshot:
        snap_path = snapshot or os.path.join('.sqldoc-snapshots', _safe_filename(database) + '.json')
        current = build_snapshot(database, tables, views, procedures)
        previous = load_snapshot(snap_path)
        if previous is None:
            click.echo(f"\nNo previous snapshot at {snap_path} - saving baseline for future change detection.")
        else:
            print_diff(diff_snapshots(previous, current), snap_path)
        save_snapshot(current, snap_path)

    # Generate AI descriptions
    if not no_ai:
        # Description cache: reuse descriptions for objects whose structure is
        # unchanged since the last run (huge speed/cost win on incremental runs).
        cache_obj = None
        cache_path = None
        if not no_cache:
            cache_path = cache or os.path.join('.sqldoc-cache', _safe_filename(database) + '.json')
            cache_obj = load_cache(cache_path)

        click.echo(f"\nGenerating AI descriptions using {mode} mode ({concurrency} parallel)"
                   f"{' with SQL definitions' if include_definitions else ''}...")
        try:
            tables = enrich_tables(tables, mode=mode, model=model, concurrency=concurrency, cache=cache_obj, include_definitions=include_definitions)
            views = enrich_views(views, mode=mode, model=model, concurrency=concurrency, cache=cache_obj, include_definitions=include_definitions)
            procedures = enrich_procedures(procedures, mode=mode, model=model, concurrency=concurrency, cache=cache_obj, include_definitions=include_definitions)
        except Exception as e:
            click.echo(f"\nAI generation failed: {e}", err=True)
            click.echo("Try --no-ai to generate schema-only documentation")
            raise click.Abort()

        if cache_obj is not None:
            save_cache(cache_obj, cache_path)
    else:
        click.echo("Skipping AI descriptions (--no-ai flag set)")

    # Render in the requested format
    click.echo(f"\nRendering documentation ({output_format})...")
    if output_format == 'markdown':
        render_markdown(database, tables, output, views=views, procedures=procedures)
    elif output_format == 'pdf':
        from sqldoc.pdf_renderer import render_pdf
        render_pdf(database, tables, output, views=views, procedures=procedures)
    elif output_format == 'json':
        render_json(database, tables, output, views=views, procedures=procedures)
    else:
        render_html(database, tables, output, views=views, procedures=procedures)

    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"\nDone! Open {output} in your browser to view the documentation.")

@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to scan')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string; postgres/mysql planned for v1.5.0)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist (default: all)')
@click.option('--output', default='pii-report.html', help='Output HTML report path')
@click.option('--sample', is_flag=True, default=False, help='Read up to 5 values per flagged column and use AI to confirm PII (opt-in; reads row data)')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI backend for --sample confirmation')
@click.option('--model', default=None, help='Model for --sample confirmation (default per mode)')
@click.option('--baseline', default=None, help='Findings-snapshot path for PII drift detection (default: .sqldoc-pii-snapshots/<database>.json)')
@click.option('--no-baseline', is_flag=True, default=False, help='Disable PII drift detection for this scan')
@click.option('--sarif', default=None, help='Also write findings as SARIF 2.1.0 to this path (for GitHub Advanced Security / Azure DevOps)')
@click.option('--json', 'json_out', default=None, help='Also write machine-readable findings (summary + all findings) as JSON to this path')
@click.option('--confidence-threshold', 'confidence_threshold', type=click.FloatRange(0.0, 1.0), default=0.0,
              help='Drop findings below this confidence score (0.0-1.0). E.g. 0.5 hides weak name-only / type-mismatch matches.')
@click.option('--fail-on', 'fail_on', type=click.Choice(['none', 'high', 'new-high']), default='none',
              help='Exit non-zero to gate CI: high = any HIGH finding; new-high = a new HIGH finding vs the baseline')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip confirmation prompts (for non-interactive use)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
def scan(config, server, database, username, password, connection_string, dialect, schemas, output, sample, mode, model, baseline, no_baseline, sarif, json_out, confidence_threshold, fail_on, yes, verify_offline):
    """Scan a SQL Server database for likely PII / regulated columns.

    Flags columns by name + data type, maps each to HIPAA / GDPR / PCI-DSS, and
    writes a compliance HTML report. With --sample, reads up to 5 values per
    flagged column and uses AI to confirm findings (values are never stored).
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    schemas = resolve('schemas', schemas)
    mode = resolve('mode', mode)
    model = resolve('model', model)
    sample = resolve('sample', sample)
    baseline = resolve('baseline', baseline)
    no_baseline = resolve('no_baseline', no_baseline)
    sarif = resolve('sarif', sarif)
    json_out = resolve('json', json_out, param='json_out')
    confidence_threshold = resolve('confidence_threshold', confidence_threshold, param='confidence_threshold')
    fail_on = resolve('fail_on', fail_on, param='fail_on')
    yes = resolve('yes', yes)

    if mode not in ('local', 'cloud'):
        raise click.UsageError(f"Invalid mode '{mode}' (must be 'local' or 'cloud').")
    if model is None:
        model = 'llama3.1:8b' if mode == 'local' else 'claude-haiku-4-5'

    # Validate custom PII patterns before connecting, so config errors fail fast.
    try:
        custom_cats = load_custom_categories(cfg.get('pii_patterns'))
    except ValueError as e:
        raise click.UsageError(f"Invalid pii_patterns in config: {e}")

    # Known-safe columns to suppress (org allowlist), from config or --config.
    allowlist = cfg.get('pii_allowlist') or []
    if not isinstance(allowlist, list):
        raise click.UsageError("pii_allowlist in config must be a list of column patterns.")

    # Confidence threshold may come from config as a string; coerce + clamp.
    try:
        confidence_threshold = max(0.0, min(1.0, float(confidence_threshold)))
    except (TypeError, ValueError):
        raise click.UsageError(f"Invalid confidence_threshold '{confidence_threshold}' (must be 0.0-1.0).")

    click.echo(f"\nsqldoc v{__version__}  -  PII / compliance scan")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Sampling: {'ON (' + mode + ' AI)' if sample else 'off (metadata only)'}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name}...")
    try:
        tables = extract_metadata(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    if schemas:
        allow = [s.strip() for s in schemas.split(',')]
        tables = [t for t in tables if t.schema in allow]

    if custom_cats:
        click.echo(f"Loaded {len(custom_cats)} custom PII pattern(s) from config.")
    findings = scan_tables(tables, extra_categories=custom_cats)

    # Suppress known-safe columns before anything else (so they are never
    # sampled, reported, gated on, or written to the baseline).
    if allowlist:
        findings, suppressed = apply_allowlist(findings, allowlist)
        if suppressed:
            click.echo(f"Suppressed {suppressed} finding(s) via the {len(allowlist)}-entry allowlist.")

    affected = len({(f.schema, f.table) for f in findings})
    click.echo(f"Flagged {len(findings)} column(s) across {affected} table(s).")

    # Optional AI data sampling reads real values (which may be actual PII).
    if sample and findings:
        click.echo(
            "\nWARNING: --sample reads up to 5 real values from each flagged column\n"
            "         to let the AI confirm findings. Values are used only for\n"
            "         confidence scoring and are never stored."
        )
        if mode == 'cloud':
            click.echo(
                "         In cloud mode these sampled values (possibly real PII) are\n"
                "         sent to Anthropic. Use --mode local to keep sampling on-network."
            )
        if yes:
            click.echo("Proceeding with data sampling (confirmed via --yes).")
            do_sample = True
        else:
            do_sample = click.confirm("Proceed with data sampling?", default=False)
        if do_sample:
            def progress(i, total, f):
                if i == 1 or i % 10 == 0 or i == total:
                    click.echo(f"  [{i}/{total}] sampling {f.schema}.{f.table}.{f.column}")
            try:
                confirm_with_sampling(findings, conn_str, mode, model, progress=progress)
            except Exception as e:
                click.echo(f"Sampling failed: {e}; continuing with name/type findings.", err=True)
        else:
            click.echo("Skipping data sampling; using name/type findings.")

    # Confidence gate: drop weak matches (applied after any sampling, so an AI
    # confirmation can rescue a low-confidence name-only match, and an AI "not
    # PII" verdict removes a false positive).
    if confidence_threshold:
        findings, dropped = filter_by_confidence(findings, confidence_threshold)
        if dropped:
            click.echo(f"Dropped {dropped} finding(s) below confidence threshold {confidence_threshold:.2f}.")

    # PII drift: diff this scan's findings against the previous baseline, then
    # overwrite it (mirrors schema change detection, but for regulated data).
    drift = None
    if not no_baseline:
        base_path = baseline or os.path.join('.sqldoc-pii-snapshots', _safe_filename(database) + '.json')
        current = findings_snapshot(database, findings)
        previous = load_snapshot(base_path)
        if previous is None:
            click.echo(f"\nNo previous scan at {base_path} - saving baseline for drift detection.")
        else:
            drift = diff_findings(previous, current)
            print_pii_diff(drift, base_path)
        save_snapshot(current, base_path)

    click.echo("\nRendering report...")
    render_pii_html(database, findings, output, sampled=sample)
    _verify_offline(output, resolve('verify_offline', verify_offline))
    if sarif:
        render_sarif(database, findings, sarif)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(findings_json(database, findings, sampled=sample), f, indent=2, default=str)
        click.echo(f"Machine-readable findings written to {json_out}")

    s = summarize(findings)
    click.echo(
        click.style(f"\nHIGH: {s['by_risk']['HIGH']}", fg='red', bold=True)
        + click.style(f"    MEDIUM: {s['by_risk']['MEDIUM']}", fg='yellow')
        + f"    LOW: {s['by_risk']['LOW']}"
    )
    click.echo(f"Open {output} in your browser for the full compliance report.")

    # CI gate: exit non-zero so a pipeline can fail on regulated-data exposure.
    gate_msg = None
    if fail_on == 'high':
        n = s['by_risk']['HIGH']
        if n:
            gate_msg = f"{n} HIGH-risk finding(s) present"
    elif fail_on == 'new-high':
        if drift is not None:
            new_high = [k for k in drift['added'] if drift['_new'].get(k, {}).get('risk') == 'HIGH']
            escalated = [c for c in drift['risk_changed'] if c['new'] == 'HIGH']
            count = len(new_high) + len(escalated)
            if count:
                gate_msg = f"{count} new HIGH-risk finding(s) since the baseline"
        # No baseline yet -> nothing to compare; the baseline was just seeded.
    if gate_msg:
        click.echo(click.style(f"\nGATE FAILED: {gate_msg} (--fail-on {fail_on}).", fg='red', bold=True))
        ctx.exit(1)


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to analyze')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string; postgres/mysql planned for v1.5.0)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist for table-scoped checks (default: all)')
@click.option('--output', default='health-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the health report as machine-readable JSON to this path')
@click.option('--top', default=20, type=click.IntRange(1, 500), help='Rows to keep for slow-query + missing-index rankings (default: 20)')
@click.option('--min-fragmentation', 'min_fragmentation', default=10.0, type=click.FloatRange(0.0, 100.0),
              help='Only report indexes fragmented at least this percent (default: 10)')
@click.option('--min-pages', 'min_pages', default=100, type=click.IntRange(1, 10_000_000),
              help='Ignore indexes smaller than this many pages for fragmentation (default: 100)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
def health(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, top, min_fragmentation, min_pages, verify_offline):
    """Analyze database health from SQL Server DMVs.

    Surfaces the slowest cached queries, tables with writes but no reads,
    optimizer-suggested missing indexes, and fragmented indexes — reading only
    server/DB statistics, never table row data. Needs VIEW SERVER STATE; any
    check that lacks permission is skipped and noted in the report.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'health', 'health')
    schemas = resolve('schemas', schemas)
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    top = resolve('top', top)
    min_fragmentation = resolve('min_fragmentation', min_fragmentation, param='min_fragmentation')
    min_pages = resolve('min_pages', min_pages, param='min_pages')

    click.echo(f"\nsqldoc v{__version__}  -  Database health analysis")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and reading system views...")
    try:
        schema_list = [s.strip() for s in schemas.split(',')] if schemas else None
        # Extract the schema too, to drive the metadata-only detectors
        # (duplicate tables, redundant indexes). Degrade gracefully if it fails.
        try:
            tables = extract_metadata(adapter)
        except Exception:
            tables = None
        report = collect_health(adapter, top=int(top),
                                min_fragmentation=float(min_fragmentation),
                                min_pages=int(min_pages), schemas=schema_list,
                                tables=tables)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()
    report.database = database

    for section, msg in report.errors:
        click.echo(click.style(f"  ! skipped {section}: {msg}", fg='yellow'), err=True)

    s = health_summarize(report)
    click.echo(
        click.style(f"Slow queries: {s['slow_queries']}", fg='red')
        + click.style(f"    Dead tables: {s['dead_tables']}", fg='yellow')
        + click.style(f"    Missing indexes: {s['missing_indexes']}", fg='blue')
        + f"    Fragmented: {s['fragmented_indexes']}"
        + f"    Unused procs: {s['unused_procedures']}"
        + f"    Dup tables: {s['duplicate_tables']}"
        + f"    Redundant idx: {s['redundant_indexes']}"
    )

    click.echo("\nRendering report...")
    render_health_html(database, report, output)
    _verify_offline(output, resolve('verify_offline', verify_offline))
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_health_json(database, report), f, indent=2, default=str)
        click.echo(f"Machine-readable health report written to {json_out}")
    click.echo(f"Open {output} in your browser for the full health report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to analyze')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string; postgres/mysql planned for v1.5.0)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist (default: all)')
@click.option('--output', default='quality-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the quality report as machine-readable JSON to this path')
@click.option('--top-values', 'top_values', default=5, type=click.IntRange(0, 50),
              help='Most-frequent values to show per column for the distribution view (0 to skip; default: 5)')
@click.option('--no-duplicates', 'no_duplicates', is_flag=True, default=False,
              help='Skip full-row duplicate detection (the heaviest check)')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the data-read confirmation prompt (for non-interactive use)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
def quality(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, top_values, no_duplicates, yes, verify_offline):
    """Profile data quality: null rates, per-column distribution, duplicates.

    Reads your table data in AGGREGATE only (COUNT / DISTINCT / MIN / MAX /
    GROUP BY); each column's most-frequent values are shown for context. Nothing
    is sent to any AI or off this machine.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'quality', 'quality')
    schemas = resolve('schemas', schemas)
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    top_values = resolve('top_values', top_values, param='top_values')
    no_duplicates = resolve('no_duplicates', no_duplicates, param='no_duplicates')
    yes = resolve('yes', yes)

    click.echo(f"\nsqldoc v{__version__}  -  Data quality profiling")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(
        "NOTE: This reads your table data in aggregate (COUNT / DISTINCT / MIN /\n"
        "      MAX / GROUP BY) and shows each column's most-frequent values. All\n"
        "      processing is local — nothing is sent to any AI or off this machine."
    )
    if not yes and not click.confirm("Proceed with aggregate data profiling?", default=True):
        click.echo("Aborted.")
        raise click.Abort()

    click.echo(f"\nConnecting to {adapter.display_name}...")
    try:
        tables = extract_metadata(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    schema_list = [s.strip() for s in schemas.split(',')] if schemas else None

    def progress(i, total, t):
        if i == 1 or i % 10 == 0 or i == total:
            click.echo(f"  [{i}/{total}] profiling {t.schema}.{t.name}")

    report = collect_quality(adapter, tables, top_values=int(top_values),
                             schemas=schema_list, detect_dupes=not no_duplicates,
                             progress=progress)
    report.database = database

    for c, msg in report.errors:
        click.echo(click.style(f"  ! skipped {c}: {msg}", fg='yellow'), err=True)

    s = quality_summarize(report)
    click.echo(
        click.style(f"\nColumns: {s['columns_profiled']}", fg='blue')
        + click.style(f"    High-null: {s['high_null_columns']}", fg='red')
        + click.style(f"    Constant: {s['constant_columns']}", fg='yellow')
        + f"    Tables with dupes: {s['tables_with_duplicates']}"
    )

    click.echo("\nRendering report...")
    render_quality_html(database, report, output)
    _verify_offline(output, resolve('verify_offline', verify_offline))
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_quality_json(database, report), f, indent=2, default=str)
        click.echo(f"Machine-readable quality report written to {json_out}")
    click.echo(f"Open {output} in your browser for the full data-quality report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to analyze')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string; postgres/mysql planned for v1.5.0)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist (default: all)')
@click.option('--output', default='intel-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the report as machine-readable JSON to this path')
@click.option('--baseline', default=None, help='A prior schema snapshot (JSON) to diff against; enables migration-script generation')
@click.option('--migration-out', 'migration_out', default=None, help='Write the generated migration DDL to this .sql path (requires --baseline)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
def intel(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, baseline, migration_out, verify_offline):
    """Schema intelligence: naming, orphaned FKs, impact analysis, migrations.

    Analyzes the extracted schema (no row data): flags inconsistent naming and
    implied-but-unenforced foreign keys, maps what depends on each table, and —
    with --baseline <snapshot.json> — generates a review-ready migration script
    from the differences.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    schemas = resolve('schemas', schemas)
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')

    click.echo(f"\nsqldoc v{__version__}  -  Schema intelligence")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name}...")
    try:
        tables = extract_metadata(adapter)
        views = extract_views(adapter)
        procedures = extract_procedures(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    if schemas:
        allow = [s.strip() for s in schemas.split(',')]
        tables = [t for t in tables if t.schema in allow]
        views = [v for v in views if v.schema in allow]
        procedures = [p for p in procedures if p.schema in allow]

    baseline_snapshot = None
    if baseline:
        baseline_snapshot = load_snapshot(baseline)
        if baseline_snapshot is None:
            click.echo(f"Baseline snapshot not found at {baseline}; skipping migration generation.", err=True)

    report = collect_intel(database, tables, views=views, procedures=procedures,
                           baseline_snapshot=baseline_snapshot)

    s = intel_summarize(report)
    click.echo(
        click.style(f"Naming issues: {s['naming_issues']}", fg='yellow')
        + click.style(f"    Orphaned FKs: {s['orphan_fks']}", fg='red')
        + click.style(f"    High-impact tables: {s['high_impact_tables']}", fg='magenta')
        + (f"    Migration: generated" if s['has_migration'] else "")
    )

    click.echo("\nRendering report...")
    render_intel_html(database, report, output)
    _verify_offline(output, resolve('verify_offline', verify_offline))
    if migration_out and report.migration_sql:
        with open(migration_out, "w", encoding="utf-8") as f:
            f.write(report.migration_sql)
        click.echo(f"Migration script written to {migration_out}")
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_intel_json(database, report), f, indent=2, default=str)
        click.echo(f"Machine-readable report written to {json_out}")
    click.echo(f"Open {output} in your browser for the full report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to analyze')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string; postgres/mysql planned for v1.5.0)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist (default: all)')
@click.option('--output', default='insights-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the report as machine-readable JSON to this path')
@click.option('--ask', 'ask', multiple=True, help='A plain-English question to turn into a T-SQL query (repeatable)')
@click.option('--no-glossary', 'no_glossary', is_flag=True, default=False, help='Skip the AI business-glossary generation (one AI call per table)')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI mode: local (Ollama) or cloud (Anthropic)')
@click.option('--model', default=None, help='Model to use (default per mode)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip AI parts (NL-to-SQL + glossary); still runs anomaly + relationship analysis')
@click.option('--concurrency', default=8, type=click.IntRange(1, 64), help='Parallel AI calls for glossary generation (default: 8)')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt (for non-interactive use)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
def insights(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, ask, no_glossary, mode, model, no_ai, concurrency, yes, verify_offline):
    """AI-powered schema insights: NL-to-SQL, anomalies, glossary, relationships.

    Turns plain-English questions into T-SQL, flags architectural anomalies,
    infers likely foreign keys, and builds an AI business glossary. Only schema
    metadata (names/types/keys) is ever sent to the AI — never row data. Use
    --no-ai to run just the heuristic anomaly + relationship analysis.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    schemas = resolve('schemas', schemas)
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    no_glossary = resolve('no_glossary', no_glossary, param='no_glossary')
    mode = resolve('mode', mode)
    model = resolve('model', model)
    no_ai = resolve('no_ai', no_ai)
    concurrency = resolve('concurrency', concurrency)
    yes = resolve('yes', yes)

    if mode not in ('local', 'cloud'):
        raise click.UsageError(f"Invalid mode '{mode}' (must be 'local' or 'cloud').")
    if model is None:
        model = 'llama3.1:8b' if mode == 'local' else 'claude-haiku-4-5'
    use_ai = not no_ai

    if use_ai:
        posture = "local (Ollama) - schema metadata only" if mode == "local" else "cloud (Anthropic) - schema metadata sent off-network"
    else:
        posture = "No AI - heuristic analysis only, nothing leaves this machine"

    click.echo(f"\nsqldoc v{__version__}  -  AI schema insights")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Mode:     {'No AI' if no_ai else mode}")
    click.echo(f"Privacy:  {posture}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    # Cloud egress guard (same posture as `doc`): only schema metadata is sent.
    if use_ai and mode == "cloud":
        click.echo(
            "WARNING: Cloud mode sends schema metadata (table/column names, data\n"
            "         types, keys) and your questions to Anthropic's API. No table\n"
            "         row data is read or sent. Use --mode local to stay on-network."
        )
        if yes:
            click.echo("Proceeding with cloud mode (confirmed via --yes).")
        elif not click.confirm("Proceed with cloud mode?", default=False):
            click.echo("Aborted. Re-run with --mode local to stay on-network.")
            raise click.Abort()

    click.echo(f"Connecting to {adapter.display_name}...")
    try:
        tables = extract_metadata(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    if schemas:
        allow = [s.strip() for s in schemas.split(',')]
        tables = [t for t in tables if t.schema in allow]

    if use_ai and (ask or not no_glossary):
        click.echo(f"Running AI analysis ({mode} mode)...")
    report = collect_insights(database, tables, questions=list(ask), use_ai=use_ai,
                              glossary=not no_glossary, mode=mode, model=model,
                              concurrency=concurrency)

    for c, msg in report.errors:
        click.echo(click.style(f"  ! {c}: {msg}", fg='yellow'), err=True)

    s = insights_summarize(report)
    click.echo(
        click.style(f"Anomalies: {s['anomalies']}", fg='red')
        + click.style(f"    Suggested FKs: {s['relationships']}", fg='blue')
        + click.style(f"    Glossary terms: {s['glossary_terms']}", fg='magenta')
        + f"    Queries: {s['queries']}"
    )

    click.echo("\nRendering report...")
    render_insights_html(database, report, output)
    _verify_offline(output, resolve('verify_offline', verify_offline))
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_insights_json(database, report), f, indent=2, default=str)
        click.echo(f"Machine-readable report written to {json_out}")
    click.echo(f"Open {output} in your browser for the full insights report.")


def _comply_all_databases(cfg, resolve, output, json_out, schemas, custom_cats,
                          allowlist, verify_offline):
    """Board-level cross-database access report driven by the .sqldoc.yml
    'databases:' list. Each database is audited independently (a failure on one
    is recorded, not fatal), then merged into one principal x database matrix."""
    db_entries = cfg.get('databases')
    if not db_entries or not isinstance(db_entries, list):
        raise click.UsageError(
            "--all-databases needs a 'databases:' list in your .sqldoc.yml — each "
            "entry a mapping with a name + connection_string (or "
            "server/database/username/password) and an optional dialect.")

    click.echo(f"\nsqldoc v{__version__}  -  Cross-database compliance report")
    click.echo(f"{'='*44}")
    click.echo(f"Databases: {len(db_entries)}")
    click.echo(f"Output:    {output}")
    click.echo(f"{'='*44}\n")

    schema_allow = [s.strip() for s in schemas.split(',')] if schemas else None
    db_access_list = []
    for entry in db_entries:
        name, adapter, _conn_str = _adapter_from_db_entry(entry)
        click.echo(f"[{name}] connecting to {adapter.display_name}...")
        try:
            tables = extract_metadata(adapter)
            if schema_allow:
                tables = [t for t in tables if t.schema in schema_allow]
            findings = scan_tables(tables, extra_categories=custom_cats)
            if allowlist:
                findings, _suppressed = apply_allowlist(findings, allowlist)
            if adapter.capabilities.access_audit:
                permissions = comply_extract_permissions(adapter)
                role_members = comply_extract_role_members(adapter)
            else:
                permissions, role_members = [], []
                click.echo(click.style(
                    f"  ! [{name}] access audit not available on {adapter.display_name}; "
                    f"grants skipped.", fg='yellow'), err=True)
            da = collect_database_access(name, findings, permissions, role_members)
            click.echo(f"  [{name}] {len(da.principals)} principal(s), {len(findings)} PII finding(s).")
        except Exception as e:
            da = DatabaseAccess(database=name, error=f"{type(e).__name__}: {e}")
            click.echo(click.style(f"  ! [{name}] failed: {e}", fg='yellow'), err=True)
        db_access_list.append(da)

    report = build_cross_db(db_access_list)
    s = summarize_multi(report)
    click.echo(
        click.style(f"\nPrincipals: {s['principals']}", fg='cyan')
        + f"    Cross-DB: {s['cross_db_principals']}"
        + click.style(f"    With PII access: {s['principals_with_pii']}", fg='yellow')
        + click.style(f"    HIGH-risk: {s['high_risk_principals']}", fg='red')
    )

    click.echo("\nRendering report...")
    render_multi_comply_html(report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_multi_comply_json(report), f, indent=2, default=str)
        click.echo(f"Machine-readable report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the cross-database compliance report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to analyze')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string; postgres/mysql planned for v1.5.0)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist (default: all)')
@click.option('--output', default='compliance-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the report as machine-readable JSON to this path')
@click.option('--no-access-audit', 'no_access_audit', is_flag=True, default=False,
              help='Skip reading sys.database_permissions (use if the account lacks VIEW DEFINITION)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
@click.option('--all-databases', 'all_databases', is_flag=True, default=False,
              help='Board-level report: audit every database in the .sqldoc.yml "databases:" list and show each user/role and their access across all of them side by side')
def comply(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, no_access_audit, verify_offline, all_databases):
    """Compliance reports: HIPAA/GDPR/PCI-DSS scope, data lineage, access audit.

    Groups the PII scan findings by regulation (with the controls each requires),
    traces data lineage through view/procedure definitions, and cross-references
    object grants (sys.database_permissions) against tables holding regulated
    data. Reads schema + catalog metadata only — never table row data.

    With --all-databases, reads the connection strings under the .sqldoc.yml
    "databases:" list and renders ONE cross-database access matrix (every
    principal x every database) instead of the single-database report.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    schemas = resolve('schemas', schemas)
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')

    try:
        custom_cats = load_custom_categories(cfg.get('pii_patterns'))
    except ValueError as e:
        raise click.UsageError(f"Invalid pii_patterns in config: {e}")
    allowlist = cfg.get('pii_allowlist') or []
    if not isinstance(allowlist, list):
        raise click.UsageError("pii_allowlist in config must be a list of column patterns.")

    all_databases = resolve('all_databases', all_databases)
    if all_databases:
        _comply_all_databases(cfg, resolve, output, json_out, schemas,
                              custom_cats, allowlist, verify_offline)
        return

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)

    click.echo(f"\nsqldoc v{__version__}  -  Compliance report")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name}...")
    try:
        tables = extract_metadata(adapter)
        views = extract_views(adapter)
        procedures = extract_procedures(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    if schemas:
        allow = [s.strip() for s in schemas.split(',')]
        tables = [t for t in tables if t.schema in allow]
        views = [v for v in views if v.schema in allow]
        procedures = [p for p in procedures if p.schema in allow]

    findings = scan_tables(tables, extra_categories=custom_cats)
    if allowlist:
        findings, suppressed = apply_allowlist(findings, allowlist)
        if suppressed:
            click.echo(f"Suppressed {suppressed} finding(s) via the allowlist.")

    # The access audit reads SQL Server object grants; skip it (with a note) on
    # dialects that don't support it, and when --no-access-audit is given.
    do_access_audit = adapter.capabilities.access_audit and not no_access_audit
    if not no_access_audit and not adapter.capabilities.access_audit:
        click.echo(click.style(
            f"  ! access audit skipped: not available on {adapter.display_name}; "
            f"reporting regulations + lineage only.", fg='yellow'), err=True)
    report = collect_compliance(database, tables, findings, views=views, procedures=procedures,
                                adapter=adapter if do_access_audit else None)

    for section, msg in report.errors:
        click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = comply_summarize(report)
    click.echo(
        click.style(f"HIPAA: {s['hipaa']}", fg='red')
        + click.style(f"    GDPR: {s['gdpr']}", fg='yellow')
        + click.style(f"    PCI-DSS: {s['pci_dss']}", fg='blue')
        + f"    Lineage flows: {s['lineage_flows']}    Access alerts: {s['access_alerts']}"
        + f"    Principals: {s['principals']} ({s['roles']} roles)"
    )

    click.echo("\nRendering report...")
    render_comply_html(database, report, output)
    _verify_offline(output, resolve('verify_offline', verify_offline))
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_comply_json(database, report), f, indent=2, default=str)
        click.echo(f"Machine-readable report written to {json_out}")
    click.echo(f"Open {output} in your browser for the full compliance report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--project-dir', 'project_dir', default=None, help='dbt project directory (default: auto-detect from the current directory)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC/connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected from the connection string)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist (default: all)')
@click.option('--output', default='dbt-docs.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the unified docs as machine-readable JSON to this path')
@click.option('--no-db', 'no_db', is_flag=True, default=False,
              help='Skip the database connection and produce dbt-only docs (no live schema merge)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def dbt(config, project_dir, server, database, username, password, connection_string, dialect,
        schemas, output, json_out, no_db, verify_offline):
    """Unify dbt model metadata with the live database schema.

    Auto-detects a dbt project (dbt_project.yml) in the current directory, reads
    each model's description/columns/tests from the schema.yml files, and — when
    a database connection is available — merges that with the actual columns,
    types, and row counts from the database, flagging documentation gaps and
    drift. Reads schema/catalog metadata only, never table row data.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    project_dir = resolve('project_dir', project_dir)
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    schemas = resolve('schemas', schemas)
    no_db = resolve('no_db', no_db)

    click.echo(f"\nsqldoc v{__version__}  -  dbt + database documentation")
    click.echo(f"{'='*44}")

    # Locate the dbt project.
    found = find_dbt_project(project_dir or '.')
    if not found:
        raise click.UsageError(
            "No dbt project found (no dbt_project.yml in the current directory or "
            "its immediate subdirectories). Pass --project-dir PATH.")
    click.echo(f"dbt project: {found}")
    project = parse_dbt_project(found)
    click.echo(f"Parsed {len(project.models)} model(s) from dbt schema files.")
    for w in project.warnings:
        click.echo(click.style(f"  ! {w}", fg='yellow'), err=True)

    # Optionally merge with the live database schema.
    tables = []
    have_conn = bool(resolve('connection_string', connection_string) or resolve('server', server)
                     or cfg.get('connection_string') or cfg.get('server'))
    if not no_db and have_conn:
        conn_str, database, server = _resolve_connection(
            resolve, server, database, username, password, connection_string, dialect)
        adapter = open_adapter(resolve, conn_str, dialect)
        click.echo(f"Connecting to {adapter.display_name} to merge the live schema...")
        try:
            tables = extract_metadata(adapter)
        except Exception as e:
            click.echo(click.style(f"  ! could not read the database schema ({e}); "
                                   f"producing dbt-only docs.", fg='yellow'), err=True)
            tables = []
        if schemas and tables:
            allow = [s.strip() for s in schemas.split(',')]
            tables = [t for t in tables if t.schema in allow]
    else:
        click.echo("No database connection given - producing dbt-only documentation.")

    doc = dbt_merge(project, tables)
    s = dbt_summarize(doc)
    click.echo(
        click.style(f"Models: {s['models']}", fg='cyan')
        + f"    Matched in DB: {s['matched_in_db']}"
        + f"    Column doc coverage: {s['doc_coverage_pct']}%"
        + click.style(f"    Undocumented DB cols: {s['undocumented_db_columns']}", fg='yellow')
        + click.style(f"    Drift: {s['drifted_columns']}", fg='red')
    )

    click.echo("\nRendering report...")
    render_dbt_html(project.name, doc, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_dbt_json(project.name, doc), f, indent=2, default=str)
        click.echo(f"Machine-readable report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the unified dbt + database docs.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default='master', help='Database to connect through (default: master; server metrics are instance-wide)')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (server monitoring is SQL Server / Azure SQL MI only)')
@click.option('--output', default='server-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the server report as machine-readable JSON to this path')
@click.option('--top', default=10, type=click.IntRange(1, 200), help='How many top running queries to show (default: 10)')
@click.option('--no-jobs', 'no_jobs', is_flag=True, default=False,
              help='Skip SQL Server Agent job monitoring (msdb)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def server(config, server, database, username, password, connection_string, dialect,
           output, json_out, top, no_jobs, verify_offline):
    """Instance-level SQL Server health + SQL Agent jobs.

    Connects at the SQL Server *instance* level (not just one database) and
    reports CPU utilization, memory breakdown (buffer pool / plan cache /
    stolen), disk volume free space + I/O latency, uptime, active connections
    and blocking chains, the top queries running right now, and SQL Server Agent
    job status (last run, failures in the last 24h, long runners, disabled jobs,
    next scheduled run). Reads only server-scoped DMVs + msdb job history —
    never table row data. Needs VIEW SERVER STATE (and msdb access for jobs).
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'server_monitoring', 'server')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    top = resolve('top', top)
    no_jobs = resolve('no_jobs', no_jobs)

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  Server health (instance-level)")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server_name if server_name else '(connection string)'}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and reading server DMVs...")
    try:
        report = collect_server(adapter, top=int(top), include_jobs=not no_jobs)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()
    report.server_name = label

    for section, msg in report.errors:
        click.echo(click.style(f"  ! skipped {section}: {msg}", fg='yellow'), err=True)

    s = server_summarize(report)
    click.echo(
        click.style(f"CPU (SQL): {s['cpu_sql_percent']}%", fg='blue')
        + f"    Memory: {round(s['memory_total_mb']/1024, 1)} GB"
        + f"    Sessions: {s['sessions']}"
        + click.style(f"    Blocking: {s['blocking_chains']}", fg='red' if s['blocking_chains'] else 'green')
        + click.style(f"    Low-disk vols: {s['low_disk_volumes']}", fg='red' if s['low_disk_volumes'] else 'green')
    )
    if not no_jobs:
        click.echo(
            click.style(f"Agent jobs: {s['jobs']}", fg='cyan')
            + click.style(f"    Failed (24h): {s['failed_jobs_24h']}", fg='red' if s['failed_jobs_24h'] else 'green')
            + f"    Long-running: {s['long_running_jobs']}    Disabled: {s['disabled_jobs']}"
        )

    click.echo("\nRendering report...")
    render_server_html(label, report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_server_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable server report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the full server health report.")


class DefaultGroup(click.Group):
    """A group that routes to the `doc` command when invoked with options but no
    subcommand — so `sqldoc --server ...` keeps working alongside `sqldoc scan`."""
    default_command = 'doc'

    def parse_args(self, ctx, args):
        if args and args[0].startswith('-') and args[0] not in ('--help', '-h', '--version'):
            args = [self.default_command, *args]
        return super().parse_args(ctx, args)


@click.group(cls=DefaultGroup)
@click.version_option(__version__, prog_name="sqldoc")
def cli():
    """sqldoc — SQL Server documentation + PII / compliance scanner."""


cli.add_command(main, name='doc')
cli.add_command(scan, name='scan')
cli.add_command(health, name='health')
cli.add_command(quality, name='quality')
cli.add_command(intel, name='intel')
cli.add_command(insights, name='insights')
cli.add_command(comply, name='comply')
cli.add_command(dbt, name='dbt')
cli.add_command(server, name='server')

# The agent subgroup is defined in sqldoc.agent.cli; imported here (after this
# module is otherwise defined) to attach it without a circular import.
from sqldoc.agent.cli import agent as _agent_group  # noqa: E402
cli.add_command(_agent_group, name='agent')


if __name__ == '__main__':
    cli()