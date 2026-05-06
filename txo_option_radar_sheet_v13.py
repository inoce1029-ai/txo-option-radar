# 台指選擇權籌碼雷達 v1.3
# Google Sheet 自動版

import io
import os
import re
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials

PRODUCT_OPT = "TXO"
PRODUCT_FUT = "TX"
SHOW_NEAR_W_COUNT = 2
SHOW_NEAR_F_COUNT = 2
SHOW_MONTH_COUNT = 2
NEAR_ZONE_WIDTH = 100
TOP_N_WALLS = 2
MAX_WALL_DISTANCE = 1000
SETTLE_HOUR = 13
SETTLE_MINUTE = 45
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
OPT_URL = "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=DailyMarketReportOpt"
FUT_URL = "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=DailyMarketReportFut"

def now_taipei():
    return datetime.now(TAIPEI_TZ)

def fetch_taifex_csv(url):
    headers = {"User-Agent":"Mozilla/5.0", "Accept":"text/csv, text/plain, */*"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    raw = r.content
    for enc in ["utf-8-sig", "big5", "cp950"]:
        try:
            return pd.read_csv(io.StringIO(raw.decode(enc)))
        except Exception:
            pass
    return pd.read_csv(io.StringIO(raw.decode("utf-8", errors="ignore")))

def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).replace("\ufeff", "").replace("\n", "").replace(" ", "").strip() for c in df.columns]
    return df

def find_col(df, candidates, required=True):
    cols = list(df.columns)
    for key in candidates:
        for c in cols:
            if key in c:
                return c
    if required:
        raise KeyError(f"找不到欄位：{candidates}\n目前欄位：{cols}")
    return None

def to_num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).str.replace("--", "", regex=False).str.replace("-", "", regex=False).str.strip(), errors="coerce")

def parse_number(x):
    try:
        txt = str(x).replace(",", "").replace("--", "").strip()
        if txt in ["", "-", "--"]:
            return None
        return float(txt)
    except Exception:
        return None

def clean_option_data(df):
    df = normalize_columns(df)
    col_date = find_col(df, ["日期", "交易日期"])
    col_contract = find_col(df, ["契約", "商品"])
    col_expiry = find_col(df, ["到期月份(週別)", "到期月份", "到期月"])
    col_strike = find_col(df, ["履約價"])
    col_cp = find_col(df, ["買賣權", "權別"])
    col_oi = find_col(df, ["未沖銷契約量", "未沖銷契約數", "未平倉", "未沖銷"])
    col_settle = None
    for keys in [["結算價"], ["最後成交價"], ["收盤價"]]:
        col_settle = find_col(df, keys, required=False)
        if col_settle: break
    out = pd.DataFrame({
        "date": df[col_date].astype(str).str.strip(),
        "contract": df[col_contract].astype(str).str.strip(),
        "expiry": df[col_expiry].astype(str).str.strip(),
        "strike": to_num(df[col_strike]),
        "cp": df[col_cp].astype(str).str.strip(),
        "oi": to_num(df[col_oi]),
        "settle": to_num(df[col_settle]) if col_settle else 0,
    })
    out = out[out["contract"].str.contains(PRODUCT_OPT, na=False)].copy()
    out["cp"] = out["cp"].replace({"買權":"C", "賣權":"P", "Call":"C", "Put":"P", "CALL":"C", "PUT":"P"})
    out.loc[out["cp"].str.contains("C|買", na=False), "cp"] = "C"
    out.loc[out["cp"].str.contains("P|賣", na=False), "cp"] = "P"
    out = out.dropna(subset=["strike", "oi"])
    out["strike"] = out["strike"].astype(int)
    out["oi"] = out["oi"].fillna(0).astype(float)
    out["settle"] = out["settle"].fillna(0).astype(float)
    return out

