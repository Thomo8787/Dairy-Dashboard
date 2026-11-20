"""Download this month's Müller collection CSV for every farm and import it."""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, Playwright, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

FOLDER = ROOT / "data" / "Muller_Milk_Collections_Monthly"
# A fresh Oracle session. The old recorded session number expires and shows no login box.
LOGIN_URL = "https://farmerappprd.mulleragroscloud.com/ords/f?p=200:LOGIN::::::"

# Account number on the Müller home page, then the farm code used in the file name.
FARMS = (
    ("641565", "ALH"),
    ("618538", "BNK"),
    ("930221", "COF"),
    ("527634", "SFR"),
)


def _account_select(page: Page):
    return page.locator("select").filter(has_text="641565")


def _select_account(page: Page, account_number: str) -> None:
    header = page.get_by_text(f"({account_number})")
    if header.count() and header.first.is_visible():
        return
    account = _account_select(page)
    account.select_option(
        value=account.locator("option", has_text=account_number).get_attribute("value")
    )
    page.get_by_text(f"({account_number})").first.wait_for()
    page.get_by_text("Daily Milk Collection Comparison").first.wait_for()


def _open_collections(page: Page) -> None:
    """Open Collections in the red bar, then Collections in that menu."""
    page.get_by_text("Collections", exact=True).first.click()
    menu_item = page.locator("#t_MenuNav_4_0").get_by_role(
        "menuitem", name="Collections", exact=True
    )
    try:
        menu_item.click(timeout=5000)
    except Exception:
        pass
    page.get_by_text("Period", exact=True).first.wait_for()


def _download_month(page: Page, period: str, destination: Path) -> None:
    _open_collections(page)
    page.locator("tr", has_text=period).locator("a").last.click()
    frame = page.locator('iframe[title="Collection By Month"]').content_frame
    frame.get_by_role("button", name="Actions ").click()
    frame.get_by_role("menuitem", name="Download").click()
    with page.expect_download() as download_info:
        frame.get_by_role("link", name=" CSV").click()
    download_info.value.save_as(destination)


def _return_home(page: Page) -> None:
    for _ in range(3):
        page.keyboard.press("Escape")
    page.get_by_text("Home", exact=True).first.click()
    page.get_by_text("Daily Milk Collection Comparison").first.wait_for()


def _headless() -> bool:
    """This PC opens a window. Render sets MULLER_HEADLESS so there is no screen."""
    flag = os.environ.get("MULLER_HEADLESS", "").strip().lower()
    return flag in {"1", "true", "yes"}


def _note_page(page: Page, reason: str) -> None:
    """Print the page Render actually opened, and keep a picture when run on this PC."""
    shot = FOLDER / "muller-login-page.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        print(f"Saved login screenshot to {shot}", flush=True)
    except Exception as exc:
        print(f"Could not save login screenshot: {exc}", flush=True)
    title = ""
    body = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        body = " ".join(page.locator("body").inner_text(timeout=5000).split())
    except Exception as exc:
        body = f"(page text unavailable: {exc})"
    print(reason, flush=True)
    print(f"Page URL: {page.url}", flush=True)
    print(f"Page title: {title}", flush=True)
    print(f"Page text: {body[:1500]}", flush=True)


def run(playwright: Playwright) -> list[Path]:
    today = dt.date.today()
    period = f"{today.year}{today.month:02d}"
    month = f"{today.year}-{today.month:02d}"
    FOLDER.mkdir(parents=True, exist_ok=True)

    headless = _headless()
    browser = playwright.chromium.launch(
        headless=headless,
        args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"] if headless else [],
    )
    context_options = {}
    if headless:
        context_options["user_agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    context = browser.new_context(**context_options)
    page = context.new_page()
    page.set_default_timeout(90_000)
    saved: list[Path] = []
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        email = page.get_by_role("textbox", name="Email Address")
        try:
            email.wait_for()
        except Exception:
            _note_page(page, "Email Address box did not appear.")
            raise
        email.fill(os.environ["MULLER_USERNAME"])
        page.get_by_role("textbox", name="Password").fill(os.environ["MULLER_PASSWORD"])
        page.get_by_role("button", name="Log In").click()
        page.get_by_text("Daily Milk Collection Comparison").first.wait_for()

        for account_number, farm_code in FARMS:
            destination = FOLDER / f"{farm_code}_MULLER_collection_details_for_{month}.csv"
            print(f"Downloading {farm_code} ({account_number})...", flush=True)
            _select_account(page, account_number)
            _download_month(page, period, destination)
            saved.append(destination)
            print(f"Saved {destination.name}", flush=True)
            _import_file(destination)
            _return_home(page)
    finally:
        context.close()
        browser.close()
    return saved


def _import_file(path: Path) -> None:
    from services.database import init_db
    from services.muller_collections_import import import_muller_collections

    init_db()
    result = import_muller_collections(path)
    print(
        f"{result['farm']}: updated {result['rows_updated']}, "
        f"added {result.get('rows_inserted', 0)}, "
        f"unmatched {result['rows_unmatched']}, ambiguous {result['rows_ambiguous']}",
        flush=True,
    )


if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
