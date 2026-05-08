# 市場資金流雷達 v1.4
# 功能：
# - 抓上市權證每日收盤行情（TWSE 官方資料）
# - 盡量抓上櫃權證每日收盤行情（TPEx 官方資料，若抓不到會保留上市資料）
# - 輸出成交量 Top 10、成交金額 Top 10、標的集中
# - 寫入 Google Sheet
# - 推播 Telegram
# - 最後追加：權證認購買超 / 認購賣超 / 認售買超 / 認售賣超 TOP10
# - 權證買賣超 TOP10：永豐金權證網優先，HiStock 備援，並標示來源

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
FOREIGN_TOP_N = 15
MIN_WARRANT_DISPLAY_COUNT = 10


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


def find_field(fields, candidates, required=False):
    for cand in candidates:
        for f in fields:
            if cand in str(f):
                return f
    if required:
        raise KeyError(f"找不到欄位 {candidates}，目前欄位：{fields}")
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


def fmt_wan_lots(x):
    """成交量以「萬張」顯示，小數點後 1 位。
    TWSE / TPEx 成交量原始多為股數；/1000 = 張，再 /10000 = 萬張。
    """
    try:
        v = float(x)
        wan_lots = v / 10_000_000
        return f"{wan_lots:.1f}萬張"
    except Exception:
        return str(x)


def fmt_yi_money(x):
    """成交金額以「億」顯示，小數點後 1 位。"""
    try:
        v = float(x)
        return f"{v / 100_000_000:.1f}億"
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
                v = fmt_yi_money(v)
            elif c in ["成交量", "盤中成交量"]:
                v = fmt_wan_lots(v)
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
    today = now_taipei().date()

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
    lines.extend(compact_rank_lines(vol_top, ["代碼", "名稱", "認購認售", "盤中成交量"], 5))
    lines.append("")
    lines.append("成交金額急增 Top 5")
    lines.extend(compact_rank_lines(amt_top, ["代碼", "名稱", "認購認售", "估算成交金額", "盤中成交量"], 5))
    lines.append("")
    lines.append("【異常權證】")
    for _, r in vol_top.head(3).iterrows():
        cp = str(r.get("認購認售", ""))
        tag = "認購熱度升溫" if cp == "認購" else "認售避險升溫" if cp == "認售" else "成交量放大"
        lines.append(f"{r.get('名稱')}：盤中成交量 {int(r.get('盤中成交量', 0)):,}，{tag}")

    return "\n".join(lines), q


def non_warrant_df(df):
    if df is None or df.empty:
        return pd.DataFrame()
    return df[~df["名稱"].apply(is_warrant_name)].copy()


def market_subset(df, market_name):
    if df is None or df.empty:
        return pd.DataFrame()
    return df[df["市場"].astype(str).eq(market_name)].copy()


def append_listed_rank_section(lines, subdf):
    lines.append("🏢 上市一般股票 / ETF")
    if subdf is None or subdf.empty:
        lines.append("　資料不足")
        return

    vol_top = subdf.sort_values("成交量", ascending=False).head(TOP_N)
    amt_top = subdf.sort_values("成交金額", ascending=False).head(TOP_N)

    lines.append("")
    lines.append("📈 成交量 Top 10")
    lines.extend(compact_rank_lines(vol_top, ["代碼", "名稱", "成交量"], 10))
    lines.append("")
    lines.append("💰 成交金額 Top 10")
    lines.extend(compact_rank_lines(amt_top, ["代碼", "名稱", "成交金額"], 10))


def append_otc_rank_section(lines, subdf):
    lines.append("🏪 上櫃一般股票 / ETF")
    if subdf is None or subdf.empty:
        lines.append("　資料不足")
        return

    amt_top = subdf.sort_values("成交金額", ascending=False).head(TOP_N)
    lines.append("")
    lines.append("💰 成交金額 Top 10")
    lines.extend(compact_rank_lines(amt_top, ["代碼", "名稱", "成交金額"], 10))


def fmt_foreign_wan_lots(x):
    """外資買賣超以「萬張」顯示，小數點後 1 位。原始單位為股。"""
    try:
        v = float(x)
        return f"{v / 10_000_000:+.1f}萬張"
    except Exception:
        return str(x)


def find_foreign_net_field(fields):
    candidates = [
        "外陸資買賣超股數",
        "外資及陸資買賣超股數",
        "外資買賣超股數",
        "外資買賣超",
    ]
    for cand in candidates:
        hit = find_field(fields, [cand])
        if hit:
            return hit
    for f in fields:
        s = str(f)
        if ("外" in s) and ("買賣超" in s) and ("股" in s):
            return f
    return None