def get_txf_reference_price():
    try:
        raw = fetch_taifex_csv(FUT_URL)
        df = normalize_columns(raw)
        col_contract = find_col(df, ["契約", "商品"])
        col_expiry = find_col(df, ["到期月份", "到期月"], required=False)
        price_col = None
        for keys in [["結算價"], ["收盤價"], ["最後成交價"], ["開盤價"]]:
            price_col = find_col(df, keys, required=False)
            if price_col: break
        if price_col is None:
            return None, "TXF自動抓取失敗：找不到價格欄位"
        fut = df[df[col_contract].astype(str).str.strip().eq(PRODUCT_FUT)].copy()
        if fut.empty:
            fut = df[df[col_contract].astype(str).str.contains(PRODUCT_FUT, na=False)].copy()
        if fut.empty:
            return None, "TXF自動抓取失敗：找不到 TX 契約"
        fut["price"] = to_num(fut[price_col])
        fut = fut.dropna(subset=["price"])
        fut = fut[fut["price"] > 0]
        if fut.empty:
            return None, "TXF自動抓取失敗：價格為空"
        if col_expiry:
            fut["_expiry_sort"] = pd.to_numeric(fut[col_expiry].astype(str).str.extract(r"(\d+)")[0], errors="coerce")
            fut = fut.sort_values(["_expiry_sort"])
        row = fut.iloc[0]
        price = float(row["price"])
        expiry = str(row[col_expiry]) if col_expiry else "近月"
        return price, f"TXF 近月 {expiry}：{price_col}"
    except Exception as e:
        return None, f"TXF自動抓取失敗：{type(e).__name__} {e}"

def find_taiex_in_json(obj):
    if isinstance(obj, dict):
        fields = obj.get("fields")
        data = obj.get("data")
        if isinstance(fields, list) and isinstance(data, list):
            for row in data:
                if isinstance(row, list) and row and "發行量加權股價指數" in str(row[0]):
                    for name in ["收盤指數", "收盤價", "收盤"]:
                        if name in fields:
                            idx = fields.index(name)
                            if idx < len(row):
                                val = parse_number(row[idx])
                                if val: return val
                    for cell in reversed(row):
                        val = parse_number(cell)
                        if val: return val
        for v in obj.values():
            found = find_taiex_in_json(v)
            if found: return found
    elif isinstance(obj, list):
        if obj and "發行量加權股價指數" in str(obj[0]):
            for cell in reversed(obj):
                val = parse_number(cell)
                if val: return val
        for v in obj:
            found = find_taiex_in_json(v)
            if found: return found
    return None

def get_taiex_reference_price():
    headers = {"User-Agent":"Mozilla/5.0", "Accept":"application/json,text/plain,*/*"}
    today = now_taipei().date()
    templates = [
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date}&type=IND&response=json",
        "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=IND",
    ]
    for i in range(10):
        ds = (today - timedelta(days=i)).strftime("%Y%m%d")
        for tmpl in templates:
            try:
                r = requests.get(tmpl.format(date=ds), headers=headers, timeout=30)
                if r.status_code != 200: continue
                price = find_taiex_in_json(r.json())
                if price:
                    return price, f"加權指數收盤價 {ds}"
            except Exception:
                continue
    return None, "加權指數自動抓取失敗"

def parse_week_tag(expiry):
    e = str(expiry).upper().strip()
    m_w = re.search(r"W(\d+)", e)
    if m_w: return "W", int(m_w.group(1))
    m_f = re.search(r"F(\d+)", e)
    if m_f: return "F", int(m_f.group(1))
    return "M", 0

def sort_key_by_expiry(expiry):
    e = str(expiry).upper().strip()
    tag, num = parse_week_tag(e)
    if tag == "W": return (num, 1, e)
    if tag == "F": return (num, 2, e)
    m = re.search(r"(\d{6})", e)
    month_num = int(m.group(1)) if m else 999999
    return (99, month_num, e)

def is_after_settlement_now():
    now = now_taipei()
    return (now.hour, now.minute) >= (SETTLE_HOUR, SETTLE_MINUTE)

