import click
import os
import yaml
from dotenv import load_dotenv
import re
from sqldoc import __version__
from sqldoc.extractor import build_connection_string
from sqldoc.adapters import get_adapter, detect_dialect, UnsupportedDialectError, DIALECT_CHOICES
from sqldoc import ai
from sqldoc.ai import enrich_tables, enrich_views, enrich_procedures, load_cache, save_cache
from sqldoc import industry as industry_mod
from sqldoc import audit as audit_mod
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
from sqldoc.intel import (collect_intel, summarize as intel_summarize,
                          collect_linked_servers, summarize_linked)
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
from sqldoc.logs import collect_logs, summarize as logs_summarize
from sqldoc.logs_renderer import render_logs_html, build_logs_json
from sqldoc.secure import collect_security, summarize as secure_summarize
from sqldoc.secure_renderer import render_secure_html, build_secure_json
from sqldoc.backup import collect_backups
from sqldoc import executive as executive_mod
from sqldoc.executive_renderer import render_executive_html
from sqldoc.waits import collect_waits, explain_waits, summarize as waits_summarize
from sqldoc.waits_renderer import render_waits_html, build_waits_json
from sqldoc.ha import collect_ha, summarize as ha_summarize
from sqldoc.ha_renderer import render_ha_html, build_ha_json
from sqldoc.deadlocks import collect_deadlocks, explain_deadlock, summarize as deadlocks_summarize
from sqldoc.deadlocks_renderer import render_deadlocks_html, build_deadlocks_json
from sqldoc.plans import collect_plans, explain_plans, summarize as plans_summarize
from sqldoc.plans_renderer import render_plans_html, build_plans_json
from sqldoc.capacity import project_capacity, summarize as capacity_summarize
from sqldoc.capacity_renderer import render_capacity_html, build_capacity_json
from sqldoc.baseline import (capture_baseline, compare_baseline, to_dict as baseline_to_dict,
                             from_dict as baseline_from_dict, summarize as baseline_summarize)
from sqldoc.baseline_renderer import render_baseline_html, build_baseline_json
from sqldoc.api import make_server as make_api_server, ENDPOINTS as API_ENDPOINTS

load_dotenv()

