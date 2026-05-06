# 台指選擇權籌碼雷達 v1.1 自動版

## 這包會做什麼

每天台灣時間 17:40 自動跑一次：

- 抓期交所 TXO 選擇權資料
- 抓 TXF 台指期結算價
- 計算 W / F / 月選
- 輸出手機閱讀版文字報表
- 產生 CSV 紀錄
- 存成 GitHub Actions artifact

## 手機設定方式

### 1. 建 GitHub repo

到 GitHub 新增一個 repository，例如：

`txo-option-radar`

### 2. 上傳這包檔案

把以下內容上傳到 repo：

- `txo_option_radar_v11.py`
- `requirements.txt`
- `.github/workflows/daily.yml`

注意：`.github/workflows/daily.yml` 要維持這個資料夾結構。

### 3. 開啟 Actions

進入 repo 後點：

`Actions`

如果有要求啟用 workflow，按同意啟用。

### 4. 手動測試一次

進入：

`Actions → TXO Option Radar Daily → Run workflow`

按一次執行。

成功後會看到：

`Artifacts → txo-option-radar-report`

點進去可以下載報告。

## 自動時間

目前設定：

台灣時間每週一～週五 17:40 自動跑。

對應 GitHub cron：

`40 9 * * 1-5`

## 下一步：接 Google Sheet

目前這版先存 GitHub artifact。
確認穩定後，可以再接 Google Sheet，讓報告每天自動寫進表格，不用下載 artifact。
