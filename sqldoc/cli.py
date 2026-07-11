import click
import os
import yaml
from dotenv import load_dotenv
import re
from sqldoc.extractor import extract_metadata, extract_views, extract_procedures, build_connection_string
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

load_dotenv()

# Config keys that .sqldoc.yml may set; each maps to the same-named CLI option.
CONFIG_KEYS = {
    'server', 'database', 'username', 'password', 'connection_string', 'output',
    'mode', 'model', 'schemas', 'no_ai', 'concurrency', 'format',
    'include_definitions',
    'snapshot', 'no_snapshot', 'cache', 'no_cache', 'sample',
    'baseline', 'no_baseline', 'sarif', 'json', 'pii_patterns', 'pii_allowlist',
    'confidence_threshold', 'fail_on', 'yes',
}


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


def _resolve_connection(resolve, server, database, username, password, connection_string):
    """Merge connection settings and return (conn_str, database, server).
    A --connection-string takes precedence over the discrete parts."""
    server = resolve('server', server)
    database = resolve('database', database)
    username = resolve('username', username)
    password = resolve('password', password)
    connection_string = resolve('connection_string', connection_string)
    if connection_string:
        return connection_string, (database or _parse_database(connection_string) or 'database'), server
    missing = [n for n, v in (('server', server), ('database', database),
                              ('username', username), ('password', password)) if not v]
    if missing:
        raise click.UsageError(
            "Missing connection settings: " + ", ".join(missing) +
            ". Provide --server/--database/--username/--password, a "
            "--connection-string, or a .sqldoc.yml config file."
        )
    return build_connection_string(server, database, username, password), database, server


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
def main(config, server, database, username, password, connection_string, output, output_format, mode, model, schemas, no_ai, concurrency, include_definitions, snapshot, no_snapshot, cache, no_cache, yes):
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

    click.echo(f"\nsqldoc v1.2.0")
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

    click.echo(f"\nDone! Open {output} in your browser to view the documentation.")

@click.command()
@click.option('--config', default='.sqldoc.yml', help='Path to config file (default: .sqldoc.yml if present)')
@click.option('--server', default=None, help='SQL Server hostname or IP')
@click.option('--database', default=None, help='Database name to scan')
@click.option('--username', default=None, help='SQL Server username')
@click.option('--password', default=None, help='SQL Server password')
@click.option('--connection-string', default=None, help='Full ODBC connection string (alternative to the four flags above)')
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
def scan(config, server, database, username, password, connection_string, schemas, output, sample, mode, model, baseline, no_baseline, sarif, json_out, confidence_threshold, fail_on, yes):
    """Scan a SQL Server database for likely PII / regulated columns.

    Flags columns by name + data type, maps each to HIPAA / GDPR / PCI-DSS, and
    writes a compliance HTML report. With --sample, reads up to 5 values per
    flagged column and uses AI to confirm findings (values are never stored).
    """
    ctx = click.get_current_context()
    cfg = load_config(config, ctx.get_parameter_source('config').name == 'COMMANDLINE')
    resolve = _make_resolver(ctx, cfg)

    conn_str, database, server = _resolve_connection(
        resolve, server, database, username, password, connection_string)
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

    click.echo("\nsqldoc v1.2.0  -  PII / compliance scan")
    click.echo(f"{'='*44}")
    click.echo(f"Server:   {server if server else '(connection string)'}")
    click.echo(f"Database: {database}")
    click.echo(f"Sampling: {'ON (' + mode + ' AI)' if sample else 'off (metadata only)'}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*44}\n")

    click.echo("Connecting to SQL Server...")
    try:
        tables = extract_metadata(conn_str)
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


class DefaultGroup(click.Group):
    """A group that routes to the `doc` command when invoked with options but no
    subcommand — so `sqldoc --server ...` keeps working alongside `sqldoc scan`."""
    default_command = 'doc'

    def parse_args(self, ctx, args):
        if args and args[0].startswith('-') and args[0] not in ('--help', '-h', '--version'):
            args = [self.default_command, *args]
        return super().parse_args(ctx, args)


@click.group(cls=DefaultGroup)
@click.version_option("1.2.0", prog_name="sqldoc")
def cli():
    """sqldoc — SQL Server documentation + PII / compliance scanner."""


cli.add_command(main, name='doc')
cli.add_command(scan, name='scan')


if __name__ == '__main__':
    cli()