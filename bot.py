import os
import re
import asyncio
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Optional, Union, List
from playwright.async_api import async_playwright, Page, Frame, Locator, TimeoutError as PWTimeoutError

# ---- Time & paths -----------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).date()
YESTERDAY = TODAY - timedelta(days=1)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---- Secrets / Environment --------------------------------------------------
USERNAME = os.getenv("WEBSITE_USER", "")
PASSWORD = os.getenv("WEBSITE_PASS", "")
LOGIN_URL = (os.getenv("LOGIN_URL") or
             "https://eclientreporting.nuvamaassetservices.com/wealthspectrum/app/loginWith")

# Email (optional)
ENABLE_EMAIL = os.getenv("ENABLE_EMAIL", "false").strip().lower() == "true"
FROM_EMAIL = (os.getenv("FROM_EMAIL") or "").strip()
TO_EMAIL = (os.getenv("TO_EMAIL") or "").strip()
SMTP_SERVER = (os.getenv("SMTP_SERVER") or "").strip()
SMTP_PORT = int((os.getenv("SMTP_PORT") or "587").strip() or "587")
SMTP_USER = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS = (os.getenv("SMTP_PASS") or "").strip()

# Report title (override via secret if needed)
REPORT_TITLE = (os.getenv("REPORT_TITLE") or "Statement of Cash Flows").strip()
DEFAULT_FILENAME = f"cash_flows_{YESTERDAY.isoformat()}.xlsx"

# ============================ Helpers ========================================
def email_config_ok() -> bool:
    if not ENABLE_EMAIL:
        return False
    req = [FROM_EMAIL, TO_EMAIL, SMTP_SERVER, str(SMTP_PORT), SMTP_USER, SMTP_PASS]
    if any(not v for v in req):
        print("[INFO] Email not sent: ENABLE_EMAIL=true but one or more SMTP fields are blank.")
        return False
    return True

async def safe_screenshot(ctx: Union[Page, Frame], name: str):
    try:
        page = ctx if isinstance(ctx, Page) else ctx.page
        path = DOWNLOAD_DIR / name
        await page.screenshot(path=str(path), full_page=True)
        print(f"[DEBUG] Saved screenshot: {path}")
    except Exception as e:
        print(f"[DEBUG] Failed to capture screenshot: {e}")

def contexts(page: Page) -> List[Union[Page, Frame]]:
    return [page, *page.frames]

async def try_click(locator: Locator, timeout: int = 1500) -> bool:
    try:
        await locator.first.scroll_into_view_if_needed(timeout=timeout)
        await locator.first.click(timeout=timeout)
        return True
    except Exception:
        return False

# --- Left sidebar "Reports" (3rd from top) ----------------------------------
async def click_reports_nav(page: Page):
    sidebar = page.locator("#reportLeftMenu")
    try:
        await sidebar.wait_for(state="visible", timeout=8000)
        link = sidebar.get_by_role("link", name=re.compile(r"\bReports\b", re.I))
        if await link.count() > 0 and await try_click(link, 3000):
            return
        links = sidebar.get_by_role("link")
        if await links.count() >= 3 and await try_click(links.nth(2), 3000):
            return
    except Exception:
        pass
    if await try_click(page.locator("#reportLeftMenu a[href*='/app/reports']"), 3000):
        return
    if await try_click(page.get_by_role("link", name=re.compile(r"\bReports\b", re.I)), 3000):
        return
    await page.get_by_text("Reports", exact=False).first.click()

# --- Report dropdown selection -----------------------------------------------
async def select_report(ctx: Union[Page, Frame], option_text: str, timeout_ms: int = 9000) -> bool:
    # 1) Native <select> near label
    try:
        label = ctx.get_by_text("Report", exact=False).first
        sel = ctx.locator("select").filter(has=label)
        if await sel.count() == 0:
            sel = ctx.locator("select")
        if await sel.count() > 0:
            await sel.first.wait_for(state="visible", timeout=timeout_ms)
            await sel.first.select_option(label=option_text)
            print("[DEBUG] Selected report via native <select>.")
            return True
    except Exception:
        pass

    # 2) Custom dropdowns
    opened = False
    try:
        box = ctx.locator("xpath=//label[contains(., 'Report')]/following::*[self::div or self::button or self::span or self::input][1]")
        opened = await try_click(box, 2000)
    except Exception:
        pass
    if not opened:
        opened = await try_click(ctx.get_by_role("combobox"), 2000)

    if opened:
        panels = [
            "[role='listbox']",
            ".mat-select-panel",
            ".cdk-overlay-pane",
            ".ng-dropdown-panel",
            ".ant-select-dropdown",
            ".p-dropdown-items-wrapper",
            ".dropdown-menu",
            "ul[role='listbox']",
            "ul[role='menu']",
            "ul",
        ]
        for pc in panels:
            try:
                cont = ctx.locator(pc)
                if await cont.count() == 0:
                    continue
                opt = cont.locator("*", has_text=re.compile(re.escape(option_text), re.I)).first
                await opt.click(timeout=timeout_ms)
                print(f"[DEBUG] Selected report via panel '{pc}'.")
                return True
            except Exception:
                continue

    # 3) Type-to-select fallback
    try:
        rs_input = ctx.locator("div[role='combobox'] input, input[role='combobox'], input[aria-autocomplete='list']")
        if await rs_input.count() == 0:
            rs_input = ctx.locator("input")
        if await rs_input.count() > 0:
            await rs_input.first.click(timeout=1500)
            await rs_input.first.fill(option_text)
            await rs_input.first.press("Enter")
            print("[DEBUG] Selected report via typing+Enter.")
            return True
    except Exception:
        pass

    # 4) Last resort: click visible text
    try:
        await ctx.get_by_text(option_text, exact=False).first.click(timeout=timeout_ms)
        print("[DEBUG] Selected report via generic text click.")
        return True
    except Exception:
        return False

