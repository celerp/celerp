# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Business records for the packaged upgrade smoke, through the HTTP API of a
running copy. Standard library only, so it runs on a bare CI runner against
any release.

  python scripts/smoke_records.py seed http://127.0.0.1:PORT
      Registers the owner on fresh data, which writes the company's first ledger
      events, and creates a location. Exits 1 if either fails or the ledger is
      empty. Modules a fresh copy has not started yet are not needed.
  python scripts/smoke_records.py snapshot http://127.0.0.1:PORT
      Prints the version, every ledger event (id, type, entity), the locations,
      and the customers and items or the status their pages answer once
      Celerp has finished starting, as one JSON line. Two snapshots of the same data are equal when nothing was lost or
      changed, including which of those pages the company is served.
  python scripts/smoke_records.py carried BEFORE.json AFTER.json
      Compares a snapshot an older copy took with one a newer copy took of the
      same data. The older copy may not serve every module page on fresh data,
      so the ledger must match exactly, every page it served must match, and the
      newer copy must serve every page. Exits 1 with the difference otherwise.
  python scripts/smoke_records.py backup-restore http://127.0.0.1:PORT FOLDER
      On seeded data: adds a customer and an attachment named in Thai, Arabic and
      Chinese, saves a snapshot of the records and the attachment to FOLDER, downloads
      a full backup there and checks it is a backup archive, adds a location, then
      restores the backup through System Recovery and restarts Celerp (unless the
      restore already did).
  python scripts/smoke_records.py restored http://127.0.0.1:PORT FOLDER
      After that restart: the records and the attachment, bytes and name, must be
      exactly those saved before the backup.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import unquote

EMAIL = "owner@example.com"
PASSWORD = "Smoke-Upgrade-Password-1"
CUSTOMER = "บริษัท ทดสอบ شركة الاختبار 测试公司"
ATTACHMENT = "ใบแจ้งหนี้ فاتورة 发票.pdf"


def transfer(method: str, url: str, token: str | None = None, data: bytes | None = None,
             content_type: str = "application/json", timeout: float = 30) -> tuple[int, bytes, dict]:
    """One request; returns the status, the body as bytes and the headers."""
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": content_type})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def http(method: str, url: str, token: str | None = None, body: dict | None = None,
         timeout: float = 30) -> tuple[int, dict]:
    status, raw, _ = transfer(method, url, token, json.dumps(body).encode() if body is not None else None,
                              timeout=timeout)
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, {"raw": raw.decode(errors="replace")}


def upload(url: str, token: str, name: str, content: bytes, mime: str, timeout: float = 600) -> tuple[int, bytes]:
    """POST one file as the multipart field ``file``."""
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    status, raw, _ = transfer("POST", url, token, body, f"multipart/form-data; boundary={boundary}", timeout)
    return status, raw


def sent_name(disposition: str) -> str:
    """The file name a browser takes from a Content-Disposition header (filename* wins)."""
    params = dict(p.strip().split("=", 1) for p in disposition.split(";")[1:] if "=" in p)
    if "filename*" in params:
        return unquote(params["filename*"].split("'", 2)[2])
    return params.get("filename", "").strip('"')


