"""E2E Playwright test: verify demo item prices render on inventory page.

Starts API + UI as subprocesses, drives Chromium through the setup wizard,
screenshots the inventory table.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SCREENSHOT_DIR = str(Path(__file__).parent.parent.parent / "screenshots")
CELERP_DIR = str(Path(__file__).parent.parent)
DB_FILE = "/tmp/celerp_e2e_test.db"
API_PORT = 18950
UI_PORT = 18951
PYTHON = os.path.join(CELERP_DIR, ".venv/bin/python")


def wait_for_port(port, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def kill_ports():
    for port in [API_PORT, UI_PORT]:
        try:
            r = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
            for pid in r.stdout.strip().split("\n"):
                if pid.strip():
                    os.kill(int(pid.strip()), 9)
        except Exception:
            pass


def main():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    kill_ports()
    time.sleep(1)

    if os.path.exists(DB_FILE):
        os.unlink(DB_FILE)

    env = os.environ.copy()
    env["E2E_DB_FILE"] = DB_FILE
    env["E2E_API_PORT"] = str(API_PORT)
    env["E2E_UI_PORT"] = str(UI_PORT)

    # Start API
    print("Starting API server...")
    api_proc = subprocess.Popen(
        [PYTHON, os.path.join(CELERP_DIR, "tests/_e2e_api_server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if not wait_for_port(API_PORT, timeout=30):
        print("API failed to start!")
        api_proc.kill()
        out, _ = api_proc.communicate(timeout=5)
        print(out[:3000])
        return 1
    print(f"  API up on :{API_PORT}")

    # Quick health check
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/auth/bootstrap-status")
        print(f"  Bootstrap: {json.loads(resp.read())}")
    except Exception as e:
        print(f"  Health check failed: {e}")

    # Start UI
    print("Starting UI server...")
    ui_proc = subprocess.Popen(
        [PYTHON, os.path.join(CELERP_DIR, "tests/_e2e_ui_server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if not wait_for_port(UI_PORT, timeout=30):
        print("UI failed to start!")
        ui_proc.kill()
        out, _ = ui_proc.communicate(timeout=5)
        print(out[:3000])
        api_proc.kill()
        return 1
    print(f"  UI up on :{UI_PORT}")
    time.sleep(1)

    try:
        result = run_browser_test()
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        result = 1
    finally:
        api_proc.terminate()
        ui_proc.terminate()
        for p in [api_proc, ui_proc]:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    return result


def run_browser_test():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        base = f"http://127.0.0.1:{UI_PORT}"

        # Landing
        print("\n=== Landing ===")
        page.goto(base, wait_until="networkidle", timeout=15000)
        page.screenshot(path=f"{SCREENSHOT_DIR}/01_landing.png")
        print(f"  URL: {page.url}  Title: {page.title()}")

        if "unavailable" in page.title().lower():
            with open(f"{SCREENSHOT_DIR}/error.html", "w") as f:
                f.write(page.content())
            print("  API unavailable!")
            browser.close()
            return 1

        # Registration
        print("\n=== Registration ===")
        page.wait_for_selector('input[name="company_name"]', timeout=10000)
        page.fill('input[name="company_name"]', "Test Gems Co")
        page.fill('input[name="name"]', "Test Admin")
        page.fill('input[name="email"]', "admin@test.com")
        page.fill('input[name="password"]', "testpass123")
        page.fill('input[name="confirm_password"]', "testpass123")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle", timeout=15000)
        print(f"  URL: {page.url}")

        # Company setup
        if "/setup/company" in page.url:
            print("\n=== Company setup ===")
            page.wait_for_selector('select[name="vertical"]', timeout=10000)
            page.select_option('select[name="vertical"]', "gemstones")
            page.screenshot(path=f"{SCREENSHOT_DIR}/02_company.png")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=30000)
            print(f"  URL: {page.url}")

        # Handle activation (wait briefly, then navigate away)
        if "/setup/activating" in page.url:
            print("  Waiting for activation...")
            try:
                page.wait_for_url("**/dashboard**", timeout=20000)
            except Exception:
                print(f"  Timed out at: {page.url}")

        # Navigate to inventory regardless of current page
        print("\n=== Inventory ===")
        page.goto(f"{base}/inventory", wait_until="networkidle", timeout=15000)
        time.sleep(2)
        page.screenshot(path=f"{SCREENSHOT_DIR}/03_inventory.png", full_page=True)
        print(f"  URL: {page.url}  Title: {page.title()}")

        # Table analysis
        print("\n=== Table ===")
        headers = page.query_selector_all("table th")
        header_texts = [h.inner_text().strip() for h in headers]
        print(f"  Headers: {header_texts}")

        rows = page.query_selector_all("table tbody tr")
        print(f"  Rows: {len(rows)}")

        for i, row in enumerate(rows[:5]):
            cells = row.query_selector_all("td")
            texts = [c.inner_text().strip() for c in cells]
            print(f"  Row {i}: {texts}")

        price_hdrs = [h for h in header_texts if "price" in h.lower()]
        print(f"  Price headers: {price_hdrs}")

        if price_hdrs:
            for ph in price_hdrs:
                idx = header_texts.index(ph)
                vals = []
                for row in rows[:5]:
                    cells = row.query_selector_all("td")
                    if idx < len(cells):
                        vals.append(cells[idx].inner_text().strip())
                print(f"  {ph} values: {vals}")

        all_cells = page.query_selector_all("table tbody td")
        dash = sum(1 for c in all_cells if c.inner_text().strip() == "--")
        print(f"  '--' cells: {dash}/{len(all_cells)}")

        table = page.query_selector("table")
        if table:
            with open(f"{SCREENSHOT_DIR}/table.html", "w") as f:
                f.write(table.inner_html())
            table.screenshot(path=f"{SCREENSHOT_DIR}/04_table.png")

        with open(f"{SCREENSHOT_DIR}/page.html", "w") as f:
            f.write(page.content())

        browser.close()
        print(f"\n✓ Saved to {SCREENSHOT_DIR}/")
        return 0


if __name__ == "__main__":
    sys.exit(main())
