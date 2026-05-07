# 台指選擇權籌碼雷達 v1.8
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

TOP_N_WALLS = 2
MAX_WALL_DISTANCE = 1000

SETTLE_HOUR = 13
SETTLE_MINUTE = 45

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

OPT_URL = "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=DailyMarketReportOpt"
FUT_URL = "https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=DailyMarketReportFut"


def now_taipei():
    return datetime.now(TAIPEI_TZ)


def fetch_csv(url):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,text/plain,*/*"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    raw = r.content
    for enc in ["utf-8-sig", "big5", "cp950"]:
        try:
            return pd.read_csv(io.StringIO(raw.decode(enc)))
        except Exception:
            pass
    return pd.read_csv(io.StringIO(raw.decode("utf-8", errors="ignore")))


def norm_cols(df):
    df = df.copy()
    df.columns = [str(c).replace("\ufeff", "").replace("\n", "").replace(" ", "").strip() for c in df.columns]
    return df


def find_col(df, keys, required=True):
    for k in keys:
        for c in df.columns:
            if k in c:
                return c
    if required:
        raise KeyError(f"找不到欄位 {keys}，目前欄位：{list(df.columns)}")
    return None


def to_num(s):
    return pd.to_numeric(
        s.astype(str).str.replace(",", "", regex=False).str.replace("--", "", regex=False).str.replace("-", "", regex=False).str.strip(),
        errors="coerce",
    )


def parse_number(x):
    try:
        t = str(x).replace(",", "").replace("--", "").strip()
        return float(t)
    except Exception:
        return None


def clean_option_data(raw):
    df = norm_cols(raw)
    c_date = find_col(df, ["日期", "交易日期"])
    c_contract = find_col(df, ["契約", "商品"])
    c_expiry = find_col(df, ["到期月份(週別)", "到期月份", "到期月"])
    c_strike = find_col(df, ["履約價"])
    c_cp = find_col(df, ["買賣權", "權別"])
    c_oi = find_col(df, ["未沖銷契約量", "未沖銷契約數", "未平倉", "未沖銷"])

    c_settle = None
    for keys in [["結算價"], ["最後成交價"], ["收盤價"]]:
        c_settle = find_col(df, keys, required=False)
        if c_settle:
            break

    out = pd.DataFrame({
        "date": df[c_date].astype(str).str.strip(),
        "contract": df[c_contract].astype(str).str.strip(),
        "expiry": df[c_expiry].astype(str).str.strip(),
        "strike": to_num(df[c_strike]),
        "cp": df[c_cp].astype(str).str.strip(),
        "oi": to_num(df[c_oi]),
        "settle": to_num(df[c_settle]) if c_settle else 0,
    })

    out = out[out["contract"].str.contains(PRODUCT_OPT, na=False)].copy()
    out.loc[out["cp"].str.contains("C|買", na=False), "cp"] = "C"
    out.loc[out["cp"].str.contains("P|賣", na=False), "cp"] = "P"
    out = out.dropna(subset=["strike", "oi"])
    out["strike"] = out["strike"].astype(int)
    out["oi"] = out["oi"].fillna(0).astype(float)
    out["settle"] = out["settle"].fillna(0).astype(float)
    return out


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
                                if val:
                                    return val
                    for cell in reversed(row):
                        val = parse_number(cell)
                        if val:
                            return val
        for v in obj.values():
            found = find_taiex_in_json(v)
            if found:
                return found
    elif isinstance(obj, list):
        if obj and "發行量加權股價指數" in str(obj[0]):
            for cell in reversed(obj):
                val = parse_number(cell)
                if val:
                    return val
        for v in obj:
            found = find_taiex_in_json(v)
            if found:
                return found
    return None


def get_taiex_price():
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"}
    today = now_taipei().date()
    urls = [
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date}&type=IND&response=json",
        "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=IND",
    ]
    for i in range(10):
        ds = (today - timedelta(days=i)).strftime("%Y%m%d")
        for tmpl in urls:
            try:
                r = requests.get(tmpl.format(date=ds), headers=headers, timeout=30)
                if r.status_code != 200:
                    continue
                val = find_taiex_in_json(r.json())
                if val:
                    return val, f"加權指數收盤價 {ds}"
            except Exception:
                pass
    return None, "加權指數自動抓取失敗"


def get_txf_price():
    try:
        raw = fetch_csv(FUT_URL)
        df = norm_cols(raw)
        c_contract = find_col(df, ["契約", "商品"])
        c_expiry = find_col(df, ["到期月份", "到期月"], required=False)
        price_col = None
        for keys in [["結算價"], ["收盤價"], ["最後成交價"], ["開盤價"]]:
            price_col = find_col(df, keys, required=False)
            if price_col:
                break
        if price_col is None:
            return None, "TXF自動抓取失敗：找不到價格欄位"

        fut = df[df[c_contract].astype(str).str.strip().eq(PRODUCT_FUT)].copy()
        if fut.empty:
            fut = df[df[c_contract].astype(str).str.contains(PRODUCT_FUT, na=False)].copy()
        if fut.empty:
            return None, "TXF自動抓取失敗：找不到 TX 契約"

        fut["price"] = to_num(fut[price_col])
        fut = fut.dropna(subset=["price"])
        fut = fut[fut["price"] > 0]
        if fut.empty:
            return None, "TXF自動抓取失敗：價格為空"

        if c_expiry:
            fut["_sort"] = pd.to_numeric(fut[c_expiry].astype(str).str.extract(r"(\d+)")[0], errors="coerce")
            fut = fut.sort_values("_sort")
        row = fut.iloc[0]
        expiry = str(row[c_expiry]) if c_expiry else "近月"
        return float(row["price"]), f"TXF 近月 {expiry}：{price_col}"
    except Exception as e:
        return None, f"TXF自動抓取失敗：{type(e).__name__} {e}"


def parse_week_tag(expiry):
    e = str(expiry).upper().strip()
    m = re.search(r"W(\d+)", e)
    if m:
        return "W", int(m.group(1))
    m = re.search(r"F(\d+)", e)
    if m:
        return "F", int(m.group(1))
    return "M", 0


def sort_key(expiry):
    e = str(expiry).upper().strip()
    tag, num = parse_week_tag(e)
    if tag == "W":
        return (num, 1, e)
    if tag == "F":
        return (num, 2, e)
    m = re.search(r"(\d{6})", e)
    month_num = int(m.group(1)) if m else 999999
    return (99, month_num, e)


def after_settle():
    n = now_taipei()
    return (n.hour, n.minute) >= (SETTLE_HOUR, SETTLE_MINUTE)


def filter_expiries(expiries):
    n = now_taipei()
    weekday = n.weekday()
    after = after_settle()
    w, f, m = [], [], []
    for exp in sorted(expiries, key=sort_key):
        tag, num = parse_week_tag(exp)
        if tag == "W":
            if weekday == 2 and after and num == 1:
                continue
            w.append(exp)
        elif tag == "F":
            if weekday == 4 and after and num == 1:
                continue
            f.append(exp)
        else:
            m.append(exp)
    selected = w[:SHOW_NEAR_W_COUNT] + f[:SHOW_NEAR_F_COUNT] + m[:SHOW_MONTH_COUNT]
    return sorted(selected, key=sort_key)


def calc_max_pain(g):
    if g.empty:
        return None
    strikes = sorted(g["strike"].unique())
    calls = g[g["cp"] == "C"].groupby("strike")["oi"].sum()
    puts = g[g["cp"] == "P"].groupby("strike")["oi"].sum()
    best_s, best_loss = None, None
    for s in strikes:
        call_loss = sum(max(0, s - k) * oi for k, oi in calls.items())
        put_loss = sum(max(0, k - s) * oi for k, oi in puts.items())
        loss = call_loss + put_loss
        if best_loss is None or loss < best_loss:
            best_s, best_loss = s, loss
    return int(best_s) if best_s is not None else None


def side_wall_base(g, basis, cp):
    x = g[g["cp"] == cp].groupby("strike", as_index=False)["oi"].sum()
    if x.empty:
        return x

    if basis is None or math.isnan(basis):
        x["distance"] = 0
        x["score"] = x["oi"]
        return x

    x["distance"] = (x["strike"] - basis).abs().clip(lower=1)
    x["score"] = x["oi"] / (1 + x["distance"] / 500)

    if cp == "C":
        side = x[x["strike"] >= basis].copy()
    else:
        side = x[x["strike"] <= basis].copy()

    near = side[side["strike"].sub(basis).abs() <= MAX_WALL_DISTANCE].copy()
    use = near if not near.empty else side
    if use.empty:
        use = x
    return use


def wall_table(g, basis, cp):
    use = side_wall_base(g, basis, cp)
    if use.empty:
        return use
    return use.sort_values(["score", "oi"], ascending=False).head(TOP_N_WALLS)


def wall_strength(g, basis, cp, walls):
    use = side_wall_base(g, basis, cp)
    if use.empty or walls is None or walls.empty:
        return None
    max_score = float(use["score"].max())
    if max_score <= 0:
        return None
    wall_score = float(walls["score"].max())
    return round(min(100, max(0, wall_score / max_score * 100)))


def zone_from_walls(walls):
    if walls is None or walls.empty:
        return None
    lo, hi = int(walls["strike"].min()), int(walls["strike"].max())
    return str(lo) if lo == hi else f"{lo}～{hi}"


def compute_delta_table(g, prev_df):
    cur = g[["expiry", "cp", "strike", "oi"]].copy()
    cur["strike"] = cur["strike"].astype(int)

    if prev_df is None or prev_df.empty:
        cur["prev_oi"] = 0
        cur["oi_change"] = None
        return cur

    prev = prev_df[["expiry", "cp", "strike", "oi"]].copy()
    prev["strike"] = prev["strike"].astype(int)
    prev = prev.rename(columns={"oi": "prev_oi"})

    merged = cur.merge(prev, on=["expiry", "cp", "strike"], how="left")
    merged["prev_oi"] = merged["prev_oi"].fillna(0)
    merged["oi_change"] = merged["oi"] - merged["prev_oi"]
    return merged


def format_strike_list(df, label_empty="首次記錄"):
    if df is None or df.empty or "oi_change" not in df.columns or df["oi_change"].isna().all():
        return label_empty
    out = []
    for _, r in df.head(3).iterrows():
        out.append(f"{int(r['strike'])}({int(r['oi_change']):+d})")
    return "、".join(out) if out else "-"


def oi_change_summary(g, basis, prev_df):
    d = compute_delta_table(g, prev_df)

    if d["oi_change"].isna().all():
        return {
            "新增Call壓力": "首次記錄",
            "新增Put支撐": "首次記錄",
            "Call撤退": "首次記錄",
            "Put撤退": "首次記錄",
        }

    if basis is None or math.isnan(basis):
        c_side = d[d["cp"] == "C"].copy()
        p_side = d[d["cp"] == "P"].copy()
    else:
        c_side = d[(d["cp"] == "C") & (d["strike"] >= basis) & (d["strike"].sub(basis).abs() <= MAX_WALL_DISTANCE)].copy()
        p_side = d[(d["cp"] == "P") & (d["strike"] <= basis) & (d["strike"].sub(basis).abs() <= MAX_WALL_DISTANCE)].copy()
        if c_side.empty:
            c_side = d[(d["cp"] == "C") & (d["strike"] >= basis)].copy()
        if p_side.empty:
            p_side = d[(d["cp"] == "P") & (d["strike"] <= basis)].copy()

    new_call = c_side[c_side["oi_change"] > 0].sort_values("oi_change", ascending=False)
    new_put = p_side[p_side["oi_change"] > 0].sort_values("oi_change", ascending=False)
    out_call = c_side[c_side["oi_change"] < 0].sort_values("oi_change")
    out_put = p_side[p_side["oi_change"] < 0].sort_values("oi_change")

    return {
        "新增Call壓力": format_strike_list(new_call),
        "新增Put支撐": format_strike_list(new_put),
        "Call撤退": format_strike_list(out_call, "-"),
        "Put撤退": format_strike_list(out_put, "-"),
    }


def analyze(expiry, g, basis, basis_src, txf, txf_src, prev_oi_df=None):
    call_oi = int(g[g["cp"] == "C"]["oi"].sum())
    put_oi = int(g[g["cp"] == "P"]["oi"].sum())

    calls = wall_table(g, basis, "C")
    puts = wall_table(g, basis, "P")

    if prev_oi_df is not None and not prev_oi_df.empty:
        prev_for_exp = prev_oi_df[prev_oi_df["expiry"].astype(str).eq(str(expiry))].copy()
    else:
        prev_for_exp = pd.DataFrame()

    change = oi_change_summary(g, basis, prev_for_exp)

    return {
        "合約": expiry,
        "結算方向基準": round(basis) if basis else None,
        "結算基準來源": basis_src,
        "TXF參考": round(txf) if txf else None,
        "TXF來源": txf_src,
        "結算靠近價": calc_max_pain(g),
        "上方大量區": zone_from_walls(calls),
        "下方大量區": zone_from_walls(puts),
        "Call OI": call_oi,
        "Put OI": put_oi,
        "OI PCR": round(put_oi / call_oi, 2) if call_oi else None,
        **change,
    }



def parse_zone(z):
    if not z:
        return None, None
    s = str(z)
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


def zone_overlap(a, b):
    a1, a2 = parse_zone(a)
    b1, b2 = parse_zone(b)
    if a1 is None or b1 is None:
        return None
    lo = max(a1, b1)
    hi = min(a2, b2)
    if lo <= hi:
        return str(lo) if lo == hi else f"{lo}～{hi}"
    return None


def confluence_lines(rows):
    base = [r for r in rows if r["合約"] != "近端綜合"]
    outs = []
    seen = set()
    for field, label in [("上方大量區", "共振壓力"), ("下方大量區", "共振支撐")]:
        for i in range(len(base)):
            for j in range(i + 1, len(base)):
                ov = zone_overlap(base[i].get(field), base[j].get(field))
                if not ov:
                    continue
                key = (field, ov)
                if key in seen:
                    continue
                seen.add(key)
                contracts = [base[i]["合約"], base[j]["合約"]]
                for k in range(len(base)):
                    if k in [i, j]:
                        continue
                    if zone_overlap(ov, base[k].get(field)):
                        contracts.append(base[k]["合約"])
                outs.append(f"{ov}：{' + '.join(dict.fromkeys(contracts))} {label}")
    return outs[:6] if outs else ["目前近端合約沒有明顯共振大量區。"]


def pick_primary(rows):
    for r in rows:
        if re.search(r"F1\b", str(r["合約"]).upper()):
            return r
    for r in rows:
        if parse_week_tag(r["合約"])[0] in ["W", "F"]:
            return r
    return rows[0] if rows else None


def direction(basis, mp):
    if mp is None or basis is None or math.isnan(basis):
        return "中性"
    diff = mp - basis
    if abs(diff) <= 100:
        return "目前接近"
    return "偏上靠" if diff > 0 else "偏下靠"


def main_zones(rows):
    near = next((r for r in rows if r["合約"] == "近端綜合"), None)
    months = [r for r in rows if parse_week_tag(r["合約"])[0] == "M" and r["合約"] != "近端綜合"]
    if near:
        up_main, dn_main = near["上方大量區"], near["下方大量區"]
    elif rows:
        up_main, dn_main = rows[0]["上方大量區"], rows[0]["下方大量區"]
    else:
        up_main = dn_main = None
    if months:
        up_2, dn_2 = months[0]["上方大量區"], months[0]["下方大量區"]
    else:
        up_2, dn_2 = up_main, dn_main
    return up_main, up_2, dn_main, dn_2



def primary_option_flow(primary, ref):
    """
    用 OI 增減與大量區位置，推估 Sell Call / Buy Call / Sell Put / Buy Put。
    注意：這是推估，不是真實逐筆買賣方。
    """
    if not primary:
        return {
            "上方Call": "資料不足",
            "下方Put": "資料不足",
            "買方追價": "資料不足",
            "避險力道": "資料不足",
            "主要SellCall區": "-",
            "主要SellPut區": "-",
            "可能BuyCall區": "-",
            "可能BuyPut區": "-",
            "整體結構": "資料不足",
        }

    new_call = str(primary.get("新增Call壓力", "-"))
    new_put = str(primary.get("新增Put支撐", "-"))
    out_call = str(primary.get("Call撤退", "-"))
    out_put = str(primary.get("Put撤退", "-"))

    sell_call_zone = (ref or primary).get("上方大量區")
    sell_put_zone = (ref or primary).get("下方大量區")

    # Sell Call / Buy Call
    if new_call and new_call not in ["-", "首次記錄", "None"]:
        upper_call = "Sell Call 增強"
    elif out_call and out_call not in ["-", "首次記錄", "None"]:
        upper_call = "Sell Call 撤退，偏 Buy Call / 軋空風險"
    else:
        upper_call = "中性"

    # Sell Put / Buy Put
    if new_put and new_put not in ["-", "首次記錄", "None"]:
        lower_put = "Sell Put 防守"
    elif out_put and out_put not in ["-", "首次記錄", "None"]:
        lower_put = "Sell Put 撤退，偏 Buy Put / 殺盤風險"
    else:
        lower_put = "中性"

    if "Sell Call 增強" in upper_call and "Sell Put 防守" in lower_put:
        structure = "區間賣方控盤"
    elif "撤退" in upper_call and "Sell Put 防守" in lower_put:
        structure = "偏多突破 / 軋空風險"
    elif "Sell Call 增強" in upper_call and "撤退" in lower_put:
        structure = "偏空破防 / 殺盤風險"
    elif "首次記錄" in new_call or "首次記錄" in new_put:
        structure = "首次記錄，先看大量區"
    else:
        structure = "中性 / 等待OI變化確認"

    return {
        "上方Call": upper_call,
        "下方Put": lower_put,
        "買方追價": "Buy Call 觀察：" + (out_call if out_call not in ["None", ""] else "-"),
        "避險力道": "Buy Put 觀察：" + (out_put if out_put not in ["None", ""] else "-"),
        "主要SellCall區": sell_call_zone,
        "主要SellPut區": sell_put_zone,
        "可能BuyCall區": out_call if out_call not in ["None", ""] else "-",
        "可能BuyPut區": out_put if out_put not in ["None", ""] else "-",
        "整體結構": structure,
    }

def fmt_line(cols, widths):
    return "  ".join([(str(c) if c is not None else "-").ljust(w) for c, w in zip(cols, widths)])


def make_report(rows, basis, basis_src, txf, txf_src, selected, all_exp):
    now = now_taipei()
    primary = pick_primary(rows)
    near = next((r for r in rows if r["合約"] == "近端綜合"), None)
    ref = near or primary
    up_main, up_2, dn_main, dn_2 = main_zones(rows)
    flow = primary_option_flow(primary, ref)

    lines = []
    lines.append("台指選擇權籌碼雷達 v1.8")
    lines.append(f"資料日：{now.strftime('%Y/%m/%d')}")
    lines.append(f"最後更新時間：{now.strftime('%Y/%m/%d %H:%M')}")
    lines.append(f"結算方向基準：加權指數 {round(basis) if basis else None}（{basis_src}）")
    lines.append(f"期貨參考：TXF {round(txf) if txf else None}（{txf_src}）")
    lines.append("")
    lines.append("【今日總結】")
    if primary and ref:
        lines.append(
            f"短線以 {primary['合約']} 為主，結算靠近價約 {primary['結算靠近價']}。"
            f"以加權位置判斷：{direction(basis, primary['結算靠近價'])}。"
            f"上方 {ref['上方大量區']} 是主要大量壓力區；"
            f"下方 {ref['下方大量區']} 是主要大量支撐區。"
        )
        lines.append(
            f"今日新增壓力：{primary.get('新增Call壓力')}；今日新增支撐：{primary.get('新增Put支撐')}。"
        )
    else:
        lines.append("資料不足，暫時不做方向判斷。")
    lines.append("")
    lines.append("【近端結算重點】")
    widths = [12, 10, 14, 14]
    lines.append(fmt_line(["合約", "結算靠近價", "上方大量區", "下方大量區"], widths))
    for r in rows:
        if r["合約"] == "近端綜合":
            continue
        lines.append(fmt_line([
            r["合約"], r["結算靠近價"], r["上方大量區"], r["下方大量區"]
        ], widths))
    lines.append("")
    lines.append("【選擇權買賣推估】")
    lines.append(f"上方 Call：{flow['上方Call']}")
    lines.append(f"下方 Put：{flow['下方Put']}")
    lines.append(f"買方追價：{flow['買方追價']}")
    lines.append(f"避險力道：{flow['避險力道']}")
    lines.append(f"主要 Sell Call 區：{flow['主要SellCall區']}")
    lines.append(f"主要 Sell Put 區：{flow['主要SellPut區']}")
    lines.append(f"可能 Buy Call 區：{flow['可能BuyCall區']}")
    lines.append(f"可能 Buy Put 區：{flow['可能BuyPut區']}")
    lines.append(f"整體結構：{flow['整體結構']}")
    lines.append("")
    lines.append("【大量區】")
    lines.append(f"上方主要大量區：{up_main}")
    lines.append(f"上方次要大量區：{up_2}")
    lines.append(f"下方主要大量區：{dn_main}")
    lines.append(f"下方次要大量區：{dn_2}")
    lines.append("")
    lines.append("【OI 變化】")
    if primary:
        lines.append(f"{primary['合約']} 新增Call壓力：{primary.get('新增Call壓力')}")
        lines.append(f"{primary['合約']} 新增Put支撐：{primary.get('新增Put支撐')}")
        lines.append(f"{primary['合約']} Call撤退：{primary.get('Call撤退')}")
        lines.append(f"{primary['合約']} Put撤退：{primary.get('Put撤退')}")
    else:
        lines.append("資料不足")
    lines.append("")
    lines.append("【破防價】")
    if primary:
        _, up_break = parse_zone(primary["上方大量區"])
        dn_break, _ = parse_zone(primary["下方大量區"])
        lines.append(f"站上 {up_break}：{primary['合約']} 上方大量區可能失效，容易往下一層壓力推進。")
        lines.append(f"跌破 {dn_break}：{primary['合約']} 下方大量區可能失效，容易往下一層支撐測試。")
    else:
        lines.append("破防價：資料不足")
    lines.append("")
    lines.append("【結算靠近方向】")
    for r in rows:
        if r["合約"] == "近端綜合":
            continue
        lines.append(f"{r['合約']}：靠 {r['結算靠近價']} 附近，{direction(basis, r['結算靠近價'])}")
    return "\n".join(lines)


def run_analysis(prev_oi_df=None):
    opt_raw = fetch_csv(OPT_URL)
    df = clean_option_data(opt_raw)
    if df.empty:
        report = "沒有抓到 TXO 台指選擇權資料。請檢查期交所資料是否已更新。"
        print(report)
        return pd.DataFrame(), report, df, []

    taiex, taiex_src = get_taiex_price()
    txf, txf_src = get_txf_price()
    if taiex is not None:
        basis, basis_src = taiex, taiex_src
    elif txf is not None:
        basis, basis_src = txf, f"加權抓取失敗，暫用 {txf_src}"
    else:
        basis, basis_src = float(df["strike"].median()), "加權/TXF皆抓取失敗，暫用履約價中位數"

    all_exp = sorted(df["expiry"].dropna().unique(), key=sort_key)
    selected = filter_expiries(all_exp)

    rows = []
    for exp in selected:
        g = df[df["expiry"] == exp].copy()
        rows.append(analyze(exp, g, basis, basis_src, txf, txf_src, prev_oi_df))

    near_df = df[df["expiry"].isin(selected)].copy()
    if not near_df.empty:
        rows.append(analyze("近端綜合", near_df, basis, basis_src, txf, txf_src, prev_oi_df))

    summary = pd.DataFrame(rows)
    summary.to_csv("txo_option_radar_summary_v18.csv", index=False, encoding="utf-8-sig")
    report = make_report(rows, basis, basis_src, txf, txf_src, selected, all_exp)
    print(report)
    return summary, report, df, selected


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


def read_previous_oi(sh):
    try:
        ws = sh.worksheet("OI原始紀錄")
        vals = ws.get_all_values()
        if len(vals) <= 1:
            return pd.DataFrame()
        df = pd.DataFrame(vals[1:], columns=vals[0])
        if df.empty or "run_time" not in df.columns:
            return pd.DataFrame()
        df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
        df["oi"] = pd.to_numeric(df["oi"], errors="coerce")
        df = df.dropna(subset=["strike", "oi"])
        df["strike"] = df["strike"].astype(int)
        latest = df["run_time"].max()
        return df[df["run_time"] == latest][["expiry", "cp", "strike", "oi"]].copy()
    except Exception:
        return pd.DataFrame()


def append_oi_history(sh, df, selected):
    ws = get_or_create_ws(sh, "OI原始紀錄", rows=2000, cols=10)
    if not ws.get_all_values():
        ws.append_row(["run_time", "date", "expiry", "cp", "strike", "oi"])

    run_time = now_taipei().strftime("%Y-%m-%d %H:%M:%S")
    cur = df[df["expiry"].isin(selected)].copy()
    rows = []
    for _, r in cur.iterrows():
        rows.append([run_time, str(r["date"]), str(r["expiry"]), str(r["cp"]), int(r["strike"]), int(r["oi"])])
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_sheet(sh, report, summary):
    ws = get_or_create_ws(sh, "今日報告", rows=300, cols=5)
    ws.clear()
    ws.update("A1", [[line] for line in report.splitlines()])

    ws2 = get_or_create_ws(sh, "近端結算表", rows=100, cols=30)
    ws2.clear()
    if summary is not None and not summary.empty:
        ws2.update("A1", [list(summary.columns)] + summary.astype(str).values.tolist())

    ws3 = get_or_create_ws(sh, "歷史紀錄", rows=1000, cols=30)
    if not ws3.get_all_values():
        ws3.append_row([
            "寫入時間", "資料日", "結算方向基準", "TXF參考", "近端綜合結算靠近價",
            "上方大量區", "下方大量區", "今日總結"
        ])

    near = None
    if summary is not None and not summary.empty:
        hit = summary[summary["合約"].astype(str).eq("近端綜合")]
        near = hit.iloc[0] if not hit.empty else summary.iloc[-1]

    summary_line = ""
    for line in report.splitlines():
        if line.startswith("短線以"):
            summary_line = line
            break

    if near is not None:
        ws3.append_row([
            now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
            now_taipei().strftime("%Y/%m/%d"),
            str(near.get("結算方向基準", "")),
            str(near.get("TXF參考", "")),
            str(near.get("結算靠近價", "")),
            str(near.get("上方大量區", "")),
            str(near.get("")),
            str(near.get("下方大量區", "")),
            str(near.get("")),
            summary_line,
        ])



def send_telegram_message(report):
    """
    Telegram 推播。
    若 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 沒設定，直接略過，不影響 Google Sheet。
    """
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
            chunk = f"台指選擇權籌碼雷達 分段 {i}/{len(chunks)}\\n\\n" + chunk

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code != 200:
                print(f"Telegram 推播失敗：{r.status_code} {r.text[:300]}")
            else:
                print("Telegram 推播成功。")
        except Exception as e:
            print(f"Telegram 推播例外：{type(e).__name__}: {e}")


def main():
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("找不到 GOOGLE_SHEET_ID")
    gc, email = get_gsheet_client()
    sh = gc.open_by_key(sheet_id)

    prev_oi = read_previous_oi(sh)
    summary, report, raw_df, selected = run_analysis(prev_oi)
    write_sheet(sh, report, summary)
    if raw_df is not None and not raw_df.empty and selected:
        append_oi_history(sh, raw_df, selected)
    print(f"已寫入 Google Sheet。Service account：{email}")
    send_telegram_message(report)


if __name__ == "__main__":
    main()
