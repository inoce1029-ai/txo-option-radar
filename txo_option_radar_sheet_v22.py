name: TXO Option Chip Radar Daily

on:
  schedule:
    # Taiwan 18:30 Mon-Fri = UTC 10:30 Mon-Fri
    - cron: "30 10 * * 1-5"
  workflow_dispatch:

jobs:
  run-txo-option-radar:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run TXO option radar
        env:
          GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
          GOOGLE_SHEET_ID: ${{ secrets.GOOGLE_SHEET_ID }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          python txo_option_radar_v22.py | tee txo_option_radar_report.txt

      - name: Upload backup report artifact
        uses: actions/upload-artifact@v4
        with:
          name: txo-option-radar-report
          path: |
            txo_option_radar_report.txt
            txo_option_radar_summary_v22.csv
