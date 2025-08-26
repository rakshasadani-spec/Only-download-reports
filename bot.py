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

# Report title (you can override via a secret)
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

# --- Click the left sidebar "Reports" (3rd from top) -------------------------
async def click_reports_nav(page: Page):
    try:
        sidebar = page.locator("#reportLeftMenu")
        await sidebar.wait_for(state="visible", timeout=8000)
        # Prefer text match inside sidebar
        link = sidebar.get_by_role("link", name=re.compile(r"\bReports\b", re.I))
        if await link.count() > 0:
            await link.first.click()
            return
        # Fallback: 3rd link in sidebar
        links = sidebar.get_by_role("link")
        if await links.count() >= 3:
            await links.nth(2).click()
            return
    except Exception:
        pass
    # URL-based fallback
    try:
        await page.locator("#reportLeftMenu a[href*='/app/reports']").first.click(timeout=6000)
        return
    except Exception:
        pass
    # Last resort (anywhere)
    try:
        await page.get_by_role("link", name=re.compile(r"\bReports\b", re.I)).first.click(timeout=6000)
        return
    except Exception:
        await page.get_by_text("Reports", exact=False).first.click()

# --- Report dropdown selection near label "Report" ---------------------------
async def select_report(ctx: Union[Page, Frame], option_text: str, timeout_ms: int = 9000) -> bool:
    # 1) Native <select> next to label
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

    # 2) Click the field next to "Report" (custom dropdowns)
    try:
        # target the first focusable control to the right of label
        box = ctx.locator("xpath=//label[contains(., 'Report')]/following::*[self::div or self::button or self::span or self::input][1]")
        await box.first.click(timeout=2000)
    except Exception:
        # try generic combobox/button
        try:
            await ctx.get_by_role("combobox").first.click(timeout=2000)
        except Exception:
            pass

    # try common option containers
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

    # 3) React-select style: type then Enter
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

    # 4) Last resort: click visible text anywhere
    try:
        await ctx.get_by_text(option_text, exact=False).first.click(timeout=timeout_ms)
        print("[DEBUG] Selected report via generic text click.")
        return True
    except Exception:
        pass

    return False

# --- Set "As on Date" to yesterday ------------------------------------------
async def set_as_on_date(ctx: Union[Page, Frame], date_iso: str, date_dmy_slash: str):
    # Prefer the input that follows the "As on Date" label
    inp = ctx.locator("xpath=//label[contains(., 'As on Date')]/following::input[1]")
    try:
        await inp.first.wait_for(state="visible", timeout=3000)
        # Try normal fill
        try:
            await inp.first.click()
            await inp.first.press("Control+A")
            await inp.first.type(date_dmy_slash)
            await inp.first.press("Enter")
            print("[DEBUG] Date set via type (dd/mm/YYYY).")
            return
        except Exception:
            pass
        # Try ISO
        try:
            await inp.first.fill(date_iso)
            await inp.first.press("Enter")
            print("[DEBUG] Date set via fill (YYYY-mm-dd).")
            return
        except Exception:
            pass
        # Force set via JS + events (handles readonly)
        try:
            await inp.first.evaluate(
                "(el, val) => { el.removeAttribute('readonly'); el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                date_dmy_slash,
            )
            print("[DEBUG] Date set via JS value+events.")
            return
        except Exception:
            pass
    except Exception:
        pass

    # Fallback: any date inputs
    try:
        di = ctx.locator("input[type='date']").first
        await di.fill(date_iso)
        print("[DEBUG] Date set via input[type=date].")
        return
    except Exception:
        print("[WARN] Could not set date; using site's default.")

async def click_execute(ctx: Union[Page, Frame]):
    try:
        await ctx.get_by_role("button", name=re.compile(r"\bexecute\b", re.I)).click()
        print("[DEBUG] Clicked Execute via role=button.")
        return
    except Exception:
        pass
    try:
        await ctx.get_by_text("Execute", exact=False).first.click()
        print("[DEBUG] Clicked Execute via text fallback.")
        return
    except Exception:
        await safe_screenshot(ctx, "debug_execute_click_failed.png")
        raise

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

        # 4) DATE = yesterday (dd/mm/YYYY preferred on this UI)
        await set_as_on_date(picked_ctx, YESTERDAY.strftime("%Y-%m-%d"), YESTERDAY.strftime("%d/%m/%Y"))

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
