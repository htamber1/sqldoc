"""Box integration via the Box SDK with JWT app authentication.

JWT server auth is preferred in healthcare/finance because it needs no
interactive user. Reports are uploaded to a configured folder (re-push updates
the existing file in place, keeping Box version history), a shared link is set at
the configured access level, and each file is tagged with ``database`` +
``scan_date`` metadata so the folder is filterable in Box.

Config (``box:`` in .sqldoc.yml)::

    box:
      jwt_config_file: /secrets/box-jwt.json    # or jwt_config: {...}
      folder_id: "123456789"
      shared_link_access: company               # open | company | collaborators
      database: MyDB                            # metadata tag (else the --push db)

The client factory + upload stream are module-level so tests inject a fake Box
client without the SDK or network.
"""
import datetime
import io

from sqldoc.integrations.base import IntegrationError, need, require, result


def build_client(cfg):
    """Build a JWT-authenticated Box client from the app config."""
    boxsdk = require("boxsdk", "box")
    if cfg.get("jwt_config_file"):
        auth = boxsdk.JWTAuth.from_settings_file(cfg["jwt_config_file"])
    elif cfg.get("jwt_config"):
        auth = boxsdk.JWTAuth.from_settings_dictionary(cfg["jwt_config"])
    else:
        raise IntegrationError("box needs jwt_config_file (path) or jwt_config (mapping).")
    return boxsdk.Client(auth)


def _stream(artifact):
    return io.BytesIO(artifact.content)


def _find_existing(folder, name):
    """Return the id of a file already named `name` in the folder, or None."""
    for item in folder.get_items():
        if getattr(item, "type", None) == "file" and getattr(item, "name", None) == name:
            return item.id
    return None


def _tag_metadata(client, file_id, database):
    """Best-effort: stamp database + scan_date metadata on the file."""
    values = {"database": str(database), "scan_date": datetime.date.today().isoformat()}
    md = client.file(file_id).metadata("global", "properties")
    try:
        md.create(values)
    except Exception:
        # Already present -> replace each key. Non-fatal if the SDK shape differs.
        try:
            ops = md.start_update()
            for k, v in values.items():
                ops.add(f"/{k}", v)
            md.update(ops)
        except Exception:
            pass


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def test(self) -> dict:
        need(self.cfg, "folder_id", integration="box")
        client = build_client(self.cfg)
        user = client.user().get()
        folder = client.folder(self.cfg["folder_id"]).get()
        return result(True, f"Connected to Box as '{getattr(user, 'name', 'service account')}'; "
                            f"folder '{getattr(folder, 'name', self.cfg['folder_id'])}' reachable.",
                      folder=getattr(folder, "name", None))

    def push_reports(self, artifacts, metrics=None, bundle=None) -> dict:
        need(self.cfg, "folder_id", integration="box")
        if not artifacts:
            raise IntegrationError("Nothing to upload (no reports were rendered).")
        client = build_client(self.cfg)
        folder = client.folder(self.cfg["folder_id"])
        database = self.cfg.get("database") or (bundle.database if bundle else
                                                (metrics or {}).get("database", "database"))
        access = self.cfg.get("shared_link_access", "company")
        uploaded, primary = [], None
        for art in artifacts:
            existing = _find_existing(folder, art.name)
            if existing:
                box_file = client.file(existing).update_contents_stream(_stream(art))
            else:
                box_file = folder.upload_stream(_stream(art), art.name)
            fid = box_file.id
            _tag_metadata(client, fid, database)
            try:
                link = client.file(fid).get_shared_link(access=access)
            except Exception:
                link = None
            uploaded.append(art.name)
            if art.kind == "executive_html" or primary is None:
                primary = link
        return result(True, f"Uploaded {len(uploaded)} report(s) to Box "
                            f"(tagged database={database}, shared_link={access}).",
                      uploaded=uploaded, url=primary)