# --- Force-set an input value with native setter + events --------------------
async def set_input_value_with_events(input_locator: Locator, value: str):
    await input_locator.evaluate(
        """(el, val) => {
            const proto = window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            desc.set.call(el, val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.blur();
        }""",
        value,
    )

# --- Set "As on Date" to yesterday ------------------------------------------
async def set_as_on_date(ctx: Union[Page, Frame], dt: datetime.date):
    iso = dt.strftime("%Y-%m-%d")
    dmy_slash = dt.strftime("%d/%m/%Y")

    # Target the input next to the "As on Date" label (common case)
    inp = ctx.locator("xpath=//label[contains(., 'As on Date')]/following::input[1]")
    try:
        await inp.first.wait_for(state="visible", timeout=4000)

        # A) type dd/mm/YYYY
        try:
            await inp.first.click()
            await inp.first.press("Control+A")
            await inp.first.type(dmy_slash)
            await inp.first.press("Tab")
            print("[DEBUG] Date set via typing (dd/mm/YYYY).")
            return
        except Exception:
            pass

        # B) fill ISO then blur
        try:
            await inp.first.fill(iso)
            await inp.first.press("Tab")
            print("[DEBUG] Date set via fill (YYYY-mm-dd).")
            return
        except Exception:
            pass

        # C) native setter + events
        try:
            await set_input_value_with_events(inp.first, dmy_slash)
            print("[DEBUG] Date set via JS setter + events.")
            return
        except Exception:
            pass

    except Exception:
        # couldn't find that labeled input; fall through
        pass

    # D) open datepicker and pick the day
    toggles = [
        "xpath=//label[contains(., 'As on Date')]/following::*[contains(@class,'datepicker') or contains(@aria-label,'calendar') or self::button][1]",
        ".mat-datepicker-toggle",
        "button[aria-label*='calendar']",
        ".p-datepicker-trigger",
    ]
    opened = False
    for t in toggles:
        if await try_click(ctx.locator(t), 1500):
            opened = True
            break

    if opened:
        labels = [
            dt.strftime("%-d %B %Y"),  # Linux: 24 August 2025
            dt.strftime("%d %B %Y"),   # zero-padded day
            dt.strftime("%-d %b %Y"),  # 24 Aug 2025
            dt.strftime("%d %b %Y"),   # padded
        ]
        for lab in labels:
            try:
                await ctx.locator(f"[aria-label='{lab}']").first.click(timeout=1200)
                print(f"[DEBUG] Date picked via datepicker aria-label '{lab}'.")
                return
            except Exception:
                continue
        # generic day cell
        try:
            cal = ctx.locator(".mat-calendar, .p-datepicker-calendar, .ui-datepicker-calendar, .cdk-overlay-pane")
            if await cal.count() > 0:
                await cal.first.get_by_text(str(dt.day), exact=True).first.click(timeout=1200)
                print("[DEBUG] Date picked via datepicker day cell.")
                return
        except Exception:
            pass

    print("[WARN] Could not set date; using site's default.")

# --- Click Execute robustly --------------------------------------------------
async def click_execute(ctx: Union[Page, Frame]):
    # Prefer a real <button> Execute, else JS-click
    labels = r"\b(Execute|Run|Generate|Submit|View Report|View)\b"
    btn = ctx.get_by_role("button", name=re.compile(labels, re.I)).first
    try:
        await btn.wait_for(state="visible", timeout=6000)
        # Try to ensure enabled
        try:
            await btn.evaluate("el => el.scrollIntoView({block:'center'})")
        except Exception:
            pass
        try:
            await btn.click()
        except Exception:
            await btn.evaluate("el => el.click()")
        print("[DEBUG] Clicked Execute.")
        return
    except Exception:
        pass

    # Text fallback anywhere
    any_text = ctx.get_by_text(re.compile(labels, re.I))
    if await try_click(any_text, 5000):
        print("[DEBUG] Clicked Execute via text fallback.")
        return

    await safe_screenshot(ctx, "debug_execute_click_failed.png")
    raise RuntimeError("Could not click Execute.")

# --- Switch to 'Report Executions' tab --------------------------------------
async def open_report_executions(ctx: Union[Page, Frame]):
    # Tabs may be role="tab" or plain links
    if await try_click(ctx.get_by_role("tab", name=re.compile(r"Report Executions", re.I)), 3000):
        return
    if await try_click(ctx.get_by_text("Report Executions", exact=False), 3000):
        return

# --- Find the download button for our report --------------------------------
async def find_download_button(ctx: Union[Page, Frame], title: str) -> Optional[Locator]:
    row = ctx.get_by_role("row").filter(has=ctx.get_by_text(title, exact=False)).first
    candidates = [
        row.get_by_role("button", name=re.compile(r"download|document|file|xlsx|excel|csv|pdf", re.I)),
        row.get_by_role("link", name=re.compile(r"download|document|file|xlsx|excel|csv|pdf", re.I)),
        row.locator("a[download]"),
        row.locator("button:has(svg), a:has(svg)"),
    ]
    for c in candidates:
        try:
            await c.wait_for(state="visible", timeout=500)
            return c
        except Exception:
            continue
    return None

# =============================== Core logic =================================
async def run_automation():
    if not USERNAME or not PASSWORD:
        raise RuntimeError("Missing WEBSITE_USER or WEBSITE_PASS.")
    if not (isinstance(LOGIN_URL, str) and LOGIN_URL.startswith("http")):
        raise RuntimeError("LOGIN_URL is invalid or empty.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downl