def filter_near_expiries(expiry_list):
    now = now_taipei()
    weekday = now.weekday()
    after_settle = is_after_settlement_now()
    w_list, f_list, m_list = [], [], []
    for exp in sorted(expiry_list, key=sort_key_by_expiry):
        tag, num = parse_week_tag(exp)
        if tag == "W":
            if weekday == 2 and after_settle and num == 1: continue
            w_list.append(exp)
        elif tag == "F":
            if weekday == 4 and after_settle and num == 1: continue
            f_list.append(exp)
        else:
            m_list.append(exp)
    selected = []
    selected.extend(w_list[:SHOW_NEAR_W_COUNT])
    selected.extend(f_list[:SHOW_NEAR_F_COUNT])
    selected.extend(m_list[:SHOW_MONTH_COUNT])
    return sorted(selected, key=sort_key_by_expiry)

def calc_max_pain(group):
    if group.empty: return None
    strikes = sorted(group["strike"].dropna().unique())
    calls = group[group["cp"] == "C"].groupby("strike")["oi"].sum()
    puts = group[group["cp"] == "P"].groupby("strike")["oi"].sum()
    best_strike, best_loss = None, None
    for s in strikes:
        call_loss = sum(max(0, s-k)*oi for k, oi in calls.items())
        put_loss = sum(max(0, k-s)*oi for k, oi in puts.items())
        loss = call_loss + put_loss
        if best_loss is None or loss < best_loss:
            best_loss, best_strike = loss, s
    return int(best_strike) if best_strike is not None else None

def wall_table(group, basis_price, cp, top_n=TOP_N_WALLS):
    g = group[group["cp"] == cp].groupby("strike", as_index=False)["oi"].sum()
    if g.empty: return g
    if basis_price is None or math.isnan(basis_price):
        g["score"] = g["oi"]
        return g.sort_values(["score", "oi"], ascending=False).head(top_n)
    distance = (g["strike"] - basis_price).abs().clip(lower=1)
    g["score"] = g["oi"] / (1 + distance / 500)
    side = g[g["strike"] >= basis_price].copy() if cp == "C" else g[g["strike"] <= basis_price].copy()
    near = side[side["strike"].sub(basis_price).abs() <= MAX_WALL_DISTANCE].copy()
    use = near if not near.empty else side
    if use.empty: use = g
    return use.sort_values(["score", "oi"], ascending=False).head(top_n)

def zone_from_walls(walls):
    if walls is None or walls.empty: return None
    lo, hi = int(walls["strike"].min()), int(walls["strike"].max())
    return f"{lo}" if lo == hi else f"{lo}～{hi}"

def analyze_bucket(raw_expiry, group, settle_basis, settle_source, txf_price, txf_source):
    max_pain = calc_max_pain(group)
    calls = wall_table(group, settle_basis, "C", TOP_N_WALLS)
    puts = wall_table(group, settle_basis, "P", TOP_N_WALLS)
    call_oi = int(group[group["cp"] == "C"]["oi"].sum())
    put_oi = int(group[group["cp"] == "P"]["oi"].sum())
    pcr_oi = round(put_oi / call_oi, 2) if call_oi else None
    return {
        "合約": raw_expiry,
        "結算方向基準": round(settle_basis) if settle_basis else None,
        "結算基準來源": settle_source,
        "TXF參考": round(txf_price) if txf_price else None,
        "TXF來源": txf_source,
        "結算靠近價": max_pain,
        "上方大量區": zone_from_walls(calls),
        "下方大量區": zone_from_walls(puts),
        "Call OI": call_oi,
        "Put OI": put_oi,
        "OI PCR": pcr_oi,
    }

def parse_zone(zone):
    if not zone: return None, None
    s = str(zone)
    if "～" in s:
        a, b = s.split("～", 1)
        try: return int(float(a)), int(float(b))
        except Exception: return None, None
    try:
        v = int(float(s)); return v, v
    except Exception:
        return None, None

def pick_primary_contract(results):
    for r in results:
        if re.search(r"F1\b", str(r["合約"]).upper()): return r
    for r in results:
        tag, _ = parse_week_tag(r["合約"])
        if tag in ["W", "F"]: return r
    return results[0] if results else None

def zone_direction(basis, max_pain):
    if max_pain is None or basis is None or math.isnan(basis): return "中性"
    diff = max_pain - basis
    if abs(diff) <= 100: return "目前接近"
    return "偏上靠" if diff > 0 else "偏下靠"

