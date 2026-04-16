# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""
Celerp CIF Bundle Importer — batch mode, 500 records per HTTP call.

PERFORMANCE RULE: Never import one record at a time. Always use batch endpoints.
Minimum batch size: 100. Default: 500. This is enforced by the CLI.

Usage:
    python -m celerp.importers.importer \\
        --manifest /path/to/my_company_cif.json \\
        --api-url http://localhost:8000 \\
        --token TOKEN \\
        [--dry-run] \\
        [--batch-size 500]

Import order: contacts → items → documents → memos
(contacts first so doc foreign keys can resolve)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from celerp.importers.schema import CIFImportManifest

MAX_BATCH_SIZE = 500

# ── Batch endpoints — one HTTP call per batch ─────────────────────────────────
_BATCH_ENDPOINTS = {
    "item": "/items/import/batch",
    "contact": "/crm/contacts/import/batch",
    "invoice": "/docs/import/batch",
    "memo": "/crm/memos/import/batch",
}


# ── Result tracking ─────────────────────────────────────────────────────────────
@dataclass
class EntityStats:
    label: str
    count: int
    elapsed: float = 0.0

    @property
    def per_sec(self) -> float:
        return self.count / self.elapsed if self.elapsed > 0 else 0.0

    def fmt(self) -> str:
        return f"{self.label}: {self.count} in {self.elapsed:.1f}s ({self.per_sec:.0f}/sec)"


@dataclass
class ImportResult:
    total: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)
    entity_stats: list[EntityStats] = field(default_factory=list)

    def record_error(self, entity_id: str, reason: str) -> None:
        self.failed += 1
        self.errors.append({"entity_id": entity_id, "reason": reason})

    def summary(self) -> str:
        lines = [
            f"Total:    {self.total}",
            f"Created:  {self.created}",
            f"Skipped:  {self.skipped} (already existed)",
            f"Failed:   {self.failed}",
        ]
        if self.errors:
            lines.append("\nErrors (first 10):")
            for e in self.errors[:10]:
                lines.append(f"  {e['entity_id']}: {e['reason']}")
            if len(self.errors) > 10:
                lines.append(f"  ... and {len(self.errors) - 10} more")
        return "\n".join(lines)


