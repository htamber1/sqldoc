"""Google Drive integration via Drive API v3 with a service account.

Reports are uploaded to a configured folder under **consistent file names**, so
re-pushing *updates* the existing file (Drive keeps the prior revisions — free
version history) rather than creating duplicates. Each file is optionally shared
(reader) with a list of email addresses.

Config (``gdrive:`` in .sqldoc.yml)::

    gdrive:
      service_account_file: /secrets/sqldoc-sa.json   # or service_account_info: {...}
      folder_id: "<drive-folder-id>"
      share_with: [team@acme.com, ciso@acme.com]

The Drive service factory and the media wrapper are module-level so tests inject
a fake service without importing the Google client.
"""
from sqldoc.integrations.base import IntegrationError, need, require, result

_SCOPES = ["https://www.googleapis.com/auth/drive"]


def build_service(cfg):
    """Build an authenticated Drive v3 service from service-account creds."""
    require("googleapiclient", "gdrive")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    if cfg.get("service_account_file"):
        creds = service_account.Credentials.from_service_account_file(
            cfg["service_account_file"], scopes=_SCOPES)
    elif cfg.get("service_account_info"):
        creds = service_account.Credentials.from_service_account_info(
            cfg["service_account_info"], scopes=_SCOPES)
    else:
        raise IntegrationError(
            "gdrive needs service_account_file (path) or service_account_info (mapping).")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _media_body(artifact):
    import io
    from googleapiclient.http import MediaIoBaseUpload
    return MediaIoBaseUpload(io.BytesIO(artifact.content), mimetype=artifact.mime, resumable=False)


def find_file(service, name, folder_id):
    """Return the id of a non-trashed file with this name in the folder, or None."""
    safe = name.replace("'", "\\'")
    q = f"name = '{safe}' and '{folder_id}' in parents and trashed = false"
    resp = service.files().list(q=q, spaces="drive",
                               fields="files(id,name)").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


class Client:
    def __init__(self, config: dict):
        self.cfg = config or {}

    def test(self) -> dict:
        need(self.cfg, "folder_id", integration="gdrive")
        service = build_service(self.cfg)
        about = service.about().get(fields="user").execute()
        email = about.get("user", {}).get("emailAddress", "service account")
        folder = service.files().get(fileId=self.cfg["folder_id"],
                                     fields="id,name").execute()
        return result(True, f"Connected to Google Drive as {email}; folder "
                            f"'{folder.get('name', self.cfg['folder_id'])}' reachable.",
                      folder=folder.get("name"))

    def _share(self, service, file_id):
        for email in self.cfg.get("share_with") or []:
            service.permissions().create(
                fileId=file_id,
                body={"type": "user", "role": "reader", "emailAddress": email},
                sendNotificationEmail=False, fields="id").execute()

    def push_reports(self, artifacts, metrics=None, bundle=None) -> dict:
        need(self.cfg, "folder_id", integration="gdrive")
        if not artifacts:
            raise IntegrationError("Nothing to upload (no reports were rendered).")
        service = build_service(self.cfg)
        folder_id = self.cfg["folder_id"]
        uploaded, primary = [], None
        for art in artifacts:
            existing = find_file(service, art.name, folder_id)
            media = _media_body(art)
            if existing:
                f = service.files().update(
                    fileId=existing, media_body=media, fields="id,webViewLink").execute()
            else:
                f = service.files().create(
                    body={"name": art.name, "parents": [folder_id]},
                    media_body=media, fields="id,webViewLink").execute()
            uploaded.append(art.name)
            self._share(service, f["id"])
            if art.kind == "executive_html" or primary is None:
                primary = f.get("webViewLink")
        shared = (self.cfg.get("share_with") or [])
        note = f" shared with {len(shared)} recipient(s)" if shared else ""
        return result(True, f"Uploaded {len(uploaded)} report(s) to Google Drive{note}.",
                      uploaded=uploaded, url=primary)
