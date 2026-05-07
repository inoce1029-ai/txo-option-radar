# 市場＋權證資金流雷達 v0.6
# 功能：
# - 抓上市權證每日收盤行情（TWSE 官方資料）
# - 盡量抓上櫃權證每日收盤行情（TPEx 官方資料，若抓不到會保留上市資料）
# - 輸出成交量 Top 10、成交金額 Top 10、標的集中
# - 寫入 Google Sheet
# - 推播 Telegram

import io
import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from urllib.parse import quote
import gspread
from google.oauth2.service_account import Credentials


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TOP_N = 10


def now_taipei():
    return datetime.now(TAIPEI_TZ)


def parse_number(x):
    try:
        s = str(x).replace(",", "").replace("--", "").replace("X", "").strip()
        if s in ["", "-", "nan", "None"]:
            return None
        return float(s)
    except Exception:
        return None


def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).replace("\ufeff", "").replace("\n", "").replace(" ", "").strip() for c in df.columns]
    return df


def find_field(fields, candidates):
    for cand in candidates:
        for f in fields:
            if cand in str(f):
                return f
    return None


def infer_cp_type(name):
    s = str(name)
    if "售" in s or "熊" in s:
        return "認售"
    if "購" in s or "牛" in s:
        return "認購"
    return "-"


def is_warrant_name(name):
    s = str(name)
    return any(k in s for k in ["購", "售", "牛", "熊"])


def clean_for_sheet(df):
    if df is None or df.empty:
        return df
    x = df.copy()
    x = x.replace([float("inf"), float("-inf")], "")
    x = x.fillna("")
    return x


def safe_update(ws, start_cell, values):
    ws.update(values=values, range_name=start_cell)


def fmt_short_money(x):
    try:
        v = float(x)
        if abs(v) >= 100000000:
            return f"{v/100000000:.1f}億"
        if abs(v) >= 10000:
            return f"{v/10000:.0f}萬"
        return f"{int(v):,}"
    except Exception:
        return str(x)


def compact_rank_lines(df, cols, top_n=5):
    if df is None or df.empty:
        return ["資料不足"]
    lines = []
    for i, (_, r) in enumerate(df.head(top_n).iterrows(), start=1):
        parts = []
        for c in cols:
            v = r.get(c, "")
            if c in ["成交金額", "估算成交金額"]:
                v = fmt_short_money(v)
            elif c in ["成交量", "盤中成交量"]:
                try:
                    v = f"{int(float(v)):,}"
                except Exception:
                    v = str(v)
            elif c == "漲跌幅%":
                v = "-" if v in [None, ""] else f"{v}%"
            parts.append(str(v))
        lines.append(f"{i}. " + "｜".join(parts))
    return lines


def fetch_json(url):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def find_tables(obj):
    tables = []
    if isinstance(obj, dict):
        if isinstance(obj.get("fields"), list) and isinstance(obj.get("data"), list):
            tables.append(obj)
        for v in obj.values():
            tables.extend(find_tables(v))
    elif isinstance(obj, list):
        for v in obj:
            tables.extend(find_tables(v))
    return tables


def fetch_twse_warrant_profile():
    """
    嘗試抓 TWSE 權證每日成交資料/基本資料 OpenAPI，用於補標的與認購認售。
    若欄位格式不同，抓不到也不影響主報表。
    """
    urls = [
        "https://openapi.twse.com.tw/v1/opendata/t187ap47_L",
    ]
    mapping = {}

    for url in urls:
        try:
            data = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).json()
            if not isinstance(data, list):
                continue

            for row in data:
                if not isinstance(row, dict):
                    continue

                code = None
                for k in row.keys():
                    if "權證代號" in k or "證券代號" in k or "代號" == k:
                        code = str(row.get(k, "")).strip()
                        break

                if not code:
                    continue

                name = ""
                underlying = ""
                cp_type = ""

                for k, v in row.items():
                    ks = str(k)
                    if not name and ("權證簡稱" in ks or "證券名稱" in ks or "權證名稱" in ks):
                        name = str(v).strip()
                    if not underlying and ("標的證券" in ks or "標的名稱" in ks or "標的代號" in ks or "標的" == ks):
                        underlying = str(v).strip()
                    if not cp_type and ("認購" in ks or "認售" in ks or "權證種類" in ks or "類型" in ks):
                        cp_type = str(v).strip()

                mapping[code] = {
                    "名稱補充": name,
                    "標的": underlying if underlying else "-",
                    "認購認售": cp_type if cp_type else infer_cp_type(name),
                }
        except Exception:
            continue

    return mapping