# Config keys that .sqldoc.yml may set; each maps to the same-named CLI option.
CONFIG_KEYS = {
    'server', 'database', 'username', 'password', 'connection_string', 'dialect', 'output',
    'mode', 'model', 'ai_backend', 'industry', 'schemas', 'no_ai', 'concurrency', 'format',
    'include_definitions',
    'snapshot', 'no_snapshot', 'cache', 'no_cache', 'sample',
    'baseline', 'no_baseline', 'sarif', 'json', 'pii_patterns', 'pii_allowlist',
    'confidence_threshold', 'fail_on', 'yes',
    'top', 'min_fragmentation', 'min_pages',
    'top_values', 'no_duplicates', 'no_glossary',
    'verify_offline',
    'project_dir', 'no_db',
    'databases', 'all_databases',
    'api_key', 'api', 'host', 'port', 'tenants', 'auth',
    'agent',
    # Integration suite: each connector owns a top-level config section.
    'sharepoint', 'confluence', 'notion', 'gdrive', 'box', 'jira',
    'servicenow', 'azuredevops', 'powerbi', 'webhook', 'webex',
    'test', 'push', 'kinds',
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


_PROVIDER_NAMES = {'anthropic': 'Anthropic', 'openai': 'OpenAI', 'gemini': 'Google Gemini'}


def resolve_ai_backend(resolve, ai_backend):
    """Resolve --ai-backend (flag > config), record it process-wide via
    ai.set_backend so every AI feature routes to the chosen backend, and return
    it (or None to derive from --mode)."""
    backend = resolve('ai_backend', ai_backend, param='ai_backend')
    try:
        ai.set_backend(backend)
    except ValueError as e:
        raise click.UsageError(str(e))
    return backend


def ai_backend_option(fn):
    """Shared --ai-backend option for AI-consuming commands."""
    return click.option(
        '--ai-backend', 'ai_backend', default=None,
        type=click.Choice(list(ai.ALL_BACKENDS)),
        help='AI backend: ollama (local) / anthropic / openai / gemini '
             '(default: derived from --mode). Cloud backends need the matching '
             'API key: ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY.')(fn)


def industry_option(fn):
    """Shared --industry option for vertical-tuned commands."""
    return click.option(
        '--industry', 'industry', default=None,
        type=click.Choice(list(industry_mod.INDUSTRY_CHOICES)),
        help='Tune AI descriptions, PII sensitivity, and compliance focus for a '
             'vertical: healthcare (HIPAA/PHI) / finance (PCI-DSS + SOX) / retail '
             '(PCI-DSS + GDPR) / government (FedRAMP + retention).')(fn)


def resolve_industry(resolve, industry):
    """Resolve --industry (flag > config), record its AI guidance process-wide
    via ai.set_industry_guidance, echo the vertical note, and return the
    IndustryProfile (or None)."""
    key = resolve('industry', industry, param='industry')
    try:
        profile = industry_mod.get_industry(key)
    except ValueError as e:
        raise click.UsageError(str(e))
    ai.set_industry_guidance(profile.ai_guidance if profile else "")
    if profile:
        click.echo(click.style(f"Industry: {profile.label}", fg='cyan')
                   + f"  -  {profile.pii_note}")
    return profile


def default_ai_model(mode, ai_backend, model):
    """Resolve the model when --model wasn't given: use the effective backend's
    default (so gpt-4o/gemini-1.5-flash apply instead of a leaked llama tag)."""
    if model is not None:
        return model
    return ai.default_model(ai.resolve_backend(mode, ai_backend))


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


def load_tenants(cfg):
    """Build the multi-tenant registry {api_key: tenant_ctx} from the config's
    `tenants:` list. Each entry: name + api_key + connection settings (a
    connection_string or server/database/username/password) + optional dialect/
    mode/model. Every tenant gets an isolated context; keys must be unique."""
    raw = cfg.get('tenants')
    if not raw:
        raise click.UsageError(
            "--multi-tenant needs a 'tenants:' list in .sqldoc.yml. Each entry needs "
            "a name, an api_key, and a connection (connection_string or "
            "server/database/username/password).")
    if not isinstance(raw, list):
        raise click.UsageError("'tenants:' must be a list of tenant mappings.")
    registry = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise click.UsageError("Each 'tenants:' entry must be a mapping.")
        name = entry.get('name')
        key = entry.get('api_key')
        if not name or not key:
            raise click.UsageError("Each tenant needs both a 'name' and an 'api_key'.")
        if key in registry:
            raise click.UsageError(
                f"Duplicate tenant api_key (tenants must have distinct keys): tenant '{name}'.")
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
                    f"Tenant '{name}' is missing connection settings: {', '.join(missing)} "
                    f"(or provide a connection_string).")
            conn_str = build_connection_string(server, database, username, password)
        database = entry.get('database') or _parse_database(conn_str) or name
        registry[key] = {
            "name": name, "conn_str": conn_str, "dialect": entry.get('dialect'),
            "database": database, "mode": entry.get('mode', 'local'),
            "model": entry.get('model'),
        }
    return registry


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
@click.option('--model', default=None, help='Model to use (default: per backend — llama3.1:8b / claude-haiku-4-5 / gpt-4o / gemini-1.5-flash)')
@ai_backend_option
@industry_option
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
def main(config, server, database, username, password, connection_string, dialect, output, output_format, mode, model, ai_backend, industry, schemas, no_ai, concurrency, include_definitions, snapshot, no_snapshot, cache, no_cache, yes, verify_offline):
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
    ai_backend = resolve_ai_backend(resolve, ai_backend)
    industry = resolve_industry(resolve, industry)
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
    # for every backend without a local default (llama tag) leaking into a cloud call.
    model = default_ai_model(mode, ai_backend, model)
    effective_backend = ai.resolve_backend(mode, ai_backend)
    is_cloud = effective_backend in ai.CLOUD_BACKENDS
    provider = _PROVIDER_NAMES.get(effective_backend, 'the cloud provider')

    # Describe the data-egress posture for the chosen backend. --include-definitions
    # widens what is sent to the AI beyond schema metadata to the actual SQL
    # bodies of views/procedures/triggers.
    payload = "schema metadata + SQL definitions" if include_definitions else "schema metadata"
    if no_ai:
        privacy = "No AI - schema only, nothing leaves this machine"
    elif not is_cloud:
        privacy = f"local (Ollama) - {payload}, no data leaves this network"
    else:
        privacy = f"cloud ({provider}) - {payload} sent off-network"

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
    if not no_ai and is_cloud:
        click.echo(
            f"WARNING: Cloud mode sends schema metadata (table names, column names,\n"
            f"         data types, keys, and row counts) to {provider}'s API. No table\n"
            f"         row data is ever read or sent. Use --mode local to keep\n"
            f"         everything on this network."
        )
        if include_definitions:
            click.echo(
                f"         --include-definitions ALSO sends the SQL bodies of your\n"
                f"         views, stored procedures, and triggers to {provider}. These\n"
                f"         definitions can embed literals, comments, or business logic -\n"
                f"         review them before enabling this in cloud mode."
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
@ai_backend_option
@industry_option
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
def scan(config, server, database, username, password, connection_string, dialect, schemas, output, sample, mode, model, ai_backend, industry, baseline, no_baseline, sarif, json_out, confidence_threshold, fail_on, yes, verify_offline):
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
    ai_backend = resolve_ai_backend(resolve, ai_backend)
    industry = resolve_industry(resolve, industry)
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
    model = default_ai_model(mode, ai_backend, model)

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

    # Vertical tuning: escalate the risk of categories most sensitive to the
    # chosen industry (e.g. PHI identifiers under healthcare) and tag them with
    # its flagship regulation, before drift/gate/report.
    if industry:
        industry_mod.apply_to_findings(findings, industry)

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
        if ai.is_cloud_backend(mode, ai_backend):
            provider = _PROVIDER_NAMES.get(ai.resolve_backend(mode, ai_backend), 'the cloud provider')
            click.echo(
                f"         In cloud mode these sampled values (possibly real PII) are\n"
                f"         sent to {provider}. Use --mode local to keep sampling on-network."
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


@click.command('scan-files')
@click.argument('files', nargs=-1, required=True, type=click.Path())
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--fail-on', 'fail_on', type=click.Choice(['none', 'high']), default='none',
              help='Exit non-zero if any (high) HIGH-risk PII column is found (for the pre-commit hook / CI)')
@click.option('--json', 'json_out', default=None, help='Write machine-readable findings to this path')
def scan_files(files, config, fail_on, json_out):
    """Scan .sql DDL FILES for likely-PII columns (no database connection).

    Parses column definitions out of CREATE TABLE / ALTER TABLE ... ADD in the
    given files and runs them through the same PII matcher as `sqldoc scan`.
    Used by the `sqldoc install-hooks` pre-commit hook to gate staged SQL, but
    runs standalone too. Honours the `pii_patterns:` / `pii_allowlist:` config.
    """
    from sqldoc.pii import scan_sql_files
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')

    try:
        custom_cats = load_custom_categories(cfg.get('pii_patterns'))
    except ValueError as e:
        raise click.UsageError(f"Invalid pii_patterns in config: {e}")
    allowlist = cfg.get('pii_allowlist') or []
    if not isinstance(allowlist, list):
        raise click.UsageError("pii_allowlist in config must be a list of column patterns.")

    findings = scan_sql_files(files, extra_categories=custom_cats)
    if allowlist:
        findings, suppressed = apply_allowlist(findings, allowlist)
        if suppressed:
            click.echo(f"Allowlist suppressed {suppressed} finding(s).")

    if not findings:
        click.echo(click.style(f"sqldoc scan-files: no PII columns found in {len(files)} file(s).", fg='green'))
    else:
        by_file = {}
        for f in findings:
            by_file.setdefault(f.schema, []).append(f)
        for path, fs in by_file.items():
            click.echo(f"\n{path}:")
            for f in sorted(fs, key=lambda x: -{'HIGH': 2, 'MEDIUM': 1, 'LOW': 0}.get(x.risk, 0)):
                colr = {'HIGH': 'red', 'MEDIUM': 'yellow', 'LOW': None}.get(f.risk)
                click.echo("  " + click.style(f"[{f.risk}]", fg=colr, bold=(f.risk == 'HIGH'))
                           + f" {f.table}.{f.column}  {f.category}  ({', '.join(f.regulations)})")
        s = summarize(findings)
        click.echo(
            click.style(f"\nHIGH: {s['by_risk']['HIGH']}", fg='red', bold=True)
            + click.style(f"    MEDIUM: {s['by_risk']['MEDIUM']}", fg='yellow')
            + f"    LOW: {s['by_risk']['LOW']}")

    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(findings_json("staged-sql", findings), f, indent=2, default=str)
        click.echo(f"Machine-readable findings written to {json_out}")

    if fail_on == 'high':
        n = summarize(findings)['by_risk']['HIGH']
        if n:
            click.echo(click.style(
                f"\nGATE FAILED: {n} HIGH-risk PII column(s) in staged SQL (--fail-on high).",
                fg='red', bold=True))
            ctx.exit(1)


@click.command('install-hooks')
@click.option('--repo', default='.', type=click.Path(), help='Repository root (default: current directory)')
@click.option('--force', is_flag=True, default=False, help='Overwrite an existing non-sqldoc pre-commit hook')
def install_hooks(repo, force):
    """Install a git pre-commit hook that PII-scans staged .sql files.

    The hook runs `sqldoc scan-files --fail-on high` over any staged .sql files
    and blocks the commit if a HIGH-risk PII column (e.g. an unflagged `ssn` or
    `credit_card`) is being introduced. Bypass a single commit with
    `git commit --no-verify`.
    """
    from sqldoc.hooks import install_hooks as _install
    result = _install(repo, force=force)
    if result["status"] == "installed":
        click.echo(click.style(result["message"], fg='green'))
        click.echo("Staged .sql files will now be PII-scanned before each commit.")
    elif result["status"] == "exists":
        click.echo(click.style(result["message"], fg='yellow'), err=True)
        raise click.exceptions.Exit(1)
    else:
        raise click.UsageError(result["message"])


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
@click.option('--linked-servers', 'linked_servers', is_flag=True, default=False,
              help='Discover linked servers (sys.servers), their security config, and test connectivity (SQL Server only)')
@click.option('--traverse-linked-servers', 'traverse_linked', is_flag=True, default=False,
              help='Also probe each reachable linked server for a version/health check (implies --linked-servers)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
def intel(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, baseline, migration_out, linked_servers, traverse_linked, verify_offline):
    """Schema intelligence: naming, orphaned FKs, impact analysis, migrations, linked servers.

    Analyzes the extracted schema (no row data): flags inconsistent naming and
    implied-but-unenforced foreign keys, maps what depends on each table, and —
    with --baseline <snapshot.json> — generates a review-ready migration script
    from the differences. With --linked-servers, also discovers linked servers
    (sys.servers), maps their security config, and tests connectivity;
    --traverse-linked-servers additionally probes each reachable one.
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

    # Linked-server network mapping (SQL Server only).
    if linked_servers or traverse_linked:
        if not adapter.capabilities.server_monitoring:
            click.echo(click.style(
                f"  ! linked-server discovery skipped: not available on "
                f"{adapter.display_name} (SQL Server only).", fg='yellow'), err=True)
        else:
            click.echo("Discovering linked servers...")
            report.linked_servers = collect_linked_servers(adapter, traverse=traverse_linked)
            for section, msg in report.linked_servers.errors:
                click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = intel_summarize(report)
    click.echo(
        click.style(f"Naming issues: {s['naming_issues']}", fg='yellow')
        + click.style(f"    Orphaned FKs: {s['orphan_fks']}", fg='red')
        + click.style(f"    High-impact tables: {s['high_impact_tables']}", fg='magenta')
        + (f"    Migration: generated" if s['has_migration'] else "")
    )
    if report.linked_servers is not None:
        ls = summarize_linked(report.linked_servers)
        click.echo(
            click.style(f"Linked servers: {ls['linked_servers']}", fg='blue')
            + click.style(f"    Reachable: {ls['reachable']}", fg='green')
            + click.style(f"    Unreachable: {ls['unreachable']}", fg='red' if ls['unreachable'] else 'green')
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
@ai_backend_option
@industry_option
@click.option('--no-ai', is_flag=True, default=False, help='Skip AI parts (NL-to-SQL + glossary); still runs anomaly + relationship analysis')
@click.option('--concurrency', default=8, type=click.IntRange(1, 64), help='Parallel AI calls for glossary generation (default: 8)')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt (for non-interactive use)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
def insights(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, ask, no_glossary, mode, model, ai_backend, industry, no_ai, concurrency, yes, verify_offline):
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
    ai_backend = resolve_ai_backend(resolve, ai_backend)
    industry = resolve_industry(resolve, industry)
    no_ai = resolve('no_ai', no_ai)
    concurrency = resolve('concurrency', concurrency)
    yes = resolve('yes', yes)

    if mode not in ('local', 'cloud'):
        raise click.UsageError(f"Invalid mode '{mode}' (must be 'local' or 'cloud').")
    model = default_ai_model(mode, ai_backend, model)
    use_ai = not no_ai
    is_cloud = ai.is_cloud_backend(mode, ai_backend)
    provider = _PROVIDER_NAMES.get(ai.resolve_backend(mode, ai_backend), 'the cloud provider')

    if use_ai:
        posture = "local (Ollama) - schema metadata only" if not is_cloud else f"cloud ({provider}) - schema metadata sent off-network"
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
    if use_ai and is_cloud:
        click.echo(
            f"WARNING: Cloud mode sends schema metadata (table/column names, data\n"
            f"         types, keys) and your questions to {provider}'s API. No table\n"
            f"         row data is read or sent. Use --mode local to stay on-network."
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
@industry_option
@click.option('--no-access-audit', 'no_access_audit', is_flag=True, default=False,
              help='Skip reading sys.database_permissions (use if the account lacks VIEW DEFINITION)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained (no external references) for air-gapped use')
@click.option('--all-databases', 'all_databases', is_flag=True, default=False,
              help='Board-level report: audit every database in the .sqldoc.yml "databases:" list and show each user/role and their access across all of them side by side')
def comply(config, server, database, username, password, connection_string, dialect, schemas, output, json_out, industry, no_access_audit, verify_offline, all_databases):
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
    industry = resolve_industry(resolve, industry)
    if industry:
        click.echo(click.style(f"Compliance focus: {industry.compliance_focus}", fg='cyan'))

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
    if industry:
        industry_mod.apply_to_findings(findings, industry)
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
    _require_capability(adapter, 'infra_monitoring', 'server')
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
    if report.dialect in ('sqlserver', 'azuresql', 'azure_managed_instance'):
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
        if report.tempdb:
            click.echo(
                click.style(f"TempDB: version store {s['tempdb_version_store_mb']} MB", fg='magenta')
                + f"    Data files: {s['tempdb_data_files']}"
                + click.style(f"    Page contention: {s['tempdb_contention']}",
                              fg='red' if s['tempdb_contention'] else 'green')
            )
    if report.backups:
        click.echo(
            click.style(f"Backups: {s['backup_databases']} db(s)", fg='cyan')
            + click.style(f"    PITR: {'on' if s['pitr_enabled'] else 'off'}",
                          fg='green' if s['pitr_enabled'] else 'red')
            + click.style(f"    Never backed up: {s['never_backed_up']}", fg='red' if s['never_backed_up'] else 'green')
            + click.style(f"    With issues: {s['backup_issues']}", fg='yellow' if s['backup_issues'] else 'green')
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


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default='master', help='Database to connect through (default: master)')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (ERRORLOG reading is SQL Server only)')
@click.option('--log-number', 'log_number', default=0, type=click.IntRange(0, 99),
              help='Which ERRORLOG archive to read (0 = current; default: 0)')
@click.option('--search', default=None, help='Only return log entries containing this text (server-side)')
@click.option('--severity', default=None, type=click.IntRange(0, 25),
              help='Only show entries at or above this severity level (e.g. 17 for serious errors)')
@click.option('--last-hours', 'last_hours', default=None, type=click.IntRange(1, 8760),
              help='Only return entries from the last N hours')
@click.option('--output', default='errorlog-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the log report as machine-readable JSON to this path')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def logs(config, server, database, username, password, connection_string, dialect,
         log_number, search, severity, last_hours, output, json_out, verify_offline):
    """Read the SQL Server ERRORLOG and highlight critical events.

    Reads sys.xp_readerrorlog with optional --search / --severity / --last-hours
    filtering, and automatically flags corruption, deadlocks, memory pressure,
    disk-full, and login-failure entries. Reads log text only — never table row
    data. Needs the EXEC right on xp_readerrorlog (typically sysadmin).
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'server_monitoring', 'logs')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  SQL Server error log")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server_name if server_name else '(connection string)'}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and reading the ERRORLOG...")
    try:
        report = collect_logs(adapter, log_number=int(log_number), search=search,
                              severity=severity, last_hours=last_hours)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    for section, msg in report.errors:
        click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = logs_summarize(report)
    cats = ", ".join(f"{k}:{v}" for k, v in s['by_category'].items()) or "none"
    click.echo(
        click.style(f"Entries: {s['entries']}", fg='blue')
        + click.style(f"    Critical: {s['critical']}", fg='red' if s['critical'] else 'green')
        + click.style(f"    Severity 17+: {s['high_severity']}", fg='red' if s['high_severity'] else 'green')
        + f"    Categories: {cats}"
    )

    click.echo("\nRendering report...")
    render_logs_html(label, report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_logs_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable log report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the full error-log report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default='master', help='Database to connect through')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (default: auto-detected; supported: SQL Server, PostgreSQL, MySQL)')
