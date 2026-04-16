"""E2E test: full Celerp setup wizard -> inventory with Postgres.

Assumes `celerp init --force` already ran and servers are up on 8000/8080.
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCREENSHOTS = Path(__file__).parent.parent.parent / "screenshots" / "pg"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)
UI = "http://localhost:8080"


def _wait_for_server(page, url, timeout=120):
    """Keep trying to load url until it succeeds or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            page.goto(url, wait_until="networkidle", timeout=8000)
            if "chrome-error" not in page.url:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # ── Step 1: Setup / Register ──────────────────────────────
        page.goto(UI, wait_until="networkidle", timeout=15000)
        page.screenshot(path=str(SCREENSHOTS / "01_landing.png"), full_page=True)
        print(f"[1] URL: {page.url}")

        # Fill the setup form
        page.fill('input[name="company_name"]', 'Test Gems Ltd')
        page.fill('input[name="name"]', 'Test Admin')
        page.fill('input[name="email"]', 'admin@test.local')
        page.fill('input[name="password"]', 'TestPass123!')
        page.fill('input[name="confirm_password"]', 'TestPass123!')

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=15000)
        page.screenshot(path=str(SCREENSHOTS / "02_after_register.png"), full_page=True)
        print(f"[2] URL after register: {page.url}")

        # ── Step 2: Company Details + Vertical ────────────────────
        assert "/setup/company" in page.url, f"Expected /setup/company, got {page.url}"

        page.select_option('select#vertical', 'gemstones')
        page.screenshot(path=str(SCREENSHOTS / "03_company_form.png"), full_page=True)
        print("[3] Selected gemstones vertical")

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=30000)
        page.screenshot(path=str(SCREENSHOTS / "04_after_company.png"), full_page=True)
        print(f"[4] URL after company submit: {page.url}")

        # ── Step 3: Wait for activation ───────────────────────────
        # The setup triggers /system/restart. The process manager respawns
        # both servers. We need to wait for them to come back.
        print("[5] Waiting for server restart...")
        time.sleep(5)  # Give servers time to die and respawn

        # Now poll until the activating-status endpoint says ready
        if not _wait_for_server(page, f"{UI}/setup/activating", timeout=60):
            print("[5] WARN: Could not reach activating page")

        page.screenshot(path=str(SCREENSHOTS / "05_activating.png"), full_page=True)
        print(f"[5] URL: {page.url}")

        # Poll the activating-status API directly
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                resp = page.evaluate("""
                    async () => {
                        try {
                            const r = await fetch('/setup/activating-status', {cache: 'no-store'});
                            return await r.json();
                        } catch(e) { return {phase: 'error', msg: e.message}; }
                    }
                """)
                print(f"[5] Status: {resp}")
                if resp.get("phase") == "ready":
                    break
            except Exception as e:
                print(f"[5] eval error: {e}")
            time.sleep(3)

        page.screenshot(path=str(SCREENSHOTS / "06_post_activation.png"), full_page=True)
        print(f"[6] URL post-activation: {page.url}")

        # Navigate to cloud page if that's where we ended up, then skip
        if "/setup/cloud" in page.url or "/dashboard" in page.url:
            pass  # Already past activation
        elif "/setup/activating" in page.url:
            # Try navigating to dashboard directly
            _wait_for_server(page, f"{UI}/dashboard", timeout=30)

        # ── Step 4: Navigate to Inventory ─────────────────────────
        if not _wait_for_server(page, f"{UI}/inventory", timeout=30):
            print("[7] FATAL: Could not reach inventory page")
            page.screenshot(path=str(SCREENSHOTS / "07_inventory_fail.png"), full_page=True)
            browser.close()
            return

        time.sleep(2)
        page.screenshot(path=str(SCREENSHOTS / "07_inventory.png"), full_page=True)
        print(f"[7] URL: {page.url}")

        # ── Step 5: Extract and analyze table ─────────────────────
        headers = page.locator("table thead th").all()
        header_texts = [h.text_content().strip() for h in headers]
        print(f"\n[TABLE] Headers: {header_texts}")

        rows = page.locator("table tbody tr").all()
        print(f"[TABLE] Rows: {len(rows)}")

        if len(rows) == 0:
            print("[TABLE] ⚠ No rows found! Checking page content...")
            content = page.content()
            if "No items" in content:
                print("[TABLE] Page says 'No items' - demo seed may have failed")
            page.screenshot(path=str(SCREENSHOTS / "08_empty_table.png"), full_page=True)
            browser.close()
            return

        # Identify price columns
        price_cols = []
        for i, h in enumerate(header_texts):
            hl = h.upper()
            if any(k in hl for k in ["COST", "WHOLESALE", "RETAIL", "PRICE"]):
                price_cols.append((i, h))
        print(f"[TABLE] Price columns: {price_cols}")

        dash_in_prices = 0
        total_price_cells = 0
        for ri, row in enumerate(rows[:10]):
            cells = row.locator("td").all()
            cell_texts = [c.text_content().strip() for c in cells]
            name = cell_texts[2] if len(cell_texts) > 2 else "?"
            for ci, ch in price_cols:
                if ci < len(cell_texts):
                    val = cell_texts[ci]
                    total_price_cells += 1
                    if val == "--":
                        dash_in_prices += 1
            prices = {ch: cell_texts[ci] if ci < len(cell_texts) else "N/A" for ci, ch in price_cols}
            print(f"  Row {ri}: {name:30s} | {prices}")

        # Screenshot of just the table
        table = page.locator("table").first
        if table.is_visible():
            table.screenshot(path=str(SCREENSHOTS / "08_table_only.png"))

        page.screenshot(path=str(SCREENSHOTS / "09_inventory_full.png"), full_page=True)

        print(f"\n{'='*60}")
        print(f"Total price cells checked: {total_price_cells}")
        print(f"Cells showing '--': {dash_in_prices}")
        if dash_in_prices > 0:
            pct = dash_in_prices / total_price_cells * 100 if total_price_cells else 0
            print(f"❌ BUG CONFIRMED: {pct:.0f}% of price cells show '--'")
        else:
            print(f"✅ All prices displaying correctly")
        print(f"{'='*60}")

        browser.close()


if __name__ == "__main__":
    run()
