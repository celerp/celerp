from pathlib import Path

def replace(path, old, new, count=1):
    p = Path(path)
    s = p.read_text()
    assert old in s, (path, old[:120])
    s2 = s.replace(old, new, count)
    assert s2 != s, path
    p.write_text(s2)

replace(
    "tests/test_services/test_connector_upsert_integration.py",
    """    co = "wm-since-1"
    watermark = datetime(2026, 5, 1, tzinfo=timezone.utc)""",
    """    co = str(await _seed_company(session, "WatermarkSync"))
    watermark = datetime(2026, 5, 1, tzinfo=timezone.utc)""",
)
replace(
    "tests/test_services/test_connector_upsert_integration.py",
    """    ctx = ConnectorContext(company_id="co-out-1", access_token="t", store_handle="s")""",
    """    co = str(await _seed_company(session, "OutboundSync"))
    ctx = ConnectorContext(company_id=co, access_token="t", store_handle="s")""",
)

replace(
    "default_modules/celerp-inventory/celerp_inventory/services.py",
    """async def set_external_link(
    session: AsyncSession, company_id, entity_id: str, platform: str, link: dict,
    *, expected_sku: str | None = None, actor_id=None, source: str = "connector",
) -> dict:""",
    """async def set_external_link(
    session: AsyncSession, company_id, entity_id: str, platform: str, link: dict,
    *, expected_sku: str | None = None,
    expected_identity: tuple[str, str | None] | None = None,
    require_unlinked: bool = False,
    actor_id=None, source: str = "connector",
) -> dict:""",
)
replace(
    "default_modules/celerp-inventory/celerp_inventory/services.py",
    """    if expected_sku is not None and normalize_sku(state.get("sku")) != normalize_sku(expected_sku):
        raise ExternalLinkConflictError(
            "Catalog SKU changed while the external product was being resolved"
        )
    links = dict(state.get("external_links") or {})
""",
    """    if expected_sku is not None and normalize_sku(state.get("sku")) != normalize_sku(expected_sku):
        raise ExternalLinkConflictError(
            "Catalog SKU changed while the external product was being resolved"
        )
    current = external_link_for_state(state, platform)
    if require_unlinked and current:
        raise ExternalLinkConflictError(
            "External product identity changed while the operation was running"
        )
    if expected_identity is not None and (
        not current or external_identity_key(platform, current) != expected_identity
    ):
        raise ExternalLinkConflictError(
            "External product identity changed while the operation was running"
        )
    links = dict(state.get("external_links") or {})
""",
)

replace(
    "celerp/connectors/woocommerce.py",
    """                await set_external_link_state(
                    session,
                    ctx.company_id,
                    row.entity_id,
                    "woocommerce",
                    remote_deleted=True,
                    source="connector",
                )""",
    """                await set_external_link_state(
                    session,
                    ctx.company_id,
                    row.entity_id,
                    "woocommerce",
                    remote_deleted=True,
                    expected_identity=identity,
                    source="connector",
                )""",
)
replace(
    "celerp/connectors/woocommerce.py",
    """                    link_updates={
                        "manage_stock": remote.get("manage_stock"),
                        "inventory_sync_paused": False,
                    },
                    actor_id=actor_id, source="connector_ui",
                )""",
    """                    link_updates={
                        "manage_stock": remote.get("manage_stock"),
                        "inventory_sync_paused": False,
                    },
                    expected_identity=(
                        str(link.get("product_id") or ""),
                        str(link.get("variation_id"))
                        if link.get("variation_id") not in (None, "")
                        else None,
                    ),
                    actor_id=actor_id, source="connector_ui",
                )""",
)
replace(
    "celerp/connectors/woocommerce.py",
    """                    expected_sku=sku,
                    actor_id=actor_id, source="connector_ui",
                )""",
    """                    expected_sku=sku,
                    expected_identity=(
                        (
                            str(link.get("product_id") or ""),
                            str(link.get("variation_id"))
                            if link.get("variation_id") not in (None, "")
                            else None,
                        )
                        if link else None
                    ),
                    require_unlinked=not bool(link),
                    actor_id=actor_id, source="connector_ui",
                )""",
)

replace(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    """        catalog_family_rows,
        external_link_for_state,
        resolve_catalog_anchor_for_item,""",
    """        catalog_family_rows,
        external_identity_key,
        external_link_for_state,
        resolve_catalog_anchor_for_item,""",
)
replace(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    """                        expected_sku=source_sku,
                        source="connector",
                    )""",
    """                        expected_sku=source_sku,
                        require_unlinked=True,
                        source="connector",
                    )""",
)
replace(
    "default_modules/celerp-docs/celerp_docs/doc_service.py",
    """                    if external_link_for_state(anchor.state or {}, "woocommerce"):
                        await set_external_link_state(
                            session, cid, anchor.entity_id, "woocommerce",
                            link_updates={"inventory_sync_paused": True},
                            source="connector",
                        )""",
    """                    link = external_link_for_state(
                        anchor.state or {}, "woocommerce"
                    )
                    if link:
                        await set_external_link_state(
                            session, cid, anchor.entity_id, "woocommerce",
                            link_updates={"inventory_sync_paused": True},
                            expected_identity=external_identity_key(
                                "woocommerce", link
                            ),
                            source="connector",
                        )""",
    2,
)

test_path = Path("tests/test_services/test_connector_upsert_integration.py")
test_path.write_text(test_path.read_text() + r"""

@pytest.mark.asyncio
async def test_external_link_compare_and_set_rejects_stale_writers(use_test_session):
    from celerp.events.engine import emit_event
    from celerp_inventory.services import (
        ExternalLinkConflictError,
        set_external_link,
    )

    session = use_test_session
    cid = await _seed_company(session, "LinkCas")
    entity_id = "item:link-cas"
    await emit_event(
        session,
        company_id=cid,
        entity_id=entity_id,
        entity_type="item",
        event_type="item.created",
        data={"sku": "CAS-1", "name": "CAS", "sell_by": "piece"},
        actor_id=None,
        location_id=None,
        source="test",
        idempotency_key=str(uuid.uuid4()),
        metadata_={},
    )

    await set_external_link(
        session, cid, entity_id, "woocommerce",
        {"product_id": "1", "sync_enabled": True},
    )
    await set_external_link(
        session, cid, entity_id, "woocommerce",
        {"product_id": "2", "sync_enabled": True},
    )

    with pytest.raises(ExternalLinkConflictError):
        await set_external_link(
            session, cid, entity_id, "woocommerce",
            {"product_id": "3", "sync_enabled": True},
            expected_identity=("1", None),
        )

    with pytest.raises(ExternalLinkConflictError):
        await set_external_link(
            session, cid, entity_id, "woocommerce",
            {"product_id": "3", "sync_enabled": True},
            require_unlinked=True,
        )
""")
