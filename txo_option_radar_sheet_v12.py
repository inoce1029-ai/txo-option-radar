# 台指選擇權籌碼雷達 v1.2
# 重點：
# 1) 排序改成 W1 → F1 → W2 → F2 → W3 → F3
# 2) 週三結算後跳過 W1
# 3) 週五結算後跳過 F1
#
# 手機 / Google Colab 可用
# 每天 17:30~18:00 後跑，抓期交所盤後公開資料。

import io
import math
import re
import json
from datetime import datetime
import pandas as pd
import requests

PRODUCT_OPT = "TXO"
PRODUCT_FUT = "TX"

NEAR_ZONE_WIDTH = 100
TOP_N_WALLS = 3
DISTANCE_WEIGHT = True

# 最多顯示幾個近端合約
SHOW_NEAR_W_COUNT = 2
SHOW_NEAR_F_COUNT = 2

# 台灣時間：選擇權日盤結算通常以 13:45 作為已結算判斷
SETTLE_HOUR = 13
SETTLE_MINUTE = 45

OPT_URL = "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=DailyMarketReportOpt"
FUT_URL = "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=DailyMarketReportFut"


def fetch_taifex_csv(url):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv, text/plain, */*"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    raw = r.content
    for enc in ["utf-8-sig", "big5", "cp950"]:
        try:
            text = raw.decode(enc)
            return pd.read_csv(io.StringIO(text))
        except Exception:
            pass

    text = raw.decode("utf-8", errors="ignore")
    return pd.read_csv(io.StringIO(text))


def normalize_columns(df):
    df = df.copy()
    df.columns = [
        str(c).replace("\ufeff", "").replace("\n", "").replace(" ", "").strip()
        for c in df.columns
    ]
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
    return pd.to_numeric(
        s.astype(str)
         .str.replace(",", "", regex=False)
         .str.replace("-", "", regex=False)
         .str.strip(),
        errors="coerce"
    )


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
        if col_settle:
            break

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

    out["cp"] = out["cp"].replace({
        "買權": "C", "賣權": "P",
        "Call": "C", "Put": "P",
        "CALL": "C", "PUT": "P"
    })
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
        # 盤後籌碼雷達優先使用 TXF 結算價，比最後成交價穩
        for keys in [["結算價"], ["收盤價"], ["最後成交價"], ["開盤價"]]:
            price_col = find_col(df, keys, required=False)
            if price_col:
                break

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
            fut["_expiry_sort"] = fut[col_expiry].astype(str).str.extract(r"(\d+)")[0]
            fut["_expiry_sort"] = pd.to_numeric(fut["_expiry_sort"], errors="coerce")
            fut = fut.sort_values(["_expiry_sort"])

        row = fut.iloc[0]
        price = float(row["price"])
        expiry = str(row[col_expiry]) if col_expiry else "近月"
        return price, f"自動抓 TXF 近月 {expiry}：{price_col}"

    except Exception as e:
        return None, f"TXF自動抓取失敗：{type(e).__name__} {e}"


def ask_manual_spot_if_needed(auto_price, auto_source):
    if auto_price is not None:
        return auto_price, auto_source

    print(auto_source)
    try:
        txt = input("請手動輸入目前台指期 / 加權位置，例如 40580；若空白則用舊估算：").strip()
        if txt:
            return float(txt.replace(",", "")), "手動輸入"
    except Exception:
        pass

    return None, "舊估算"


def estimate_spot_fallback(df):
    active = df[(df["settle"] > 0) & (df["oi"] > 0)].copy()
    if active.empty:
        active = df[df["oi"] > 0].copy()
    if active.empty:
        return float("nan")
    return float(active["strike"].median())


def parse_week_tag(expiry):
    e = str(expiry).upper().strip()
    m_w = re.search(r"W(\d+)", e)
    if m_w:
        return "W", int(m_w.group(1))
    m_f = re.search(r"F(\d+)", e)
    if m_f:
        return "F", int(m_f.group(1))
    return "M", 0


def contract_label(expiry):
    e = str(expiry).upper().strip()
    tag, num = parse_week_tag(e)

    if tag == "W":
        return f"週三選 W{num}"
    if tag == "F":
        return f"週五選 F{num}"
    return f"月選 {e}"


def sort_key_by_expiry(expiry):
    """
    排序改成：
    W1 → F1 → W2 → F2 → W3 → F3 → 月選
    """
    e = str(expiry).upper().strip()
    tag, num = parse_week_tag(e)

    if tag == "W":
        return (num, 1, e)
    if tag == "F":
        return (num, 2, e)
    return (99, 9, e)


def is_after_settlement_now():
    now = datetime.now()
    return (now.hour, now.minute) >= (SETTLE_HOUR, SETTLE_MINUTE)


def filter_near_expiries(expiry_list):
    """
    只保留近端 W/F，並自動排除已結算的最近合約。
    規則：
    - 排序：W1 → F1 → W2 → F2 → W3 → F3
    - 週三 13:45 後：W1 視為已結算，跳過
    - 週五 13:45 後：F1 視為已結算，跳過
    - 跳過後自動遞補下一個 W / F
    - 顯示最近 SHOW_NEAR_W_COUNT 個 W、SHOW_NEAR_F_COUNT 個 F
    - 月選永遠保留
    """
    now = datetime.now()
    weekday = now.weekday()  # Mon=0, Wed=2, Fri=4
    after_settle = is_after_settlement_now()

    w_list, f_list, m_list = [], [], []

    for exp in sorted(expiry_list, key=sort_key_by_expiry):
        tag, num = parse_week_tag(exp)

        if tag == "W":
            # 週三結算後，W1 不再顯示
            if weekday == 2 and after_settle and num == 1:
                continue
            w_list.append(exp)

        elif tag == "F":
            # 週五結算後，F1 不再顯示
            if weekday == 4 and after_settle and num == 1:
                continue
            f_list.append(exp)

        else:
            m_list.append(exp)

    selected = []
    selected.extend(w_list[:SHOW_NEAR_W_COUNT])
    selected.extend(f_list[:SHOW_NEAR_F_COUNT])
    selected.extend(m_list)

    return sorted(selected, key=sort_key_by_expiry)


def calc_max_pain(group):
    if group.empty:
        return None

    strikes = sorted(group["strike"].dropna().unique())
    calls = group[group["cp"] == "C"].groupby("strike")["oi"].sum()
    puts = group[group["cp"] == "P"].groupby("strike")["oi"].sum()

    best_strike = None
    best_loss = None

    for s in strikes:
        call_loss = sum(max(0, s - k) * oi for k, oi in calls.items())
        put_loss = sum(max(0, k - s) * oi for k, oi in puts.items())
        loss = call_loss + put_loss

        if best_loss is None or loss < best_loss:
            best_loss = loss
            best_strike = s

    return int(best_strike) if best_strike is not None else None


def wall_table(group, spot, cp, top_n=3):
    g = group[group["cp"] == cp].groupby("strike", as_index=False)["oi"].sum()
    if g.empty:
        return g

    if math.isnan(spot):
        g["score"] = g["oi"]
    else:
        distance = (g["strike"] - spot).abs().clip(lower=1)
        g["score"] = g["oi"] / (1 + distance / 500) if DISTANCE_WEIGHT else g["oi"]

    if cp == "C" and not math.isnan(spot):
        g = g[g["strike"] >= spot]
    if cp == "P" and not math.isnan(spot):
        g = g[g["strike"] <= spot]

    return g.sort_values(["score", "oi"], ascending=False).head(top_n)


def zone_from_walls(walls):
    if walls.empty:
        return None
    lo = int(walls["strike"].min())
    hi = int(walls["strike"].max())
    if lo == hi:
        return f"{lo}"
    return f"{lo}～{hi}"


def analyze_bucket(label, raw_expiry, group, global_spot, spot_source):
    spot = global_spot
    max_pain = calc_max_pain(group)

    calls = wall_table(group, spot, "C", TOP_N_WALLS)
    puts = wall_table(group, spot, "P", TOP_N_WALLS)

    near_zone = None
    if max_pain:
        near_zone = f"{max_pain - NEAR_ZONE_WIDTH}～{max_pain + NEAR_ZONE_WIDTH}"

    call_oi = int(group[group["cp"] == "C"]["oi"].sum())
    put_oi = int(group[group["cp"] == "P"]["oi"].sum())
    pcr_oi = round(put_oi / call_oi, 2) if call_oi else None

    return {
        "類別": label,
        "原始週別": raw_expiry,
        "現價來源": spot_source,
        "現價參考": None if math.isnan(spot) else round(spot),
        "最大痛點": max_pain,
        "結算可能靠近區": near_zone,
        "上方壓回區": zone_from_walls(calls),
        "下方撐住區": zone_from_walls(puts),
        "Call OI": call_oi,
        "Put OI": put_oi,
        "OI PCR": pcr_oi,
    }


def parse_zone(zone):
    if not zone:
        return None, None
    s = str(zone)
    if "～" in s:
        a, b = s.split("～", 1)
        try:
            return int(float(a)), int(float(b))
        except Exception:
            return None, None
    try:
        v = int(float(s))
        return v, v
    except Exception:
        return None, None


def pick_primary_contract(results):
    """優先拿 F1，其次第一個非月選，作為短線一句話參考。"""
    for r in results:
        if "週五選 F1" in r["類別"]:
            return r
    for r in results:
        if r["類別"] not in ["近端綜合"] and not r["類別"].startswith("月選"):
            return r
    return results[0] if results else None


def zone_direction(spot, max_pain):
    if max_pain is None or spot is None or math.isnan(spot):
        return "中性"
    diff = max_pain - spot
    if abs(diff) <= 100:
        return "目前接近結算靠近價"
    if diff > 0:
        return "偏上靠"
    return "偏下靠"


def make_one_sentence(results, spot):
    main = pick_primary_contract(results)
    near = next((r for r in results if r["類別"] == "近端綜合"), None)
    if not main:
        return "資料不足，暫時不做方向判斷。"

    ref = near if near else main
    pressure = ref.get("上方壓回區")
    support = ref.get("下方撐住區")
    mp = main.get("最大痛點")
    direction = zone_direction(spot, mp)

    return (
        f"短線以{main['類別']}為主，結算靠近價約 {mp}，目前判斷：{direction}。"
        f" 上方 {pressure} 是主要壓回區，下方 {support} 是主要撐住區。"
    )


def format_table_line(cols, widths):
    out = []
    for c, w in zip(cols, widths):
        txt = str(c) if c is not None else "-"
        out.append(txt.ljust(w))
    return "  ".join(out)


def short_contract_name(label):
    s = str(label)
    s = s.replace("週三選 ", "")
    s = s.replace("週五選 ", "")
    if s.startswith("月選"):
        return "月選"
    return s


def get_main_zones(results):
    near = next((r for r in results if r["類別"] == "近端綜合"), None)
    if near:
        main_pressure = near.get("上方壓回區")
        main_support = near.get("下方撐住區")
    else:
        main_pressure = results[0].get("上方壓回區") if results else None
        main_support = results[0].get("下方撐住區") if results else None

    # 強壓/強撐：用月選
    monthly = next((r for r in results if str(r["類別"]).startswith("月選")), None)
    strong_pressure = monthly.get("上方壓回區") if monthly else main_pressure
    strong_support = monthly.get("下方撐住區") if monthly else main_support

    return main_pressure, strong_pressure, main_support, strong_support


def break_prices(results):
    main = pick_primary_contract(results)
    if not main:
        return None, None, None

    p_lo, p_hi = parse_zone(main.get("上方壓回區"))
    s_lo, s_hi = parse_zone(main.get("下方撐住區"))

    up_break = p_hi
    down_break = s_lo
    return main, up_break, down_break


def format_report(results, spot, spot_source, selected_expiries, all_expiries):
    now = datetime.now()
    main_pressure, strong_pressure, main_support, strong_support = get_main_zones(results)
    main_contract, up_break, down_break = break_prices(results)

    lines = []
    lines.append("台指選擇權籌碼雷達 v1.2")
    lines.append(f"資料日：{now.strftime('%Y/%m/%d')}")
    lines.append(f"現價基準：{round(spot) if not math.isnan(spot) else None}（{spot_source}）")
    lines.append("")

    lines.append("【今日一句話】")
    lines.append(make_one_sentence(results, spot))
    lines.append("")

    lines.append("【近端結算重點】")
    widths = [12, 10, 14, 14, 8]
    lines.append(format_table_line(["合約", "結算靠近價", "上方壓回區", "下方撐住區", "OI PCR"], widths))
    for r in results:
        if r["類別"] == "近端綜合":
            continue
        lines.append(format_table_line([
            short_contract_name(r["類別"]),
            r["最大痛點"],
            r["上方壓回區"],
            r["下方撐住區"],
            r["OI PCR"],
        ], widths))
    lines.append("")

    lines.append("【主力區】")
    lines.append(f"主壓力：{main_pressure}")
    lines.append(f"強壓力：{strong_pressure}")
    lines.append(f"主支撐：{main_support}")
    lines.append(f"強支撐：{strong_support}")
    lines.append("")

    lines.append("【破防價】")
    if main_contract and up_break:
        lines.append(f"站上 {up_break}：{main_contract['類別']} 上方壓回區可能失效，容易往下一層壓力推進。")
    else:
        lines.append("站上破防價：資料不足")
    if main_contract and down_break:
        lines.append(f"跌破 {down_break}：{main_contract['類別']} 下方撐住區可能失效，容易往下一層支撐測試。")
    else:
        lines.append("跌破破防價：資料不足")
    lines.append("")

    lines.append("【結算靠近方向】")
    for r in results:
        if r["類別"] == "近端綜合":
            continue
        mp = r["最大痛點"]
        direction = zone_direction(spot, mp)
        lines.append(f"{r['類別']}：靠 {mp} 附近，{direction}")
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
        return None, None

    auto_price, auto_source = get_txf_reference_price()
    spot, spot_source = ask_manual_spot_if_needed(auto_price, auto_source)

    if spot is None:
        spot = estimate_spot_fallback(df)

    all_expiries = sorted(df["expiry"].dropna().unique(), key=sort_key_by_expiry)
    selected_expiries = filter_near_expiries(all_expiries)

    results = []
    for expiry in selected_expiries:
        g = df[df["expiry"] == expiry].copy()
        if not g.empty:
            results.append(analyze_bucket(contract_label(expiry), expiry, g, spot, spot_source))

    # 綜合只用目前顯示的近端合約，不再用全部遠月，避免被遠月扭曲
    near_df = df[df["expiry"].isin(selected_expiries)].copy()
    if not near_df.empty:
        results.append(analyze_bucket("近端綜合", "目前顯示合約加總", near_df, spot, spot_source))

    print(format_report(results, spot, spot_source, selected_expiries, all_expiries))

    summary = pd.DataFrame(results)
    summary.to_csv("txo_option_radar_summary_v12.csv", index=False, encoding="utf-8-sig")
    print("已輸出：txo_option_radar_summary_v12.csv")

    return summary, df





# =========================
# Google Sheet 自動寫入
# =========================

import os
import gspread
from google.oauth2.service_account import Credentials
from contextlib import redirect_stdout
from io import StringIO


def get_gsheet_client():
    """
    從 GitHub Secrets 讀取 GOOGLE_SERVICE_ACCOUNT_JSON。
    """
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("找不到 GOOGLE_SERVICE_ACCOUNT_JSON，請先在 GitHub Secrets 設定。")

    info = json.loads(raw_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds), info.get("client_email", "")


def get_or_create_worksheet(sheet, title, rows=200, cols=20):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)


def write_report_to_sheet(report_text, summary_df):
    """
    寫入 Google Sheet：
    - 今日報告：手機閱讀文字版
    - 歷史紀錄：每天追加一列摘要
    - 近端結算表：當日表格
    """
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("找不到 GOOGLE_SHEET_ID，請先在 GitHub Secrets 設定。")

    gc, client_email = get_gsheet_client()
    sh = gc.open_by_key(sheet_id)

    # 1) 今日報告
    ws_report = get_or_create_worksheet(sh, "今日報告", rows=300, cols=5)
    ws_report.clear()
    lines = report_text.splitlines()
    ws_report.update("A1", [[line] for line in lines])

    # 2) 近端結算表
    ws_table = get_or_create_worksheet(sh, "近端結算表", rows=100, cols=20)
    ws_table.clear()
    if summary_df is not None and not summary_df.empty:
        values = [list(summary_df.columns)] + summary_df.astype(str).values.tolist()
        ws_table.update("A1", values)

    # 3) 歷史紀錄：追加一筆近端綜合摘要
    ws_hist = get_or_create_worksheet(sh, "歷史紀錄", rows=1000, cols=20)
    existing = ws_hist.get_all_values()
    if not existing:
        ws_hist.append_row([
            "寫入時間", "資料日", "現價參考", "近端綜合結算靠近價",
            "主壓力", "主支撐", "OI PCR", "報告摘要"
        ])

    now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y/%m/%d")

    near = None
    if summary_df is not None and not summary_df.empty:
        hit = summary_df[summary_df["類別"].astype(str).eq("近端綜合")]
        if not hit.empty:
            near = hit.iloc[0]
        else:
            near = summary_df.iloc[-1]

    if near is not None:
        ws_hist.append_row([
            now_txt,
            today,
            str(near.get("現價參考", "")),
            str(near.get("最大痛點", "")),
            str(near.get("上方壓回區", "")),
            str(near.get("下方撐住區", "")),
            str(near.get("OI PCR", "")),
            report_text.splitlines()[4] if len(report_text.splitlines()) > 4 else ""
        ])

    print(f"已寫入 Google Sheet。Service account：{client_email}")


def main_sheet():
    """
    GitHub Actions 使用這個入口。
    """
    buf = StringIO()
    with redirect_stdout(buf):
        summary, raw_data = main()

    report_text = buf.getvalue()
    print(report_text)
    write_report_to_sheet(report_text, summary)


if __name__ == "__main__":
    main_sheet()