def parse_foreign_table(obj, market_name, data_day):
    rows_out = []

    tables = find_tables(obj)
    for t in tables:
        fields = [str(x).strip() for x in t.get("fields", [])]
        data = t.get("data", [])

        code_f = find_field(fields, ["證券代號", "代號"])
        name_f = find_field(fields, ["證券名稱", "名稱"])
        net_f = find_foreign_net_field(fields)

        if not code_f or not name_f or not net_f:
            continue

        idx = {f: fields.index(f) for f in fields}
        for row in data:
            if not isinstance(row, list) or len(row) < len(fields):
                continue
            code = str(row[idx[code_f]]).strip()
            name = str(row[idx[name_f]]).strip()
            net = parse_number(row[idx[net_f]])
            if not code or net is None:
                continue
            rows_out.append({
                "市場": market_name,
                "資料日": data_day,
                "代碼": code,
                "名稱": name,
                "外資買賣超股數": int(net),
                "外資買賣超萬張": round(net / 10_000_000, 1),
            })

    dict_rows = obj if isinstance(obj, list) else []
    if dict_rows:
        for row in dict_rows:
            if not isinstance(row, dict):
                continue
            keys = list(row.keys())
            code_k = find_field(keys, ["證券代號", "代號", "股票代號"])
            name_k = find_field(keys, ["證券名稱", "名稱", "股票名稱"])
            net_k = find_foreign_net_field(keys)
            if not code_k or not name_k or not net_k:
                continue
            code = str(row.get(code_k, "")).strip()
            name = str(row.get(name_k, "")).strip()
            net = parse_number(row.get(net_k))
            if not code or net is None:
                continue
            rows_out.append({
                "市場": market_name,
                "資料日": data_day,
                "代碼": code,
                "名稱": name,
                "外資買賣超股數": int(net),
                "外資買賣超萬張": round(net / 10_000_000, 1),
            })

    if not rows_out:
        return pd.DataFrame()
    return pd.DataFrame(rows_out).drop_duplicates(subset=["市場", "代碼"])


def fetch_twse_foreign_daily():
    today = now_taipei().date()
    url_templates = [
        "https://www.twse.com.tw/rwd/zh/fund/T86?date={ds}&selectType=ALLBUT0999&response=json",
        "https://openapi.twse.com.tw/v1/exchangeReport/TWT86U_ALLBUT0999",
    ]
    for i in range(10):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        for tmpl in url_templates:
            try:
                url = tmpl.format(ds=ds)
                obj = fetch_json(url)
                out = parse_foreign_table(obj, "上市", ds)
                if not out.empty:
                    return out
            except Exception:
                continue
    return pd.DataFrame()


def fetch_tpex_foreign_daily():
    today = now_taipei().date()
    url_templates = [
        "https://www.tpex.org.tw/www/zh-tw/threeInstitutional/daily?date={roc}&response=json",
        "https://www.tpex.org.tw/www/zh-tw/threeInstitutional/daily?date={roc}&type=Daily&response=json",
        "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?date={roc}&type=Daily&response=json",
        "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D&d={roc2}",
    ]
    for i in range(10):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
        roc2 = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
        for tmpl in url_templates:
            try:
                obj = fetch_json(tmpl.format(roc=roc, roc2=roc2))
                out = parse_foreign_table(obj, "上櫃", ds)
                if not out.empty:
                    return out
            except Exception:
                continue
    return pd.DataFrame()


def load_foreign_data():
    frames = []
    twse = fetch_twse_foreign_daily()
    tpex = fetch_tpex_foreign_daily()
    if twse is not None and not twse.empty:
        frames.append(twse)
    if tpex is not None and not tpex.empty:
        frames.append(tpex)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["市場", "代碼"])
    return df


def foreign_rank_lines(df, ascending=False):
    if df is None or df.empty:
        return ["資料不足"]
    x = df.copy()
    if "外資買賣超股數" not in x.columns:
        return ["資料不足"]
    if ascending:
        x = x[x["外資買賣超股數"] < 0].sort_values("外資買賣超股數", ascending=True).head(FOREIGN_TOP_N)
    else:
        x = x[x["外資買賣超股數"] > 0].sort_values("外資買賣超股數", ascending=False).head(FOREIGN_TOP_N)
    if x.empty:
        return ["資料不足"]
    lines = []
    for i, (_, r) in enumerate(x.iterrows(), start=1):
        lines.append(f"{i}. {r.get('名稱')}｜{fmt_foreign_wan_lots(r.get('外資買賣超股數'))}")
    return lines