@click.option('--output', default='security-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the security report as machine-readable JSON to this path')
@click.option('--fail-under', 'fail_under', default=None, type=click.IntRange(0, 100),
              help='Exit non-zero if the security score is below this threshold (for CI gating)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def secure(config, server, database, username, password, connection_string, dialect,
           output, json_out, fail_under, verify_offline):
    """Scan for security misconfigurations and score them 0-100.

    Runs dialect-aware hardening checks (SQL Server / PostgreSQL / MySQL) and
    reports HIGH/MEDIUM/LOW findings with a unified 0-100 security score. Reads
    only server configuration + catalog metadata — never table row data.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'infra_monitoring', 'secure')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  Security scan")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server_name if server_name else '(connection string)'}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and running hardening checks...")
    try:
        report = collect_security(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    for section, msg in report.errors:
        click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = secure_summarize(report)
    score_color = 'green' if s['score'] >= 75 else ('yellow' if s['score'] >= 40 else 'red')
    click.echo(
        click.style(f"Security score: {s['score']}/100 (grade {s['grade']})", fg=score_color)
        + click.style(f"    HIGH: {s['high']}", fg='red')
        + click.style(f"    MEDIUM: {s['medium']}", fg='yellow')
        + click.style(f"    LOW: {s['low']}", fg='blue')
    )

    click.echo("\nRendering report...")
    render_secure_html(label, report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_secure_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable security report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the full security report.")

    if fail_under is not None and s['score'] < int(fail_under):
        click.echo(click.style(
            f"\nSecurity score {s['score']} is below the --fail-under threshold {fail_under}.", fg='red'), err=True)
        raise SystemExit(1)


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='Database hostname or IP')
@click.option('--database', default='master', help='Database to connect through')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (SQL Server / PostgreSQL / MySQL)')
@click.option('--output', default='waits-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the wait report as machine-readable JSON to this path')
@click.option('--top', default=15, type=click.IntRange(1, 100), help='How many top wait types to show (default: 15)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip the AI explanation of the top waits')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI backend for the explanation')
@click.option('--model', default=None, help='Model to use (default per mode)')
@ai_backend_option
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def waits(config, server, database, username, password, connection_string, dialect,
          output, json_out, top, no_ai, mode, model, ai_backend, yes, verify_offline):
    """Analyze what the server is waiting on and explain it with AI.

    Reads wait statistics (SQL Server sys.dm_os_wait_stats / PostgreSQL
    pg_stat_activity+pg_locks / MySQL performance_schema), categorizes them into
    IO / Lock / Memory / CPU / Network, and (unless --no-ai) asks an LLM to
    explain the top waits and suggest fixes. Only wait-type names + percentages
    are sent to the AI — never table row data.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'infra_monitoring', 'waits')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    top = resolve('top', top)
    no_ai = resolve('no_ai', no_ai)
    mode = resolve('mode', mode)
    model = resolve('model', model)
    ai_backend = resolve_ai_backend(resolve, ai_backend)

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  Wait statistics")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and reading wait stats...")
    try:
        report = collect_waits(adapter, top=int(top))
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    for section, msg in report.errors:
        click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = waits_summarize(report)
    click.echo(
        click.style(f"Wait types: {s['waits']}", fg='blue')
        + f"    Top category: {s['top_category']}"
        + f"    Categories: " + ", ".join(f"{k} {v}%" for k, v in s['category_percent'].items())
    )

    if not no_ai and report.waits:
        if ai.is_cloud_backend(mode, ai_backend):
            provider = _PROVIDER_NAMES.get(ai.resolve_backend(mode, ai_backend), 'the cloud provider')
            click.echo(f"Cloud AI: only wait-type names + percentages are sent to {provider} (no schema, no data).")
            if not yes and not click.confirm("Proceed with cloud mode?", default=False):
                click.echo("Skipping AI (re-run with --mode local or --no-ai).")
                no_ai = True
        if not no_ai:
            click.echo(f"Explaining top waits with AI ({mode})...")
            try:
                report.ai_explanation = explain_waits(report, mode=mode, model=model)
            except Exception as e:
                click.echo(click.style(f"  ! AI explanation skipped: {type(e).__name__}: {e}", fg='yellow'), err=True)

    click.echo("\nRendering report...")
    render_waits_html(label, report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_waits_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable wait report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the full wait-statistics report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='Database hostname or IP')