def fetch_twse_warrant_daily():
    """
    用 TWSE 每日收盤行情抓上市權證。
    歷史查最近 10 天，找到最新有資料的交易日。
    """
    today = now_taipei().date()
    profile = fetch_twse_warrant_profile()

    for i in range(10):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ds}&type=ALLBUT0999&response=json"

        try:
            obj = fetch_json(url)
            tables = find_tables(obj)
            rows_out = []

            for t in tables:
                title = str(t.get("title", ""))
                fields = [str(x).strip() for x in t.get("fields", [])]
                data = t.get("data", [])

                code_f = find_field(fields, ["證券代號"])
                name_f = find_field(fields, ["證券名稱"])
                vol_f = find_field(fields, ["成交股數", "成交量"])
                amt_f = find_field(fields, ["成交金額"])
                close_f = find_field(fields, ["收盤價"])
                chg_f = find_field(fields, ["漲跌價差", "漲跌"])

                if not code_f or not name_f or not vol_f or not amt_f:
                    continue

                idx = {f: fields.index(f) for f in fields}

                for row in data:
                    if not isinstance(row, list) or len(row) < len(fields):
                        continue

                    code = str(row[idx[code_f]]).strip()
                    name = str(row[idx[name_f]]).strip()

                    volume_units = parse_number(row[idx[vol_f]])
                    amount = parse_number(row[idx[amt_f]])
                    close = parse_number(row[idx[close_f]]) if close_f else None
                    change = parse_number(row[idx[chg_f]]) if chg_f else None

                    if volume_units is None or amount is None:
                        continue

                    prof = profile.get(code, {})
                    underlying = prof.get("標的", "-")
                    cp_type = prof.get("認購認售", infer_cp_type(name))
                    if cp_type not in ["認購", "認售"]:
                        cp_type = infer_cp_type(name)

                    rows_out.append({
                        "市場": "上市",
                        "資料日": ds,
                        "代碼": code,
                        "名稱": name,
                        "標的": underlying,
                        "認購認售": cp_type,
                        "成交量": int(volume_units),
                        "成交量張": round(volume_units / 1000, 2),
                        "成交金額": int(amount),
                        "收盤價": close,
                        "漲跌": change,
                    })

            if rows_out:
                return pd.DataFrame(rows_out)

        except Exception:
            continue

    return pd.DataFrame()


def fetch_tpex_warrant_daily():
    """
    嘗試抓 TPEx 上櫃權證。
    TPEx 新舊 API 路徑偶爾會調整，所以這裡用多組 URL 嘗試。
    抓不到時回傳空表，不影響上市權證報告。
    """
    today = now_taipei().date()

    # type=EW / 02 / warrant 等只是多路徑嘗試，抓到才使用
    url_templates = [
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyCloseQuotes?date={roc}&type=EW&response=json",
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyCloseQuotes?date={roc}&type=02&response=json",
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={roc}&type=EW&response=json",
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={roc}&type=02&response=json",
    ]

    for i in range(10):
        d = today - timedelta(days=i)
        roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
        ds = d.strftime("%Y%m%d")

        for tmpl in url_templates:
            url = tmpl.format(roc=roc)
            try:
                obj = fetch_json(url)
                tables = find_tables(obj)
                rows_out = []

                for t in tables:
                    title = str(t.get("title", ""))
                    fields = [str(x).strip() for x in t.get("fields", [])]
                    data = t.get("data", [])

                    code_f = find_field(fields, ["代號", "證券代號"])
                    name_f = find_field(fields, ["名稱", "證券名稱"])
                    vol_f = find_field(fields, ["成交股數", "成交量"])
                    amt_f = find_field(fields, ["成交金額"])
                    close_f = find_field(fields, ["收盤", "收盤價"])
                    chg_f = find_field(fields, ["漲跌"])

                    if not code_f or not name_f or not vol_f or not amt_f:
                        continue

                    idx = {f: fields.index(f) for f in fields}

                    for row in data:
                        if not isinstance(row, list) or len(row) < len(fields):
                            continue

                        code = str(row[idx[code_f]]).strip()
                        name = str(row[idx[name_f]]).strip()

                        volume_units = parse_number(row[idx[vol_f]])
                        amount = parse_number(row[idx[amt_f]])
                        close = parse_number(row[idx[close_f]]) if close_f else None
                        change = parse_number(row[idx[chg_f]]) if chg_f else None

                        if volume_units is None or amount is None:
                            continue

                        rows_out.append({
                            "市場": "上櫃",
                            "資料日": ds,
                            "代碼": code,
                            "名稱": name,
                            "標的": "-",
                            "認購認售": infer_cp_type(name),
                            "成交量": int(volume_units),
                            "成交量張": round(volume_units / 1000, 2),
                            "成交金額": int(amount),
                            "收盤價": close,
                            "漲跌": change,
                        })

                if rows_out:
                    return pd.DataFrame(rows_out)

            except Exception:
                continue

    return pd.DataFrame()