def append_foreign_section(lines, foreign_df):
    lines.append("🏦 外資買賣超")
    if foreign_df is None or foreign_df.empty:
        lines.append("　資料不足")
        return
    lines.append("")
    lines.append("📈 外資買超 Top 15")
    lines.extend(foreign_rank_lines(foreign_df, ascending=False))
    lines.append("")
    lines.append("📉 外資賣超 Top 15")
    lines.extend(foreign_rank_lines(foreign_df, ascending=True))


# ============================================================
# 權證買賣超 TOP10：HiStock 多層 fallback 版
# ============================================================

def empty_warrant_bs_top10():
    return {
        "認購買超": pd.DataFrame(columns=["名稱", "金額"]),
        "認購賣超": pd.DataFrame(columns=["名稱", "金額"]),
        "認售買超": pd.DataFrame(columns=["名稱", "金額"]),
        "認售賣超": pd.DataFrame(columns=["名稱", "金額"]),
    }



def mark_warrant_bs_source(bs, source_name):
    """替四個權證買賣超表補來源欄位，方便 Telegram / Sheet 檢查資料從哪裡來。"""
    if not bs:
        return bs
    out = {}
    for k, df in bs.items():
        if df is None or df.empty:
            out[k] = df
            continue
        x = df.copy()
        x["來源"] = source_name
        out[k] = x
    return out


def fetch_sinotrade_warrant_bs_top10():
    """
    權證盤後買賣超 TOP10：永豐金權證網優先來源。

    永豐金權證網的市場統計頁有「投資人買超權證_金額排行 / 投資人賣超權證_金額排行」。
    這裡用通用解析，不硬綁表格 id：
    1) 讀取 marketW.jsp
    2) 從所有 HTML table 找出買超 / 賣超、金額欄、名稱欄
    3) 用權證名稱自動判斷認購 / 認售
    4) 輸出四類：認購買超、認購賣超、認售買超、認售賣超
    5) 若頁面改成 JS 動態載入而抓不到，回傳空表讓 HiStock 備援接手
    """
    out = empty_warrant_bs_top10()
    url = "https://warrant.sinotrade.com.tw/j/marketW.jsp"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://warrant.sinotrade.com.tw/",
        "Connection": "keep-alive",
    }

    def flatten_columns(df):
        x = df.copy()
        cols = []
        for c in x.columns:
            if isinstance(c, tuple):
                cols.append("".join([str(v) for v in c if str(v) != "nan"]).replace("\n", "").replace(" ", ""))
            else:
                cols.append(str(c).replace("\n", "").replace(" ", ""))
        x.columns = cols
        return x

    def parse_amount_to_yuan(v):
        try:
            s = str(v).replace(",", "").replace("+", "").replace("−", "-").strip()
            if s in ["", "-", "--", "nan", "None"]:
                return None
            unit = 1
            if "億" in s:
                unit = 100_000_000
            elif "萬" in s:
                unit = 10_000
            s = s.replace("億", "").replace("萬", "")
            return float(s) * unit
        except Exception:
            return None

    def fmt_amount_wan(v):
        try:
            return f"{float(v) / 10_000:.1f}萬"
        except Exception:
            return ""

    def clean_name(v):
        s = str(v).replace("\n", "").replace("\r", "").strip()
        s = re.sub(r"\s+", "", s)
        return s

    def pick_col(cols, keywords, exclude=None):
        exclude = exclude or []
        for c in cols:
            cs = str(c)
            if any(k in cs for k in keywords) and not any(x in cs for x in exclude):
                return c
        return None

    def parse_candidate_table(df, side_word):
        if df is None or df.empty:
            return pd.DataFrame(columns=["名稱", "金額"])
        x = flatten_columns(df).dropna(how="all").copy()
        if x.empty:
            return pd.DataFrame(columns=["名稱", "金額"])

        cols = list(x.columns)
        name_col = pick_col(cols, ["權證名稱", "名稱", "商品", "股票"], exclude=["標的"])
        amt_col = pick_col(cols, ["買賣超金額", "金額"], exclude=["成交"])

        # 若欄名不標準，就用內容特徵補抓
        if name_col is None:
            best_col, best_score = None, -1
            for c in cols:
                cs = str(c)
                if any(k in cs for k in ["代號", "排名", "金額", "張數", "比例", "%"]):
                    continue
                vals = x[c].astype(str).head(50).tolist()
                score = sum(1 for v in vals if any(k in v for k in ["購", "售", "牛", "熊"]))
                if score > best_score:
                    best_col, best_score = c, score
            if best_score >= 3:
                name_col = best_col

        if amt_col is None:
            best_col, best_score = None, -1
            for c in cols:
                cs = str(c)
                if any(k in cs for k in ["代號", "排名", "張數", "比例", "%", "流通"]):
                    continue
                nums = x[c].apply(parse_amount_to_yuan).dropna()
                if len(nums) < 3:
                    continue
                score = len(nums) + min(nums.abs().median() / 1_000_000, 50)
                if score > best_score:
                    best_col, best_score = c, score
            amt_col = best_col

        if name_col is None or amt_col is None:
            return pd.DataFrame(columns=["名稱", "金額"])

        y = x[[name_col, amt_col]].copy()
        y.columns = ["名稱", "原始金額"]
        y["名稱"] = y["名稱"].apply(clean_name)
        y["金額_num"] = y["原始金額"].apply(parse_amount_to_yuan)
        y = y.dropna(subset=["金額_num"])
        y = y[y["名稱"].apply(is_warrant_name)]
        y = y[~y["名稱"].astype(str).str.fullmatch(r"\d{4,6}", na=False)]
        if y.empty:
            return pd.DataFrame(columns=["名稱", "金額"])

        if side_word == "賣超":
            y = y.sort_values("金額_num", ascending=True if (y["金額_num"] < 0).any() else False)
        else:
            y = y.sort_values("金額_num", ascending=False)

        y = y.head(30).copy()
        y["認購認售"] = y["名稱"].apply(infer_cp_type)
        y["金額"] = y["金額_num"].apply(fmt_amount_wan)
        return y[["名稱", "認購認售", "金額", "金額_num"]]

    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        html = r.text

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")

        # 把每個表格前後文一起評分，優先抓「投資人買超/賣超 + 金額排行」
        candidates = {"買超": [], "賣超": []}
        for tb in tables:
            context_parts = [tb.get_text(" ", strip=True)]
            p = tb.parent
            for _ in range(3):
                if p is None:
                    break
                context_parts.append(p.get_text(" ", strip=True)[:2000])
                p = p.parent
            prev = tb.find_previous()
            for _ in range(8):
                if prev is None:
                    break
                try:
                    context_parts.append(prev.get_text(" ", strip=True)[:600])
                except Exception:
                    pass
                prev = prev.find_previous()
            context = " ".join(context_parts)

            try:
                arr = pd.read_html(io.StringIO(str(tb)))
            except Exception:
                arr = []
            if not arr:
                continue

            for side in ["買超", "賣超"]:
                score = 0
                if "投資人" in context:
                    score += 5
                if side in context:
                    score += 10
                if "權證" in context:
                    score += 5
                if "金額" in context:
                    score += 5
                if "張數" in context and "金額" not in context:
                    score -= 5
                if score <= 0:
                    continue
                y = parse_candidate_table(arr[0], side)
                if y is not None and not y.empty:
                    candidates[side].append((score, y))

        for side in ["買超", "賣超"]:
            if not candidates[side]:
                continue
            candidates[side].sort(key=lambda z: z[0], reverse=True)
            y = candidates[side][0][1]
            for cp in ["認購", "認售"]:
                key = f"{cp}{side}"
                sub = y[y["認購認售"] == cp].head(10).copy()
                out[key] = sub[["名稱", "金額"]] if not sub.empty else out[key]

        hit_count = sum(0 if v is None or v.empty else len(v) for v in out.values())
        if hit_count:
            print(f"永豐金權證買賣超：成功抓到 {hit_count} 筆。")
        else:
            print("永豐金權證買賣超：未抓到可用資料，改用 HiStock 備援。")

    except Exception as e:
        print(f"永豐金權證買賣超抓取失敗，改用 HiStock 備援：{e}")

    return mark_warrant_bs_source(out, "永豐金")