def make_summary(results, settle_basis):
    main = pick_primary_contract(results)
    near = next((r for r in results if r["合約"] == "近端綜合"), None)
    if not main: return "資料不足，暫時不做方向判斷。"
    ref = near if near else main
    return (f"短線以 {main['合約']} 為主，結算靠近價約 {main.get('結算靠近價')}。"
            f"以加權位置判斷：{zone_direction(settle_basis, main.get('結算靠近價'))}。"
            f"上方 {ref.get('上方大量區')} 是主要大量壓力區，下方 {ref.get('下方大量區')} 是主要大量支撐區。")

def format_table_line(cols, widths):
    return "  ".join([(str(c) if c is not None else "-").ljust(w) for c, w in zip(cols, widths)])

def get_mass_zones(results):
    near = next((r for r in results if r["合約"] == "近端綜合"), None)
    months = [r for r in results if parse_week_tag(r["合約"])[0] == "M" and r["合約"] != "近端綜合"]
    main_pressure = near.get("上方大量區") if near else (results[0].get("上方大量區") if results else None)
    main_support = near.get("下方大量區") if near else (results[0].get("下方大量區") if results else None)
    secondary_pressure = months[0].get("上方大量區") if months else main_pressure
    secondary_support = months[0].get("下方大量區") if months else main_support
    return main_pressure, secondary_pressure, main_support, secondary_support

def break_prices(results):
    main = pick_primary_contract(results)
    if not main: return None, None, None
    _, p_hi = parse_zone(main.get("上方大量區"))
    s_lo, _ = parse_zone(main.get("下方大量區"))
    return main, p_hi, s_lo

def format_report(results, settle_basis, settle_source, txf_price, txf_source, selected_expiries, all_expiries):
    now = now_taipei()
    main_pressure, secondary_pressure, main_support, secondary_support = get_mass_zones(results)
    main_contract, up_break, down_break = break_prices(results)
    lines = []
    lines.append("台指選擇權籌碼雷達 v1.3")
    lines.append(f"資料日：{now.strftime('%Y/%m/%d')}")
    lines.append(f"結算方向基準：加權指數 {round(settle_basis) if settle_basis else None}（{settle_source}）")
    lines.append(f"期貨參考：TXF {round(txf_price) if txf_price else None}（{txf_source}）")
    lines.append("")
    lines.append("【今日總結】")
    lines.append(make_summary(results, settle_basis))
    lines.append("")
    lines.append("【近端結算重點】")
    widths = [12, 10, 14, 14, 8]
    lines.append(format_table_line(["合約", "結算靠近價", "上方大量區", "下方大量區", "OI PCR"], widths))
    for r in results:
        if r["合約"] == "近端綜合": continue
        lines.append(format_table_line([r["合約"], r["結算靠近價"], r["上方大量區"], r["下方大量區"], r["OI PCR"]], widths))
    lines.append("")
    lines.append("【大量區】")
    lines.append(f"上方主要大量區：{main_pressure}")
    lines.append(f"上方次要大量區：{secondary_pressure}")
    lines.append(f"下方主要大量區：{main_support}")
    lines.append(f"下方次要大量區：{secondary_support}")
    lines.append("")
    lines.append("【破防價】")
    lines.append(f"站上 {up_break}：{main_contract['合約']} 上方大量區可能失效，容易往下一層壓力推進。" if main_contract and up_break else "站上破防價：資料不足")
    lines.append(f"跌破 {down_break}：{main_contract['合約']} 下方大量區可能失效，容易往下一層支撐測試。" if main_contract and down_break else "跌破破防價：資料不足")
    lines.append("")
    lines.append("【結算靠近方向】")
    for r in results:
        if r["合約"] == "近端綜合": continue
        mp = r["結算靠近價"]
        lines.append(f"{r['合約']}：靠 {mp} 附近，{zone_direction(settle_basis, mp)}")
    lines.append("")
    lines.append("【程式資訊】")
    lines.append(f"目前顯示合約：{', '.join(map(str, selected_expiries))}")
    lines.append(f"全部可用合約：{', '.join(map(str, sorted(all_expiries, key=sort_key_by_expiry)))}")
    return "\n".join(lines)