def load_warrant_data():
    twse = fetch_twse_warrant_daily()
    tpex = fetch_tpex_warrant_daily()

    frames = []
    if not twse.empty:
        frames.append(twse)
    if not tpex.empty:
        frames.append(tpex)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["市場", "代碼"])
    return df


def top_underlyings(df, metric, top_n=5):
    if df.empty or "標的" not in df.columns:
        return "-"
    x = df.copy()
    x["標的"] = x["標的"].replace("", "-").fillna("-")
    x = x[x["標的"] != "-"]
    if x.empty:
        return "資料不足"
    g = x.groupby("標的", as_index=False)[metric].sum().sort_values(metric, ascending=False).head(top_n)
    return "、".join(g["標的"].astype(str).tolist())


def summarize(df, vol_top, amt_top):
    if df.empty:
        return "今日未抓到權證資料。"

    total_count = len(df)
    call_count = int((df["認購認售"] == "認購").sum())
    put_count = int((df["認購認售"] == "認售").sum())
    call_ratio = round(call_count / total_count * 100, 1) if total_count else 0
    put_ratio = round(put_count / total_count * 100, 1) if total_count else 0

    vol_focus = top_underlyings(vol_top, "成交量")
    amt_focus = top_underlyings(amt_top, "成交金額")

    tone = "偏認購" if call_count > put_count else "偏認售" if put_count > call_count else "認購認售均衡"

    return (
        f"今日權證成交熱度{tone}。"
        f"認購占比約 {call_ratio}%，認售占比約 {put_ratio}%。"
        f"成交量集中標的：{vol_focus}。"
        f"成交金額集中標的：{amt_focus}。"
    )


def fmt_money(x):
    try:
        return f"{int(float(x)):,}"
    except Exception:
        return str(x)


def fmt_table(rows, cols):
    lines = []
    lines.append(" / ".join(cols))
    for _, r in rows.iterrows():
        vals = []
        for c in cols:
            v = r.get(c, "")
            if c in ["成交金額", "成交量"]:
                v = fmt_money(v)
            vals.append(str(v))
        lines.append(" / ".join(vals))
    return "\n".join(lines)



def is_tw_market_time():
    now = now_taipei()
    if now.weekday() >= 5:
        return False
    return time(9, 0) <= now.time() <= time(14, 0)


def make_ex_ch(row):
    market = str(row.get("市場", ""))
    code = str(row.get("代碼", "")).strip()
    if not code:
        return None
    prefix = "otc" if market == "上櫃" else "tse"
    return f"{prefix}_{code}.tw"


