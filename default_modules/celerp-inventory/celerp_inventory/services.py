# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

import hashlib
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.events.engine import emit_event, find_event_by_idempotency
from celerp.inventory_codes import (
    BarcodeConflictError,
    RfidEpcConflictError,
    normalize_rfid_epc,
    validate_barcode,
    validate_rfid_epc,
)
from celerp.models.company import Company, Location
from celerp.models.projections import Projection
from celerp.importers.tabular import CsvImportSpec
from celerp.services.field_schema import AMOUNT_ITEM_KEYS
from celerp.services.money import to_stored_float, unit_price_from_total
from celerp.services.permissions import role_has_permission
from celerp.services.pricing import derived_price_keys, get_price_config, is_derived, price_key
from celerp.services.units import (
    build_unit_map,
    get_company_units,
    is_pieces_unit,
    is_weight_unit,
)

# Internally assigned SKUs/barcodes are short zero-padded sequences; imported
# EAN-13/GTIN-14 barcodes (13-14 digits) are excluded from the sequence scan so
# they are never re-used as the next internal code.
_MAX_SEQ_DIGITS = 9
_SEQ_WIDTH = 6


async def lock_item_code_namespace(session: AsyncSession, company_id) -> None:
    """Serialize SKU/barcode allocation for a company.

    Two concurrent creates each read the same max sequence and mint the same next
    code; the barcode unique index then rejects the loser with a 409. Taking a row
    lock on the company here makes the second allocator wait for the first to
    commit, so it reads the updated max and mints the next code instead of colliding.
    The lock is held until the caller's transaction commits or rolls back; every
    allocation and barcode check in that request must run after this call.

    The mode is FOR NO KEY UPDATE, not FOR UPDATE. Every ledger insert takes an
    implicit foreign-key KEY SHARE lock on its company row and holds it to commit,
    so a plain FOR UPDATE here would have to upgrade past that share lock: two
    transactions that have each already emitted an event for the company both hold
    KEY SHARE and then block on each other's row lock, which PostgreSQL breaks by
    aborting one with a deadlock (40P01). FOR NO KEY UPDATE does not conflict with
    KEY SHARE, so the upgrade never happens, while it still conflicts with another
    FOR NO KEY UPDATE, keeping barcode allocators serialized for every module.
    """
    await session.execute(
        select(Company.id).where(Company.id == company_id).with_for_update(key_share=True)
    )