@click.option('--database', default='master', help='Database to connect through')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (SQL Server / PostgreSQL / MySQL)')
@click.option('--output', default='ha-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the HA report as machine-readable JSON to this path')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def ha(config, server, database, username, password, connection_string, dialect,
       output, json_out, verify_offline):
    """Monitor high-availability / replication: roles, sync state, and lag.

    Reports the Always On availability group / streaming replication / replica
    topology for the connected instance (SQL Server / PostgreSQL / MySQL),
    including each replica's role, synchronization state, and lag. Reads only
    replication catalog metadata — never table row data.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'infra_monitoring', 'ha')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  High-availability monitoring")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and reading replication state...")
    try:
        report = collect_ha(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    for section, msg in report.errors:
        click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = ha_summarize(report)
    if not report.ha_enabled:
        click.echo(click.style(f"No replication configured ({report.mechanism}).", fg='yellow'))
    else:
        lag = f"{s['max_lag_seconds']}s" if s['max_lag_seconds'] is not None else "n/a"
        click.echo(
            click.style(f"Replicas: {s['replicas']}", fg='blue')
            + click.style(f"    Unhealthy: {s['unhealthy']}", fg='red' if s['unhealthy'] else 'green')
            + f"    Max lag: {lag}"
        )

    click.echo("\nRendering report...")
    render_ha_html(label, report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_ha_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable HA report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the full HA report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='Database hostname or IP')
@click.option('--database', default='master', help='Database to connect through')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (SQL Server / PostgreSQL / MySQL)')
@click.option('--output', default='deadlocks-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the deadlock report as machine-readable JSON to this path')
@click.option('--no-ai', is_flag=True, default=False, help='Skip the AI explanation of the deadlock')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI backend for the explanation')
@click.option('--model', default=None, help='Model to use (default per mode)')
@ai_backend_option
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def deadlocks(config, server, database, username, password, connection_string, dialect,
              output, json_out, no_ai, mode, model, ai_backend, yes, verify_offline):
    """Find and visualize deadlocks, with an AI explanation of the cause and fix.

    SQL Server parses deadlock graphs from the system_health extended-events
    session; PostgreSQL reports the pg_stat_database deadlock count + current
    blocking chains; MySQL reports the ER_LOCK_DEADLOCK error count. Deadlock
    graphs are drawn as SVG wait-for diagrams. With AI (default), explains the
    cyclic dependency and how to prevent it (this sends the deadlock's SQL to
    the model).
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'infra_monitoring', 'deadlocks')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    no_ai = resolve('no_ai', no_ai)
    mode = resolve('mode', mode)
    model = resolve('model', model)
    ai_backend = resolve_ai_backend(resolve, ai_backend)

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  Deadlock analysis")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and looking for deadlocks...")
    try:
        report = collect_deadlocks(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    for section, msg in report.errors:
        click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = deadlocks_summarize(report)
    click.echo(
        click.style(f"Deadlocks recorded: {s['total_count']}", fg='red' if s['total_count'] else 'green')
        + f"    Graphs: {s['graph_events']}"
        + (f"    Current blocking: {s['current_blocking']}" if s['current_blocking'] else "")
    )

    graph_events = [e for e in report.events if e.processes]
    if not no_ai and graph_events:
        if ai.is_cloud_backend(mode, ai_backend):
            provider = _PROVIDER_NAMES.get(ai.resolve_backend(mode, ai_backend), 'the cloud provider')
            click.echo(f"Cloud AI: the deadlock's SQL statements are sent to {provider} for the explanation.")
            if not yes and not click.confirm("Proceed with cloud mode?", default=False):
                click.echo("Skipping AI (re-run with --mode local or --no-ai).")
                no_ai = True
        if not no_ai:
            click.echo(f"Explaining the deadlock with AI ({mode})...")
            try:
                report.ai_explanation = explain_deadlock(report, mode=mode, model=model)
            except Exception as e:
                click.echo(click.style(f"  ! AI explanation skipped: {type(e).__name__}: {e}", fg='yellow'), err=True)

    click.echo("\nRendering report...")
    render_deadlocks_html(label, report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_deadlocks_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable deadlock report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the full deadlock report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='Database hostname or IP')
@click.option('--database', default='master', help='Database to connect through')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (SQL Server / PostgreSQL / MySQL)')
@click.option('--output', default='plans-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the plans report as machine-readable JSON to this path')
@click.option('--top', default=20, type=click.IntRange(1, 100), help='How many worst plans to pull (default: 20)')
@click.option('--explain-top', 'explain_top', default=5, type=click.IntRange(0, 100),
              help='How many of the worst plans to explain with AI (0 to skip; default: 5)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip the AI plan explanations')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI backend for the explanations')
@click.option('--model', default=None, help='Model to use (default per mode)')
@ai_backend_option
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def plans(config, server, database, username, password, connection_string, dialect,
          output, json_out, top, explain_top, no_ai, mode, model, ai_backend, yes, verify_offline):
    """Analyze the worst cached query plans and recommend fixes with AI.

    Pulls the top-N worst-performing cached queries (SQL Server
    dm_exec_query_stats+query_plan / PostgreSQL pg_stat_statements / MySQL
    performance_schema). On SQL Server the XML plan is parsed for anti-patterns
    (table scans, key lookups, implicit conversions, missing indexes, spills).
    AI explains why each is slow and the exact index/rewrite to fix it (the
    query text is sent to the model; no table row data).
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'infra_monitoring', 'plans')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    top = resolve('top', top)
    no_ai = resolve('no_ai', no_ai)
    mode = resolve('mode', mode)
    model = resolve('model', model)
    ai_backend = resolve_ai_backend(resolve, ai_backend)

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  Query plan analysis")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and pulling worst plans...")
    try:
        report = collect_plans(adapter, top=int(top))
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    for section, msg in report.errors:
        click.echo(click.style(f"  ! {section}: {msg}", fg='yellow'), err=True)

    s = plans_summarize(report)
    pats = ", ".join(f"{k}:{v}" for k, v in s['pattern_counts'].items()) or "none"
    click.echo(
        click.style(f"Plans: {s['plans']}", fg='blue')
        + click.style(f"    High-severity: {s['high_severity']}", fg='red' if s['high_severity'] else 'green')
        + f"    Patterns: {pats}"
    )

    if not no_ai and report.plans and int(explain_top) > 0:
        if ai.is_cloud_backend(mode, ai_backend):
            provider = _PROVIDER_NAMES.get(ai.resolve_backend(mode, ai_backend), 'the cloud provider')
            click.echo(f"Cloud AI: the query text + detected plan patterns are sent to {provider}.")
            if not yes and not click.confirm("Proceed with cloud mode?", default=False):
                click.echo("Skipping AI (re-run with --mode local or --no-ai).")
                no_ai = True
        if not no_ai:
            click.echo(f"Explaining the top {explain_top} plan(s) with AI ({mode})...")
            explain_plans(report, mode=mode, model=model, limit=int(explain_top))

    click.echo("\nRendering report...")
    render_plans_html(label, report, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_plans_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable plans report written to {json_out}")
    _verify_offline(output, resolve('verify_offline', verify_offline))
    click.echo(f"Open {output} in your browser for the full query-plan report.")


@click.command()
@click.option('--store', 'store_path', default=None, help='Path to the agent SQLite store (default: ~/.sqldoc/agent.db)')
@click.option('--database', default=None, help='Only project this database (default: all monitored databases)')
@click.option('--output', default='capacity-report.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the capacity report as machine-readable JSON to this path')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def capacity(store_path, database, output, json_out, verify_offline):
    """Project capacity + growth trends from the agent's recorded history.

    Reads the sqldoc agent's metric history and projects: days until disk full,
    days until the database reaches its max size, the fastest-growing tables with
    30/60/90-day projections, and the fragmentation trend — with SVG sparklines.
    Requires the agent to have completed at least two polling cycles.
    """
    from sqldoc.agent.store import AgentStore
    from sqldoc.agent import db_path

    path = store_path or db_path()
    if not os.path.exists(path):
        raise click.UsageError(
            f"Agent store not found at {path}. Start the agent (sqldoc agent start) so it "
            f"records metrics, or pass --store PATH.")
    store = AgentStore(path)
    databases = [database] if database else store.list_databases()

    click.echo(f"\nsqldoc v{__version__}  -  Capacity planning")
    click.echo(f"{'='*44}")
    click.echo(f"Store:     {path}")
    click.echo(f"Databases: {len(databases)}")
    click.echo(f"{'='*44}\n")

    if not databases:
        click.echo(click.style("No monitored databases found in the agent store.", fg='yellow'))

    reports = []
    for name in databases:
        history = store.metrics_history(name)
        table_history = store.table_size_history(name)
        report = project_capacity(name, history, table_history)
        reports.append(report)
        s = capacity_summarize(report)
        if not report.sufficient:
            click.echo(click.style(f"{name}: insufficient history ({report.points} point(s)).", fg='yellow'))
        else:
            dfull = s['disk_days_until_full']
            dmax = s['db_days_until_max']
            click.echo(
                click.style(f"{name}: {report.points} points / {report.span_days}d", fg='cyan')
                + f"    Disk full in: {dfull if dfull is not None else 'n/a'} d"
                + f"    Max size in: {dmax if dmax is not None else 'n/a'} d"
                + click.style(f"    Growing tables: {s['growing_tables']}", fg='blue')
            )

    click.echo("\nRendering report...")
    render_capacity_html(reports, output)
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_capacity_json(reports), f, indent=2, default=str)
        click.echo(f"Machine-readable capacity report written to {json_out}")
    _verify_offline(output, verify_offline)
    click.echo(f"Open {output} in your browser for the full capacity report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='Database hostname or IP')
@click.option('--database', default='master', help='Database to connect through')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES),
              help='Database dialect (SQL Server / PostgreSQL / MySQL)')
@click.option('--capture', is_flag=True, default=False,
              help='Capture the current state as the new baseline (instead of comparing against it)')
@click.option('--baseline-file', 'baseline_file', default=None,
              help='Baseline JSON path (default: .sqldoc-baseline-<database>.json)')
@click.option('--threshold', default=25.0, type=click.FloatRange(0.0, 10000.0),
              help='Flag metrics/queries that regressed more than this percent (default: 25)')
@click.option('--output', default='baseline-report.html', help='Output HTML report path (comparison mode)')
@click.option('--json', 'json_out', default=None, help='Also write the comparison as machine-readable JSON to this path')
@click.option('--fail-on-regression', 'fail_on_regression', is_flag=True, default=False,
              help='Exit non-zero if any regression is found (for CI gating)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def baseline(config, server, database, username, password, connection_string, dialect,
             capture, baseline_file, threshold, output, json_out, fail_on_regression, verify_offline):
    """Capture a performance baseline and detect regressions against it.

    First run with --capture to save a snapshot (connection count, wait
    categories, top query average times, SQL Agent job durations). Later runs
    capture the current state and flag anything that has regressed by more than
    --threshold percent. Works on SQL Server, PostgreSQL, and MySQL. Reads only
    performance statistics — never table row data.
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server_name = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    _require_capability(adapter, 'infra_monitoring', 'baseline')
    output = resolve('output', output)
    json_out = resolve('json', json_out, param='json_out')
    bfile = baseline_file or f".sqldoc-baseline-{_safe_filename(database)}.json"

    label = server_name or database
    click.echo(f"\nsqldoc v{__version__}  -  Performance baseline")
    click.echo(f"{'='*44}\n")

    click.echo(f"Connecting to {adapter.display_name} and capturing performance stats...")
    try:
        current = capture_baseline(adapter)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    if capture:
        import json as _json
        with open(bfile, "w", encoding="utf-8") as f:
            _json.dump(baseline_to_dict(current), f, indent=2, default=str)
        click.echo(click.style(
            f"Baseline captured ({len(current.metrics)} metrics, {len(current.queries)} queries) "
            f"-> {bfile}", fg='green'))
        return

    if not os.path.exists(bfile):
        raise click.UsageError(
            f"No baseline found at {bfile}. Run 'sqldoc baseline --capture' first to record one.")
    import json as _json
    with open(bfile, encoding="utf-8") as f:
        base = baseline_from_dict(_json.load(f))

    report = compare_baseline(base, current, threshold_pct=float(threshold))
    s = baseline_summarize(report)
    click.echo(
        click.style(f"Regressions: {s['anomalies']}", fg='red' if s['anomalies'] else 'green')
        + f"    Metric: {s['metric_regressions']}    Query: {s['query_regressions']}"
        + f"    Compared: {s['metrics_compared']}    Worst: +{s['worst_change_pct']}%"
    )
    for a in report.anomalies[:10]:
        click.echo(click.style(f"  ! {a.metric}: {a.baseline} -> {a.current} (+{a.change_pct}%)", fg='yellow'))

    click.echo("\nRendering report...")
    render_baseline_html(label, report, output)
    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(build_baseline_json(label, report), f, indent=2, default=str)
        click.echo(f"Machine-readable baseline report written to {json_out}")
    _verify_offline(output, verify_offline)
    click.echo(f"Open {output} in your browser for the full baseline report.")

    if fail_on_regression and report.anomalies:
        click.echo(click.style(f"\n{len(report.anomalies)} regression(s) exceeded the threshold.", fg='red'), err=True)
        raise SystemExit(1)


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string (alternative to the four flags above)')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES), help='Database dialect (default: auto-detected)')
@click.option('--schemas', default=None, help='Comma-separated schema allowlist (default: all)')
@click.option('--output', default='executive-summary.html', help='Output HTML report path')
@click.option('--json', 'json_out', default=None, help='Also write the summary as machine-readable JSON to this path')
@industry_option
@click.option('--no-baseline', 'no_baseline', is_flag=True, default=False,
              help='Do not read/write the trend snapshot (skip the vs-last-run arrows)')
@click.option('--baseline', default=None, help='Trend-snapshot path (default: .sqldoc-exec-snapshots/<database>.json)')
@click.option('--verify-offline', 'verify_offline', is_flag=True, default=False,
              help='After rendering, verify the HTML report is fully self-contained for air-gapped use')
def executive(config, server, database, username, password, connection_string, dialect,
              schemas, output, json_out, industry, no_baseline, baseline, verify_offline):
    """Single-page, plain-English health + risk summary for a CTO / CISO.

    Aggregates the deep technical commands into four scores — data protection
    (PII), backups, security, and performance — plus an overall score, the top 3
    things to fix, and trend arrows vs the last run. No jargon. Sections that
    aren't available on the database's dialect are simply omitted. Reads schema +
    catalog metadata only (the PII scan reads no row data).
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
    no_baseline = resolve('no_baseline', no_baseline)
    industry = resolve_industry(resolve, industry)

    click.echo(f"\nsqldoc v{__version__}  -  Executive summary")
    click.echo(f"{'='*44}")
    click.echo(f"Database: {database}")
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

    caps = adapter.capabilities

    # PII (all dialects). Escalate for the industry if one was chosen.
    findings = scan_tables(tables)
    if industry:
        industry_mod.apply_to_findings(findings, industry)
    click.echo(f"  data protection: scanned {len(tables)} table(s), {len(findings)} sensitive column(s)")

    # Each remaining section is best-effort + capability-gated; a failure warns
    # and leaves that score out of the summary rather than aborting.
    health_sum = backup_report = security_report = None
    if caps.health:
        try:
            health_sum = health_summarize(collect_health(adapter))
            click.echo("  performance: analyzed")
        except Exception as e:
            click.echo(click.style(f"  ! performance check skipped: {e}", fg='yellow'), err=True)
    if caps.infra_monitoring:
        try:
            backup_report = collect_backups(adapter)
            click.echo("  backups: checked")
        except Exception as e:
            click.echo(click.style(f"  ! backup check skipped: {e}", fg='yellow'), err=True)
        try:
            security_report = collect_security(adapter)
            click.echo("  security: scanned")
        except Exception as e:
            click.echo(click.style(f"  ! security scan skipped: {e}", fg='yellow'), err=True)

    # Trend snapshot (previous run's scores).
    previous = None
    base_path = baseline or os.path.join('.sqldoc-exec-snapshots', _safe_filename(database) + '.json')
    if not no_baseline:
        previous = load_snapshot(base_path)

    summary = executive_mod.build_summary(
        database, health_summary=health_sum, findings=findings,
        backup_report=backup_report, security_report=security_report,
        previous=previous,
        generated_label=f"Generated for {database}.")

    if not no_baseline:
        save_snapshot(executive_mod.to_snapshot(summary), base_path)

    click.echo("\nRendering report...")
    render_executive_html(summary, output)
    _verify_offline(output, resolve('verify_offline', verify_offline))
    if json_out:
        import json as _json
        with open(json_out, "w", encoding="utf-8") as f:
            _json.dump(executive_mod.build_executive_json(summary), f, indent=2, default=str)
        click.echo(f"Machine-readable summary written to {json_out}")

    def _fmt(v):
        return "N/A" if v is None else str(v)
    click.echo(
        click.style(f"\nOverall: {summary.overall_score}/100 ({summary.overall_label})", bold=True)
        + f"   data-protection {_fmt(summary.pii_safety_score)}"
        + f"   backups {_fmt(summary.backup_compliance_pct)}%"
        + f"   security {_fmt(summary.security_score)}"
        + f"   performance {_fmt(summary.health_score)}")
    if summary.top_risks:
        click.echo("\nTop priorities:")
        for i, r in enumerate(summary.top_risks, 1):
            colr = {'Critical': 'red', 'High': 'yellow', 'Medium': 'blue'}.get(r['severity'])
            click.echo("  " + click.style(f"{i}. [{r['severity']}] {r['title']}", fg=colr))
    else:
        click.echo(click.style("\nNo urgent issues found.", fg='green'))
    click.echo(f"\nOpen {output} in your browser for the full executive report.")


@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--api', is_flag=True, default=True, help='Start the JSON REST API server (default mode)')
@click.option('--host', default='127.0.0.1', help='Bind host (default: 127.0.0.1 — localhost only)')
@click.option('--port', default=8090, type=int, help='Bind port (default: 8090)')
@click.option('--server', default=None, help='Database hostname or IP (the API target)')
@click.option('--database', default=None, help='Database name (the API target)')
@click.option('--username', default=None, help='Database username')
@click.option('--password', default=None, help='Database password')
@click.option('--connection-string', default=None, help='Full connection string for the API target')
@click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES), help='Database dialect')
@click.option('--api-key', 'api_key', default=None, help='Require this key via the X-API-Key header (else from .sqldoc.yml api_key)')
@click.option('--multi-tenant', 'multi_tenant', is_flag=True, default=False,
              help='Serve multiple isolated tenants from the .sqldoc.yml "tenants:" list; each tenant has its own api_key + database and cannot reach another tenant\'s data')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI mode for POST /api/query')
@click.option('--model', default=None, help='Model for POST /api/query')
def serve(config, api, host, port, server, database, username, password, connection_string,
          dialect, api_key, multi_tenant, mode, model):
    """Start a local REST API exposing sqldoc commands as JSON endpoints.

    Other tools/dashboards can call GET /api/doc, /api/scan, /api/health,
    /api/secure, /api/server, /api/waits, /api/plans, /api/ha, /api/backup,
    POST /api/query (natural-language to SQL), and GET /api/agent/status.
    Authenticate with an X-API-Key header matching the configured api_key.

    With --multi-tenant, one server hosts many customers: each tenant in the
    .sqldoc.yml "tenants:" list has its own api_key and database, and a key can
    only ever reach its own tenant's data (the foundation for a hosted SaaS).
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    host = resolve('host', host)
    port = int(resolve('port', port))

    if multi_tenant:
        tenants = load_tenants(cfg)
        api_ctx = {"tenants": tenants, "mode": resolve('mode', mode),
                   "model": resolve('model', model)}
        click.echo(f"\nsqldoc v{__version__}  -  REST API server (multi-tenant)")
        click.echo(f"{'='*44}")
        click.echo(f"Listening: http://{host}:{port}/api")
        click.echo(f"Tenants:   {len(tenants)} (isolated; X-API-Key selects the tenant)")
        for t in tenants.values():
            click.echo(f"   - {t['name']}: {t['database']}")
        click.echo(f"Endpoints: " + ", ".join(sorted(p for _m, p in API_ENDPOINTS)))
        click.echo(f"{'='*44}\n")
        httpd = make_api_server(host, port, api_ctx)
        click.echo("Server running. Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            click.echo("\nShutting down.")
            httpd.shutdown()
        return

    api_key = api_key or cfg.get('api_key') or (cfg.get('api') or {}).get('key')

    # The API target database (optional — /api/agent/status works without it).
    conn_str = None
    resolved_db = None
    try:
        conn_str, resolved_db, server = _resolve_connection(
            resolve, server, database, username, password, connection_string, dialect)
    except click.UsageError:
        conn_str = None

    from sqldoc.authn import build_authenticator
    try:
        authn = build_authenticator(cfg)
    except ValueError as e:
        raise click.UsageError(f"Invalid auth config: {e}")

    api_ctx = {"conn_str": conn_str, "dialect": resolve('dialect', dialect),
               "database": resolved_db, "api_key": api_key, "authn": authn,
               "mode": resolve('mode', mode), "model": resolve('model', model)}

    sso_desc = f"SSO ({authn.cfg.provider}/{authn.cfg.method})" if authn else None
    auth_desc = " + ".join(filter(None, ["X-API-Key" if api_key else None, sso_desc])) \
        or "OPEN (no api_key or SSO configured)"
    click.echo(f"\nsqldoc v{__version__}  -  REST API server")
    click.echo(f"{'='*44}")
    click.echo(f"Listening: http://{host}:{port}/api")
    click.echo(f"Target:    {resolved_db or '(none — only /api/agent/status)'}")
    click.echo(f"Auth:      {auth_desc}")
    click.echo(f"Endpoints: " + ", ".join(sorted(p for _m, p in API_ENDPOINTS)))
    click.echo(f"{'='*44}\n")
    if not api_key and not authn:
        click.echo(click.style("  ! No api_key or SSO set — the API is unauthenticated. Bind to "
                               "localhost and/or set api_key or auth in .sqldoc.yml.", fg='yellow'), err=True)

    httpd = make_api_server(host, port, api_ctx)
    click.echo("Server running. Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nShutting down.")
        httpd.shutdown()


@click.command()
@click.option('--command', 'command_filter', default=None, help='Only show runs of this command (e.g. scan)')
@click.option('--database', default=None, help='Only show runs against this database')
@click.option('--user', default=None, help='Only show runs by this OS user')
@click.option('--since', default=None, help='Only show runs at/after this ISO timestamp (e.g. 2026-07-01)')
@click.option('--limit', default=50, type=int, help='Max entries to show (default: 50)')
@click.option('--format', 'out_format', default='table', type=click.Choice(['table', 'json', 'csv']),
              help='Output format for --export or stdout (default: table)')
@click.option('--export', 'export_path', default=None, help='Write the (filtered) trail to this file')
@click.option('--summary', is_flag=True, default=False, help='Show aggregate counts (by command / database / user) instead of rows')
def audit(command_filter, database, user, since, limit, out_format, export_path, summary):
    """Query and export the audit trail of sqldoc command runs.

    Every sqldoc command that runs against a database is logged to
    ~/.sqldoc/audit.log (and the agent store) with a timestamp, command,
    dialect, database, OS user, the options used (secrets redacted), and a
    result summary. This command reads that trail back, filtered and exported.
    """
    entries = audit_mod.read_entries()
    entries = audit_mod.query(entries, command=command_filter, database=database,
                              user=user, since=since)

    if summary:
        s = audit_mod.summarize(entries)
        click.echo(click.style(f"\nAudit trail: {s['total']} run(s), {s['errors']} error(s)", bold=True))
        click.echo("\nBy command:")
        for k, v in s['by_command'].items():
            click.echo(f"  {v:>5}  {k}")
        if s['by_database']:
            click.echo("\nBy database:")
            for k, v in s['by_database'].items():
                click.echo(f"  {v:>5}  {k}")
        if s['by_user']:
            click.echo("\nBy user:")
            for k, v in s['by_user'].items():
                click.echo(f"  {v:>5}  {k}")
        return

    # Newest first, capped.
    shown = list(reversed(entries))[:limit]

    if export_path:
        import json as _json
        if out_format == 'csv' or export_path.lower().endswith('.csv'):
            data = audit_mod.to_csv(shown)
        else:
            data = _json.dumps(shown, indent=2, default=str)
        with open(export_path, 'w', encoding='utf-8') as f:
            f.write(data)
        click.echo(f"Exported {len(shown)} audit entr(y/ies) to {export_path}")
        return

    if out_format == 'json':
        import json as _json
        click.echo(_json.dumps(shown, indent=2, default=str))
        return
    if out_format == 'csv':
        click.echo(audit_mod.to_csv(shown))
        return

    if not shown:
        click.echo("No audit entries match. (Runs are recorded to ~/.sqldoc/audit.log.)")
        return
    click.echo(click.style(f"\n{len(shown)} audit entr(y/ies) (newest first):\n", bold=True))
    for e in shown:
        res = e.get('result') or ''
        colr = 'red' if str(res).startswith('error') else 'green'
        click.echo(
            click.style(e.get('at', '?'), fg='cyan')
            + "  " + click.style(f"{e.get('command', '?'):<12}", bold=True)
            + f" db={e.get('database') or '-'}"
            + f" dialect={e.get('dialect') or '-'}"
            + f" user={e.get('user') or '-'}"
            + "  " + click.style(res or '-', fg=colr))


# --- integration commands --------------------------------------------------
# Every publishing/ticketing connector shares one shape: --test verifies
# connectivity/auth; --push collects the database once and hands the bundle to
# the connector (as rendered reports, flat metrics, or actionable issues). The
# factory below stamps out that shape so each connector module only implements
# its client.

def _integration_thresholds(sec):
    """Issue-tracker thresholds from a connector's config section."""
    out = {}
    for k in ("security_min", "health_min", "backup_max_age_hours"):
        if sec.get(k) is not None:
            out[k] = sec[k]
    return out


def _run_integration(name, push_mode, config, server, database, username, password,
                     connection_string, dialect, schemas, do_test, do_push, kinds):
    from sqldoc.integrations import get_client, section, IntegrationError
    from sqldoc.integrations.reports import (
        gather, render_artifacts, metrics as _metrics, finding_events)

    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)
    sec = section(cfg, name)

    if not do_test and not do_push:
        raise click.UsageError(
            f"Specify --test (verify connectivity) or --push (collect + publish) "
            f"for 'sqldoc {name}'.")

    try:
        client = get_client(name, sec)
    except IntegrationError as e:
        raise click.UsageError(str(e))

    click.echo(f"\nsqldoc v{__version__}  -  {name} integration")
    click.echo(f"{'='*44}")

    if do_test:
        click.echo(f"Testing {name} connectivity...")
        try:
            res = client.test()
        except IntegrationError as e:
            click.echo(click.style(f"  x {e}", fg='red'), err=True)
            raise SystemExit(1)
        click.echo(click.style(f"  ok  {res.get('detail')}", fg='green'))
        if not do_push:
            return

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string, dialect)
    adapter = open_adapter(resolve, conn_str, dialect)
    click.echo(f"Collecting {database} from {adapter.display_name}...")
    try:
        bundle = gather(adapter, database, schemas=resolve('schemas', schemas))
    except Exception as e:
        click.echo(click.style(f"Collection failed: {e}", fg='red'), err=True)
        raise click.Abort()
    for note in bundle.notes:
        click.echo(click.style(f"  ! {note}", fg='yellow'), err=True)

    try:
        if push_mode == 'metrics':
            res = client.push_metrics(_metrics(bundle))
        elif push_mode == 'issues':
            events = finding_events(bundle, _integration_thresholds(sec))
            if not events:
                click.echo(click.style("  No findings exceeded the configured thresholds.",
                                       fg='green'))
            res = client.create_issues(events, metrics=_metrics(bundle))
        else:  # 'reports'
            kinds_list = [k.strip() for k in kinds.split(',')] if kinds else None
            artifacts = render_artifacts(bundle, kinds_list)
            res = client.push_reports(artifacts, metrics=_metrics(bundle), bundle=bundle)
    except IntegrationError as e:
        click.echo(click.style(f"  x push failed: {e}", fg='red'), err=True)
        raise SystemExit(1)

    click.echo(click.style(f"  ok  {res.get('detail')}", fg='green'))
    if res.get("url"):
        click.echo(f"  {res['url']}")


