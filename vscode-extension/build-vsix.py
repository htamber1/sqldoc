#!/usr/bin/env python3
"""Package the sqldoc VS Code extension into a valid .vsix without npm/vsce.

A .vsix is an Open Packaging Conventions (OPC) zip: a root
``extension.vsixmanifest`` + ``[Content_Types].xml`` plus every shipped file
under an ``extension/`` folder. Because this extension is plain CommonJS (no
build step), we can assemble that zip directly from the source files.

Run: ``python build-vsix.py`` -> writes ``sqldoc-vscode.vsix`` here.
"""
import json
import os
import zipfile
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Files shipped inside the extension/ folder of the package.
EXTENSION_FILES = ["package.json", "extension.js", "README.md"]
# LICENSE is copied from the repo root if present.
LICENSE_SRC = os.path.join(REPO_ROOT, "LICENSE")

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="js" ContentType="application/javascript" />
  <Default Extension="md" ContentType="text/markdown" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
  <Default Extension="txt" ContentType="text/plain" />
  <Default Extension="png" ContentType="image/png" />
</Types>
"""


def build_manifest(pkg: dict) -> str:
    ident = pkg["name"]
    publisher = pkg["publisher"]
    version = pkg["version"]
    display = pkg.get("displayName", ident)
    description = pkg.get("description", "")
    engine = pkg.get("engines", {}).get("vscode", "^1.75.0")
    tags = escape(",".join(pkg.get("keywords", [])))
    categories = escape(",".join(pkg.get("categories", ["Other"])))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{escape(ident)}" Version="{escape(version)}" Publisher="{escape(publisher)}" />
    <DisplayName>{escape(display)}</DisplayName>
    <Description xml:space="preserve">{escape(description)}</Description>
    <Tags>{tags}</Tags>
    <Categories>{categories}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(engine)}" />
      <Property Id="Microsoft.VisualStudio.Services.Links.Source" Value="https://github.com/htamber1/sqldoc" />
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true" />
    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" Addressable="true" />
  </Assets>
</PackageManifest>
"""


def main():
    with open(os.path.join(HERE, "package.json"), encoding="utf-8") as f:
        pkg = json.load(f)

    out_path = os.path.join(HERE, "sqldoc-vscode.vsix")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", build_manifest(pkg))
        for name in EXTENSION_FILES:
            src = os.path.join(HERE, name)
            with open(src, encoding="utf-8") as f:
                z.writestr(f"extension/{name}", f.read())
        if os.path.exists(LICENSE_SRC):
            with open(LICENSE_SRC, encoding="utf-8") as f:
                z.writestr("extension/LICENSE.txt", f.read())

    size = os.path.getsize(out_path)
    print(f"Wrote {out_path} ({size:,} bytes)")


if __name__ == "__main__":
    main()