def main():
    raw_opt = fetch_taifex_csv(OPT_URL)
    df = clean_option_data(raw_opt)
    if df.empty:
        print("沒有抓到 TXO 台指選擇權資料。請檢查期交所資料是否已更新。")
        return None, None, ""
    taiex_price, taiex_source = get_taiex_reference_price()
    txf_price, txf_source = get_txf_reference_price()
    if taiex_price is not None:
        settle_basis, settle_source = taiex_price, taiex_source
    elif txf_price is not None:
        settle_basis, settle_source = txf_price, f"加權抓取失敗，暫用 {txf_source}"
    else:
        settle_basis, settle_source = float(df["strike"].median()), "加權/TXF皆抓取失敗，暫用履約價中位數"
    all_expiries = sorted(df["expiry"].dropna().unique(), key=sort_key_by_expiry)
    selected_expiries = filter_near_expiries(all_expiries)
    results = []
    for expiry in selected_expiries:
        g = df[df["expiry"] == expiry].copy()
        if not g.empty:
            results.append(analyze_bucket(expiry, g, settle_basis, settle_source, txf_price, txf_source))
    near_df = df[df["expiry"].isin(selected_expiries)].copy()
    if not near_df.empty:
        results.append(analyze_bucket("近端綜合", near_df, settle_basis, settle_source, txf_price, txf_source))
    report = format_report(results, settle_basis, settle_source, txf_price, txf_source, selected_expiries, all_expiries)
    print(report)
    summary = pd.DataFrame(results)
    summary.to_csv("txo_option_radar_summary_v13.csv", index=False, encoding="utf-8-sig")
    return summary, df, report

def get_gsheet_client():
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json: raise RuntimeError("找不到 GOOGLE_SERVICE_ACCOUNT_JSON，請先在 GitHub Secrets 設定。")
    info = json.loads(raw_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds), info.get("client_email", "")

def get_or_create_worksheet(sheet, title, rows=200, cols=20):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)

def write_report_to_sheet(report_text, summary_df):
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id: raise RuntimeError("找不到 GOOGLE_SHEET_ID，請先在 GitHub Secrets 設定。")
    gc, client_email = get_gsheet_client()
    sh = gc.open_by_key(sheet_id)
    ws_report = get_or_create_worksheet(sh, "今日報告", rows=300, cols=5)
    ws_report.clear()
    ws_report.update("A1", [[line] for line in report_text.splitlines()])
    ws_table = get_or_create_worksheet(sh, "近端結算表", rows=100, cols=20)
    ws_table.clear()
    if summary_df is not None and not summary_df.empty:
        values = [list(summary_df.columns)] + summary_df.astype(str).values.tolist()
        ws_table.update("A1", values)
    ws_hist = get_or_create_worksheet(sh, "歷史紀錄", rows=1000, cols=20)
    existing = ws_hist.get_all_values()
    if not existing:
        ws_hist.append_row(["寫入時間", "資料日", "結算方向基準", "TXF參考", "近端綜合結算靠近價", "上方大量區", "下方大量區", "OI PCR", "報告摘要"])
    near = None
    if summary_df is not None and not summary_df.empty:
        hit = summary_df[summary_df["合約"].astype(str).eq("近端綜合")]
        near = hit.iloc[0] if not hit.empty else summary_df.iloc[-1]
    if near is not None:
        summary_line = ""
        for line in report_text.splitlines():
            if line.startswith("短線以"):
                summary_line = line; break
        ws_hist.append_row([now_taipei().strftime("%Y-%m-%d %H:%M:%S"), now_taipei().strftime("%Y/%m/%d"), str(near.get("結算方向基準", "")), str(near.get("TXF參考", "")), str(near.get("結算靠近價", "")), str(near.get("上方大量區", "")), str(near.get("下方大量區", "")), str(near.get("OI PCR", "")), summary_line])
    print(f"已寫入 Google Sheet。Service account：{client_email}")

def main_sheet():
    summary, raw_data, report_text = main()
    write_report_to_sheet(report_text, summary)

if __name__ == "__main__":
    main_sheet()
