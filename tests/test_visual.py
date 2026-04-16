import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

try:
    from PIL import Image, ImageChops
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

BASE_URL = os.getenv("CELERP_UI_BASE_URL", "").rstrip("/")
BASELINE_DIR = Path(os.getenv("CELERP_VISUAL_BASELINE_DIR", str(Path(__file__).resolve().parents[1] / "visual_baselines")))
OUT_DIR = Path(os.getenv("CELERP_VISUAL_OUT_DIR", str(BASELINE_DIR.parent / "current")))

EMAIL = os.getenv("CELERP_UI_EMAIL", "admin@demo.test")
PASSWORD = os.getenv("CELERP_UI_PASSWORD", "demo-password")

VIEWPORT = {"width": 1440, "height": 900}

# fail if more than 1% pixels differ
MAX_DIFF_RATIO = float(os.getenv("CELERP_VISUAL_MAX_DIFF_RATIO", "0.01"))

ROUTES: list[tuple[str, str]] = [
    ("/", "dashboard"),
    ("/inventory", "inventory"),
    ("/docs", "docs"),
    ("/crm", "crm"),
    ("/lists", "lists"),
    ("/accounting", "accounting"),
    ("/manufacturing", "manufacturing"),
    ("/subscriptions", "subscriptions"),
    ("/settings", "settings"),
    ("/reports", "reports"),
]

pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="Visual regression tests require CELERP_UI_BASE_URL to be set",
)


def _login(page: Page) -> None:
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
    page.set_viewport_size(VIEWPORT)
    page.get_by_label("Email").fill(EMAIL)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("networkidle", timeout=60_000)


def _pixel_diff_ratio(a_path: Path, b_path: Path) -> float:
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow is required for pixel diff — install it with: pip install Pillow")
    a = Image.open(a_path).convert("RGBA")
    b = Image.open(b_path).convert("RGBA")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    bbox = diff.getbbox()
    if bbox is None:
        return 0.0
    # Count non-zero pixels by converting to 1-bit mask
    mask = diff.convert("L").point(lambda p: 255 if p else 0)
    diff_pixels = mask.histogram()[255]
    total_pixels = a.size[0] * a.size[1]
    return diff_pixels / total_pixels


@pytest.fixture()
def logged_in_page(page: Page) -> Page:
    _login(page)
    return page


@pytest.mark.parametrize("route,slug", ROUTES)
def test_visual_route(logged_in_page: Page, route: str, slug: str) -> None:
    update = os.getenv("UPDATE_SNAPSHOTS") in {"1", "true", "TRUE", "yes", "YES"}

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    logged_in_page.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
    logged_in_page.wait_for_load_state("networkidle", timeout=60_000)
    logged_in_page.wait_for_timeout(500)

    current_path = OUT_DIR / f"{slug}.png"
    baseline_path = BASELINE_DIR / f"{slug}.png"

    logged_in_page.screenshot(path=str(current_path), full_page=True)

    if update or not baseline_path.exists():
        current_path.replace(baseline_path)
        pytest.skip("Baseline snapshot updated (or created). Re-run without UPDATE_SNAPSHOTS=1 to compare.")

    diff_ratio = _pixel_diff_ratio(baseline_path, current_path)

    if diff_ratio > MAX_DIFF_RATIO:
        pytest.fail(
            f"Visual regression for {route} (diff_ratio={diff_ratio:.4%} > {MAX_DIFF_RATIO:.4%}). "
            f"baseline={baseline_path} current={current_path}"
        )