def merge_warrant_bs_sources(primary, fallback):
    """四個分類逐項補洞：永豐金有資料就用永豐金，缺的分類才用 HiStock。"""
    out = empty_warrant_bs_top10()
    for key in out.keys():
        p = primary.get(key) if primary else pd.DataFrame()
        f = fallback.get(key) if fallback else pd.DataFrame()
        out[key] = p if p is not None and not p.empty else f
    return out


def fetch_warrant_bs_top10():
    """
    權證盤後買賣超 TOP10 優化版。
    資料源優先序：
    1) 永豐金權證網：較貼近權證市場統計，優先使用
    2) HiStock：可抓認購 / 認售標的買賣超，當備援

    元大 / 富邦目前比較適合抓權證搜尋、成交量 / 成交值排行；
    投資人買賣超金額排行這塊，永豐金頁面有明確分類，所以先放第一順位。
    """
    primary = fetch_sinotrade_warrant_bs_top10()
    need_fallback = any(primary.get(k) is None or primary.get(k).empty for k in primary.keys())
    if not need_fallback:
        return primary

    fallback = mark_warrant_bs_source(fetch_histock_warrant_bs_top10(), "HiStock備援")
    return merge_warrant_bs_sources(primary, fallback)


def fetch_histock_warrant_bs_top10():
    """
    權證盤後買賣超 TOP10。
    HiStock 備援版：抓投資人權證買賣超排名，並做多層 fallback。

    抓取邏輯：
    1. 先抓主頁
    2. 自動尋找頁面上「認購/認售 + 買超/賣超」的分類連結
    3. 有分類連結就分別打開分類頁
    4. 找不到分類連結時，回到主頁所有表格中用欄位與金額型態判斷
    5. 最後輸出：名稱｜金額，金額以萬顯示，小數後 1 位
    """
    out = empty_warrant_bs_top10()

    base_url = "https://histock.tw"
    main_url = "https://histock.tw/stock/warrantstats.aspx"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://histock.tw/",
        "Connection": "keep-alive",
    }

    targets = {
        "認購買超": ("認購", "買超"),
        "認購賣超": ("認購", "賣超"),
        "認售買超": ("認售", "買超"),
        "認售賣超": ("認售", "賣超"),
    }

    def abs_url(href):
        href = str(href or "").strip()
        if not href:
            return ""
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return base_url + href
        return base_url + "/" + href.lstrip("./")

    def fetch_html(url):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            # HiStock 多為繁中頁，requests 通常可自動判斷；這裡補強
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:
            print(f"HiStock 抓取失敗：{url}｜{e}")
            return ""

    def flatten_columns(df):
        x = df.copy()
        cols = []
        for c in x.columns:
            if isinstance(c, tuple):
                cols.append("".join([str(v) for v in c if str(v) != "nan"]).replace("\n", "").replace(" ", ""))
            else:
                cols.append(str(c).replace("\n", "").replace(" ", ""))
        x.columns = cols
        return x

    def parse_amount_num(v):
        try:
            s = (
                str(v)
                .replace(",", "")
                .replace("+", "")
                .replace("−", "-")
                .replace("億", "")
                .replace("萬", "")
                .strip()
            )
            if s in ["", "-", "--", "nan", "None"]:
                return None
            return float(s)
        except Exception:
            return None

    def fmt_amount_yi(v):
        """
        HiStock 買賣超金額改用「萬」顯示，小數後 1 位。
        來源通常是元。
        """
        try:
            num = float(v)
            wan = num / 10_000
            return f"{wan:.1f}萬"
        except Exception:
            return ""

    def clean_name(v):
        s = str(v).replace("\n", "").replace("\r", "").strip()
        s = re.sub(r"\s+", "", s)
        return s

    def is_bad_name(s):
        s = str(s)
        bad_words = [
            "股票", "名稱", "標的", "排名", "代號", "買超", "賣超",
            "買賣超", "金額", "張數", "流通", "nan", "None", "--"
        ]
        return (not s) or any(k in s for k in bad_words)

    def table_contexts_from_html(html):
        """
        回傳 [(context_text, table_html), ...]
        context_text 會抓 table 前後附近文字，避免分類文字在 table 外造成誤判。
        """
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")
            items = []

            for tb in tables:
                context_parts = []

                # table 自己文字
                context_parts.append(tb.get_text(" ", strip=True))

                # 往上抓 parent / grandparent 的文字
                p = tb.parent
                depth = 0
                while p is not None and depth < 3:
                    context_parts.append(p.get_text(" ", strip=True)[:2000])
                    p = p.parent
                    depth += 1

                # 往前抓幾個兄弟節點，常見標題在 table 前面
                prev = tb.find_previous()
                steps = 0
                while prev is not None and steps < 8:
                    try:
                        context_parts.append(prev.get_text(" ", strip=True)[:500])
                    except Exception:
                        pass
                    prev = prev.find_previous()
                    steps += 1

                context = " ".join(context_parts)
                items.append((context, str(tb)))

            return items

        except Exception:
            return []

    def discover_category_urls(html):
        """
        從主頁找分類連結。
        若找不到，後續仍會用主頁 fallback。
        """
        found = {k: [] for k in targets.keys()}

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a"):
                txt = a.get_text(" ", strip=True)
                href = a.get("href", "")
                combo = txt + " " + href

                for key, (cp_word, side_word) in targets.items():
                    if cp_word in combo and side_word in combo:
                        u = abs_url(href)
                        if u and u not in found[key]:
                            found[key].append(u)

            # 也掃 onclick / data-url
            for tag in soup.find_all(True):
                attrs_text = " ".join([str(v) for v in tag.attrs.values()])
                tag_text = tag.get_text(" ", strip=True)
                combo = tag_text + " " + attrs_text

                for key, (cp_word, side_word) in targets.items():
                    if cp_word in combo and side_word in combo:
                        m = re.search(r"['\"]([^'\"]*warrantstats[^'\"]*)['\"]", attrs_text)
                        if m:
                            u = abs_url(m.group(1))
                            if u and u not in found[key]:
                                found[key].append(u)

        except Exception as e:
            print(f"HiStock 分類連結解析失敗：{e}")

        return found

    def dataframe_from_table_html(table_html):
        try:
            arr = pd.read_html(io.StringIO(table_html))
            if not arr:
                return None
            return flatten_columns(arr[0])
        except Exception:
            return None

    def select_columns(x):
        """
        找名稱欄與金額欄。
        名稱欄：優先 股票/名稱/標的，否則找中文比例高的欄。
        金額欄：優先 買賣超金額/金額，否則找數字且金額級距大的欄。
        """
        if x is None or x.empty:
            return None, None

        name_col = None
        amt_col = None

        for c in x.columns:
            cs = str(c)
            if name_col is None and ("股票" in cs or "名稱" in cs or "標的" in cs):
                name_col = c
            if amt_col is None and ("買賣超金額" in cs or "金額" in cs):
                amt_col = c

        if name_col is None:
            best_col = None
            best_score = -1
            for c in x.columns:
                vals = x[c].astype(str).head(30).tolist()
                joined = " ".join(vals)

                if any(k in str(c) for k in ["代號", "排名", "金額", "張數", "流通", "漲跌", "比例"]):
                    continue
                if any(k in joined for k in ["買超", "賣超", "金額", "張數", "排名"]):
                    continue

                score = sum(1 for v in vals if re.search(r"[\u4e00-\u9fff]", v))
                if score > best_score:
                    best_score = score
                    best_col = c

            if best_score >= 3:
                name_col = best_col

        if amt_col is None:
            best_col = None
            best_score = -1

            for c in x.columns:
                cs = str(c)
                if any(k in cs for k in ["代號", "排名", "張數", "流通", "比例", "%"]):
                    continue

                nums = x[c].apply(parse_amount_num)
                valid = nums.dropna()
                if len(valid) < 3:
                    continue

                # 金額欄通常數值較大
                magnitude = valid.abs().median()
                score = len(valid) + min(magnitude / 1_000_000, 50)
                if score > best_score:
                    best_score = score
                    best_col = c

            amt_col = best_col

        return name_col, amt_col

    def parse_one_table(x, side_word):
        if x is None or x.empty:
            return pd.DataFrame(columns=["名稱", "金額"])

        x = x.dropna(how="all").copy()
        if x.empty:
            return pd.DataFrame(columns=["名稱", "金額"])

        name_col, amt_col = select_columns(x)
        if name_col is None or amt_col is None:
            return pd.DataFrame(columns=["名稱", "金額"])

        y = x[[name_col, amt_col]].copy()
        y.columns = ["名稱", "原始金額"]
        y["名稱"] = y["名稱"].apply(clean_name)
        y["金額_num"] = y["原始金額"].apply(parse_amount_num)

        y = y.dropna(subset=["金額_num"])
        y = y[~y["名稱"].apply(is_bad_name)]

        # 避免代號欄誤判成名稱
        y = y[~y["名稱"].astype(str).str.fullmatch(r"\d{4,6}", na=False)]

        if y.empty:
            return pd.DataFrame(columns=["名稱", "金額"])

        if side_word == "賣超":
            if (y["金額_num"] < 0).any():
                y = y.sort_values("金額_num", ascending=True)
            else:
                y = y.sort_values("金額_num", ascending=False)
        else:
            y = y.sort_values("金額_num", ascending=False)

        y = y.head(10).copy()
        y["金額"] = y["金額_num"].apply(fmt_amount_yi)
        return y[["名稱", "金額"]]

    def score_candidate(context, x, cp_word, side_word):
        text = context + " " + " ".join([str(c) for c in getattr(x, "columns", [])])
        score = 0
        if cp_word in text:
            score += 10
        if side_word in text:
            score += 10
        if "買賣超金額" in text:
            score += 5
        if "金額" in text:
            score += 2
        if "股票" in text or "名稱" in text or "標的" in text:
            score += 2
        return score

    def extract_from_html(html, cp_word, side_word, require_context=False):
        items = table_contexts_from_html(html)
        candidates = []

        for context, table_html in items:
            if require_context and (cp_word not in context or side_word not in context):
                continue

            x = dataframe_from_table_html(table_html)
            y = parse_one_table(x, side_word)

            if y is None or y.empty:
                continue

            sc = score_candidate(context, x, cp_word, side_word)
            candidates.append((sc, y))

        if not candidates:
            return pd.DataFrame(columns=["名稱", "金額"])

        candidates.sort(key=lambda z: z[0], reverse=True)
        return candidates[0][1]

    try:
        main_html = fetch_html(main_url)
        if not main_html:
            return out

        cat_urls = discover_category_urls(main_html)

        for key, (cp_word, side_word) in targets.items():
            html_candidates = []

            # 1) 先試分類連結
            for u in cat_urls.get(key, []):
                h = fetch_html(u)
                if h:
                    html_candidates.append((u, h, True))

            # 2) 再試主頁，但要求 context 符合分類
            html_candidates.append((main_url, main_html, True))

            # 3) 最後 fallback：主頁不要求 context，只抓最像買賣超表的表格
            html_candidates.append((main_url, main_html, False))

            result = pd.DataFrame(columns=["名稱", "金額"])

            for u, h, require_context in html_candidates:
                result = extract_from_html(h, cp_word, side_word, require_context=require_context)
                if result is not None and not result.empty:
                    break

            out[key] = result if result is not None else pd.DataFrame(columns=["名稱", "金額"])

            if out[key].empty:
                print(f"HiStock {key}：資料不足，可能是分類參數或表格結構變更。")
            else:
                print(f"HiStock {key}：成功抓到 {len(out[key])} 筆。")

    except Exception as e:
        print(f"HiStock 權證買賣超 TOP10 抓取失敗：{e}")

    return out



