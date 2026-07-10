import click
import os
from dotenv import load_dotenv
from sqldoc.extractor import extract_metadata, extract_views, extract_procedures
from sqldoc.ai import enrich_tables, enrich_views, enrich_procedures
from sqldoc.renderer import render_html

load_dotenv()

@click.command()
@click.option('--server', required=True, help='SQL Server hostname or IP')
@click.option('--database', required=True, help='Database name to document')
@click.option('--username', required=True, help='SQL Server username')
@click.option('--password', required=True, help='SQL Server password')
@click.option('--output', default='documentation.html', help='Output HTML file path')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI mode: local (Ollama) or cloud (Anthropic)')
@click.option('--model', default=None, help='Model to use (default: llama3.1:8b for local, claude-haiku-4-5 for cloud)')
@click.option('--schemas', default=None, help='Comma-separated list of schemas to include (default: all)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip AI descriptions, output schema only')
@click.option('--concurrency', default=8, type=click.IntRange(1, 64), help='Parallel AI calls during enrichment (default: 8)')
@click.option('--yes', '-y', is_flag=True, default=False, help='Skip the cloud-mode confirmation prompt (for non-interactive use)')
def main(server, database, username, password, output, mode, model, schemas, no_ai, concurrency, yes):
    """sqldoc — Automated SQL Server database documentation generator."""

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