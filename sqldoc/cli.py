import click
import os
from dotenv import load_dotenv
from sqldoc.extractor import extract_metadata
from sqldoc.ai import enrich_tables
from sqldoc.renderer import render_html

load_dotenv()

@click.command()
@click.option('--server', required=True, help='SQL Server hostname or IP')
@click.option('--database', required=True, help='Database name to document')
@click.option('--username', required=True, help='SQL Server username')
@click.option('--password', required=True, help='SQL Server password')
@click.option('--output', default='documentation.html', help='Output HTML file path')
@click.option('--mode', default='local', type=click.Choice(['local', 'cloud']), help='AI mode: local (Ollama) or cloud (Anthropic)')
@click.option('--model', default='llama3.1:8b', help='Model to use (default: llama3.1:8b for local)')
@click.option('--schemas', default=None, help='Comma-separated list of schemas to include (default: all)')
@click.option('--no-ai', is_flag=True, default=False, help='Skip AI descriptions, output schema only')
def main(server, database, username, password, output, mode, model, schemas, no_ai):
    """sqldoc — Automated SQL Server database documentation generator."""

    click.echo(f"\nsqldoc v0.1.0")
    click.echo(f"{'='*40}")
    click.echo(f"Server:   {server}")
    click.echo(f"Database: {database}")
    click.echo(f"Mode:     {'No AI' if no_ai else mode}")
    click.echo(f"Output:   {output}")
    click.echo(f"{'='*40}\n")

    # Extract metadata
    click.echo("Connecting to SQL Server...")
    try:
        tables = extract_metadata(server, database, username, password)
    except Exception as e:
        click.echo(f"Connection failed: {e}", err=True)
        raise click.Abort()

    click.echo(f"Found {len(tables)} tables across {len(set(t.schema for t in tables))} schemas")

    # Filter schemas if specified
    if schemas:
        schema_list = [s.strip() for s in schemas.split(',')]
        tables = [t for t in tables if t.schema in schema_list]
        click.echo(f"Filtered to {len(tables)} tables in schemas: {', '.join(schema_list)}")

    # Generate AI descriptions
    if not no_ai:
        click.echo(f"\nGenerating AI descriptions using {mode} mode...")
        try:
            tables = enrich_tables(tables, mode=mode, model=model)
        except Exception as e:
            click.echo(f"\nAI generation failed: {e}", err=True)
            click.echo("Try --no-ai to generate schema-only documentation")
            raise click.Abort()
    else:
        click.echo("Skipping AI descriptions (--no-ai flag set)")

    # Render HTML
    click.echo(f"\nRendering documentation...")
    render_html(database, tables, output)

    click.echo(f"\nDone! Open {output} in your browser to view the documentation.")

if __name__ == '__main__':
    main()