async def _next_seq(session: AsyncSession, company_id) -> int:
    """Return the next integer in the shared SKU/barcode sequence for a company.

    Scans integer-valued SKUs and barcodes together so the two namespaces never
    collide (a barcode assigned during a split is never re-used as a SKU on the
    next create). Only barcodes with <= _MAX_SEQ_DIGITS digits count, excluding
    imported EAN-13/GTIN-14 barcodes while covering every internally assigned one.
    """
    sku_vals = (await session.execute(
        select(Projection.state["sku"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    barcode_vals = (await session.execute(
        select(Projection.state["barcode"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    all_vals = list(sku_vals) + [v for v in barcode_vals if v and len(v) <= _MAX_SEQ_DIGITS]
    return max((int(v) for v in all_vals if v and str(v).isdigit()), default=0) + 1


async def allocate_internal_codes(session: AsyncSession, company_id, count: int = 1) -> list[str]:
    """Lock the company's code namespace and return ``count`` fresh codes, each free.

    The lock makes the scan-then-mint atomic against concurrent allocators. Codes are
    zero-padded to the standard internal width and are guaranteed distinct within the
    returned batch. Starting from the next sequential value, any candidate already held
    as a barcode OR an rfid_epc is skipped: the two fields share one physical-code
    namespace, and the sequence scan counts integer SKUs and barcodes only, so a numeric
    EPC can equal the next sequential code. Skipping here is the single guard every
    internal-mint path inherits, so no caller re-implements the availability check.
    """
    await lock_item_code_namespace(session, company_id)
    codes: list[str] = []
    candidate = await _next_seq(session, company_id)
    while len(codes) < count:
        code = str(candidate).zfill(_SEQ_WIDTH)
        if not await _code_in_use(session, company_id, code):
            codes.append(code)
        candidate += 1
    return codes


async def _code_in_use(
    session: AsyncSession, company_id, code, *, exclude_entity_id=None
) -> bool:
    """True when ``code`` already occupies EITHER physical-code slot of another item.

    A barcode and an RFID / EPC are both physical-code identifiers drawn from one
    namespace, so a value in use as a barcode is not free to reuse as an EPC and vice
    versa. This single query over BOTH ``state ->> 'barcode'`` and
    ``state ->> 'rfid_epc'`` is the sole cross-field collision check; both writers call
    it under ``lock_item_code_namespace`` so the read-then-write is serialized.
    ``exclude_entity_id`` skips one item's own row so re-asserting an item's current
    value is not read as a self-collision.
    """
    if not code:
        return False
    value = str(code)
    query = select(Projection.entity_id).where(
        Projection.company_id == company_id,
        Projection.entity_type == "item",
        or_(
            Projection.state["barcode"].as_string() == value,
            Projection.state["rfid_epc"].as_string() == value,
        ),
    )
    if exclude_entity_id is not None:
        query = query.where(Projection.entity_id != exclude_entity_id)
    return (await session.execute(query)).first() is not None


async def assert_barcode_available(
    session: AsyncSession, company_id, barcode, *, exclude_entity_id=None
) -> None:
    """Raise BarcodeConflictError if another item in the company already holds ``barcode``.

    An empty or absent barcode is always available. This is the application-side
    check that yields a clean 409; the DB unique index is the final backstop for
    writers that bypass it. ``exclude_entity_id`` skips one item's own row so a
    barcode change that re-asserts the item's current value is not read as a
    self-collision.
    """
    if not barcode:
        return
    if await _code_in_use(session, company_id, barcode, exclude_entity_id=exclude_entity_id):
        raise BarcodeConflictError(barcode)


async def assert_rfid_epc_available(
    session: AsyncSession, company_id, rfid_epc, *, exclude_entity_id=None
) -> None:
    """Raise RfidEpcConflictError if another item in the company already holds ``rfid_epc``.

    Mirrors ``assert_barcode_available``: an empty or absent value is always available,
    the value is normalized (trimmed + upper-cased) before the check so lookup matches
    storage, and the shared ``_code_in_use`` query catches a collision against either
    physical-code slot. The DB unique index is the final backstop.
    """
    normalized = normalize_rfid_epc(rfid_epc)
    if not normalized:
        return
    if await _code_in_use(session, company_id, normalized, exclude_entity_id=exclude_entity_id):
        raise RfidEpcConflictError(normalized)


async def create_item(session, company_id: str, data: dict, actor_id: str | None = None):
    entity_id = data.get("entity_id", f"item:{uuid.uuid4()}")
    return await emit_event(
        session,
        company_id=company_id,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data=data,
        actor_id=actor_id,
        location_id=data.get("location_id"),
        source="api",
        idempotency_key=data.get("idempotency_key", str(uuid.uuid4())),
        metadata_={},
    )



def _legacy_external_link(platform: str, idem_key: str) -> dict:
    """Decode connector identity stored by releases before external_links existed."""
    parts = (idem_key or "").split(":")
    if platform == "shopify" and len(parts) >= 3:
        return {"product_id": parts[1], "variant_id": parts[2], "sync_enabled": True}
    if platform == "woocommerce" and len(parts) >= 2:
        link = {"product_id": parts[1], "sync_enabled": True}
        if len(parts) >= 3 and parts[2]:
            link["variation_id"] = parts[2]
        return link
    return {}


def external_link_for_state(state: dict, platform: str) -> dict:
    """Return one normalized external product link without mutating item state."""
    links = state.get("external_links") or {}
    raw = links.get(platform) if isinstance(links, dict) else None
    if isinstance(raw, dict) and raw.get("detached") is True:
        return {}
    if isinstance(raw, dict) and raw.get("product_id") not in (None, ""):
        return dict(raw)
    return _legacy_external_link(platform, str(state.get("idempotency_key") or ""))


def _external_ids(platform: str, state: dict) -> dict:
    """Flatten one normalized link into connector adapter field names."""
    link = external_link_for_state(state, platform)
    if not link:
        return {}
    if platform == "shopify":
        return {
            "shopify_product_id": str(link.get("product_id") or ""),
            "shopify_variant_id": str(link.get("variant_id") or ""),
        }
    if platform == "woocommerce":
        out = {"woocommerce_product_id": str(link.get("product_id") or "")}
        if link.get("variation_id") not in (None, ""):
            out["woocommerce_variation_id"] = str(link["variation_id"])
        return out
    return {}


def normalize_sku(value) -> str:
    """Canonical SKU comparison key."""
    return str(value or "").strip().casefold()


def _is_structural_product_anchor_state(state: dict) -> bool:
    """True when a row is structurally a product root, independent of physical codes."""
    return (
        str(state.get("status") or "").lower() != "merged"
        and not any((
            state.get("catalog_item_id"),
            state.get("lot"),
            state.get("parent_item_id"),
            state.get("split_from"),
            state.get("transformed_from"),
        ))
    )


def _is_product_anchor_state(state: dict) -> bool:
    """Infer a product root only when historical state is unambiguous."""
    if not _is_structural_product_anchor_state(state):
        return False
    links = state.get("external_links") or {}
    if isinstance(links, dict) and any(
        isinstance(link, dict) and link.get("product_id") not in (None, "")
        for link in links.values()
    ):
        return True
    idem = str(state.get("idempotency_key") or "")
    if idem.startswith(("shopify:", "woocommerce:")):
        return True
    return not bool(state.get("barcode") or state.get("rfid_epc"))


def _external_variant_key(platform: str) -> str:
    return "variant_id" if platform == "shopify" else "variation_id"


def _same_external_identity(
    platform: str, link: dict, product_id: str, variation_id: str | None
) -> bool:
    if str(link.get("product_id") or "") != str(product_id):
        return False
    actual = link.get(_external_variant_key(platform))
    return (str(actual) if actual not in (None, "") else None) == (
        str(variation_id) if variation_id not in (None, "") else None
    )


def _select_external_anchor(rows: list[Projection], platform: str, product_id: str,
                            variation_id: str | None) -> Projection | None:
    matches = [
        r for r in rows
        if _same_external_identity(
            platform, external_link_for_state(r.state or {}, platform),
            product_id, variation_id,
        )
    ]
    if not matches:
        return None
    roots = [r for r in matches if _is_product_anchor_state(r.state or {})]
    if len(roots) == 1:
        return roots[0]
    if len(roots) > 1:
        explicit = [
            r for r in roots
            if isinstance(((r.state or {}).get("external_links") or {}).get(platform), dict)
        ]
        if len(explicit) == 1:
            return explicit[0]
        raise ValueError(
            f"Multiple catalog items claim {platform} product {product_id}"
            + (f" variation {variation_id}" if variation_id else "")
        )
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"Multiple inventory rows claim {platform} product {product_id}"
        + (f" variation {variation_id}" if variation_id else "")
    )


async def resolve_external_product(
    session: AsyncSession,
    company_id,
    platform: str,
    product_id: str,
    variation_id: str | None = None,
    sku: str | None = None,
) -> Projection | None:
    """Resolve external identity first, then one unambiguous catalog SKU."""
    cid = uuid.UUID(str(company_id))
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == cid,
            Projection.entity_type == "item",
        )
    )).scalars().all()

    linked = _select_external_anchor(rows, platform, str(product_id), variation_id)
    if linked is not None:
        return linked

    norm_sku = str(sku or "").strip().casefold()
    if not norm_sku:
        return None
    candidates = [
        r for r in rows
        if _is_product_anchor_state(r.state or {})
        and str((r.state or {}).get("sku") or "").strip().casefold() == norm_sku
    ]
    if len(candidates) == 1:
        candidate = candidates[0]
        existing_link = external_link_for_state(candidate.state or {}, platform)
        if (
            existing_link
            and not _same_external_identity(
                platform, existing_link, str(product_id), variation_id
            )
            and not deleted_external_link_may_relink(existing_link)
        ):
            raise ValueError(
                f"SKU {sku!r} is already linked to a different {platform} product"
            )
        return candidate
    if len(candidates) > 1:
        raise ValueError(f"SKU {sku!r} matches multiple catalog products")
    return None


def _connector_event_idem(prefix: str, payload: dict) -> str:
    content = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha1(content.encode()).hexdigest()[:16]}"


def deleted_external_link_may_relink(link: dict | None) -> bool:
    """Only remote deletion, never an intentional user disable, permits identity replacement."""
    return bool(link and link.get("remote_deleted") is True)


def external_link_intentionally_disabled(link: dict | None) -> bool:
    """A user-disabled live link blocks product-side inbound mutation."""
    return bool(
        link
        and link.get("sync_enabled") is False
        and link.get("remote_deleted") is not True
    )


def relinked_external_sync_enabled(link: dict | None) -> bool:
    """Replacing a dead remote identity preserves the user's prior sync preference."""
    if not link:
        return True
    return bool(link.get("sync_enabled", True))


async def upsert_external_product(
    company_id: str,
    *,
    platform: str,
    product_id: str,
    variation_id: str | None,
    sku: str,
    name: str,
    description: str | None = None,
    sale_price: float | None = None,
    quantity: float | None = None,
    seed_quantity: bool = False,
    link_fields: dict | None = None,
    inventory_type: str | None = None,
    sell_by: str | None = None,
) -> tuple[str, str]:
    """Create or link one external product without making the connector an inventory engine."""
    from celerp.db import SessionLocal as AsyncSessionLocal

    cid = uuid.UUID(str(company_id))
    product_id = str(product_id)
    variation_id = str(variation_id) if variation_id not in (None, "") else None
    identity = f"{platform}:{product_id}" + (f":{variation_id}" if variation_id else "")
    sku_lock = str(sku or "").strip().casefold()
    lock_key = f"external-product:{cid}:{platform}:{sku_lock or identity}"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": lock_key},
        )
        row = await resolve_external_product(
            session, cid, platform, product_id, variation_id, sku=sku
        )
        legacy_row = None
        legacy_cleaned = False
        if row is not None and not _is_product_anchor_state(row.state or {}):
            legacy_row = row
            row = await resolve_catalog_anchor_for_item(
                session, cid, legacy_row.entity_id
            )
        if row is not None:
            row = await session.get(
                Projection,
                {"company_id": cid, "entity_id": row.entity_id},
                with_for_update=True,
                populate_existing=True,
            )

        incoming_link = {
            "product_id": product_id,
            "sync_enabled": True,
            "remote_deleted": False,
            **(link_fields or {}),
        }
        if variation_id:
            incoming_link[_external_variant_key(platform)] = variation_id

        if row is None:
            entity_id = f"item:{identity}"
            data: dict = {
                "sku": sku,
                "name": name,
                "sell_by": sell_by or "piece",
                "external_links": {platform: incoming_link},
                "idempotency_key": identity,
            }
            if inventory_type is not None:
                data["inventory_type"] = inventory_type
            if description is not None:
                data["description"] = description
            if sale_price is not None:
                data["sale_price"] = sale_price
                data["retail_price"] = sale_price
            if seed_quantity and quantity is not None:
                data["quantity"] = float(quantity)
            event_idem = _connector_event_idem(f"{identity}:create", data)
            entry = await emit_event(
                session, company_id=cid, entity_id=entity_id, entity_type="item",
                event_type="item.created", data=data, actor_id=None, location_id=None,
                source="connector", idempotency_key=event_idem, metadata_={},
            )
            await session.commit()
            return ("noop" if getattr(entry, "was_deduped", False) else "created", entity_id)

        entity_id = row.entity_id
        state = dict(row.state or {})

        if legacy_row is not None and legacy_row.entity_id != entity_id:
            legacy_state = dict(legacy_row.state or {})
            legacy_changes: dict = {}
            if str(legacy_state.get("idempotency_key") or "") == identity:
                legacy_changes["idempotency_key"] = {
                    "old": legacy_state.get("idempotency_key"), "new": None,
                }
            legacy_links = dict(legacy_state.get("external_links") or {})
            if platform in legacy_links:
                cleaned_links = dict(legacy_links)
                cleaned_links.pop(platform, None)
                legacy_changes["external_links"] = {
                    "old": legacy_links, "new": cleaned_links,
                }
            if legacy_changes:
                cleanup_data = {"fields_changed": legacy_changes}
                await emit_event(
                    session,
                    company_id=cid,
                    entity_id=legacy_row.entity_id,
                    entity_type="item",
                    event_type="item.updated",
                    data=cleanup_data,
                    actor_id=None,
                    location_id=None,
                    source="connector",
                    idempotency_key=_connector_event_idem(
                        f"{identity}:legacy-clean:{legacy_row.entity_id}:v{legacy_row.version}",
                        cleanup_data,
                    ),
                    metadata_={},
                )
                legacy_cleaned = True
        explicit = ((state.get("external_links") or {}).get(platform)
                    if isinstance(state.get("external_links"), dict) else None)
        if external_link_intentionally_disabled(explicit):
            if legacy_cleaned:
                await session.commit()
            return "disabled", entity_id

        links = dict(state.get("external_links") or {})
        previous_link = external_link_for_state(state, platform)
        links[platform] = {
            **previous_link,
            **incoming_link,
            "sync_enabled": relinked_external_sync_enabled(previous_link),
        }
        desired = {"sku": sku, "name": name, "external_links": links}
        if inventory_type is not None:
            desired["inventory_type"] = inventory_type
        if sell_by is not None:
            desired["sell_by"] = sell_by
        if description is not None:
            desired["description"] = description
        if sale_price is not None:
            desired["sale_price"] = sale_price
            desired["retail_price"] = sale_price

        fields_changed = {
            key: {"old": state.get(key), "new": value}
            for key, value in desired.items() if state.get(key) != value
        }
        if not fields_changed:
            if legacy_cleaned:
                await session.commit()
            return "noop", entity_id

        event_data = {"fields_changed": fields_changed}
        entry = await emit_event(
            session, company_id=cid, entity_id=entity_id, entity_type="item",
            event_type="item.updated", data=event_data, actor_id=None, location_id=None,
            source="connector",
            idempotency_key=_connector_event_idem(
                f"{identity}:update:{entity_id}:v{row.version}", event_data
            ),
            metadata_={},
        )
        await session.commit()
        if links[platform].get("sync_enabled") is False:
            # Identity repair for a remotely deleted product is allowed even when the
            # user intentionally left product sync disabled, but callers must not
            # continue with product-side mutation such as media pulls.
            return "disabled", entity_id
        return ("noop" if getattr(entry, "was_deduped", False) else "updated", entity_id)


async def set_external_link_state(
    session: AsyncSession, company_id, entity_id: str, platform: str, *,
    sync_enabled: bool | None = None, remote_deleted: bool | None = None,
    link_updates: dict | None = None, actor_id=None, source: str = "connector",
) -> dict:
    """Patch one external link while preserving every other channel identity."""
    cid = uuid.UUID(str(company_id))
    row = await session.get(Projection, {"company_id": cid, "entity_id": entity_id}, with_for_update=True)
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    current = external_link_for_state(row.state or {}, platform)
    if not current:
        raise ValueError(f"Item {entity_id!r} is not linked to {platform}")
    updated = dict(current)
    if sync_enabled is not None: updated["sync_enabled"] = bool(sync_enabled)
    if remote_deleted is not None: updated["remote_deleted"] = bool(remote_deleted)
    if link_updates: updated.update(link_updates)
    return await set_external_link(session, cid, entity_id, platform, updated,
                                   actor_id=actor_id, source=source)


async def set_external_link(
    session: AsyncSession, company_id, entity_id: str, platform: str, link: dict,
    *, actor_id=None, source: str = "connector",
) -> dict:
    """Create or replace one channel link without touching any other channel."""
    cid = uuid.UUID(str(company_id))
    row = await session.get(Projection, {"company_id": cid, "entity_id": entity_id}, with_for_update=True)
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    state = dict(row.state or {})
    links = dict(state.get("external_links") or {})
    normalized = dict(link)
    normalized["product_id"] = str(normalized["product_id"])
    variant_key = _external_variant_key(platform)
    if normalized.get(variant_key) not in (None, ""):
        normalized[variant_key] = str(normalized[variant_key])
    links[platform] = normalized
    if links == (state.get("external_links") or {}):
        return normalized
    data = {"fields_changed": {"external_links": {"old": state.get("external_links") or {}, "new": links}}}
    await emit_event(
        session, company_id=cid, entity_id=entity_id, entity_type="item",
        event_type="item.updated", data=data, actor_id=actor_id, location_id=None,
        source=source,
        idempotency_key=_connector_event_idem(f"external-link:{platform}:{entity_id}:v{row.version}", data),
        metadata_={},
    )
    return normalized


async def detach_external_link(
    session: AsyncSession, company_id, entity_id: str, platform: str, *,
    actor_id=None, source: str = "connector_ui",
) -> bool:
    """Detach one platform identity while preserving local item and other channels."""
    cid = uuid.UUID(str(company_id))
    row = await session.get(
        Projection, {"company_id": cid, "entity_id": entity_id}, with_for_update=True
    )
    if row is None or row.entity_type != "item":
        return False
    state = dict(row.state or {})
    links = dict(state.get("external_links") or {})
    raw = links.get(platform) if isinstance(links, dict) else None
    if isinstance(raw, dict) and raw.get("detached") is True:
        return False
    if not external_link_for_state(state, platform):
        return False
    links[platform] = {"detached": True}
    data = {
        "fields_changed": {
            "external_links": {
                "old": state.get("external_links") or {},
                "new": links,
            }
        }
    }
    await emit_event(
        session, company_id=cid, entity_id=entity_id, entity_type="item",
        event_type="item.updated", data=data, actor_id=actor_id, location_id=None,
        source=source,
        idempotency_key=_connector_event_idem(
            f"external-detach:{platform}:{entity_id}:v{row.version}", data
        ),
        metadata_={},
    )
    return True


async def detach_external_links_for_platform(
    session: AsyncSession, company_id, platform: str, *, actor_id=None
) -> int:
    """Detach every item identity for one platform in the caller's transaction."""
    cid = uuid.UUID(str(company_id))
    entity_ids = (await session.execute(
        select(Projection.entity_id).where(
            Projection.company_id == cid,
            Projection.entity_type == "item",
        )
    )).scalars().all()
    detached = 0
    for entity_id in entity_ids:
        if await detach_external_link(
            session, cid, entity_id, platform, actor_id=actor_id
        ):
            detached += 1
    return detached


async def resolve_catalog_anchor_for_item(session: AsyncSession, company_id, entity_id: str) -> Projection:
    """Resolve a selected catalog or lot row to one unambiguous product anchor."""
    cid = uuid.UUID(str(company_id))
    row = await session.get(Projection, {"company_id": cid, "entity_id": entity_id})
    if row is None or row.entity_type != "item":
        raise ValueError(f"Item {entity_id!r} not found")
    state = row.state or {}

    catalog_item_id = state.get("catalog_item_id")
    if catalog_item_id:
        parent = await session.get(
            Projection, {"company_id": cid, "entity_id": str(catalog_item_id)}
        )
        if (
            parent is None
            or parent.entity_type != "item"
            or not _is_structural_product_anchor_state(parent.state or {})
        ):
            raise ValueError(f"Item {entity_id!r} references an invalid catalog product anchor")
        return parent

    parent_item_id = state.get("parent_item_id")
    if parent_item_id:
        parent = await session.get(
            Projection, {"company_id": cid, "entity_id": str(parent_item_id)}
        )
        if (
            parent is not None
            and parent.entity_type == "item"
            and _is_structural_product_anchor_state(parent.state or {})
        ):
            return parent

    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    ))).scalars().all()
    key = _family_keys(rows).get(row.entity_id)
    if key and key[0] == "anchor":
        anchor = next(
            (candidate for candidate in rows if candidate.entity_id == key[1]),
            None,
        )
        if (
            anchor is not None
            and _is_structural_product_anchor_state(anchor.state or {})
        ):
            return anchor

    sku = normalize_sku(state.get("sku"))
    if not sku:
        raise ValueError(f"Item {entity_id!r} has no catalog SKU to resolve")
    raise ValueError(f"SKU {state.get('sku')!r} does not resolve to one catalog product anchor")


