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

# --- Left sidebar "Reports" (3rd from top) ----------------------------------
async def click_reports_nav(page: Page):
    try:
        sidebar = page.locator("#reportLeftMenu")
        await sidebar.wait_for(state="visible", timeout=8000)
        link = sidebar.get_by_role("link", name=re.compile(r"\bReports\b", re.I))
        if await link.count() > 0:
            await link.first.click()
            return
        links = sidebar.get_by_role("link")
        if await links.count() >= 3:
            await links.nth(2).click()
            return