def warrant_bs_rank_lines(df):
    if df is None or df.empty:
        return ["資料不足"]
    lines = []
    for i, (_, r) in enumerate(df.head(10).iterrows(), start=1):
        name = str(r.get("名稱", "")).strip()
        amt = str(r.get("金額", "")).strip()
        src = str(r.get("來源", "")).strip()
        tail = f"｜{src}" if src else ""
        lines.append(f"{i}. {name}｜{amt}{tail}")
    return lines


def append_warrant_bs_top10_section(lines, bs):
    lines.append("🎯 權證買賣超 TOP10")
    lines.append("")

    lines.append("📈 認購買超 TOP10")
    lines.extend(warrant_bs_rank_lines(bs.get("認購買超") if bs else pd.DataFrame()))
    lines.append("")

    lines.append("📉 認購賣超 TOP10")
    lines.extend(warrant_bs_rank_lines(bs.get("認購賣超") if bs else pd.DataFrame()))
    lines.append("")

    lines.append("📈 認售買超 TOP10")
    lines.extend(warrant_bs_rank_lines(bs.get("認售買超") if bs else pd.DataFrame()))
    lines.append("")

    lines.append("📉 認售賣超 TOP10")
    lines.extend(warrant_bs_rank_lines(bs.get("認售賣超") if bs else pd.DataFrame()))