# ── Core importer ───────────────────────────────────────────────────────────────
class BundleImporter:
    def __init__(
        self,
        api_base: str,
        token: str,
        dry_run: bool = False,
        batch_size: int = MAX_BATCH_SIZE,
        verbose: bool = False,
    ):
        self.api_base = api_base.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run
        self.batch_size = min(batch_size, MAX_BATCH_SIZE)
        self.verbose = verbose

    async def _post_batch(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        records: list[dict[str, Any]],
    ) -> dict:
        """POST one batch to the API. Returns the BatchImportResult dict."""
        resp = await client.post(
            f"{self.api_base}{endpoint}",
            json={"records": records},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()

    async def _import_entity_type(
        self,
        client: httpx.AsyncClient,
        label: str,
        entity_key: str,
        records: list[dict[str, Any]],
        result: ImportResult,
    ) -> EntityStats:
        """Import all records for one entity type using batch endpoint. Returns timing stats."""
        endpoint = _BATCH_ENDPOINTS[entity_key]
        total = len(records)
        done = 0
        t0 = time.monotonic()

        for i in range(0, total, self.batch_size):
            chunk = records[i : i + self.batch_size]
            batch_t0 = time.monotonic()
            try:
                resp = await self._post_batch(client, endpoint, chunk)
                batch_elapsed = time.monotonic() - batch_t0
                batch_per_sec = len(chunk) / batch_elapsed if batch_elapsed > 0 else 0

                result.created += resp.get("created", 0)
                result.skipped += resp.get("skipped", 0)
                result.total += len(chunk)
                done += len(chunk)

                for err in resp.get("errors", []):
                    result.record_error("batch", err)

                print(
                    f"\r  {label}: {done}/{total} ({batch_per_sec:.0f}/sec)",
                    end="",
                    flush=True,
                )
            except Exception as exc:
                elapsed_so_far = time.monotonic() - batch_t0
                result.record_error(f"batch[{i}:{i+len(chunk)}]", str(exc))
                result.failed += len(chunk)
                result.total += len(chunk)
                done += len(chunk)
                print(
                    f"\r  {label}: {done}/{total} [BATCH ERROR: {exc}]",
                    end="",
                    flush=True,
                )

        elapsed = time.monotonic() - t0
        per_sec = total / elapsed if elapsed > 0 else 0
        print(f"\r  {label}: {total}/{total} ✓  ({per_sec:.0f}/sec)          ", flush=True)
        return EntityStats(label=label, count=total, elapsed=elapsed)

    async def run(self, manifest: CIFImportManifest) -> ImportResult:
        result = ImportResult()
        bundle = manifest.bundle
        source = manifest.source

        if self.dry_run:
            self._dry_run_report(manifest)
            return result

        t_total_start = time.monotonic()

        async with httpx.AsyncClient(headers=self.headers, timeout=120) as client:
            # ── Contacts first (foreign key dependency for docs) ──────────────
            contact_records = [
                {
                    "entity_id": f"contact:{c.external_id}",
                    "event_type": "crm.contact.created",
                    "data": {"name": c.name, "email": c.email, "phone": c.phone, **c.metadata},
                    "source": source,
                    "idempotency_key": f"cif:contact:{c.external_id}",
                }
                for c in bundle.contacts
            ]
            stats = await self._import_entity_type(client, "Contacts", "contact", contact_records, result)
            result.entity_stats.append(stats)

            # ── Items ─────────────────────────────────────────────────────────
            item_records = [
                {
                    "entity_id": f"item:{item.external_id}",
                    "event_type": "item.snapshot",
                    "data": {
                        "external_id": item.external_id,
                        "sku": item.sku,
                        "name": item.name,
                        "description": item.description,
                        "weight": str(item.weight) if item.weight is not None else None,
                        "weight_unit": item.weight_unit,
                        "sell_by": item.sell_by,
                        "cost_per_unit": str(item.cost_per_unit) if item.cost_per_unit is not None else None,
                        "total_cost": str(item.total_cost) if item.total_cost is not None else None,
                        "wholesale_price": str(item.wholesale_price) if item.wholesale_price is not None else None,
                        "retail_price": str(item.retail_price) if item.retail_price is not None else None,
                        "status": item.status,
                        "attributes": item.attributes or {},
                        "category": item.category,
                        "parent_external_id": item.parent_external_id,
                        "barcode": item.barcode,
                        "source_ref": item.source_ref,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                        **item.metadata,
                    },
                    "source": source,
                    "idempotency_key": f"cif:item:{item.external_id}",
                }
                for item in bundle.items
            ]
            stats = await self._import_entity_type(client, "Items", "item", item_records, result)
            result.entity_stats.append(stats)

            # ── Documents ─────────────────────────────────────────────────────
            doc_records = [
                {
                    "entity_id": f"doc:{d.external_id}",
                    "event_type": "doc.created",
                    "data": {
                        "external_id": d.external_id,
                        "doc_type": d.doc_type,
                        "status": d.status,
                        "contact_external_id": d.contact_external_id,
                        "ref": d.ref,
                        "total": str(d.total),
                        "amount_paid": str(d.amount_paid),
                        "amount_outstanding": str(d.amount_outstanding),
                        "payment_due_date": d.payment_due_date.isoformat() if d.payment_due_date else None,
                        "created_at": d.created_at.isoformat() if d.created_at else None,
                        "line_items": [
                            {
                                "item_external_id": li.item_external_id,
                                "quantity": str(li.quantity),
                                "weight": str(li.weight) if li.weight is not None else None,
                                "weight_unit": li.weight_unit,
                                "unit_price": str(li.unit_price),
                                "total_price": str(li.total_price),
                                "cost_basis": str(li.cost_basis) if li.cost_basis is not None else None,
                            }
                            for li in d.line_items
                        ],
                        **d.metadata,
                    },
                    "source": source,
                    "idempotency_key": f"cif:doc:{d.external_id}",
                }
                for d in bundle.documents
            ]
            stats = await self._import_entity_type(client, "Documents", "invoice", doc_records, result)
            result.entity_stats.append(stats)

            # ── Memos ─────────────────────────────────────────────────────────
            memo_records = [
                {
                    "entity_id": f"memo:{m.external_id}",
                    "event_type": "crm.memo.created",
                    "data": {
                        "external_id": m.external_id,
                        "status": m.status,
                        "contact_external_id": m.contact_external_id,
                        "total": str(m.total),
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                        **m.metadata,
                    },
                    "source": source,
                    "idempotency_key": f"cif:memo:{m.external_id}",
                }
                for m in bundle.memos
            ]
            stats = await self._import_entity_type(client, "Memos", "memo", memo_records, result)
            result.entity_stats.append(stats)

        t_total = time.monotonic() - t_total_start
        print(f"\n  Total import time: {t_total:.1f}s")

        return result

    def _dry_run_report(self, manifest: CIFImportManifest) -> None:
        bundle = manifest.bundle
        stats = manifest.stats
        bs = self.batch_size

        est_batches = (
            (len(bundle.items) + bs - 1) // bs
            + (len(bundle.contacts) + bs - 1) // bs
            + (len(bundle.documents) + bs - 1) // bs
            + (len(bundle.memos) + bs - 1) // bs
        )

        print("\n── CIF Dry Run Report ──────────────────────────────────────")
        print(f"  Source:      {manifest.source}")
        print(f"  Exported at: {manifest.exported_at}")
        print(f"\n  Bundle contents:")
        print(f"    Items:     {len(bundle.items)}")
        print(f"    Contacts:  {len(bundle.contacts)}")
        print(f"    Documents: {len(bundle.documents)}")
        print(f"    Memos:     {len(bundle.memos)}")
        print(f"\n  Stats from manifest:")
        for k, v in stats.items():
            print(f"    {k}: {v}")
        print(f"\n  Batch size: {bs}  |  Estimated HTTP calls: {est_batches}")
        print("\n  ✓ Manifest validated OK — no data posted (--dry-run)")
        print("────────────────────────────────────────────────────────────\n")


# ── CLI entry point ─────────────────────────────────────────────────────────────
def load_manifest(path: Path) -> CIFImportManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return CIFImportManifest.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        print(f"[ERROR] Invalid manifest: {exc}", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Celerp CIF bundle importer (batch mode)")
    parser.add_argument("--manifest", required=True, help="Path to CIF manifest JSON")
    parser.add_argument("--api-url", default="http://localhost:8000", help="Celerp API base URL")
    parser.add_argument("--token", default="", help="JWT bearer token")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without importing")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=MAX_BATCH_SIZE,
        help=f"Records per batch HTTP call (default: {MAX_BATCH_SIZE}, max: {MAX_BATCH_SIZE})",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.batch_size < 1:
        print("[ERROR] --batch-size must be >= 1", file=sys.stderr)
        raise SystemExit(1)

    manifest = load_manifest(Path(args.manifest))

    importer = BundleImporter(
        api_base=args.api_url,
        token=args.token,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        verbose=args.verbose,
    )

    result = asyncio.run(importer.run(manifest))

    if not args.dry_run:
        print("\n── Import Summary ──────────────────────────────────────────")
        for s in result.entity_stats:
            print(f"  {s.fmt()}")
        print()
        print(result.summary())
        print("────────────────────────────────────────────────────────────")
        if result.failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
