from dotenv import load_dotenv
load_dotenv()

from sqldoc.extractor import extract_metadata
from sqldoc.ai import generate_table_description

tables = extract_metadata("localhost", "AdventureWorks2022", "sa", "SqlDoc123!")

table = tables[3]
print(f"Table: {table.schema}.{table.name}")
print(f"Generating description using local Ollama...")
description = generate_table_description(table, mode="local")
print(f"\nAI Description:\n{description}")