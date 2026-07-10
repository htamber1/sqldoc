from dotenv import load_dotenv
load_dotenv()

from sqldoc.extractor import extract_metadata, build_connection_string
from sqldoc.ai import enrich_tables
from sqldoc.renderer import render_html

print("Extracting metadata...")
tables = extract_metadata(build_connection_string("localhost", "AdventureWorks2022", "sa", "SqlDoc123!"))

# Test with just 3 tables first to keep it fast
tables = tables[:3]

print("Generating AI descriptions...")
tables = enrich_tables(tables, mode="local")

print("Rendering HTML...")
render_html("AdventureWorks2022", tables, "output.html")