def make_integration_command(name, summary, push_mode):
    @click.command(name=name)
    @click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
    @click.option('--server', default=None, help='Database hostname or IP (for --push)')
    @click.option('--database', default=None, help='Database name (for --push)')
    @click.option('--username', default=None, help='Database username (for --push)')
    @click.option('--password', default=None, help='Database password (for --push)')
    @click.option('--connection-string', default=None, help='Full connection string (for --push)')
    @click.option('--dialect', default=None, type=click.Choice(DIALECT_CHOICES), help='Database dialect')
    @click.option('--schemas', default=None, help='Comma-separated schema allowlist (for --push)')
    @click.option('--test', 'do_test', is_flag=True, default=False, help='Verify connectivity/auth and exit')
    @click.option('--push', 'do_push', is_flag=True, default=False,
                  help='Collect the database and publish reports/metrics/issues to ' + name)
    @click.option('--kinds', default=None,
                  help='Comma-separated report kinds to publish (default: all) — '
                       'doc_html, doc_pdf, executive_html, pii_html, pii_json, health_json, metrics_json')
    def _cmd(config, server, database, username, password, connection_string, dialect,
             schemas, do_test, do_push, kinds):
        _run_integration(name, push_mode, config, server, database, username, password,
                         connection_string, dialect, schemas, do_test, do_push, kinds)
    _cmd.__doc__ = summary
    return _cmd


