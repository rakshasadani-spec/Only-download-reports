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
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# ---- Time & paths -----------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).date()
YESTERDAY = TODAY - timedelta(days=1)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---- Secrets / Environment --------------------------------------------------
USERNAME = os.getenv("WEBSITE_USER", "")
PASSWORD = os.getenv("WEBSITE_PASS", "")

# Safe fallback: if secret LOGIN_URL is unset OR empty, use default
LOGIN_URL = (os.getenv("LOGIN_URL") or
             "https://eclientreporting.nuvamaassetservices.com/wealthspectrum/app/loginWith")

# Optional email settings (robust defaults / safe skipping)
ENABLE_EMAIL = os.getenv("ENABLE_EMAIL", "false").strip().lower() == "true"
FROM_EMAIL = (os.getenv("FROM_EMAIL") or "").strip()
TO_EMAIL = (os.getenv("TO_EMAIL") or "").strip()
SMTP_SERVER = (os.getenv("SMTP_SERVER") or "").strip()
SMTP_PORT = int((os.getenv("SMTP_PORT") or "587").strip() or "587")
SMTP_USER = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS = (os.getenv("SMTP_PASS") or "").strip()

# ---- Report config ----------------------------------------------------------
REPORT_TITLE = os.getenv("REPORT_TITLE", "Statement of Capital Flows")
DEFAULT_FILENAME = f"capital_flows_{YESTERDAY.isoformat()}.xlsx"

# ============================ Helpers ========================================
def email_config_ok() -> bool:
    if not ENABLE_EMAIL:
        return False
    required = [FROM_EMAIL, TO_EMAIL, SMTP_SERVER, str(SMTP_PORT), SMTP_USER, SMTP_PASS]
    if any(not v for v in required):
        print("[INFO] Email not sent: ENABLE_EMAIL=true but one or more SMTP fields are blank.")
        return False
    return True

async def safe_screenshot(page, name: str):
    try:
        path = DOWNLOAD_DIR / name
        await page.screenshot(path=str(path), full_page=True)
        print(f"[DEBUG] Saved screenshot: {path}")
    except Exception as e:
        print(f"[DEBUG] Failed to capture screenshot: {e}")

async def select_report_by_text(page, label_text: str, option_text: str, timeout_ms: int = 8000):
    """
    Tries multiple strategies to select a report option by visible text.
    Works with native <select> or custom dropdowns (combobox -> listbox/options, li items, menus).
    """
    # 1) Native <select> near the 'Report' label
    try:
        sel = page.locator("select").filter(has=page.get_by_text(label_text, exact=False))
        if await sel.count() == 0:
            sel = page.locator("select").first
        await sel.wait_for(state="visible", timeout=timeout_ms)
        await sel.select_option(label=option_text)
        return True
    except Exception:
        pass

    # 2) Click a combobox near the label, then choose option by role
    try:
        # Try a labeled combobox
        label = page.get_by_text(label_text, exact=False).first
        # Find a combobox in the same row/section
        combo = page.get_by_role("combobox")
        # Prefer the first combobox that appears after clicking near label
        try:
            await label.click(timeout=2000)
        except Exception:
            pass
        await combo.first.click(timeout=timeout_ms)

        # Options by ARIA role
        listbox = page.get_by_role("listbox")
        if await listbox.count() > 0:
            opt = listbox.get_by_role("option", name=re.compile(re.escape(option_text), re.I)).first
            await opt.click(timeout=timeout_ms)
            return True

        # Options as menu items
        menuitem = page.get_by_role("menuitem", name=re.compile(re.escape(option_text), re.I)).first
        await menuitem.click(timeout=timeout_ms)
        return True
    except Exception:
        pass

    # 3) Generic dropdown patterns: open a trigger near 'Report', then click text in a popup/panel
    try:
        # Common triggers: elements with down-arrow icons or classes, buttons near "Report"
        triggers = [
            page.locator("button:has(svg)"),
            page.locator("button:has(i)"),
            page.locator("button[aria-haspopup='listbox']"),
            page.locator("span:has(svg)"),
            page.locator("div[role='button']"),
        ]
        # Try each trigger to open a panel
        opened = False
        for t in triggers:
            if await t.count() == 0:
                continue
            try:
                await t.first.click(timeout=1500)
                # If a panel/popup appears, we proceed
                if await page.locator("[role='listbox'], .dropdown-menu, .mat-select-panel, ul[role='listbox']").count() > 0:
                    opened = True
                    break
            except Exception:
                continue

        # Try to click the option by visible text anywhere in an open panel
        if opened:
            candidates = [
                page.get_by_role("option", name=re.compile(re.escape(option_text), re.I)).first,
                page.get_by_text(option_text, exact=False).first,
                page.locator("li", has_text=re.compile(re.escape(option_text), re.I)).first,
                page.locator("[role='menuitem']", has_text=re.compile(re.escape(option_text), re.I)).first,
            ]
            for c in candidates:
                try:
                    await c.click(timeout=timeout_ms)
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    # 4) Absolute fallback: search for any element with the exact text and click it
    try:
        await page.get_by_text(option_text, exact=False).first.click(timeout=timeout_ms)
        return True
    except Exception:
        pass

    return False

