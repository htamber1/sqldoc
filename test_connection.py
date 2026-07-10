from sqldoc.extractor import extract_metadata, build_connection_string

print("Connecting to AdventureWorks2022...")

tables = extract_metadata(build_connection_string(
    server="localhost",
    database="AdventureWorks2022",
    username="sa",
    password="SqlDoc123!",
))

print(f"Success! Found {len(tables)} tables\n")

# Show first 5 tables as a preview
for table in tables[:5]:
    print(f"{table.schema}.{table.name} ({table.row_count} rows, {len(table.columns)} columns)")