def expected_market_data_day(now=None):
    """預期資料日：這版排程固定週一～週五 18:00 跑，因此以執行當天作為預期資料日。"""
    now = now or now_taipei()
    return now.strftime("%Y%m%d")


def data_update_status(df, now=None):
    now = now or now_taipei()
    expected = expected_market_data_day(now)
    if df is None or df.empty or "資料日" not in df.columns:
        return "❌ 資料狀態：抓取失敗", expected, "無資料"
    data_day = str(df["資料日"].iloc[0])
    if data_day == expected:
        return "✅ 資料狀態：已更新", expected, data_day
    return "⚠️ 資料狀態：尚未更新", expected, data_day


def build_report(df):
    now = now_taipei()
    status_text, expected_day, actual_day = data_update_status(df, now)
    foreign_df = load_foreign_data()

    if df.empty:
        report = "\n".join([
            "📊 市場資金流雷達 v1.4",
            status_text,
            f"📅 資料日：{actual_day}",
            f"🕒 最後更新：{now.strftime('%Y/%m/%d %H:%M')}",
            "",
            "🔥 今日總覽",
            "今日未抓到市場資料，請檢查 TWSE / TPEx 資料來源。",
        ])
        return report, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    data_day = str(df["資料日"].iloc[0])

    stock_df = non_warrant_df(df)
    listed_df = market_subset(stock_df, "上市")
    otc_df = market_subset(stock_df, "上櫃")

    market_vol_top = stock_df.sort_values("成交量", ascending=False).head(TOP_N) if not stock_df.empty else pd.DataFrame()
    market_amt_top = stock_df.sort_values("成交金額", ascending=False).head(TOP_N) if not stock_df.empty else pd.DataFrame()

    warrant_vol_top = pd.DataFrame()
    warrant_amt_top = pd.DataFrame()
    focus = pd.DataFrame()

    report_lines = []
    report_lines.append("📊 市場資金流雷達 v1.4")
    report_lines.append(status_text)
    if status_text.startswith("⚠️"):
        report_lines.append(f"預期資料日：{expected_day}")
    report_lines.append(f"📅 資料日：{data_day}")
    report_lines.append(f"🕒 最後更新：{now.strftime('%Y/%m/%d %H:%M')}")
    report_lines.append("")
    report_lines.append("🔥 今日總覽")
    report_lines.append(f"市場資料：{len(df)} 檔")
    report_lines.append(f"一般股票 / ETF：{len(stock_df)} 檔")
    report_lines.append("")
    report_lines.append("━━━━━━━━━━━━━━")
    report_lines.append("")

    append_listed_rank_section(report_lines, listed_df)
    report_lines.append("")
    report_lines.append("━━━━━━━━━━━━━━")
    report_lines.append("")

    append_otc_rank_section(report_lines, otc_df)
    report_lines.append("")
    report_lines.append("━━━━━━━━━━━━━━")
    report_lines.append("")

    append_foreign_section(report_lines, foreign_df)
    report_lines.append("")
    report_lines.append("━━━━━━━━━━━━━━")
    report_lines.append("")

    # 新增：權證認購 / 認售買賣超 TOP10，放在報告最後面
    warrant_bs_top10 = fetch_warrant_bs_top10()
    append_warrant_bs_top10_section(report_lines, warrant_bs_top10)

    foreign_buy_top = (
        foreign_df[foreign_df["外資買賣超股數"] > 0]
        .sort_values("外資買賣超股數", ascending=False)
        .head(FOREIGN_TOP_N)
    ) if not foreign_df.empty else pd.DataFrame()

    foreign_sell_top = (
        foreign_df[foreign_df["外資買賣超股數"] < 0]
        .sort_values("外資買賣超股數", ascending=True)
        .head(FOREIGN_TOP_N)
    ) if not foreign_df.empty else pd.DataFrame()

    return "\n".join(report_lines), market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, foreign_buy_top, foreign_sell_top


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