def _is_explicit_catalog_anchor_state(state: dict) -> bool:
    """True for a structural root carrying durable catalog identity/history."""
    if not _is_structural_product_anchor_state(state):
        return False
    links = state.get("external_links") or {}
    if isinstance(links, dict) and any(
        isinstance(link, dict)
        and link.get("detached") is not True
        and link.get("product_id") not in (None, "")
        for link in links.values()
    ):
        return True
    idem = str(state.get("idempotency_key") or "")
    return bool(state.get("_catalog_sku_aliases")) or idem.startswith(
        ("shopify:", "woocommerce:")
    )


def _family_keys(rows: list[Projection]) -> dict[str, tuple[str, str]]:
    """Resolve structural catalog families, using SKU only for legacy inference."""
    roots_by_sku: dict[str, list[Projection]] = {}
    explicit_by_sku: dict[str, list[Projection]] = {}

    for row in rows:
        state = row.state or {}
        sku = normalize_sku(state.get("sku"))
        if _is_product_anchor_state(state) and sku:
            roots_by_sku.setdefault(sku, []).append(row)
        if not _is_explicit_catalog_anchor_state(state):
            continue
        sku_keys = {sku}
        sku_keys.update(
            normalize_sku(value)
            for value in (state.get("_catalog_sku_aliases") or [])
        )
        for key in sku_keys:
            if key:
                explicit_by_sku.setdefault(key, []).append(row)

    keys: dict[str, tuple[str, str]] = {}
    for row in rows:
        state = row.state or {}
        catalog_item_id = state.get("catalog_item_id")
        if catalog_item_id:
            keys[row.entity_id] = ("anchor", str(catalog_item_id))
            continue

        sku = normalize_sku(state.get("sku"))
        explicit = {
            candidate.entity_id: candidate
            for candidate in (explicit_by_sku.get(sku, []) if sku else [])
        }
        if len(explicit) == 1:
            keys[row.entity_id] = ("anchor", next(iter(explicit)))
            continue
        if len(explicit) > 1:
            keys[row.entity_id] = ("sku", sku)
            continue

        if _is_product_anchor_state(state):
            keys[row.entity_id] = ("anchor", row.entity_id)
            continue

        roots = {
            candidate.entity_id: candidate
            for candidate in (roots_by_sku.get(sku, []) if sku else [])
        }
        keys[row.entity_id] = (
            ("anchor", next(iter(roots)))
            if len(roots) == 1
            else ("sku", sku)
        )
    return keys