sharepoint = make_integration_command(
    'sharepoint',
    "Publish sqldoc reports to SharePoint Online (Microsoft Graph API).\n\n"
    "--test verifies the Azure AD app + site access. --push uploads the HTML / PDF /\n"
    "executive / PII reports plus the structured JSON to a document library and adds\n"
    "a structured summary row (metrics) to a SharePoint List. Configure tenant_id,\n"
    "client_id, client_secret, site_id, folder, and list_name under 'sharepoint:'.",
    push_mode='reports')


confluence = make_integration_command(
    'confluence',
    "Publish sqldoc docs to Confluence Cloud (REST API v2, API-token auth).\n\n"
    "--test verifies the space; --push creates/updates one page per database with\n"
    "the executive scorecard, a PII findings table, a health summary, and doc stats,\n"
    "and attaches the full HTML reports. Configure base_url, email, api_token,\n"
    "space_key, and optionally parent_page_id under 'confluence:'.",
    push_mode='reports')


notion = make_integration_command(
    'notion',
    "Publish sqldoc docs to Notion (official API, integration token).\n\n"
    "--test verifies the token; --push creates a doc page per database (executive\n"
    "scorecard + health as blocks), a child 'PII Findings' database (one row per\n"
    "sensitive column), and — if a tracker database_id is set — upserts a metrics\n"
    "row with the scores as properties. Configure token, parent_page_id, and\n"
    "optionally database_id under 'notion:'.",
    push_mode='reports')