def write_sheet(report, df, market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, foreign_buy_top, foreign_sell_top):
    sheet_id = os.environ.get("WARRANT_GOOGLE_SHEET_ID") or os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("找不到 GOOGLE_SHEET_ID 或 WARRANT_GOOGLE_SHEET_ID")

    gc, email = get_gsheet_client()
    sh = gc.open_by_key(sheet_id)

    ws_report = get_or_create_ws(sh, "權證今日報告", rows=300, cols=5)
    ws_report.clear()
    safe_update(ws_report, "A1", [[line] for line in report.splitlines()])

    write_df(get_or_create_ws(sh, "市場成交量Top10", rows=100, cols=20), market_vol_top)
    write_df(get_or_create_ws(sh, "市場成交金額Top10", rows=100, cols=20), market_amt_top)
    write_df(get_or_create_ws(sh, "權證成交量Top10", rows=100, cols=20), warrant_vol_top)
    write_df(get_or_create_ws(sh, "權證成交金額Top10", rows=100, cols=20), warrant_amt_top)
    write_df(get_or_create_ws(sh, "權證標的集中", rows=100, cols=20), focus)
    write_df(get_or_create_ws(sh, "外資買超Top15", rows=100, cols=20), foreign_buy_top)
    write_df(get_or_create_ws(sh, "外資賣超Top15", rows=100, cols=20), foreign_sell_top)
    write_df(get_or_create_ws(sh, "市場原始資料", rows=5000, cols=20), df)

    ws_hist = get_or_create_ws(sh, "市場權證歷史紀錄", rows=1000, cols=20)
    if not ws_hist.get_all_values():
        ws_hist.append_row(["寫入時間", "資料日", "成交量集中", "成交金額集中", "今日總結"])

    data_day = str(df["資料日"].iloc[0]) if df is not None and not df.empty else now_taipei().strftime("%Y%m%d")
    summary_line = ""
    for line in report.splitlines():
        if line.startswith("市場資料："):
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
    report, market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, foreign_buy_top, foreign_sell_top = build_report(df)

    print(report)

    df.to_csv("market_flow_raw_v13.csv", index=False, encoding="utf-8-sig")
    market_vol_top.to_csv("market_volume_top10_v13.csv", index=False, encoding="utf-8-sig")
    market_amt_top.to_csv("market_amount_top10_v13.csv", index=False, encoding="utf-8-sig")
    foreign_buy_top.to_csv("foreign_buy_top15_v13.csv", index=False, encoding="utf-8-sig")
    foreign_sell_top.to_csv("foreign_sell_top15_v13.csv", index=False, encoding="utf-8-sig")

    write_sheet(report, df, market_vol_top, market_amt_top, warrant_vol_top, warrant_amt_top, focus, foreign_buy_top, foreign_sell_top)
    send_telegram(report)


if __name__ == "__main__":
    main()