def catalog_family_rows(
    rows: list[Projection], anchor: Projection
) -> list[Projection]:
    """Return rows belonging to an anchor's canonical product family."""
    keys = _family_keys(rows)
    key = keys.get(anchor.entity_id)
    if key is None:
        return []
    return [row for row in rows if keys.get(row.entity_id) == key]


async def aggregate_sellable_quantity_for_anchor(
    session: AsyncSession, company_id, anchor: Projection
) -> float:
    """Aggregate currently sellable stock for one catalog product family."""
    from celerp_inventory.projections import is_item_available

    cid = uuid.UUID(str(company_id))
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    ))).scalars().all()
    return sum(
        float((row.state or {}).get("quantity") or 0)
        for row in catalog_family_rows(rows, anchor)
        if is_item_available(row.state or {})
    )


async def aggregate_sellable_quantity_for_sku(
    session: AsyncSession, company_id, sku: str
) -> float:
    """Legacy SKU-family aggregate retained for callers without an anchor."""
    from celerp_inventory.projections import is_item_available

    cid = uuid.UUID(str(company_id))
    norm = normalize_sku(sku)
    if not norm:
        return 0.0
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == cid,
        Projection.entity_type == "item",
    ))).scalars().all()
    return sum(
        float((row.state or {}).get("quantity") or 0)
        for row in rows
        if normalize_sku((row.state or {}).get("sku")) == norm
        and is_item_available(row.state or {})
    )


def build_channel_states(rows: list[Projection]) -> dict[str, dict[str, dict]]:
    """Derive product-family channel state from canonical family identity."""
    keys = _family_keys(rows)
    by_family: dict[tuple[str, str], list[Projection]] = {}
    for row in rows:
        key = keys.get(row.entity_id)
        if key and key[1]:
            by_family.setdefault(key, []).append(row)

    result: dict[str, dict[str, dict]] = {row.entity_id: {} for row in rows}
    platforms: set[str] = {"shopify", "woocommerce"}
    for row in rows:
        links = (row.state or {}).get("external_links") or {}
        if isinstance(links, dict):
            platforms.update(str(key) for key in links)

    for family_rows in by_family.values():
        explicit_roots = [
            row
            for row in family_rows
            if _is_explicit_catalog_anchor_state(row.state or {})
        ]
        product_roots = explicit_roots or [
            row for row in family_rows if _is_product_anchor_state(row.state or {})
        ]
        for platform in platforms:
            linked = [
                row for row in family_rows
                if external_link_for_state(row.state or {}, platform)
            ]
            if not linked:
                continue
            if len(product_roots) > 1:
                for row in family_rows:
                    result[row.entity_id][platform] = {
                        "linked": True, "enabled": False, "ambiguous": True,
                    }
                continue
            roots = [
                row for row in linked if _is_product_anchor_state(row.state or {})
            ]
            candidates = roots or linked
            identities = {
                external_identity_key(
                    platform,
                    external_link_for_state(row.state or {}, platform),
                )
                for row in candidates
            }
            if len(identities) != 1:
                for row in family_rows:
                    result[row.entity_id][platform] = {
                        "linked": True, "enabled": False, "ambiguous": True,
                    }
                continue
            try:
                anchor = _choose_outbound_anchor(candidates, platform)
            except ValueError:
                for row in family_rows:
                    result[row.entity_id][platform] = {
                        "linked": True, "enabled": False, "ambiguous": True,
                    }
                continue
            link = external_link_for_state(anchor.state or {}, platform)
            enabled = (
                anchor.is_sync_to_shopify is True
                if platform == "shopify"
                else link.get("sync_enabled") is not False
            )
            state = {
                "linked": True,
                "enabled": bool(enabled),
                "anchor_id": anchor.entity_id,
                "remote_deleted": bool(link.get("remote_deleted")),
            }
            for row in family_rows:
                result[row.entity_id][platform] = dict(state)
    return result

def external_identity_key(platform: str, link: dict) -> tuple[str, str | None]:
    """Canonical external product identity for one platform."""
    product_id = str(link.get("product_id") or "")
    variant_key = _external_variant_key(platform)
    variant = link.get(variant_key)
    return product_id, (str(variant) if variant not in (None, "") else None)


def _choose_outbound_anchor(candidates: list[Projection], platform: str) -> Projection:
    if len(candidates) == 1:
        return candidates[0]
    roots = [r for r in candidates if _is_product_anchor_state(r.state or {})]
    explicit_roots = [
        r for r in roots
        if isinstance(((r.state or {}).get("external_links") or {}).get(platform), dict)
    ]
    if len(explicit_roots) == 1:
        return explicit_roots[0]
    if len(roots) == 1:
        return roots[0]
    raise ValueError(f"Multiple item rows claim the same {platform} product identity")


async def _items_with_external_id(
    company_id: str,
    platform: str,
    require_sync_flag: bool = False,
    *,
    inventory_only: bool = False,
) -> list[dict]:
    """Return one outbound row per linked external product identity."""
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp_inventory.projections import is_item_available

    cid = uuid.UUID(str(company_id))
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Projection).where(
                Projection.company_id == cid,
                Projection.entity_type == "item",
            )
        )).scalars().all()

    roots_by_sku: dict[str, int] = {}
    family_keys = _family_keys(rows)
    sellable_by_family: dict[tuple[str, str], float] = {}
    for r in rows:
        st = r.state or {}
        sku_key = normalize_sku(st.get("sku"))
        if sku_key and _is_product_anchor_state(st):
            roots_by_sku[sku_key] = roots_by_sku.get(sku_key, 0) + 1
        family_key = family_keys.get(r.entity_id)
        if family_key and is_item_available(st):
            sellable_by_family[family_key] = (
                sellable_by_family.get(family_key, 0.0)
                + float(st.get("quantity") or 0)
            )

    grouped: dict[tuple[str, str | None], list[Projection]] = {}
    for r in rows:
        st = r.state or {}
        link = external_link_for_state(st, platform)
        if not link or link.get("product_id") in (None, "") or link.get("remote_deleted") is True:
            continue
        if inventory_only and link.get("inventory_sync_paused") is True:
            continue
        if platform == "shopify":
            if require_sync_flag and r.is_sync_to_shopify is not True:
                continue
        elif link.get("sync_enabled") is False:
            continue
        key = external_identity_key(platform, link)
        grouped.setdefault(key, []).append(r)

    out: list[dict] = []
    for candidates in grouped.values():
        r = _choose_outbound_anchor(candidates, platform)
        st = r.state or {}
        link = external_link_for_state(st, platform)
        sku_key = normalize_sku(st.get("sku"))
        if sku_key and roots_by_sku.get(sku_key, 0) > 1:
            raise ValueError(
                f"SKU {st.get('sku')!r} matches multiple catalog product anchors"
            )
        out.append({
            "entity_id": r.entity_id,
            "sku": st.get("sku"),
            "name": st.get("name"),
            "description": st.get("description"),
            "sale_price": st.get("sale_price", st.get("retail_price")),
            "quantity": sellable_by_family.get(
                family_keys.get(r.entity_id), 0.0
            ),
            "files": st.get("files") or [],
            "inventory_type": st.get("inventory_type", "stocked"),
            "sell_by": st.get("sell_by"),
            "external_link": link,
            **_external_ids(platform, st),
        })
    return out


