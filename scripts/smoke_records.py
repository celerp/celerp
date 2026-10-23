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
  python scripts/smoke_records.py first-run PORT_FILE PRESETS
      On a new install, every business type the setup page offers (each preset
      in the PRESETS folder that is not hidden) must set up a company as the
      setup page does it: the owner registers and sets up their company with the
      first type, and a new company is set up with each other one, its currency
      and timezone and then its type. The server must say a restart is needed
      exactly when a module the type turns on (with the modules it needs) is
      not running; Celerp then restarts, as the setup page does, and is followed
      onto the port PORT_FILE names. After each type every module the company
      uses must be running, and at the end every company must keep its type.
  python scripts/smoke_records.py demo PORT_FILE FOLDER SAMPLE TEXT
      Moves the SAMPLE company in as Settings > Import does (as the first owner on
      fresh data, or the owner already there), following Celerp through the restart
      onto the port PORT_FILE names. Then, with TEXT as the customer's name, the
      invoice number prefix and an attachment's name: finalizes an invoice, checks
      the attachment downloads with its bytes and name, the invoice PDF and the
      document and contact CSV exports carry TEXT, and a company backup restores
      as a new company holding the invoice and the attachment.
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


def register(api: str) -> str:
    """Registers the owner on fresh data; returns their token."""
    status, body = http("POST", api + "/auth/register", body={
        "company_name": "Smoke Trading", "email": EMAIL, "name": "Owner", "password": PASSWORD})
    if status != 200:
        fail(f"register: {status} {body}")
    return body["access_token"]


def seed(api: str) -> None:
    token = register(api)
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
    # Module routes answer only once they all do, so one read covers every write below.
    try:
        module_get(api, "/crm/contacts", token)
    except RuntimeError as exc:
        fail(str(exc))
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


def _ok(status: int, body, what: str):
    if status not in (200, 201, 202):
        fail(f"{what}: {status} {str(body)[:600]}")
    return body


def _rows(body) -> list:
    return body if isinstance(body, list) else body.get("items", [])


def _download(api: str, path: str, token: str) -> tuple[bytes, str]:
    status, raw, headers = transfer("GET", api + path, token, timeout=600)
    if status != 200:
        fail(f"GET {path}: {status} {raw[:300]!r}")
    return raw, sent_name(headers.get("content-disposition", ""))


# The roles that carry stock value (celerp.accounting_roles.INVENTORY_VALUE_ROLES; a
# test keeps the two equal): the API refuses one account for stock and anything else.
STOCK_ROLES = frozenset({"inventory", "inventory_purchased", "inventory_opening", "landed_freight",
                         "landed_insurance", "landed_duty", "landed_import_vat"})


def posting_choices(readiness: list[dict]) -> dict:
    """The posting accounts a user finishing a migration picks: for each needed role not
    already settled, the first account offered, or the proposed one to add. Stock roles
    take an account another stock role already uses; any other role skips those."""
    stock = {row["current"] or row["preselect"] for row in readiness if row["role"] in STOCK_ROLES} - {None}
    roles, add_accounts = {}, []
    for row in readiness:
        if not row["required"] or row["current"] or row["preselect"]:
            continue
        codes = [c["code"] for c in row["candidates"]]
        if row["role"] in STOCK_ROLES:
            codes = [c for c in codes if c in stock] + [c for c in codes if c not in stock]
        else:
            codes = [c for c in codes if c not in stock]
        if codes:
            roles[row["role"]] = codes[0]
        else:
            add_accounts.append({**row["proposal"], "role": row["role"]})
    return {"roles": roles, "add_accounts": add_accounts}


def _serving(port_file: str, deadline: float | None = None) -> str:
    """The API the running copy publishes in its api-port file. Celerp rewrites the
    file as it restarts, so a missing file or one that does not name a port yet is
    read again until ``deadline`` (the caller's, or 60 s from now); then the run fails."""
    deadline = time.monotonic() + 60 if deadline is None else deadline
    while True:
        try:
            value = Path(port_file).read_text().strip()
        except OSError as exc:
            value, why = "", str(exc)
        else:
            why = f"it holds {value!r}"
        if value.isdigit() and 0 < int(value) < 65536:
            return "http://127.0.0.1:" + value
        if time.monotonic() >= deadline:
            fail(f"no API port in {port_file}: {why}")
        time.sleep(0.2)