gdrive = make_integration_command(
    'gdrive',
    "Upload sqldoc reports to Google Drive (Drive API v3, service account).\n\n"
    "--test verifies the service account + folder; --push uploads the HTML/PDF/JSON\n"
    "reports to the configured folder under consistent names (re-push updates the\n"
    "same file, so Drive keeps revision history) and shares them with the configured\n"
    "emails. Configure service_account_file (or service_account_info), folder_id,\n"
    "and share_with under 'gdrive:'.",
    push_mode='reports')


box = make_integration_command(
    'box',
    "Upload sqldoc reports to Box (Box SDK, JWT app auth).\n\n"
    "--test verifies the JWT app + folder; --push uploads the reports to the folder\n"
    "(re-push updates in place, keeping Box version history), sets a shared link at\n"
    "the configured access level, and tags each file with database + scan_date\n"
    "metadata. Configure jwt_config_file (or jwt_config), folder_id, and\n"
    "shared_link_access under 'box:'.",
    push_mode='reports')


jira = make_integration_command(
    'jira',
    "Raise Jira issues from sqldoc findings that exceed thresholds.\n\n"
    "--test verifies the project; --push scans the database and creates issues —\n"
    "HIGH PII -> Security, failed health -> Bug, backup staleness -> Task (all\n"
    "configurable) — with full detail, a link back to the report, and no duplicate\n"
    "of an already-open issue. Configure base_url, email, api_token, project_key,\n"
    "issue_types, and thresholds (security_min/health_min) under 'jira:'.",
    push_mode='issues')


