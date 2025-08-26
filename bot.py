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
        await locator.first.click(timeout=timeout)
        return True
    except Exception:
        return False

# --- Left sidebar "Reports" (3rd from top) ----------------------------------
async def click_reports_nav(page: Page):
    # 1) Sidebar container
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
    # 2) URL-based inside sidebar
    if await try_click(page.locator("#reportLeftMenu a[href*='/app/reports']"), 3000):
        return
    # 3) Anywhere
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

    # 2) Click the control next to "Report" (custom dropdowns)
    opened = False
    try:
        box = ctx.locator("xpath=//label[contains(., 'Report')]/following::*[self::div or self::button or self::span or self::input][1]")
        opened = await try_click(box, 2000)
    except Exception:
        pass
    if not opened:
        opened = await try_click(ctx.get_by_role("combobox"), 2000)

    # panels to search options in
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

    inp = ctx.locator("xpath=//label[contains(., 'As on Date')]/following::input[1]")
    try:
        await inp.first.wait_for(state="visible", timeout=4000)

        # Strategy A: type dd/mm/YYYY
        try:
            await inp.first.click()
            await inp.first.press("Control+A")
            await inp.first.type(dmy_slash)
            await inp.first.press("Enter")
            print("[DEBUG] Date set via typing (dd/mm/YYYY).")
            return
        except Exception:
            pass

        # Strategy B: fill ISO
        try:
            await inp.first.fill(iso)
            await inp.first.press("Enter")
            print("[DEBUG] Date set via fill (YYYY-mm-dd).")
            return
        except Exception:
            pass

        # Strategy C: JS setter + events
        try:
            await set_input_value_with_events(inp.first, dmy_slash)
            print("[DEBUG] Date set via JS setter + events.")
            return
        except Exception:
            pass

    except Exception:
        pass  # couldn't find labeled input

    # Strategy D: open datepicker and click day
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
            dt.strftime("%-d %B %Y"),  # 24 August 2025 (Linux)
            dt.strftime("%d %B %Y"),   # 24 August 2025 (zero-padded)
            dt.strftime("%-d %b %Y"),  # 24 Aug 2025
            dt.strftime("%d %b %Y"),   # 24 Aug 2025 (padded)
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

async def click_execute(ctx: Union[Page, Frame]):
    # Prefer role=button Execute
    btn = ctx.get_by_role("button", name=re.compile(r"\bexecute\b", re.I)).first
    try:
        await btn.wait_for(state="visible", timeout=5000)
        await btn.click()
        print("[DEBUG] Clicked Execute via role=button.")
        return
    except Exception:
        pass
    # Text fallback
    if await try_click(ctx.get_by_text("Execute", exact=False), 5000):
        print("[DEBUG] Clicked Execute via text fallback.")
        return
    await safe_screenshot(ctx, "debug_execute_click_failed.png")
    raise RuntimeError("Could not click Execute.")

async def find_download_button(ctx: Union[Page, Frame], title: str) -> Optional[Locator]:
    row = ctx.get_by_role("row").filter(has=ctx.get_by_text(title, exact=False)).first
    candidates = [
        row.get_by_role("button", name=re.compile(r"download|document|file|xlsx|excel|csv", re.I)),
        row.get_by_role("link", name=re.compile(r"download|document|file|xlsx|excel|csv", re.I)),
        row.locator("a[download]"),
        row.locator("button:has(svg), a:has(svg)"),
        ctx.get_by_role("button", name=re.compile(r"document", re.I)).first,
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
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # 1) LOGIN
        await page.goto(LOGIN_URL, wait_until="networkidle")
        creds = [
            ("input[name='username']", "input[name='password']"),
            ("input#username", "input#password"),
            ("input[autocomplete='username']", "input[autocomplete='current-password']"),
        ]
        filled = False
        for u_sel, p_sel in creds:
            try:
                await page.fill(u_sel, USERNAME, timeout=2500)
                await page.fill(p_sel, PASSWORD, timeout=2500)
                filled = True
                break
            except PWTimeoutError:
                continue
        if not filled:
            try:
                await page.get_by_placeholder(re.compile("user", re.I)).fill(USERNAME, timeout=2500)
                await page.get_by_placeholder(re.compile("pass|pwd", re.I)).fill(PASSWORD, timeout=2500)
                filled = True
            except PWTimeoutError:
                pass
        if not filled:
            await safe_screenshot(page, "debug_login_fields.png")
            raise RuntimeError("Could not locate login fields.")
        try:
            await page.get_by_role("button", name=re.compile(r"log ?in", re.I)).click()
        except PWTimeoutError:
            await page.click("button[type='submit']")
        await page.wait_for_load_state("networkidle")

        # 2) REPORTS TAB (left sidebar)
        await click_reports_nav(page)
        await page.wait_for_load_state("networkidle")

        # 3) SELECT REPORT (Page or frames)
        picked_ctx: Optional[Union[Page, Frame]] = None
        for ctx in contexts(page):
            ok = await select_report(ctx, REPORT_TITLE, timeout_ms=9000)
            if ok:
                picked_ctx = ctx
                break
        if not picked_ctx:
            await safe_screenshot(page, "debug_select_report_failed.png")
            raise RuntimeError(f"Could not select report '{REPORT_TITLE}'.")

        # 4) DATE = yesterday (force-set so Execute enables)
        await set_as_on_date(picked_ctx, YESTERDAY)

        # 5) EXECUTE
        await click_execute(picked_ctx)

        # 6) WAIT + DOWNLOAD
        btn = None
        for _ in range(180):   # ~90s
            btn = await find_download_button(picked_ctx, REPORT_TITLE)
            if btn:
                break
            await picked_ctx.wait_for_timeout(500)
        if not btn:
            await safe_screenshot(picked_ctx, "debug_download_icon_missing.png")
            raise RuntimeError("Report did not become downloadable in time.")

        async with page.expect_download() as dl_info:
            await btn.click()
        download = await dl_info.value
        suggested = download.suggested_filename or DEFAULT_FILENAME
        file_path = DOWNLOAD_DIR / suggested
        await download.save_as(file_path)
        print(f"Downloaded: {file_path}")

        await context.close()
        await browser.close()

    if email_config_ok():
        email_files([file_path])
    else:
        print("[INFO] Skipping email step.")

# =============================== Email helper ================================
def email_files(paths):
    msg = MIMEMultipart()
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    msg["Subject"] = f"{REPORT_TITLE} – {YESTERDAY.isoformat()}"
    body = f"Attached is the {REPORT_TITLE} for {YESTERDAY.isoformat()}."
    msg.attach(MIMEText(body, "plain"))
    for p in paths:
        with open(p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={Path(p).name}")
        msg.attach(part)
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        print("Email sent to:", TO_EMAIL)

# =============================== Entrypoint ==================================
if __name__ == "__main__":
    asyncio.run(run_automation())