def _sample(port_file: str, sample: str) -> tuple[str, str, str]:
    """Move the sample company in as Settings > Import does and finish it: as the
    first owner on fresh data, or as the owner already there. Celerp restarts on a
    new port when the sample needs modules it has not turned on; the run is then
    followed through ``port_file``. Returns the API, the company id and a token for it."""
    api = _serving(port_file)
    status, _ = http("POST", api + "/auth/login-force", body={"email": EMAIL, "password": PASSWORD})
    owner = login(api) if status == 200 else None
    prefix = "/migrations" if owner else "/migrations/bootstrap"
    data = Path(sample).read_bytes()
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{Path(sample).name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    status, raw, _ = transfer("POST", api + prefix + "/scan", owner, body, f"multipart/form-data; boundary={boundary}", 600)
    scan = _ok(status, raw[:600], f"POST {prefix}/scan") and json.loads(raw)
    token = scan["scan_token"]
    _ok(*http("POST", api + ("/migrations/scan/decisions" if owner else prefix + "/decisions"), owner, {
        "scan_token": token, "mode": "full_history", "cutover_date": None, "mappings": {}, "prepared_by": None}),
        "migration decisions")
    name = scan["scan"].get("company_name") or "Sample company"
    if owner:
        started = _ok(*http("POST", api + "/migrations/start-from-scan", owner,
                            {"scan_token": token, "company_name": name}), "POST /migrations/start-from-scan")
        session = owner
    else:
        started = _ok(*http("POST", api + prefix + "/start", body={
            "scan_token": token, "company_name": name, "name": "Owner", "email": EMAIL, "password": PASSWORD}),
            f"POST {prefix}/start")
        session = started["access_token"]
    run = started["run_id"]
    deadline = time.monotonic() + 900
    while True:
        api = _serving(port_file, deadline)
        try:
            status, view = http("GET", f"{api}/migrations/{run}", session)
        except OSError:
            status, view = 0, {}
        if status == 200 and view["status"] == "ready_to_finalize":
            break
        if status == 200 and view["status"] in ("failed", "interrupted", "cancelled", "completed"):
            fail(f"the sample migration stopped at {view['status']}: {view.get('error')}")
        if time.monotonic() > deadline:
            fail(f"the sample migration is not ready to finish after 900 s: {status} {str(view)[:300]}")
        time.sleep(2)
    print(f"sample migration ready on {api}" + (", after the restart that turned on its modules"
                                                 if started.get("preparing") else ""))
    roles = _ok(*http("GET", f"{api}/migrations/{run}/posting-accounts", session), "posting accounts")["roles"]
    choices = posting_choices(roles)
    done = _ok(*http("POST", f"{api}/migrations/{run}/finalize", session, choices), "finalize the sample migration")
    if done["status"] != "completed":
        fail(f"finishing the sample migration left it {done['status']}: {done.get('error')}")
    switched = _ok(*http("POST", f"{api}/auth/switch-company/{done['company_id']}", login(api)), "switch to the sample")
    return api, done["company_id"], switched["access_token"]


def _modules(api: str, token: str, what: str) -> dict[str, dict]:
    """The installed modules as the Modules page lists them for the company, by name."""
    return {m["name"]: m for m in _ok(*http("GET", api + "/companies/me/modules", token), f"GET /companies/me/modules {what}")}


def turned_on(preset: list[str], modules: dict[str, dict]) -> set[str]:
    """The company's modules once it chooses a type, as the server adds them: those
    it already uses, and the type's installed modules with every module they need."""
    wanted = [n for n in preset if n in modules]
    need: set[str] = set()
    while wanted:
        name = wanted.pop()
        if name not in need:
            need.add(name)
            wanted += [d for d in modules[name]["depends_on"] if d in modules]
    return need | {n for n, m in modules.items() if m["enabled"]}


def _restarted(port_file: str, api: str, owner: str) -> str:
    """Restart Celerp as the setup page does and follow it onto its new port."""
    boot = _ok(*http("GET", api + "/health"), "GET /health")["boot"]
    _ok(*http("POST", api + "/system/restart", owner), "POST /system/restart")
    deadline = time.monotonic() + 240
    while True:
        api = _serving(port_file, deadline)
        try:
            status, body = http("GET", api + "/health", timeout=5)
            if status == 200 and body.get("boot") != boot and http("GET", api + "/health/ready", timeout=5)[0] == 200:
                return api
        except OSError:
            pass
        if time.monotonic() > deadline:
            fail("Celerp did not come back within 240 s of the restart")
        time.sleep(1)


def first_run(port_file: str, presets: str) -> None:
    offered = (json.loads(p.read_text("utf-8")) for p in Path(presets).glob("*.json"))
    types = {p["name"]: p.get("modules", []) for p in offered if not p.get("hidden")}
    if not types:
        fail(f"no business types in {presets}")
    api = _serving(port_file)
    owner = register(api)
    companies = {}
    for n, vertical in enumerate(sorted(types)):
        if n:
            token = _ok(*http("POST", api + "/companies", owner, {"name": f"Smoke {vertical}"}),
                        f"POST /companies for {vertical}")["access_token"]
        else:
            token = owner
        _ok(*http("PATCH", api + "/companies/me", token, {"currency": "USD", "timezone": "UTC"}),
            f"currency and timezone for {vertical}")
        before = _modules(api, token, f"before choosing {vertical}")
        need = turned_on(types[vertical], before)
        stopped = sorted(m for m in need if not before[m]["running"])
        chosen = _ok(*http("POST", api + "/companies/me/business-type", token, {"vertical": vertical}),
                     f"business type {vertical}")
        if bool(chosen.get("restart_required")) != bool(stopped):
            fail(f"{vertical}: the server says " + ("a restart is needed, but every module it uses is running"
                                                   if chosen.get("restart_required") else
                                                   f"no restart is needed, but {', '.join(stopped)} are not running"))
        company_id = _ok(*http("GET", api + "/companies/me", token), "GET /companies/me")["id"]
        companies[company_id] = vertical
        if stopped:
            # The setup page restarts here; without a restart it goes straight to the dashboard.
            api = _restarted(port_file, api, owner)
            owner = login(api)
            token = _ok(*http("POST", f"{api}/auth/switch-company/{company_id}", owner),
                        f"switch to the {vertical} company")["access_token"]
        after = _modules(api, token, f"after choosing {vertical}")
        for name in sorted(need):
            if not after.get(name, {}).get("enabled") or not after[name]["running"]:
                fail(f"{vertical}: {'after the restart ' if stopped else ''}{name} is not running: "
                     f"{str(after.get(name))[:300]}")
        print(f"{vertical}: set up, " + ("restarted and its modules are running" if stopped
                                         else "its modules were already running"))
    for company_id, vertical in companies.items():
        token = _ok(*http("POST", f"{api}/auth/switch-company/{company_id}", login(api)),
                    f"switch to the {vertical} company")["access_token"]
        kept = _ok(*http("GET", api + "/companies/me", token), "GET /companies/me at the end").get("settings", {}).get("vertical")
        if kept != vertical:
            fail(f"at the end the {vertical} company has business type {kept!r}")
    print(f"{len(types)} business types set up on a new install, each with its modules running")


def demo(port_file: str, folder: str, sample: str, text: str) -> None:
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    api, company_id, token = _sample(port_file, sample)
    try:
        module_get(api, "/crm/contacts", token)
    except RuntimeError as exc:
        fail(str(exc))
    contact_id = _ok(*http("POST", api + "/crm/contacts", token, {"name": text, "contact_type": "customer"}),
                     "POST /crm/contacts")["id"]
    item = _rows(_ok(*http("GET", api + "/items?limit=1", token), "GET /items"))[0]
    _ok(*http("PATCH", api + "/docs/sequences/invoice", token, {"prefix": text}), "number invoices")
    doc = _ok(*http("POST", api + "/docs", token, {
        "doc_type": "invoice", "contact_id": contact_id,
        "line_items": [{"item_id": item.get("entity_id") or item.get("id"), "name": text, "quantity": 1, "unit_price": 10}],
        "subtotal": 10, "total": 10}), "POST /docs")
    doc_id = doc.get("id") or doc.get("entity_id")
    _ok(*http("POST", f"{api}/docs/{doc_id}/finalize", token), "finalize the invoice")
    number = _ok(*http("GET", f"{api}/docs/{doc_id}", token), "GET the invoice").get("doc_number", "")
    if not number.startswith(f"{text}-"):  # the sequence adds the separator
        fail(f"the invoice is numbered {number!r}, not {text}-...")
    name = f"{text}.pdf"
    content = b"%PDF-1.4\n% " + text.encode() + b" " + uuid.uuid4().hex.encode() + b"\n%%EOF\n"
    status, raw = upload(f"{api}/docs/{doc_id}/files", token, name, content, "application/pdf")
    file_id = _ok(status, raw[:300], f"attach {name}") and json.loads(raw)["id"]
    got, got_name = _download(api, f"/docs/{doc_id}/files/{file_id}", token)
    if (got_name, got) != (name, content):
        fail(f"the attachment came back as {got_name!r} ({len(got)} bytes), not as {name!r} ({len(content)} bytes)")
    pdf, pdf_name = _download(api, f"/docs/{doc_id}/pdf", token)
    # The docs module names the PDF after the number with "/" as "-" and spaces as "_".
    if not pdf.startswith(b"%PDF") or pdf_name != f"{number}.pdf".replace("/", "-").replace(" ", "_"):
        fail(f"the invoice PDF starts {pdf[:8]!r} and downloads as {pdf_name!r}")
    for path in ("/docs/export/csv", "/crm/contacts/export/csv"):
        csv, csv_name = _download(api, path, token)
        must = number if path.startswith("/docs") else text
        if must not in csv.decode("utf-8-sig"):
            fail(f"{path} ({csv_name}) does not hold {must!r}")
    print(f"sample company moved in; invoice {number} with {name} (bytes and name as attached), "
          f"its PDF {pdf_name} and the document and contact CSV exports")

    backup, backup_name = _download(api, "/company-backups/download", token)
    (out / backup_name).write_bytes(backup)
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="mode"\r\n\r\nnew_company\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{backup_name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode() + backup + f"\r\n--{boundary}--\r\n".encode()
    status, raw, _ = transfer("POST", api + "/company-backups/read", token, body,
                              f"multipart/form-data; boundary={boundary}", 600)
    preview = _ok(status, raw[:600], f"read the company backup {backup_name}") and json.loads(raw)
    restored = _ok(*http("POST", api + "/company-backups/restore", token, {
        "upload_token": preview["upload_token"], "mode": "new_company", "plan_fingerprint": preview["plan_fingerprint"],
        "company_name": f"{text} restored"}), "restore the company backup")
    copy = restored["access_token"]
    docs = _rows(_ok(*http("GET", api + "/docs?limit=500", copy), "GET /docs in the restored company"))
    match = [d for d in docs if d.get("doc_number") == number]
    if restored["company_id"] == company_id or not match:
        fail(f"the company restored from {backup_name} holds no invoice {number}")
    copy_id = match[0].get("id") or match[0].get("entity_id")
    files = _ok(*http("GET", f"{api}/docs/{copy_id}", copy), "GET the restored invoice").get("files") or []
    kept = [f for f in files if f.get("filename") == name]
    got, got_name = _download(api, f"/docs/{copy_id}/files/{kept[0]['id']}", copy) if kept else (b"", "")
    if (got_name, got) != (name, content):
        fail(f"the restored invoice {number} lost {name}: {files}")
    print(f"company backup {backup_name} restored as {restored['company_name']!r}, with invoice {number} and {name}")


def carried(before_path: str, after_path: str) -> None:
    before, after = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (before_path, after_path))
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
    elif len(sys.argv) == 4 and sys.argv[1] == "first-run":
        first_run(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 6 and sys.argv[1] == "demo":
        demo(*sys.argv[2:])
    elif len(sys.argv) == 4 and sys.argv[1] in ("backup-restore", "restored"):
        {"backup-restore": backup_restore, "restored": restored}[sys.argv[1]](sys.argv[2].rstrip("/"), sys.argv[3])
    else:
        fail(__doc__)