async def list_items_with_external_id(company_id: str, platform: str) -> list[dict]:
    """Items currently enabled for outbound synchronization with platform."""
    return await _items_with_external_id(
        company_id, platform, require_sync_flag=(platform == "shopify"),
        inventory_only=True,
    )


async def list_items_modified_since_last_sync(company_id: str, platform: str) -> list[dict]:
    """Outbound product rows; failed idempotent writes retry on reconciliation."""
    return await _items_with_external_id(
        company_id, platform, require_sync_flag=(platform == "shopify")
    )


async def upsert_from_connector(company_id: str, item) -> str:
    """
    Create or update an item from a connector payload. Returns the write outcome:
    "created", "updated", or "noop" (this exact content was already applied).

    `item` must have: sku, name, idempotency_key (stable per external item).
    Optional: sale_price, quantity, cost_price, description.

    Uses a fresh DB session so the connector does not need to manage
    session lifecycle. Idempotency is enforced at the ledger level.
    """
    from celerp.db import SessionLocal as AsyncSessionLocal
    from celerp.events.engine import connector_upsert

    idem_key = item.idempotency_key
    if not idem_key:
        raise ValueError("idempotency_key required for connector upserts")

    data = {
        "sku": item.sku,
        "name": item.name,
    }
    if item.sale_price is not None:
        data["sale_price"] = item.sale_price
        data["retail_price"] = item.sale_price   # canonical selling-price field
    if item.quantity:
        data["quantity"] = item.quantity
    if getattr(item, "cost_price", None) is not None:
        data["cost_price"] = item.cost_price     # else margin/COGS/valuation read zero cost
    if getattr(item, "description", None):
        data["description"] = item.description

    async with AsyncSessionLocal() as session:
        # Derived price lists are computed from the base at read time; a store-synced price
        # must not be stored under a derived key (it would be masked on every read, then
        # resurface as a stale manual price if the factor is ever removed).
        from celerp.services.pricing import derived_price_keys, get_price_config
        derived = derived_price_keys((await get_price_config(session, company_id))[0])
        for key in derived:
            data.pop(key, None)
        outcome = await connector_upsert(
            session, company_id=company_id, entity_type="item",
            event_type="item.created", idem_key=idem_key, data=data,
        )
        await session.commit()
        return outcome


# ---------------------------------------------------------------------------
# Semantic catalog import
# ---------------------------------------------------------------------------
#
# One committer, three transports: the browser CSV importer (POST /import/rows),
# the agent commit (POST /import/commit), and the raw event batch
# (POST /import/batch) all converge on commit_import_batch below. The business
# transformation (location resolution, unit canonicalization, quantity/rate
# derivation, dynamic attributes, idempotency) lives in build_import_records so
# it is applied identically no matter which transport delivered the rows.


class ImportRecord(BaseModel):
    entity_id: str
    event_type: str
    data: dict
    source: str
    idempotency_key: str
    source_ts: str | None = None


class BatchImportResult(BaseModel):
    created: int
    skipped: int
    updated: int = 0
    errors: list[str]
    batch_id: str | None = None


class BatchImportRequest(BaseModel):
    records: list[ImportRecord] = Field(..., max_length=500)
    filename: str | None = None
    upsert: bool = False


# Columns with dedicated item fields; everything else on a row is a category
# attribute. Shared with the UI mapping form (imported from here) so the split
# between core fields and attributes has one source of truth.
_CORE_ITEM_COLS: frozenset[str] = frozenset({
    "sku", "name", "category", "quantity",
    "weight", "weight_ct", "weight_unit", "gross_weight", "gross_weight_unit",
    "sell_by", "pieces", "status",
    "barcode", "hs_code", "short_description", "description", "notes", "location_name",
    "location_id", "created_at", "updated_at",
})

# Max distinct values before an attribute column is treated as free-text instead
# of a select field when a schema is inferred from the import.
_DROPDOWN_THRESHOLD = 30


def _derive_import_qty(row: dict, sell_by: str, unit_map: dict[str, dict]) -> float:
    """Derive the stock quantity from an import row.

    Priority:
    1. An explicit ``quantity`` or ``qty`` column is trusted unconditionally.
    2. Otherwise fall back to the semantic field for the unit type:
       - pieces-type (e.g. ``piece``) -> ``pieces`` column
       - weight-type (e.g. ``carat``, ``gram``) -> ``weight`` or ``weight_ct``
       - other (service, volume, length, unknown) -> 0.0

    Returns a float; never raises.
    """
    def _to_float(val) -> float | None:
        s = str(val).strip() if val is not None else ""
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    explicit = _to_float(row.get("quantity")) if "quantity" in row else _to_float(row.get("qty"))
    if explicit is not None:
        return explicit
    if is_pieces_unit(sell_by, unit_map):
        return _to_float(row.get("pieces")) or 0.0
    if is_weight_unit(sell_by, unit_map):
        return _to_float(row.get("weight")) or _to_float(row.get("weight_ct")) or 0.0
    return 0.0


def _collect_category_attributes(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    """Return {category: {col: [distinct_values]}} for all attribute columns."""
    result: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        cat = str(row.get("category", "") or "").strip() or "_uncategorized"
        if cat not in result:
            result[cat] = {}
        for k, v in row.items():
            if k in _CORE_ITEM_COLS or k.endswith("_price") or k.endswith("_price_total"):
                continue
            v_str = str(v).strip() if v is not None else ""
            if not v_str:
                continue
            if k not in result[cat]:
                result[cat][k] = []
            if v_str not in result[cat][k]:
                result[cat][k].append(v_str)
    return result


def _infer_category_schemas(cat_attr_values: dict[str, dict[str, list[str]]]) -> dict[str, list[dict]]:
    """Convert collected attribute values into schema field definitions."""
    schemas: dict[str, list[dict]] = {}
    for cat, cols in cat_attr_values.items():
        if cat == "_uncategorized":
            continue
        fields = []
        for key, distinct_vals in cols.items():
            if len(distinct_vals) <= _DROPDOWN_THRESHOLD:
                ftype = "select"
                options = sorted(distinct_vals)
            else:
                ftype = "text"
                options = []
            fields.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "type": ftype,
                "options": options,
            })
        if fields:
            schemas[cat] = fields
    return schemas


# Item import columns, single-sourced here for every transport (the browser
# mapping UI and the agent preview/commit routes). Price columns are dynamic:
# one unit-price column per importable price list, plus a virtual "_total"
# column that back-calculates the unit price at confirm time.
ITEM_IMPORT_BASE_COLS = ["sku", "name", "sell_by", "category", "quantity"]
ITEM_IMPORT_TAIL_COLS = [
    "weight", "weight_unit", "gross_weight", "gross_weight_unit", "pieces",
    "barcode", "hs_code", "purchase_sku", "purchase_name", "purchase_unit",
    "purchase_conversion_factor", "short_description", "description", "notes",
    "location_name",
]


def importable_price_lists(price_lists: list[dict]) -> list[dict]:
    """Price lists whose values can be imported. Derived lists are computed from
    the base price list at read time, so the mapper never offers their columns."""
    return [pl for pl in price_lists if pl.get("name") and not is_derived(pl)]


