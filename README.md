# Nuvama Daily Capital Flows – Auto Downloader (9:00 AM IST)

This repo logs into Nuvama Wealth Spectrum, runs **Statement of Capital Flows** for **yesterday (IST)**, downloads the file into `downloads/`, and (optionally) emails it. A GitHub Action runs it **daily at 9:00 AM IST** (03:30 UTC), and you can also run it manually.

## Setup

1) **Secrets** (GitHub → Settings → Secrets and variables → Actions):
- `WEBSITE_USER` – your Nuvama login ID
- `WEBSITE_PASS` – your Nuvama password
- *(optional)* `LOGIN_URL`
- *(optional email)* `ENABLE_EMAIL`=`true`, `FROM_EMAIL`, `TO_EMAIL`, `SMTP_SERVER`, `SMTP_PORT`=`587`, `SMTP_USER`, `SMTP_PASS` (use app password if Gmail/Workspace)

2) **Files**
- `bot.py` – Playwright automation
- `.github/workflows/daily.yml` – CI schedule (03:30 UTC / 09:00 IST) + manual runs
- `requirements.txt` – `playwright==1.54.0`

3) **Manual run**
- GitHub → **Actions** → **Nuvama Daily Capital Flows Report** → **Run workflow**

4) **Result**
- Downloaded file is saved under `downloads/` and also uploaded as **Artifacts** of the workflow run.

## Local test (optional)
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
export WEBSITE_USER=... WEBSITE_PASS=...
python bot.py