def module_get(api: str, path: str, token: str, timeout: float = 120) -> tuple[int, dict]:
    """GET a module route. /health/ready answers once the database does, and
    module routes answer 503 "still starting" until the UI process has reported
    the modules it started; ask again, as that says. Raises RuntimeError when
    the route is still starting after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        status, body = http("GET", api + path, token)
        if not (status == 503 and isinstance(body, dict) and "still starting" in str(body.get("detail"))):
            return status, body
        if time.monotonic() > deadline:
            raise RuntimeError(f"GET {path} still answers that Celerp is starting after {timeout:.0f} s")
        time.sleep(1)


def fail(why: str) -> None:
    print(why, file=sys.stderr)
    sys.exit(1)


def login(api: str) -> str:
    status, body = http("POST", api + "/auth/login-force", body={"email": EMAIL, "password": PASSWORD})
    if status != 200:
        fail(f"login: {status} {body}")
    return body["access_token"]


def seed(api: str) -> None:
    status, body = http("POST", api + "/auth/register", body={
        "company_name": "Smoke Trading", "email": EMAIL, "name": "Owner", "password": PASSWORD})
    if status != 200:
        fail(f"register: {status} {body}")
    token = body["access_token"]
    status, body = http("POST", api + "/companies/me/locations", token, {"name": "Smoke Warehouse", "type": "warehouse"})
    if status != 200:
        fail(f"POST /companies/me/locations: {status} {body}")
    status, body = http("GET", api + "/ledger?limit=1", token)
    if status != 200 or not body.get("total"):
        fail(f"GET /ledger after registering: {status} {body}")
    print(f"registered the owner ({body['total']} ledger events) and created a location")


def snapshot(api: str) -> None:
    print(json.dumps(records(api, login(api)), sort_keys=True))


def records(api: str, token: str) -> dict:
    _, health = http("GET", api + "/health")
    status, ledger = http("GET", api + "/ledger?limit=1000", token)
    if status != 200:
        fail(f"GET /ledger: {status} {ledger}")
    served = {}
    for path, field in (("/companies/me/locations", "name"), ("/crm/contacts", "name"), ("/items", "sku")):
        try:
            status, body = module_get(api, path, token)
        except RuntimeError as exc:
            fail(str(exc))
        rows = body if isinstance(body, list) else body.get("items", [])
        served[path] = sorted(r.get(field) for r in rows) if status == 200 else status
    return {
        "version": health.get("version"),
        "ledger": [[e["id"], e["event_type"], e["entity_id"]] for e in ledger.get("items", [])],
        "served": served,
    }


def attachment(api: str, token: str, contact_id: str, file_id: str) -> dict:
    status, raw, headers = transfer("GET", f"{api}/crm/contacts/{contact_id}/files/{file_id}", token)
    if status != 200:
        fail(f"GET the attachment {file_id}: {status} {raw[:300]!r}")
    return {"name": sent_name(headers.get("content-disposition", "")), "sha256": hashlib.sha256(raw).hexdigest()}


def backup_restore(api: str, folder: str) -> None:
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    token = login(api)
    status, body = http("POST", api + "/crm/contacts", token, {"name": CUSTOMER})
    if status != 200:
        fail(f"POST /crm/contacts: {status} {body}")
    contact_id = body["id"]
    content = b"%PDF-1.4\n% smoke attachment " + uuid.uuid4().hex.encode() + b"\n%%EOF\n"
    status, raw = upload(f"{api}/crm/contacts/{contact_id}/files", token, ATTACHMENT, content, "application/pdf")
    if status != 200:
        fail(f"upload {ATTACHMENT}: {status} {raw[:300]!r}")
    file_id = json.loads(raw)["id"]
    saved = attachment(api, token, contact_id, file_id)
    if saved != {"name": ATTACHMENT, "sha256": hashlib.sha256(content).hexdigest()}:
        fail(f"the attachment came back as {saved}, not as uploaded")
    before = {"records": records(api, token), "contact_id": contact_id, "file_id": file_id, "attachment": saved}
    (out / "before.json").write_text(json.dumps(before, ensure_ascii=False), "utf-8")

    status, raw, headers = transfer("GET", api + "/backup/export", token, timeout=600)
    if status != 200:
        fail(f"GET /backup/export: {status} {raw[:300]!r}")
    name = sent_name(headers.get("content-disposition", ""))
    if not (name.startswith("celerp-backup-") and name.endswith(".celerp-backup")):
        fail(f"the full backup downloads as {name!r}")
    archive = out / name
    archive.write_bytes(raw)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getnames()
    if not {"database.dump", "meta.json"} <= set(members):
        fail(f"{name} holds no database.dump and meta.json: {members[:20]}")

    status, body = http("POST", api + "/companies/me/locations", token, {"name": "Made after the backup", "type": "warehouse"})
    if status != 200:
        fail(f"POST /companies/me/locations after the backup: {status} {body}")

    status, raw = upload(api + "/backup/import", token, name, raw, "application/gzip")
    page = raw.decode(errors="replace")
    if status != 200 or "flash--success" not in page:
        fail(f"POST /backup/import: {status} {page[:600]}")
    # A restore that changed the module set restarts by itself (its answer carries the
    # script that follows the restart); otherwise restart as the Restart button does.
    if "<script" not in page.lower():
        status, body = http("POST", api + "/system/restart", login(api))
        if status != 200:
            fail(f"POST /system/restart: {status} {body}")
    print(f"backed up {len(members)} files as {name}, restored it from {archive}, restart requested")


def restored(api: str, folder: str) -> None:
    before = json.loads((Path(folder) / "before.json").read_text("utf-8"))
    token = login(api)
    after = {"records": records(api, token), "contact_id": before["contact_id"], "file_id": before["file_id"],
             "attachment": attachment(api, token, before["contact_id"], before["file_id"])}
    if after != before:
        fail(f"the restore did not bring back the records as backed up:\n before {before}\n after  {after}")
    print(f"restored {len(after['records']['ledger'])} ledger events, the records and {ATTACHMENT} exactly")


def carried(before_path: str, after_path: str) -> None:
    before, after = (json.loads(open(p, encoding="utf-8").read()) for p in (before_path, after_path))
    if before["ledger"] != after["ledger"]:
        fail(f"the ledger changed:\n before {before['ledger']}\n after  {after['ledger']}")
    changed = {path: (rows, after["served"].get(path)) for path, rows in before["served"].items()
               if isinstance(rows, list) and after["served"].get(path) != rows}
    if changed:
        fail(f"records changed (before, after): {changed}")
    unserved = {path: status for path, status in after["served"].items() if not isinstance(status, list)}
    if unserved:
        fail(f"pages not served after the upgrade: {unserved}")
    print(f"ledger ({len(after['ledger'])} events) and records carried from {before['version']} to {after['version']}")


if __name__ == "__main__":
    # Records and names in any script, whatever code page the console or the
    # redirected output would otherwise get.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if len(sys.argv) == 4 and sys.argv[1] == "carried":
        carried(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] in ("seed", "snapshot"):
        {"seed": seed, "snapshot": snapshot}[sys.argv[1]](sys.argv[2].rstrip("/"))
    elif len(sys.argv) == 4 and sys.argv[1] in ("backup-restore", "restored"):
        {"backup-restore": backup_restore, "restored": restored}[sys.argv[1]](sys.argv[2].rstrip("/"), sys.argv[3])
    else:
        fail(__doc__)
