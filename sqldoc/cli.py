import click
import os
import yaml
from dotenv import load_dotenv
from sqldoc.extractor import extract_metadata, extract_views, extract_procedures
from sqldoc.ai import enrich_tables, enrich_views, enrich_procedures
from sqldoc.renderer import render_html

load_dotenv()

# Config keys that .sqldoc.yml may set; each maps to the same-named CLI option.
CONFIG_KEYS = {
    'server', 'database', 'username', 'password', 'output',
    'mode', 'model', 'schemas', 'no_ai', 'concurrency', 'yes',
}


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
@click.option('--output', default='documentation.html', help='Output HTML file path')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI mode: local (Ollama) or cloud (Anthropic)')
@click.option('--model', default=None, help='Model to use (default: llama3.1:8b for local, claude-haiku-4-5 for cloud)')
@click.option('--schemas', default=None, help='Comma-separated list of schemas to include (default: all)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip AI descriptions, output schema only')
@click.option('--concurrency', default=8, type=click.IntRange(1, 64), help='Parallel AI calls during enrichment (default: 8)')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt (for non-interactive use)')
def main(config, server, database, username, password, output, mode, model, schemas, no_ai, concurrency, yes):
    """sqldoc — Automated SQL Server database documentation generator."""

    # Merge config file under CLI flags: an explicit CLI flag always wins, then
    # a .sqldoc.yml value, then the built-in default.
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')

    def resolve(name, value):
        if ctx.get_parameter_source(name).name == 'COMMANDLINE':
            return value
        return cfg.get(name, value)

    server = resolve('server', server)
    database = resolve('database', database)
    username = resolve('username', username)
    password = resolve('password', password)
    output = resolve('output', output)
    mode = resolve('mode', mode)
    model = resolve('model', model)
    schemas = resolve('schemas', schemas)
    no_ai = resolve('no_ai', no_ai)
    concurrency = resolve('concurrency', concurrency)
    yes = resolve('yes', yes)

    # Validate merged values (config can supply out-of-range/invalid values that
    # bypass Click's per-option validation, which only sees CLI input).
    missing = [n for n, v in (('server', server), ('database', database),
                              ('username', username), ('password', password)) if not v]
    if missing:
        raise click.UsageError(
            "Missing required connection settings: " + ", ".join(missing) +
            ". Provide them via CLI flags or a .sqldoc.yml config file."
        )
    if mode not in ('local', 'cloud'):
        raise click.UsageError(f"Invalid mode '{mode}' (must be 'local' or 'cloud').")
    if not isinstance(concurrency, int) or not (1 <= concurrency <= 64):
        raise click.UsageError(f"Invalid concurrency '{concurrency}' (must be an integer 1-64).")

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
    click.echo(f"Server:   {server}")
    click.echo(f"Database: {database}")
    click.echo(f"Mode:     {'No AI' if no_ai else mode}")
    click.echo(f"Privacy:  {privacy}")
    click.echo(f"Output:   {output}")
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
        tables = extract_metadata(server, database, username, password)
        views = extract_views(server, database, username, password)
        procedures = extract_procedures(server, database, username, password)
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

    # Generate AI descriptions
    if not no_ai:
        click.echo(f"\nGenerating AI descriptions using {mode} mode ({concurrency} parallel)...")
        try:
            tables = enrich_tables(tables, mode=mode, model=model, concurrency=concurrency)
            views = enrich_views(views, mode=mode, model=model, concurrency=concurrency)
            procedures = enrich_procedures(procedures, mode=mode, model=model, concurrency=concurrency)
        except Exception as e:
            click.echo(f"\nAI generation failed: {e}", err=True)
            click.echo("Try --no-ai to generate schema-only documentation")
            raise click.Abort()
    else:
        click.echo("Skipping AI descriptions (--no-ai flag set)")

    # Render HTML
    click.echo(f"\nRendering documentation...")
    render_html(database, tables, output, views=views, procedures=procedures)

    click.echo(f"\nDone! Open {output} in your browser to view the documentation.")

if __name__ == '__main__':
    main()