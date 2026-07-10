import click
import os
import yaml
from dotenv import load_dotenv
import re
from sqldoc.extractor import extract_metadata, extract_views, extract_procedures, build_connection_string
from sqldoc.ai import enrich_tables, enrich_views, enrich_procedures, load_cache, save_cache
from sqldoc.renderer import render_html
from sqldoc.markdown_renderer import render_markdown
from sqldoc.snapshot import build_snapshot, load_snapshot, save_snapshot, diff_snapshots, iter_diff_lines

load_dotenv()

# Config keys that .sqldoc.yml may set; each maps to the same-named CLI option.
CONFIG_KEYS = {
    'server', 'database', 'username', 'password', 'connection_string', 'output',
    'mode', 'model', 'schemas', 'no_ai', 'concurrency', 'format',
    'snapshot', 'no_snapshot', 'cache', 'no_cache', 'yes',
}


def _parse_database(connection_string: str):
    """Best-effort extraction of the database name from a connection string,
    for labeling output and naming snapshot/cache files."""
    m = re.search(r'(?:DATABASE|Initial\s+Catalog)\s*=\s*([^;]+)', connection_string, re.IGNORECASE)
    return m.group(1).strip() if m else None

_DIFF_COLORS = {'add': 'green', 'remove': 'red', 'change': 'yellow'}


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
}


def resolve_format(fmt, output):
    """Pick the output format: explicit --format wins, else infer from the
    output file extension, else default to html."""
    if fmt:
        return fmt
    return _EXT_FORMAT.get(os.path.splitext(output)[1].lower(), 'html')


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
@click.option('--output', default='documentation.html', help='Output file path')
@click.option('--format', 'output_format', default=None, type=click.Choice(['html', 'markdown', 'pdf']), help='Output format (default: inferred from --output extension, else html)')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI mode: local (Ollama) or cloud (Anthropic)')
@click.option('--model', default=None, help='Model to use (default: llama3.1:8b for local, claude-haiku-4-5 for cloud)')
@click.option('--schemas', default=None, help='Comma-separated list of schemas to include (default: all)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip AI descriptions, output schema only')
@click.option('--concurrency', default=8, type=click.IntRange(1, 64), help='Parallel AI calls during enrichment (default: 8)')
@click.option('--snapshot', default=None, help='JSON schema-snapshot path for change detection (default: .sqldoc-snapshots/<database>.json)')
@click.option('--no-snapshot', is_flag=True, default=False, help='Disable schema snapshot + change detection for this run')
@click.option('--cache', default=None, help='AI description cache path (default: .sqldoc-cache/<database>.json)')
@click.option('--no-cache', is_flag=True, default=False, help='Disable the AI description cache (always regenerate)')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt (for non-interactive use)')
def main(config, server, database, username, password, connection_string, output, output_format, mode, model, schemas, no_ai, concurrency, snapshot, no_snapshot, cache, no_cache, yes):
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

    if mode not in ('local', 'cloud'):
        raise click.UsageError(f"Invalid mode '{mode}' (must be 'local' or 'cloud').")
    if not isinstance(concurrency, int) or not (1 <= concurrency <= 64):
        raise click.UsageError(f"Invalid concurrency '{concurrency}' (must be an integer 1-64).")
    if output_format not in (None, 'html', 'markdown', 'pdf'):
        raise click.UsageError(f"Invalid format '{output_format}' (must be html, markdown, or pdf).")

    output_format = resolve_format(output_format, output)

    # Resolve the model per backend when not explicitly set, so --model works
    # for both without a local default (llama tag) leaking into cloud calls.
    if model is None:
        model = 'llama3.1:8b' if mode == 'local' else 'claude-haiku-4-5'

    # Describe the data-egress posture for the chosen mode
    if no_ai:
        privacy = "No AI - schema only, nothing leaves this machine"
    elif mode == "local":
        privacy = "local (Ollama) - no data leaves this network"
    else:
        privacy = "cloud (Anthropic) - schema metadata sent off-network"

    click.echo(f"\nsqldoc v0.1.0")
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
        if yes:
            click.echo("Proceeding with cloud mode (confirmed via --yes).")
        elif not click.confirm("Proceed with cloud mode?", default=False):
            click.echo("Aborted. Re-run with --mode local to stay on-network.")
            raise click.Abort()

    # Extract metadata
    click.echo("Connecting to SQL Server...")
    try:
        tables = extract_metadata(conn_str)
        views = extract_views(conn_str)
        procedures = extract_procedures(conn_str)
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

        click.echo(f"\nGenerating AI descriptions using {mode} mode ({concurrency} parallel)...")
        try:
            tables = enrich_tables(tables, mode=mode, model=model, concurrency=concurrency, cache=cache_obj)
            views = enrich_views(views, mode=mode, model=model, concurrency=concurrency, cache=cache_obj)
            procedures = enrich_procedures(procedures, mode=mode, model=model, concurrency=concurrency, cache=cache_obj)
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
    else:
        render_html(database, tables, output, views=views, procedures=procedures)

    click.echo(f"\nDone! Open {output} in your browser to view the documentation.")

if __name__ == '__main__':
    main()