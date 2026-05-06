# 台指選擇權籌碼雷達 v1.3 Google Sheet 自動版

更新內容：
- 結算方向基準改用加權指數
- TXF 近月只作期貨參考
- W/F 合約顯示完全照期交所資料來源
- 月選只保留當月與次月
- 「主力區」改成「大量區」
- 「今日一句話」改成「今日總結」
- 上下方大量區限制距離，避免遠價履約價扭曲
- 近端綜合只用目前顯示合約

上傳到 GitHub：
1. 覆蓋 txo_option_radar_sheet_v13.py
2. 覆蓋 requirements.txt
3. 覆蓋 .github/workflows/daily_sheet.yml

測試：
Actions → TXO Option Radar Google Sheet Daily → Run workflow