def fetch_mis_quotes(watch_df, max_symbols=80):
    """
    盤中近即時報價。
    使用 TWSE MIS；只抓 watchlist 前 max_symbols 檔，避免全市場掃描過大。
    """
    if watch_df is None or watch_df.empty:
        return pd.DataFrame()

    w = watch_df.copy()
    w["ex_ch"] = w.apply(make_ex_ch, axis=1)
    w = w.dropna(subset=["ex_ch"]).head(max_symbols)
    if w.empty:
        return pd.DataFrame()

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
        "Accept": "application/json,text/plain,*/*",
    }

    sess = requests.Session()
    try:
        sess.get("https://mis.twse.com.tw/stock/fibest.jsp", headers=headers, timeout=10)
    except Exception:
        pass

    rows = []
    channels = w["ex_ch"].tolist()
    for i in range(0, len(channels), 30):
        chunk = channels[i:i + 30]
        ex_ch = "|".join(chunk)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={quote(ex_ch)}&json=1&delay=0&_={int(now_taipei().timestamp()*1000)}"
        try:
            obj = sess.get(url, headers=headers, timeout=20).json()
            arr = obj.get("msgArray", [])
            for item in arr:
                code = str(item.get("c", "")).strip()
                name = str(item.get("n", "")).strip()
                price = parse_number(item.get("z"))
                if price is None:
                    price = parse_number(item.get("y"))
                y = parse_number(item.get("y"))
                vol = parse_number(item.get("v"))
                tv = parse_number(item.get("tv"))

                if vol is None:
                    vol = 0

                change_pct = None
                if price is not None and y not in [None, 0]:
                    change_pct = round((price - y) / y * 100, 2)

                rows.append({
                    "代碼": code,
                    "名稱": name,
                    "盤中價": price,
                    "昨收": y,
                    "漲跌幅%": change_pct,
                    "盤中成交量": int(vol) if vol is not None else 0,
                    "單筆量": int(tv) if tv is not None else 0,
                    "估算成交金額": int(price * vol) if price is not None and vol is not None else 0,
                })
        except Exception:
            continue

    q = pd.DataFrame(rows)
    if q.empty:
        return q

    meta = w[["代碼", "標的", "認購認售", "成交量", "成交金額", "收盤價"]].copy()
    q = q.merge(meta, on="代碼", how="left", suffixes=("", "_昨"))
    q = q.drop_duplicates(subset=["代碼"])
    return q


def build_intraday_section(df):
    now = now_taipei()
    lines = []
    lines.append("【權證盤中熱度】")
    lines.append(f"更新時間：{now.strftime('%Y/%m/%d %H:%M')}")

    if not is_tw_market_time():
        lines.append("目前非台股日盤盤中時段；盤中監控只在 09:00～14:00 每 15 分鐘更新。")
        return "\n".join(lines), pd.DataFrame()

    if df is None or df.empty:
        lines.append("盤中監控資料不足：尚無盤後 watchlist。")
        return "\n".join(lines), pd.DataFrame()

    warrant_df = df[df["名稱"].apply(is_warrant_name)].copy()
    if warrant_df.empty:
        lines.append("盤中監控資料不足：盤後資料中未找到權證 watchlist。")
        return "\n".join(lines), pd.DataFrame()

    # 追蹤昨日權證成交金額/成交量前段
    watch = warrant_df.sort_values(["成交金額", "成交量"], ascending=False).head(80)
    q = fetch_mis_quotes(watch, max_symbols=80)

    if q.empty:
        lines.append("盤中即時報價暫時抓不到；下方仍保留每日盤後資料更新。")
        return "\n".join(lines), q

    vol_top = q.sort_values("盤中成交量", ascending=False).head(10)
    amt_top = q.sort_values("估算成交金額", ascending=False).head(10)

    call_count = int((q["認購認售"] == "認購").sum()) if "認購認售" in q.columns else 0
    put_count = int((q["認購認售"] == "認售").sum()) if "認購認售" in q.columns else 0
    tone_call = "偏強" if call_count > put_count else "偏弱" if call_count < put_count else "中性"
    tone_put = "偏強" if put_count > call_count else "偏弱" if put_count < call_count else "中性"

    lines.append(f"盤中成功追蹤：{len(q)} 檔")
    lines.append(f"認購熱度：{tone_call}")
    lines.append(f"認售熱度：{tone_put}")
    lines.append("")
    lines.append("成交量急增 Top 5")
    lines.extend(compact_rank_lines(vol_top, ["代碼", "名稱", "認購認售", "盤中價", "漲跌幅%", "盤中成交量"], 5))
    lines.append("")
    lines.append("成交金額急增 Top 5")
    lines.extend(compact_rank_lines(amt_top, ["代碼", "名稱", "認購認售", "盤中價", "估算成交金額", "盤中成交量"], 5))
    lines.append("")
    lines.append("【異常權證】")
    for _, r in vol_top.head(3).iterrows():
        cp = str(r.get("認購認售", ""))
        tag = "認購熱度升溫" if cp == "認購" else "認售避險升溫" if cp == "認售" else "成交量放大"
        lines.append(f"{r.get('名稱')}：盤中成交量 {int(r.get('盤中成交量', 0)):,}，{tag}")

    return "\n".join(lines), q


