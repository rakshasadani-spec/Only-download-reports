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
LOGIN_URL = os.getenv(
    "LOGIN_URL",
    "https://eclientreporting.nuvamaassetservices.com/wealthspectrum/app/loginWith",
)

# Optional email settings (robust defaults / safe skipping)
ENABLE_EMAIL = os.getenv("ENABLE_EMAIL", "false").strip().lower() == "true"
FROM_EMAIL = (os.getenv("FROM_EMAIL") or "").strip()
TO_EMAIL = (os.getenv("TO_EMAIL") or "").strip()
SMTP_SERVER = (os.getenv("SMTP_SERVER") or "").strip()
# handle empty string safely
SMTP_PORT = int((os.getenv("SMTP_PORT") or "587").strip() or "587")
SMTP_USER = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS = (os.getenv("SMTP_PASS") or "").strip()

# ---- Report config ----------------------------------------------------------
REPORT_TITLE = "Statement of Capital Flows"
DEFAULT_FILENAME = f"capital_flows_{YESTERDAY.isoformat()}.xlsx"


def email_config_ok() -> bool:
    """Return True only if emailing is enabled AND all fields are non-empty."""
    if not ENABLE_EMAIL:
        return False
    required = [FROM_EMAIL, TO_EMAIL, SMTP_SERVER, str(SMTP_PORT), SMTP_USER, SMTP_PASS]
    if any(not v for v in required):
        print("[INFO] Email not sent: ENABLE_EMAIL=true but one or more SMTP fields are blank.")
        return False
    return True


# =============================== Core logic =================================
async def run_automation():
    # Fail fast if creds missing
    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "Missing WEBSITE_USER or WEBSITE_PASS. Add them as GitHub Secrets or environment variables."
        )

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
            raise RuntimeError("Could not locate login fields. Update selectors in bot.py.")

        # Click Log in
        try:
            await page.get_by_role("button", name=re.compile(r"log ?in", re.I)).click()
        except PWTimeoutError:
            # Generic submit fallback
            await page.click("button[type='submit']")
        await page.wait_for_load_state("networkidle")

        # ----------------------- 2) REPORTS TAB ------------------------------
        # Left-side "Reports" navigation
        try:
            await page.get_by_role("link", name=re.compile(r"reports?", re.I)).click(timeout=8000)
        except PWTimeoutError:
            # Some apps use a button/div/span
            await page.get_by_text("Reports", exact=False).first.click()
        await page.wait_for_load_state("networkidle")

        # ----------------------- 3) SELECT REPORT ----------------------------
        # First try a native <select> adjacent to "Report"
        selected = False
        try:
            sel = page.locator("select").filter(has=page.get_by_text("Report"))
            if await sel.count() == 0:
                sel = page.locator("select").first
            await sel.select_option(label=REPORT_TITLE)
            selected = True
        except Exception:
            # Custom dropdown: click combobox then option
            try:
                dd = page.get_by_role("combobox").first
                await dd.click()
                await page.get_by_role("option", name=REPORT_TITLE).click()
                selected = True
            except Exception:
                # Very generic fallback: click near the "Report" label then the option text
                try:
                    await page.get_by_text("Report", exact=False).first.click()
                    await page.get_by_text(REPORT_TITLE, exact=False).first.click()
                    selected = True
                except Exception:
                    pass

        if not selected:
            raise RuntimeError("Could not select 'Statement of Capital Flows' in a dropdown. Update selectors.")

        # ----------------------- 4) DATE = YESTERDAY -------------------------
        # Try HTML5 date inputs first
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
            # Try label-based
            try:
                await page.get_by_label(re.compile(r"from date|start date", re.I)).fill(iso_date)
                await page.get_by_label(re.compile(r"to date|end date", re.I)).fill(iso_date)
                date_set = True
            except Exception:
                # Fallback: two text inputs near a "Date" label; try d-m-Y format
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
            await page.get_by_text("Execute", exact=False).first.click()

        # ----------------------- 6) WAIT + DOWNLOAD --------------------------
        # Find the result row for our report, wait until a download icon appears, then click.
        async def find_download_button():
            # A row containing the report title
            row = page.get_by_role("row").filter(has=page.get_by_text(REPORT_TITLE, exact=False)).first
            # Candidate clickable elements
            candidates = [
                row.get_by_role("button", name=re.compile(r"download|document|file|xlsx|excel|csv", re.I)),
                row.get_by_role("link", name=re.compile(r"download|document|file|xlsx|excel|csv", re.I)),
                row.locator("a[download]"),
                row.locator("button:has(svg), a:has(svg)"),
            ]
            for c in candidates:
                try:
                    await c.wait_for(state="visible", timeout=500)
                    return c
                except PWTimeoutError:
                    continue
            return None

        # Poll for up to ~60 seconds (spinner → document icon)
        btn = None
        for _ in range(120):  # 120 * 500ms = ~60s
            btn = await find_download_button()
            if btn:
                break
            await page.wait_for_timeout(500)

        if not btn:
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

    # Optional email (only if fully configured)
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