def build_item_import_spec(price_lists: list[dict]) -> CsvImportSpec:
    """Build the item import spec with dynamic price columns from the company's
    price lists. Shared by the browser mapper and the agent preview/commit."""
    price_cols = [price_key(pl["name"]) for pl in importable_price_lists(price_lists)]
    price_total_cols = [f"{col}_total" for col in price_cols]
    type_map: dict = {"quantity": float, "weight": float, "pieces": float}
    for col in price_cols + price_total_cols:
        type_map[col] = float
    return CsvImportSpec(
        cols=ITEM_IMPORT_BASE_COLS + price_cols + price_total_cols + ITEM_IMPORT_TAIL_COLS,
        required={"name", "sell_by"},
        type_map=type_map,
    )


@dataclass
class ImportBuild:
    records: list[dict]              # ImportRecord-shaped dicts ready for the committer
    errors: list[dict]              # {"row", "field", "message"}
    locations_to_create: list[str]


async def build_import_records(
    session: AsyncSession,
    company_id,
    rows: list[dict],
    *,
    upsert: bool,
    dry_run: bool,
    create_missing_locations: bool = False,
) -> ImportBuild:
    """Transform mapped business rows into semantic item import records.

    Retry identity and item identity are deliberately separate. Create rows get a
    per-import row key; upserts first resolve one existing item by physical barcode
    or an unambiguous SKU, then key the patch by its target and canonical content.

    ``dry_run`` never creates locations. Missing named locations are reported in
    ``locations_to_create`` and are accepted only when the caller is authorised to
    create company locations; commit then creates those locations before resolving
    the rows. An upsert that omits ``location_name`` preserves the target location.
    """
    loc_rows = (await session.execute(
        select(Location).where(Location.company_id == company_id)
    )).scalars().all()
    location_map: dict[str, str] = {loc.name: str(loc.id) for loc in loc_rows}

    default_location_id: str | None = None
    if len(loc_rows) == 1:
        default_location_id = str(loc_rows[0].id)
    else:
        for loc in loc_rows:
            if loc.is_default:
                default_location_id = str(loc.id)
                break

    loc_names_needed: list[str] = []
    for row in rows:
        name = str(row.get("location_name", "") or "").strip()
        if name and name not in location_map and name not in loc_names_needed:
            loc_names_needed.append(name)

    locations_to_create = list(loc_names_needed)
    if not dry_run and create_missing_locations:
        for name in loc_names_needed:
            loc = Location(id=uuid.uuid4(), company_id=company_id, name=name, type="warehouse")
            session.add(loc)
            await session.flush()
            location_map[name] = str(loc.id)

    company = await session.get(Company, company_id)
    currency = ((company.settings or {}).get("currency") if company else None) or "USD"

    cat_sell_by: dict[str, str] = {}
    try:
        from celerp_verticals.routes import _all_categories  # type: ignore
        for cat in _all_categories().values():
            if cat.get("default_sell_by"):
                cat_sell_by[cat["name"]] = cat["default_sell_by"]
    except ImportError:
        pass

    units = await get_company_units(session, company_id)
    unit_canonical = {u["name"].lower(): u["name"] for u in units}
    unit_map = build_unit_map(units)

    # Resolve upsert targets once for the batch. Barcode is a physical-lot
    # identity. SKU is intentionally non-unique and is usable only when exactly
    # one current item has it.
    by_barcode: dict[str, Projection] = {}
    by_sku: dict[str, list[Projection]] = {}
    if upsert:
        barcodes = {str(r.get("barcode") or "").strip() for r in rows} - {""}
        skus = {str(r.get("sku") or "").strip() for r in rows} - {""}
        predicates = []
        if barcodes:
            predicates.append(Projection.state["barcode"].as_string().in_(barcodes))
        if skus:
            predicates.append(Projection.state["sku"].as_string().in_(skus))
        if predicates:
            matches = (await session.execute(
                select(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_type == "item",
                    or_(*predicates),
                )
            )).scalars().all()
            for proj in matches:
                state = proj.state or {}
                barcode = str(state.get("barcode") or "").strip()
                sku = str(state.get("sku") or "").strip()
                if barcode:
                    by_barcode[barcode] = proj
                if sku:
                    by_sku.setdefault(sku, []).append(proj)

    def _has_value(row: dict, key: str) -> bool:
        return key in row and str(row.get(key) or "").strip() != ""

    records: list[dict] = []
    errors: list[dict] = []
    for i, row in enumerate(rows):
        sku = str(row.get("sku", "") or "").strip()
        name = str(row.get("name", "") or "").strip()
        barcode = str(row.get("barcode", "") or "").strip()
        loc_name = str(row.get("location_name", "") or "").strip()

        target: Projection | None = None
        if upsert:
            barcode_target = by_barcode.get(barcode) if barcode else None
            sku_matches = by_sku.get(sku, []) if sku else []
            if barcode_target is not None:
                if len(sku_matches) == 1 and sku_matches[0].entity_id != barcode_target.entity_id:
                    errors.append({
                        "row": i + 1,
                        "field": "sku",
                        "message": "SKU and barcode resolve to different existing items",
                    })
                    continue
                target = barcode_target
            elif len(sku_matches) == 1:
                target = sku_matches[0]
            elif len(sku_matches) > 1:
                errors.append({
                    "row": i + 1,
                    "field": "sku",
                    "message": f"SKU '{sku}' matches multiple lots; include a barcode to choose one",
                })
                continue

        # Location is required for a new item. Upsert without an explicit location
        # preserves the target's current location rather than inventing a default.
        if loc_name:
            location_id = location_map.get(loc_name)
            missing_named_location = loc_name in loc_names_needed
            if not location_id and not (missing_named_location and create_missing_locations):
                errors.append({
                    "row": i + 1,
                    "field": "location_name",
                    "message": f"Location '{loc_name}' does not exist and your role cannot create locations",
                })
                continue
        elif target is not None:
            location_id = str(target.location_id) if target.location_id else None
        else:
            location_id = default_location_id
            if not location_id:
                errors.append({
                    "row": i + 1,
                    "field": "location_name",
                    "message": "No location resolved: add a location_name column or set a default location",
                })
                continue

        sell_by = (
            unit_canonical.get(str(row.get("sell_by", "") or "").strip().lower())
            or str(row.get("sell_by", "") or "").strip()
            or cat_sell_by.get(str(row.get("category", "") or "").strip())
            or ""
        )
        qty = _derive_import_qty(row, sell_by, unit_map)
        amount_source = any(_has_value(row, k) for k in ("quantity", "qty", "pieces", "weight", "weight_ct"))

        def _flt(key: str, _row: dict = row) -> float | None:
            raw = str(_row.get(key, "") or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        attrs: dict = {}
        for key, value in row.items():
            if key in _CORE_ITEM_COLS or key.endswith("_price") or key.endswith("_price_total"):
                continue
            value_s = str(value).strip() if value is not None else ""
            if value_s:
                attrs[key] = value_s

        data = {
            "sku": sku,
            "name": name,
            "quantity": qty,
            "category": str(row.get("category", "") or "").strip() or None,
            "weight": _flt("weight") or _flt("weight_ct"),
            "weight_unit": unit_canonical.get(str(row.get("weight_unit", "") or "").strip().lower())
            or str(row.get("weight_unit", "") or "").strip() or None,
            "gross_weight": _flt("gross_weight"),
            "gross_weight_unit": unit_canonical.get(str(row.get("gross_weight_unit", "") or "").strip().lower())
            or str(row.get("gross_weight_unit", "") or "").strip() or None,
            "pieces": _flt("pieces"),
            "sell_by": sell_by or None,
            "barcode": barcode or None,
            "hs_code": str(row.get("hs_code", "") or "").strip() or None,
            "short_description": str(row.get("short_description", "") or "").strip() or None,
            "description": str(row.get("description", "") or "").strip() or None,
            "notes": str(row.get("notes", "") or "").strip() or None,
            "location_id": location_id,
            "attributes": attrs,
        }

        # Use the target quantity for total->unit conversion on an upsert that does
        # not itself change quantity. Otherwise a price-only upsert would divide by 1.
        price_qty = qty
        if target is not None and not amount_source:
            try:
                price_qty = float((target.state or {}).get("quantity") or 0)
            except (TypeError, ValueError):
                price_qty = 0

        for col_key in row:
            if col_key.endswith("_price_total"):
                unit_key = col_key[: -len("_total")]
                total_val = _flt(col_key)
                if total_val is None or _flt(unit_key) is not None:
                    continue
                if unit_key == "cost_price":
                    data["cost_total"] = total_val
                    continue
                data[unit_key] = to_stored_float(
                    unit_price_from_total(total_val, price_qty or 1, currency)
                )
            elif col_key.endswith("_price") and _flt(col_key) is not None:
                data[col_key] = _flt(col_key)

        if target is None:
            idem = f"row:{i + 1}"
            data["idempotency_key"] = idem
            records.append({
                "entity_id": f"item:{uuid.uuid4()}",
                "event_type": "item.created",
                "data": data,
                "source": "csv_import",
                "idempotency_key": idem,
            })
            continue

        current = target.state or {}
        if _has_value(row, "sell_by") and sell_by != str(current.get("sell_by") or "") and not amount_source:
            errors.append({
                "row": i + 1,
                "field": "sell_by",
                "message": "Changing sell_by during upsert requires quantity, pieces, or weight",
            })
            continue

        # Blank cells are non-destructive during upsert. This keeps a narrow CSV
        # from clearing fields it never intended to manage. Explicit clearing stays
        # on the normal item edit API where validation and audit semantics exist.
        patch: dict = {"name": name}
        if sku:
            patch["sku"] = sku
        if _has_value(row, "sell_by"):
            patch["sell_by"] = sell_by
        if amount_source:
            patch["quantity"] = qty
        for key in (
            "category", "weight", "weight_unit", "gross_weight", "gross_weight_unit",
            "pieces", "barcode", "hs_code", "short_description", "description", "notes",
        ):
            if _has_value(row, key):
                value = data.get(key)
                if value is not None:
                    patch[key] = value
        if loc_name:
            patch["location_id"] = location_id
        if attrs:
            merged_attrs = dict(current.get("attributes") or {})
            merged_attrs.update(attrs)
            patch["attributes"] = merged_attrs
        for key, value in data.items():
            if (key.endswith("_price") or key == "cost_total") and value is not None:
                patch[key] = value

        canonical_patch = json.dumps(patch, sort_keys=True, separators=(",", ":"), default=str)
        idem = f"csv:item:{target.entity_id}:patch:{hashlib.sha256(canonical_patch.encode()).hexdigest()}"
        records.append({
            "entity_id": target.entity_id,
            "event_type": "item.patched",
            "data": patch,
            "source": "csv_import",
            "idempotency_key": idem,
        })

    return ImportBuild(records=records, errors=errors, locations_to_create=locations_to_create)


async def import_items(
    session: AsyncSession,
    company_id,
    actor_id,
    role: str,
    settings: dict,
    rows: list[dict],
    *,
    upsert: bool,
    filename: str | None,
    idempotency_key: str | None,
) -> BatchImportResult:
    """Import mapped business rows through the canonical committer.

    Builds records server-side (creating any missing locations), commits them in
    chunks of 500, then auto-merges any newly discovered attribute columns into
    the company's category schemas (best-effort, gated on manage_company_settings).
    Shared by the browser importer and the agent commit path.

    Creates use ``import-attempt + row ordinal`` identity, so equal SKUs and rows
    without SKUs remain distinct lots while an exact retry of the same attempt is a
    no-op. Upserts resolve a concrete existing entity first and use a hash of the
    resulting patch, so the same patch dedupes but a later changed patch still applies.
    """
    can_create_locations = role_has_permission(settings, role, "manage_company_settings")
    build = await build_import_records(
        session, company_id, rows, upsert=upsert, dry_run=False,
        create_missing_locations=can_create_locations,
    )

    # Creation retry identity belongs to the import content + row ordinal, not
    # SKU/barcode. This keeps same-SKU/no-SKU rows distinct inside one file while
    # making an exact re-submit of the same mapped rows a no-op. A caller-supplied
    # key (e.g. the agent preview hash) may pin the same identity explicitly.
    if idempotency_key:
        batch_key = idempotency_key
    else:
        canonical_batch = json.dumps(
            {"upsert": upsert, "rows": rows},
            sort_keys=True, separators=(",", ":"), default=str,
        )
        batch_key = f"csv:{hashlib.sha256(canonical_batch.encode()).hexdigest()}"
    for rec in build.records:
        if rec["event_type"] == "item.created":
            rec["idempotency_key"] = f"{batch_key}:{rec['idempotency_key']}"
            rec["data"]["idempotency_key"] = rec["idempotency_key"]

    user = SimpleNamespace(id=actor_id)

    created = skipped = updated = 0
    errors: list[str] = [f"Row {e['row']}: {e['message']}" for e in build.errors]
    batch_id: str | None = None

    _CHUNK = 500
    all_records = build.records
    for i in range(0, max(len(all_records), 1), _CHUNK):
        chunk = all_records[i : i + _CHUNK]
        if not chunk:
            break
        body = BatchImportRequest(
            records=[ImportRecord(**r) for r in chunk],
            filename=filename,
            upsert=upsert,
        )
        result = await commit_import_batch(session, company_id, user, role, settings, body)
        created += result.created
        skipped += result.skipped
        updated += result.updated
        errors.extend(result.errors)
        if result.batch_id:
            batch_id = result.batch_id

    # Auto-merge discovered attribute keys into category schemas. Best-effort:
    # mutating category schemas is a settings change, so the caller's role must
    # carry manage_company_settings; without it the merge is skipped and the
    # import still succeeds.
    if build.records and role_has_permission(settings, role, "manage_company_settings"):
        inferred = _infer_category_schemas(_collect_category_attributes(rows))
        if inferred:
            await _merge_category_schemas(session, company_id, inferred)
            await session.commit()

    return BatchImportResult(
        created=created, skipped=skipped, updated=updated, errors=errors, batch_id=batch_id
    )


async def _merge_category_schemas(session: AsyncSession, company_id, incoming: dict[str, list[dict]]) -> None:
    """Append newly discovered attribute keys to the company's category schemas.

    Never overwrites an existing key (user customizations are preserved). This is
    the sole path that grows category schemas from imported attribute columns; it
    stages the change on the company row and leaves the commit to import_items.
    """
    company = await session.get(Company, company_id)
    if company is None:
        return
    settings = dict(company.settings)
    cat_schemas: dict[str, list[dict]] = dict(settings.get("category_schemas") or {})
    added = False
    for cat, new_fields in incoming.items():
        existing = cat_schemas.get(cat) or []
        existing_keys = {f["key"] for f in existing}
        max_pos = max((f.get("position", 0) for f in existing), default=-1)
        appended = []
        for nf in new_fields:
            if nf["key"] not in existing_keys:
                max_pos += 1
                appended.append({**nf, "position": max_pos, "editable": True, "required": False, "visible_to_roles": [], "show_in_table": True})
                existing_keys.add(nf["key"])
        if appended:
            cat_schemas[cat] = existing + appended
            added = True
    if added:
        settings["category_schemas"] = cat_schemas
        company.settings = settings


async def commit_import_batch(
    session: AsyncSession,
    company_id,
    user,
    role: str,
    settings: dict,
    body: BatchImportRequest,
) -> BatchImportResult:
    """Commit item import records through one bounded, company-scoped writer.

    Exact retries resolve through the ledger before any allocation or uniqueness
    check. ``body.upsert`` keeps the legacy raw-CIF contract, but binds a replay to
    the entity created by the original ledger event rather than trusting a newly
    supplied entity id. Semantic upserts arrive as ``item.patched`` records whose
    idempotency key is already target+content aware.
    """
    from sqlalchemy import delete as _delete

    from celerp_inventory.models_import_batch import ImportBatch
    from celerp.models.ledger import LedgerEntry

    keys = list(dict.fromkeys(r.idempotency_key for r in body.records))
    existing_rows = []
    if keys:
        existing_rows = (await session.execute(
            select(LedgerEntry).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.idempotency_key.in_(keys),
            )
        )).scalars().all()
    existing: dict[str, LedgerEntry] = {row.idempotency_key: row for row in existing_rows}

    units = await get_company_units(session, company_id)
    valid_units: frozenset[str] = frozenset(u["name"] for u in units)
    derived_keys = derived_price_keys((await get_price_config(session, company_id))[0])

    created = skipped = updated = 0
    errors: list[str] = []
    created_entity_ids: list[str] = []
    created_keys: list[str] = []

    for rec in body.records:
        data = dict(rec.data)
        data.pop("status", None)
        data.pop("created_at", None)
        data.pop("updated_at", None)
        data.pop("idempotency_key", None)
        for key in derived_keys:
            data.pop(key, None)
        if "allow_splitting" in data and not isinstance(data["allow_splitting"], bool):
            data["allow_splitting"] = str(data["allow_splitting"]).strip().lower() in (
                "true", "yes", "1", "y", "t",
            )

        event_type = rec.event_type
        entity_id = rec.entity_id
        idem_key = rec.idempotency_key
        primary = existing.get(idem_key)

        if event_type == "item.patched":
            if primary is not None:
                if primary.event_type == "item.patched" and primary.entity_id == entity_id:
                    skipped += 1
                else:
                    errors.append(f"{entity_id}: idempotency key was already used for another operation")
                    skipped += 1
                continue
        elif event_type == "item.created":
            if primary is not None:
                if primary.event_type != "item.created":
                    errors.append(f"{entity_id}: idempotency key was already used for another operation")
                    skipped += 1
                    continue
                if not body.upsert:
                    skipped += 1
                    continue
                # Legacy raw-CIF upsert: the ledger, not the caller's fresh UUID,
                # owns the identity of the item created on the first import.
                entity_id = primary.entity_id
                event_type = "item.patched"
                canonical_patch = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
                idem_key = (
                    f"{rec.idempotency_key}:upsert:"
                    f"{hashlib.sha256(canonical_patch.encode()).hexdigest()}"
                )
                replay = await find_event_by_idempotency(session, company_id, idem_key)
                if replay is not None:
                    if replay.event_type == "item.patched" and replay.entity_id == entity_id:
                        skipped += 1
                    else:
                        errors.append(f"{entity_id}: idempotency key was already used for another operation")
                        skipped += 1
                    continue
        elif event_type == "item.snapshot":
            if primary is not None:
                if primary.event_type == "item.snapshot" and primary.entity_id == entity_id:
                    skipped += 1
                else:
                    errors.append(f"{entity_id}: idempotency key was already used for another operation")
                    skipped += 1
                continue
        else:
            errors.append(f"{entity_id}: event type {event_type!r} is not import-safe")
            skipped += 1
            continue

        stored_proj: Projection | None = None
        if event_type == "item.patched":
            stored_proj = await session.get(
                Projection, {"company_id": company_id, "entity_id": entity_id}
            )
            if stored_proj is None or stored_proj.entity_type != "item":
                errors.append(f"{entity_id}: upsert target was not found")
                skipped += 1
                continue
        else:
            existing_projection = await session.get(
                Projection, {"company_id": company_id, "entity_id": entity_id}
            )
            if existing_projection is not None:
                errors.append(f"{entity_id}: entity already exists")
                skipped += 1
                continue

        # Imported price values modify the same protected business data as the
        # interactive pricing surfaces. Import/export authority does not imply
        # permission to set prices.
        price_keys = {
            key for key, value in data.items()
            if value is not None and (key.endswith("_price") or key == "cost_total")
        }
        if price_keys and not role_has_permission(settings, role, "set_inventory_prices"):
            errors.append(
                f"Row (SKU={data.get('sku', '?')}): editing {sorted(price_keys)} "
                "requires the set_inventory_prices permission"
            )
            skipped += 1
            continue

        sell_by = str(data.get("sell_by") or "").strip()
        if event_type != "item.patched" and not sell_by:
            errors.append(f"Row (SKU={data.get('sku', '?')}): sell_by is required")
            skipped += 1
            continue
        if sell_by and valid_units and sell_by not in valid_units:
            errors.append(
                f"Row (SKU={data.get('sku', '?')}): sell_by '{sell_by}' is not a valid unit"
            )
            skipped += 1
            continue

        if event_type == "item.patched" and stored_proj is not None:
            if not role_has_permission(settings, role, "edit_inventory_amounts"):
                gated = set(AMOUNT_ITEM_KEYS & set(data))
                stored_sell_by = str((stored_proj.state or {}).get("sell_by") or "").strip()
                if sell_by and sell_by != stored_sell_by:
                    gated.add("sell_by")
                if gated:
                    errors.append(
                        f"Row (SKU={data.get('sku', '?')}): editing {sorted(gated)} "
                        "requires the edit_inventory_amounts permission"
                    )
                    skipped += 1
                    continue

        negative_amount = None
        for key in AMOUNT_ITEM_KEYS & set(data):
            value = data.get(key)
            if value in (None, ""):
                continue
            try:
                if float(value) < 0:
                    negative_amount = key
                    break
            except (TypeError, ValueError):
                pass
        if negative_amount is not None:
            errors.append(
                f"Row (SKU={data.get('sku', '?')}): {negative_amount} cannot be negative"
            )
            skipped += 1
            continue

        # Creation follows the ordinary internal-code primitive, after replay
        # detection, so a retry cannot consume a new SKU/barcode.
        if event_type == "item.created":
            await lock_item_code_namespace(session, company_id)
            if not str(data.get("sku") or "").strip():
                data["sku"] = (await allocate_internal_codes(session, company_id))[0]
            sku = str(data.get("sku") or "")
            if not data.get("barcode") and sku.isdigit():
                if await _code_in_use(session, company_id, sku):
                    data["barcode"] = (await allocate_internal_codes(session, company_id))[0]
                else:
                    data["barcode"] = sku

        row_barcode = data.get("barcode")
        row_epc = data.get("rfid_epc")
        if row_barcode or row_epc:
            try:
                validate_barcode(row_barcode)
                validate_rfid_epc(row_epc)
                await lock_item_code_namespace(session, company_id)
                await assert_barcode_available(
                    session, company_id, row_barcode, exclude_entity_id=entity_id
                )
                await assert_rfid_epc_available(
                    session, company_id, row_epc, exclude_entity_id=entity_id
                )
            except (ValueError, BarcodeConflictError, RfidEpcConflictError) as exc:
                errors.append(f"Row (SKU={data.get('sku', '?')}): {exc}")
                skipped += 1
                continue

        if event_type != "item.patched":
            data["idempotency_key"] = idem_key

        loc_id: uuid.UUID | None = None
        raw_loc = data.get("location_id")
        if raw_loc:
            try:
                loc_id = uuid.UUID(str(raw_loc))
            except ValueError:
                errors.append(f"Row (SKU={data.get('sku', '?')}): invalid location_id")
                skipped += 1
                continue

        try:
            entry = await emit_event(
                session,
                company_id=company_id,
                entity_id=entity_id,
                entity_type="item",
                event_type=event_type,
                data=data,
                actor_id=user.id,
                location_id=loc_id,
                source=rec.source,
                idempotency_key=idem_key,
                metadata_={"source_ts": rec.source_ts} if rec.source_ts else {},
            )
        except Exception as exc:
            if len(errors) < 10:
                errors.append(f"{entity_id}: {exc}")
            continue

        existing[idem_key] = entry
        if getattr(entry, "was_deduped", False):
            skipped += 1
            continue

        if event_type == "item.patched":
            updated += 1
        else:
            created_entity_ids.append(entity_id)
            created_keys.append(idem_key)
            created += 1

    batch_id: str | None = None
    if created > 0:
        new_batch_id = uuid.uuid4()
        batch = ImportBatch(
            id=new_batch_id,
            company_id=company_id,
            entity_type="item",
            filename=body.filename,
            row_count=created,
            entity_ids=created_entity_ids,
            idempotency_keys=created_keys,
            status="active",
        )
        session.add(batch)
        batch_id = str(new_batch_id)

        # Auto-wipe demo items on first real import.
        demo_eids = (await session.execute(
            select(LedgerEntry.entity_id).where(
                LedgerEntry.company_id == company_id,
                LedgerEntry.source == "demo",
                LedgerEntry.entity_type == "item",
            ).distinct()
        )).scalars().all()
        if demo_eids:
            await session.execute(
                _delete(Projection).where(
                    Projection.company_id == company_id,
                    Projection.entity_id.in_(demo_eids),
                )
            )
            await session.execute(
                _delete(LedgerEntry).where(
                    LedgerEntry.company_id == company_id,
                    LedgerEntry.entity_id.in_(demo_eids),
                )
            )

    await session.commit()
    return BatchImportResult(
        created=created, skipped=skipped, updated=updated, errors=errors, batch_id=batch_id
    )