def build_report(df):
    now = now_taipei()
    intraday_text, intraday_df = build_intraday_section(df)

    if df.empty:
        report = "\n".join([
            "市場＋權證資金流雷達 v0.6",
            f"資料日：{now.strftime('%Y/%m/%d')}",
            f"最後更新時間：{now.strftime('%Y/%m/%d %H:%M')}",
            "",
            intraday_text,
            "",
            "【每日盤後權證熱度】",
            f"更新時間：{now.strftime('%Y/%m/%d %H:%M')}",
            "今日未抓到市場資料，請檢查 TWSE / TPEx 資料來源。",
        ])
        return report, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), intraday_df

    data_day = str(df["資料日"].iloc[0])

    market_vol_top = df.sort_values("成交量", ascending=False).head(TOP_N)
    market_amt_top = df.sort_values("成交金額", ascending=False).head(TOP_N)

    warrant_df = df[df["名稱"].apply(is_warrant_name)].copy()
    warrant_vol_top = warrant_df.sort_values("成交量", ascending=False).head(TOP_N) if not warrant_df.empty else pd.DataFrame()
    warrant_amt_top = warrant_df.sort_values("成交金額", ascending=False).head(TOP_N) if not warrant_df.empty else pd.DataFrame()

    focus_rows = []
    if not warrant_df.empty:
        x = warrant_df.copy()
        x["標的顯示"] = x["標的"].replace("", "-").fillna("-")
        x.loc[x["標的顯示"].eq("-"), "標的顯示"] = x["名稱"].astype(str).str.extract(r"^([^購售牛熊]+)")[0].fillna(x["名稱"])
        for metric in ["成交量", "成交金額"]:
            g = x.groupby(["標的顯示", "認購認售"], as_index=False)[metric].sum()
            g = g.sort_values(metric, ascending=False).head(10)
            g["排行類型"] = metric
            focus_rows.append(g)
    focus = pd.concat(focus_rows, ignore_index=True) if focus_rows else pd.DataFrame()

    market_vol_focus = top_underlyings(market_vol_top, "成交量")
    market_amt_focus = top_underlyings(market_amt_top, "成交金額")
    warrant_count = len(warrant_df)

    report_lines = []
    report_lines.append("市場＋權證資金流雷達 v0.6")
    report_lines.append(f"資料日：{data_day}")
    report_lines.append(f"最後更新時間：{now.strftime('%Y/%m/%d %H:%M')}")
    report_lines.append("")
    report_lines.append(intraday_text)
    report_lines.append("")
    report_lines.append("【每日盤後權證熱度】")
    report_lines.append(f"更新時間：{now.strftime('%Y/%m/%d %H:%M')}")
    report_lines.append("")
    report_lines.append("【盤後權證總結】")
    report_lines.append(f"今日權證約 {warrant_count} 檔。")
    if focus.empty:
        report_lines.append("權證標的集中：資料不足")
    else:
        vol_focus = "、".join(focus[focus["排行類型"].eq("成交量")]["標的顯示"].astype(str).head(5).tolist())
        amt_focus = "、".join(focus[focus["排行類型"].eq("成交金額")]["標的顯示"].astype(str).head(5).tolist())
        report_lines.append(f"權證成交量集中：{vol_focus}")
        report_lines.append(f"權證成交金額集中：{amt_focus}")
    report_lines.append("")
    report_lines.append("")
    report_lines.append("權證成交量 Top 10")
    if warrant_vol_top.empty:
        report_lines.append("資料不足")
    else:
        report_lines.extend(compact_rank_lines(warrant_vol_top, ["代碼", "名稱", "認購認售", "成交量", "收盤價", "漲跌"], 10))
    report_lines.append("")
    report_lines.append("權證成交金額 Top 10")
    if warrant_amt_top.empty:
        report_lines.append("資料不足")
    else:
        report_lines.extend(compact_rank_lines(warrant_amt_top, ["代碼", "名稱", "認購認售", "成交金額", "成交量", "收盤價"], 10))
    report_lines.append("")
    report_lines.append("【每日盤後全市場熱點｜參考用】")
    report_lines.append(f"更新時間：{now.strftime('%Y/%m/%d %H:%M')}")
    report_lines.append(f"今日共抓到 {len(df)} 檔市場資料。")
    report_lines.append(f"全市場成交量集中：{market_vol_focus}")
    report_lines.append(f"全市場成交金額集中：{market_amt_focus}")
    report_lines.append("")
    report_lines.append("市場成交量 Top 10")
    report_lines.extend(compact_rank_lines(market_vol_top, ["代碼", "名稱", "成交量", "收盤價", "漲跌"], 10))
    report_lines.append("")
    report_lines.append("市場成交金額 Top 10")
    report_lines.extend(compact_rank_lines(market_amt_top, ["代碼", "名稱", "成交金額", "成交量", "收盤價"], 10))
    report_lines.append("")
    report_lines.append(f"資料來源：上市 {int((df['市場'] == '上市').sum())} 檔；上櫃 {int((df['市場'] == '上櫃').sum())} 檔")
    report_lines.append("說明：市場 Top10 含股票 / ETF / 權證；權證 Top10 另行篩選名稱含 購 / 售 / 牛 / 熊。")

    return "\n".join(report_lines), market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, intraday_df