servicenow = make_integration_command(
    'servicenow',
    "Open ServiceNow incidents + update CMDB from sqldoc findings.\n\n"
    "--test verifies the instance; --push creates an incident per critical finding\n"
    "(security below threshold, failed health, backup staleness, HIGH PII) with\n"
    "urgency/impact from severity, and updates the database's CI record with\n"
    "documentation metadata. Schema-change change-requests are raised by the agent.\n"
    "Configure instance_url, username, password, ci_class, and thresholds under\n"
    "'servicenow:'.",
    push_mode='issues')


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
cli.add_command(scan_files, name='scan-files')
cli.add_command(install_hooks, name='install-hooks')
cli.add_command(health, name='health')
cli.add_command(quality, name='quality')
cli.add_command(intel, name='intel')
cli.add_command(insights, name='insights')
cli.add_command(comply, name='comply')
cli.add_command(dbt, name='dbt')
cli.add_command(server, name='server')
cli.add_command(logs, name='logs')
cli.add_command(secure, name='secure')
cli.add_command(waits, name='waits')
cli.add_command(ha, name='ha')
cli.add_command(deadlocks, name='deadlocks')
cli.add_command(plans, name='plans')
cli.add_command(capacity, name='capacity')
cli.add_command(baseline, name='baseline')
cli.add_command(executive, name='executive')
cli.add_command(serve, name='serve')
cli.add_command(audit, name='audit')
cli.add_command(sharepoint, name='sharepoint')
cli.add_command(confluence, name='confluence')
cli.add_command(notion, name='notion')
cli.add_command(gdrive, name='gdrive')
cli.add_command(box, name='box')
cli.add_command(jira, name='jira')
cli.add_command(servicenow, name='servicenow')


# --- audit trail hook ------------------------------------------------------
# Wrap every command's callback so each run is recorded (timestamp, command,
# dialect, database, user, options, result) to ~/.sqldoc/audit.log + the agent
# store. Recording is best-effort and never breaks the command. `audit` itself
# and the daemon-control `agent` subgroup are not recorded (they don't run
# against a database).
import functools as _functools  # noqa: E402

_AUDIT_SKIP = {'audit'}


def _wrap_command_for_audit(command_obj, name):
    orig = command_obj.callback
    if orig is None or getattr(orig, '_sqldoc_audited', False):
        return
    @_functools.wraps(orig)
    def wrapper(*args, **kwargs):
        result = "ok"
        try:
            return orig(*args, **kwargs)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            result = "ok" if code == 0 else f"exit {code}"
            raise
        except click.exceptions.Exit as e:
            code = getattr(e, 'exit_code', 0)
            result = "ok" if code == 0 else f"exit {code}"
            raise
        except click.Abort:
            result = "aborted"
            raise
        except click.ClickException as e:
            result = f"error: {e.format_message()}"
            raise
        except Exception as e:
            result = f"error: {type(e).__name__}: {e}"
            raise
        finally:
            try:
                audit_mod.record_command(name, kwargs, result=result)
            except Exception:
                pass
    wrapper._sqldoc_audited = True
    command_obj.callback = wrapper


for _name, _cmd in list(cli.commands.items()):
    if _name not in _AUDIT_SKIP and isinstance(_cmd, click.Command) and not isinstance(_cmd, click.Group):
        _wrap_command_for_audit(_cmd, _name)

# The agent subgroup is defined in sqldoc.agent.cli; imported here (after this
# module is otherwise defined) to attach it without a circular import.
from sqldoc.agent.cli import agent as _agent_group  # noqa: E402
cli.add_command(_agent_group, name='agent')


if __name__ == '__main__':
    cli()