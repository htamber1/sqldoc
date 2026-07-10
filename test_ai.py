from dotenv import load_dotenv
load_dotenv()

from sqldoc.extractor import extract_metadata, build_connection_string
from sqldoc.ai import generate_table_description

tables = extract_metadata(build_connection_string("localhost", "AdventureWorks2022", "sa", "SqlDoc123!"))

table = tables[3]
print(f"Table: {table.schema}.{table.name}")
print(f"Generating description using local Ollama...")
description = generate_table_description(table, mode="local")
print(f"\nAI Description:\n{description}")