# =============================== Core logic =================================
async def run_automation():
    # Fail fast if creds missing
    if not USERNAME or not PASSWORD:
        raise RuntimeError("Missing WEBSITE_USER or WEBSITE_PASS. Add them as GitHub Secrets or env vars.")

    # Sanity check for LOGIN_URL
    if not (isinstance(LOGIN_URL, str) and LOGIN_URL.startswith("http")):
        raise RuntimeError("LOGIN_URL is invalid or empty. Fix the secret or let the default be used.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # ----------------------- 1) LOGIN ------------------------------------
        await page.goto(LOGIN_URL, wait_until="networkidle")

        # Try common username/password selectors
        login_attempts = [
            ("input[name='username']", "input[name='password']"),
            ("input#username", "input#password"),
            ("input[autocomplete='username']", "input[autocomplete='current-password']"),
        ]
        filled = False
        for u_sel, p_sel in login_attempts:
            try:
                await page.fill(u_sel, USERNAME, timeout=2500)
                await page.fill(p_sel, PASSWORD, timeout=2500)
                filled = True
                break
            except PWTimeoutError:
                continue

        if not filled:
            # Fallback by placeholder/label
            try:
                await page.get_by_placeholder(re.compile("user", re.I)).fill(USERNAME, timeout=2500)
                await page.get_by_placeholder(re.compile("pass|pwd", re.I)).fill(PASSWORD, timeout=2500)
                filled = True
            except PWTimeoutError:
                pass

        if not filled:
            await safe_screenshot(page, "debug_login_fields.png")
            raise RuntimeError("Could not locate login fields. Update selectors in bot.py.")

        # Click Log in
        try:
            await page.get_by_role("button", name=re.compile(r"log ?in", re.I)).click()
        except PWTimeoutError:
            await page.click("button[type='submit']")
        await page.wait_for_load_state("networkidle")

        # ----------------------- 2) REPORTS TAB ------------------------------
        try:
            await page.locator("a[href*='/app/reports']").first.click(timeout=8000)
        except PWTimeoutError:
            reports_links = page.get_by_role("link", name=re.compile(r"reports?", re.I))
            if await reports_links.count() == 0:
                await page.get_by_text("Reports", exact=False).first.click()
            else:
                await reports_links.first.click()
        await page.wait_for_load_state("networkidle")

        # ----------------------- 3) SELECT REPORT ----------------------------
        ok = await select_report_by_text(page, label_text="Report", option_text=REPORT_TITLE, timeout_ms=9000)
        if not ok:
            await safe_screenshot(page, "debug_select_report_failed.png")
            raise RuntimeError(f"Could not select '{REPORT_TITLE}' in a dropdown. Update selectors.")

        # ----------------------- 4) DATE = YESTERDAY -------------------------
        date_set = False
        iso_date = YESTERDAY.strftime("%Y-%m-%d")
        dmy_date = YESTERDAY.strftime("%d-%m-%Y")

        try:
            from_inp = page.locator("input[type='date']").nth(0)
            to_inp = page.locator("input[type='date']").nth(1)
            await from_inp.fill(iso_date)
            await to_inp.fill(iso_date)
            date_set = True
        except Exception:
            try:
                await page.get_by_label(re.compile(r"from date|start date", re.I)).fill(iso_date)
                await page.get_by_label(re.compile(r"to date|end date", re.I)).fill(iso_date)
                date_set = True
            except Exception:
                try:
                    grp = page.get_by_text("Date", exact=False).locator("xpath=ancestor::*[1]")
                    inputs = grp.locator("input")
                    if await inputs.count() >= 2:
                        await inputs.nth(0).fill(dmy_date)
                        await inputs.nth(1).fill(dmy_date)
                        date_set = True
                except Exception:
                    pass

        if not date_set:
            print("[WARN] Could not auto-set dates; relying on site's default date range.")

        # ----------------------- 5) EXECUTE ---------------------------------
        try:
            await page.get_by_role("button", name=re.compile("execute", re.I)).click()
        except PWTimeoutError:
            try:
                await page.get_by_text("Execute", exact=False).first.click()
            except Exception:
                await safe_screenshot(page, "debug_execute_click_failed.png")
                raise

        # ----------------------- 6) WAIT + DOWNLOAD --------------------------
        async def find_download_button():
            row = page.get_by_role("row").filter(has=page.get_by_text(REPORT_TITLE, exact=False)).first
            candidates = [
                row.get_by_role("button", name=re.compile(r"download|document|file|xlsx|excel|csv", re.I)),
                row.get_by_role("link", name=re.compile(r"download|document|file|xlsx|excel|csv", re.I)),
                row.locator("a[download]"),
                row.locator("button:has(svg), a:has(svg)"),
                page.get_by_role("button", name=re.compile(r"document", re.I)).first,
            ]
            for c in candidates:
                try:
                    await c.wait_for(state="visible", timeout=500)
                    return c
                except PWTimeoutError:
                    continue
            return None

        btn = None
        for _ in range(160):  # ~80s @ 500ms
            btn = await find_download_button()
            if btn:
                break
            await page.wait_for_timeout(500)

        if not btn:
            await safe_screenshot(page, "debug_download_icon_missing.png")
            raise RuntimeError("Report did not become downloadable in time. Increase timeout or refine selectors.")

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
    msg["Subject"] = f"Statement of Capital Flows – {YESTERDAY.isoformat()}"

    body = f"Attached is the Statement of Capital Flows for {YESTERDAY.isoformat()}."
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