def get_gsheet_client():
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("找不到 GOOGLE_SERVICE_ACCOUNT_JSON")
    info = json.loads(raw_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds), info.get("client_email", "")


def get_or_create_ws(sh, title, rows=200, cols=20):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def write_df(ws, df):
    ws.clear()
    if df is None or df.empty:
        safe_update(ws, "A1", [["無資料"]])
    else:
        safe = clean_for_sheet(df)
        safe_update(ws, "A1", [list(safe.columns)] + safe.astype(str).values.tolist())


def write_sheet(report, df, market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, intraday_df):
    sheet_id = os.environ.get("WARRANT_GOOGLE_SHEET_ID") or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("找不到 GOOGLE_SHEET_ID 或 WARRANT_GOOGLE_SHEET_ID")

    gc, email = get_gsheet_client()
    sh = gc.open_by_key(sheet_id)

    ws_report = get_or_create_ws(sh, "權證今日報告", rows=300, cols=5)
    ws_report.clear()
    safe_update(ws_report, "A1", [[line] for line in report.splitlines()])

    write_df(get_or_create_ws(sh, "盤中權證監控", rows=100, cols=20), intraday_df)
    write_df(get_or_create_ws(sh, "市場成交量Top10", rows=100, cols=20), market_vol_top)
    write_df(get_or_create_ws(sh, "市場成交金額Top10", rows=100, cols=20), market_amt_top)
    write_df(get_or_create_ws(sh, "權證成交量Top10", rows=100, cols=20), warrant_vol_top)
    write_df(get_or_create_ws(sh, "權證成交金額Top10", rows=100, cols=20), warrant_amt_top)
    write_df(get_or_create_ws(sh, "權證標的集中", rows=100, cols=20), focus)
    write_df(get_or_create_ws(sh, "市場原始資料", rows=5000, cols=20), df)

    # 歷史簡表
    ws_hist = get_or_create_ws(sh, "市場權證歷史紀錄", rows=1000, cols=20)
    if not ws_hist.get_all_values():
        ws_hist.append_row(["寫入時間", "資料日", "成交量集中", "成交金額集中", "今日總結"])
    data_day = str(df["資料日"].iloc[0]) if df is not None and not df.empty else now_taipei().strftime("%Y%m%d")
    summary_line = ""
    for line in report.splitlines():
        if line.startswith("今日權證"):
            summary_line = line
            break
    ws_hist.append_row([
        now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
        data_day,
        top_underlyings(market_vol_top, "成交量") if market_vol_top is not None and not market_vol_top.empty else "-",
        top_underlyings(market_amt_top, "成交金額") if market_amt_top is not None and not market_amt_top.empty else "-",
        summary_line,
    ])
    print(f"已寫入 Google Sheet。Service account：{email}")


def send_telegram(report):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram 未設定，略過推播。")
        return

    text = report.strip()
    max_len = 3500
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]

    for i, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            chunk = f"權證資金流雷達 分段 {i}/{len(chunks)}\n\n" + chunk
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            print("Telegram 推播成功。")
        else:
            print(f"Telegram 推播失敗：{r.status_code} {r.text[:300]}")


def main():
    df = load_warrant_data()
    report, market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, intraday_df = build_report(df)

    print(report)
    df.to_csv("market_warrant_flow_raw_v06.csv", index=False, encoding="utf-8-sig")
    market_vol_top.to_csv("market_volume_top10_v06.csv", index=False, encoding="utf-8-sig")
    market_amt_top.to_csv("market_amount_top10_v06.csv", index=False, encoding="utf-8-sig")

    write_sheet(report, df, market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, intraday_df)
    send_telegram(report)


if __name__ == "__main__":
    main()
