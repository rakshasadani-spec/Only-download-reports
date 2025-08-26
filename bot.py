name: Nuvama Daily Capital Flows Report

on:
  schedule:
    # 03:30 UTC = 09:00 IST daily
    - cron: "30 3 * * *"
  workflow_dispatch: {}   # enables the “Run workflow” button

permissions:
  contents: read

concurrency:
  group: nuvama-daily
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Cache pip
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('**/requirements.txt') }}
          restore-keys: |
            ${{ runner.os }}-pip-

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          python -m playwright install --with-deps chromium

      - name: Run report downloader
        env:
          WEBSITE_USER: ${{ secrets.WEBSITE_USER }}
          WEBSITE_PASS: ${{ secrets.WEBSITE_PASS }}
          # Optional overrides (default LOGIN_URL already set in bot.py)
          LOGIN_URL: ${{ secrets.LOGIN_URL }}
          # Optional email settings
          ENABLE_EMAIL: ${{ secrets.ENABLE_EMAIL }}
          FROM_EMAIL: ${{ secrets.FROM_EMAIL }}
          TO_EMAIL: ${{ secrets.TO_EMAIL }}
          SMTP_SERVER: ${{ secrets.SMTP_SERVER }}
          SMTP_PORT: ${{ secrets.SMTP_PORT }}
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
        run: |
          python --version
          python bot.py

      - name: Upload downloaded files
        if: always()   # upload even if the run failed (helps debugging)
        uses: actions/upload-artifact@v4
        with:
          name: capital-flows-${{ github.run_id }}
          path: downloads/**
          if-no-files-found: warn
