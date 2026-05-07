#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
US Market After-hours Radar v0.4
- Runs after US cash market close and sends a beautified Telegram report.
- Designed for GitHub Actions at 07:00 Taiwan time, Tue-Sat (23:00 UTC Mon-Fri).

Data source: Yahoo Finance endpoints / yfinance fallback.
This is for market monitoring only, not investment advice.
"""

import os
import math
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")
NY_TZ = ZoneInfo("America/New_York")

APP_NAME = "美股盤後雷達 v0.4"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

# Yahoo symbols
WATCHLIST = [
    {"label": "小娜斯達克", "symbols": ["MNQ=F", "NQ=F"]},
    {"label": "費半指數", "symbols": ["^SOX"]},
    {"label": "TSM ADR", "symbols": ["TSM"]},
    {"label": "NVDA", "symbols": ["NVDA"]},
]

# Sector fallback for high-frequency names. Yahoo assetProfile will fill more names when available.
SECTOR_FALLBACK = {
    "NVDA": "AI晶片", "AMD": "AI晶片", "AVGO": "AI晶片", "ARM": "AI晶片",
    "INTC": "半導體", "MU": "記憶體", "TSM": "半導體", "ASML": "半導體", "QCOM": "手機晶片", "SMCI": "AI伺服器",
    "AAPL": "大型科技", "MSFT": "大型科技", "GOOGL": "大型科技", "GOOG": "大型科技", "META": "大型科技",
    "AMZN": "電商雲端", "NFLX": "串流媒體", "ADBE": "軟體", "CRM": "軟體", "ORCL": "軟體", "NOW": "軟體",
    "TSLA": "電動車", "RIVN": "電動車", "LCID": "電動車", "NIO": "中概電動車", "XPEV": "中概電動車", "LI": "中概電動車",
    "JPM": "金融", "BAC": "金融", "WFC": "金融", "C": "金融", "GS": "金融", "MS": "金融",
    "UNH": "醫療保健", "LLY": "醫療保健", "NVO": "醫療保健", "PFE": "醫療保健", "MRNA": "生技醫療",
    "XOM": "能源", "CVX": "能源", "OXY": "能源", "COP": "能源",
    "WMT": "零售", "COST": "零售", "HD": "零售", "TGT": "零售",
    "DELL": "AI伺服器", "HPE": "AI伺服器", "ANET": "網通", "MRVL": "網通", "CSCO": "網通",
    "BA": "航太國防", "LMT": "航太國防", "RTX": "航太國防",
    "PLTR": "AI軟體", "SOUN": "AI軟體", "SOFI": "金融科技", "COIN": "加密概念", "MSTR": "加密概念", "MARA": "加密概念", "RIOT": "加密概念",
}


def now_tw() -> dt.datetime:
    return dt.datetime.now(TW_TZ)


def fmt_num(x, digits=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:,.{digits}f}"


def fmt_int(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{int(round(x)):,}"


def fmt_pct(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.2f}%"


def direction_emoji(change_pct: Optional[float]) -> str:
    if change_pct is None or (isinstance(change_pct, float) and math.isnan(change_pct)):
        return "⚪"
    if change_pct > 0.15:
        return "🟢"
    if change_pct < -0.15:
        return "🔴"
    return "⚪"


def volume_to_wan_shares(vol):
    if vol is None or pd.isna(vol):
        return "—"
    return f"{vol/10000:,.1f}萬股"


def amount_to_usd_wan(amount):
    if amount is None or pd.isna(amount):
        return "—"
    return f"{amount/10000:,.1f}萬美元"


def amount_to_usd_yi(amount):
    if amount is None or pd.isna(amount):
        return "—"
    yi = float(amount) / 100_000_000
    if yi >= 10:
        return f"{yi:,.1f}億"
    return f"{yi:,.2f}億"


def signed_num(x, digits=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:,.{digits}f}"


def short_name(name: str, max_len=18):
    name = str(name or "")
    return name if len(name) <= max_len else name[:max_len]


TAIWAN_REFERENCES = {
    "AI晶片": ["台積電", "世芯-KY", "創意", "智原"],
    "半導體": ["台積電", "聯發科", "聯電", "力積電"],
    "手機晶片": ["聯發科", "台積電", "穩懋", "宏捷科"],
    "AI伺服器": ["緯穎", "廣達", "緯創", "技嘉", "英業達"],
    "記憶體": ["南亞科", "華邦電", "群聯", "威剛"],
    "網通": ["智邦", "啟碁", "中磊", "台光電"],
    "大型科技": ["鴻海", "台達電", "大立光", "玉晶光"],
    "電商雲端": ["緯穎", "廣達", "仁寶", "英業達"],
    "軟體": ["緯穎", "廣達", "精誠", "緯創"],
    "AI軟體": ["緯穎", "廣達", "緯創", "凌群"],
    "電動車": ["貿聯-KY", "和大", "胡連", "致茂", "同致"],
    "中概電動車": ["貿聯-KY", "和大", "胡連", "台達電"],
    "金融": ["富邦金", "國泰金", "中信金", "兆豐金"],
    "金融科技": ["精誠", "凌群", "零壹", "敦陽科"],
    "能源": ["台塑化", "台塑", "南亞", "中石化"],
    "醫療保健": ["保瑞", "藥華藥", "美時", "中裕"],
    "生技醫療": ["保瑞", "藥華藥", "美時", "中裕"],
    "零售": ["統一超", "全家", "寶雅", "富邦媒"],
    "航太國防": ["漢翔", "寶一", "千附精密", "雷虎"],
    "加密概念": ["技嘉", "微星", "華擎", "撼訊"],
}


def taiwan_refs_for_sectors(sectors, limit=6):
    out = []
    for sec in sectors:
        for name in TAIWAN_REFERENCES.get(str(sec), []):
            if name not in out:
                out.append(name)
            if len(out) >= limit:
                return out
    return out


def market_tone(watch):
    score = 0
    sox_chg = None
    tsm_chg = None
    nvda_chg = None
    for r in watch:
        chg = r.get("chg")
        if chg is None or pd.isna(chg):
            continue
        label = r.get("label")
        if label == "小娜斯達克": score += 1 if chg > 0 else -1 if chg < 0 else 0
        if label == "費半指數":
            score += 1 if chg > 0 else -1 if chg < 0 else 0
            sox_chg = chg
        if label == "TSM ADR":
            score += 1 if chg > 0 else -1 if chg < 0 else 0
            tsm_chg = chg
        if label == "NVDA":
            score += 1 if chg > 0 else -1 if chg < 0 else 0
            nvda_chg = chg
    if score >= 3: tone = "偏強"
    elif score <= -3: tone = "偏弱"
    else: tone = "分歧"
    semi_ok = [x for x in [sox_chg, tsm_chg, nvda_chg] if x is not None and not pd.isna(x)]
    if semi_ok:
        semi_score = sum(1 if x > 0 else -1 if x < 0 else 0 for x in semi_ok)
        semi = "偏強" if semi_score >= 2 else "偏弱" if semi_score <= -2 else "分歧"
    else:
        semi = "資料不足"
    return tone, semi


def yahoo_quote(symbols: List[str]) -> Dict[str, dict]:
    if not symbols:
        return {}
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    params = {"symbols": ",".join(symbols), "fields": "regularMarketPrice,regularMarketChangePercent,regularMarketChange,regularMarketVolume,shortName,marketCap,quoteType"}
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        items = r.json().get("quoteResponse", {}).get("result", [])
        return {x.get("symbol"): x for x in items if x.get("symbol")}
    except Exception:
        return {}


def yf_fallback_quote(symbol: str) -> Optional[dict]:
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="5d", interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        last = hist.iloc[-1]
        prev_close = hist.iloc[-2]["Close"] if len(hist) >= 2 else last.get("Open", last["Close"])
        price = float(last["Close"])
        change = price - float(prev_close)
        pct = change / float(prev_close) * 100 if prev_close else None
        info = {}
        try:
            info = t.fast_info or {}
        except Exception:
            pass
        return {
            "symbol": symbol,
            "shortName": symbol,
            "regularMarketPrice": price,
            "regularMarketChange": change,
            "regularMarketChangePercent": pct,
            "regularMarketVolume": float(last.get("Volume", 0)),
        }
    except Exception:
        return None


def get_best_quote(symbols: List[str]) -> Tuple[str, Optional[dict]]:
    q = yahoo_quote(symbols)
    for s in symbols:
        item = q.get(s)
        if item and item.get("regularMarketPrice") is not None:
            return s, item
    for s in symbols:
        item = yf_fallback_quote(s)
        if item:
            return s, item
    return symbols[0], None


def get_watchlist_rows() -> List[dict]:
    rows = []
    for w in WATCHLIST:
        used_symbol, q = get_best_quote(w["symbols"])
        if not q:
            rows.append({"label": w["label"], "symbol": used_symbol, "price": None, "chg": None, "pct": None, "vol": None})
            continue
        rows.append({
            "label": w["label"],
            "symbol": used_symbol,
            "price": q.get("regularMarketPrice"),
            "chg": q.get("regularMarketChange"),
            "pct": q.get("regularMarketChangePercent"),
            "vol": q.get("regularMarketVolume"),
        })
    return rows


def get_yahoo_most_active(count=80) -> List[dict]:
    """Yahoo Finance predefined most-actives screener."""
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    params = {"scrIds": "most_actives", "count": count}
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=25)
        r.raise_for_status()
        quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
        return quotes or []
    except Exception:
        return []


def normalize_active_quotes(quotes: List[dict]) -> pd.DataFrame:
    rows = []
    for q in quotes:
        symbol = q.get("symbol")
        price = q.get("regularMarketPrice")
        vol = q.get("regularMarketVolume") or q.get("averageDailyVolume3Month")
        if not symbol or price is None or vol is None:
            continue
        quote_type = q.get("quoteType")
        # Keep equities and ADR-like names, drop ETFs/funds when quoteType says so.
        if quote_type and quote_type not in {"EQUITY"}:
            continue
        amount = float(price) * float(vol)
        rows.append({
            "symbol": symbol,
            "name": q.get("shortName") or q.get("longName") or symbol,
            "price": float(price),
            "chg": q.get("regularMarketChange"),
            "chg_pct": q.get("regularMarketChangePercent"),
            "volume": float(vol),
            "amount_usd": amount,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["symbol"]).sort_values("amount_usd", ascending=False)
    return df


def get_sector_for_symbol(symbol: str, cache: Dict[str, str]) -> str:
    if symbol in cache:
        return cache[symbol]
    if symbol in SECTOR_FALLBACK:
        cache[symbol] = SECTOR_FALLBACK[symbol]
        return cache[symbol]
    try:
        info = yf.Ticker(symbol).get_info()
        sector = info.get("sector") or info.get("industry") or "其他"
        mapping = {
            "Technology": "科技",
            "Communication Services": "通訊服務",
            "Consumer Cyclical": "非必需消費",
            "Financial Services": "金融",
            "Healthcare": "醫療保健",
            "Energy": "能源",
            "Industrials": "工業",
            "Consumer Defensive": "民生消費",
            "Basic Materials": "原物料",
            "Utilities": "公用事業",
            "Real Estate": "房地產",
        }
        cache[symbol] = mapping.get(sector, sector)
        return cache[symbol]
    except Exception:
        cache[symbol] = "其他"
        return "其他"


def build_sector_rank(df: pd.DataFrame, topn=10) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    sector_cache = {}
    # Limit lookups to top 40 by amount to keep GitHub Actions stable.
    d = df.head(40).copy()
    d["sector"] = [get_sector_for_symbol(s, sector_cache) for s in d["symbol"]]
    g = d.groupby("sector", as_index=False).agg(
        amount_usd=("amount_usd", "sum"),
        volume=("volume", "sum"),
        count=("symbol", "count"),
        leaders=("symbol", lambda x: ", ".join(list(x)[:5])),
    )
    return g.sort_values("amount_usd", ascending=False).head(topn)



# Direct US ticker -> Taiwan related supply chain references.
# Order is intentionally practical for Telegram: closest linkage first, then broader supply chain.
US_TW_SUPPLY_CHAIN = {
    "NVDA": ["台積電", "緯穎", "廣達", "緯創", "鴻海", "技嘉", "微星", "奇鋐", "雙鴻", "台達電"],
    "AMD": ["台積電", "華碩", "技嘉", "微星", "緯創", "廣達", "神達", "奇鋐"],
    "AVGO": ["台積電", "日月光投控", "景碩", "欣興", "台光電", "聯茂", "智邦"],
    "TSM": ["台積電", "日月光投控", "創意", "世芯-KY", "旺矽", "家登", "辛耘", "弘塑"],
    "ASML": ["台積電", "家登", "帆宣", "漢唐", "辛耘", "弘塑", "京鼎"],
    "ARM": ["台積電", "聯發科", "世芯-KY", "創意", "智原"],
    "SMCI": ["緯穎", "廣達", "緯創", "英業達", "神達", "營邦", "勤誠", "奇鋐", "雙鴻", "台達電"],
    "DELL": ["緯創", "仁寶", "廣達", "英業達", "緯穎", "台達電"],
    "HPE": ["廣達", "緯創", "英業達", "緯穎", "神達"],
    "AAPL": ["鴻海", "大立光", "玉晶光", "台積電", "台郡", "臻鼎-KY", "可成", "和碩"],
    "MSFT": ["緯穎", "廣達", "緯創", "仁寶", "英業達", "台達電"],
    "GOOGL": ["廣達", "緯創", "台積電", "台達電", "智邦", "台光電"],
    "GOOG": ["廣達", "緯創", "台積電", "台達電", "智邦", "台光電"],
    "META": ["廣達", "緯穎", "緯創", "台達電", "智邦", "奇鋐"],
    "AMZN": ["緯穎", "廣達", "緯創", "英業達", "仁寶", "台達電"],
    "TSLA": ["貿聯-KY", "台達電", "和大", "胡連", "致茂", "同致", "健和興"],
    "RIVN": ["貿聯-KY", "台達電", "和大", "胡連", "致茂"],
    "LI": ["貿聯-KY", "台達電", "和大", "胡連"],
    "NIO": ["貿聯-KY", "台達電", "和大", "胡連"],
    "XPEV": ["貿聯-KY", "台達電", "和大", "胡連"],
    "MU": ["南亞科", "華邦電", "群聯", "威剛", "創見", "十銓"],
    "WDC": ["群聯", "威剛", "創見", "十銓", "南亞科"],
    "STX": ["群聯", "威剛", "創見", "十銓"],
    "QCOM": ["台積電", "聯發科", "穩懋", "宏捷科", "日月光投控"],
    "INTC": ["台積電", "聯電", "日月光投控", "欣興", "景碩"],
    "MRVL": ["台積電", "智邦", "台光電", "聯茂", "欣興"],
    "ANET": ["智邦", "台光電", "聯茂", "台達電", "啟碁"],
    "CSCO": ["智邦", "啟碁", "中磊", "台達電", "台光電"],
    "PLTR": ["緯穎", "廣達", "緯創", "精誠", "凌群"],
    "SOUN": ["緯穎", "廣達", "緯創", "精誠"],
    "COIN": ["技嘉", "微星", "華擎", "撼訊"],
    "MSTR": ["技嘉", "微星", "華擎", "撼訊"],
}


def get_strong_weak(active_df: pd.DataFrame, topn=10):
    if active_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = active_df.copy()
    d["chg"] = pd.to_numeric(d.get("chg"), errors="coerce")
    strong = d[d["chg"] > 0].sort_values(["chg", "amount_usd"], ascending=[False, False]).head(topn)
    weak = d[d["chg"] < 0].sort_values(["chg", "amount_usd"], ascending=[True, False]).head(topn)
    return strong, weak


def supply_chain_top(symbols: List[str], limit=10) -> List[str]:
    out = []
    for sym in symbols:
        sym = str(sym).upper()
        # Direct mapping first for accuracy.
        refs = US_TW_SUPPLY_CHAIN.get(sym)
        if refs:
            candidates = refs
        else:
            # Fallback by sector, only when no direct ticker mapping exists.
            sec = SECTOR_FALLBACK.get(sym, "其他")
            candidates = TAIWAN_REFERENCES.get(sec, [])
        for name in candidates:
            if name not in out:
                out.append(name)
            if len(out) >= limit:
                return out
    return out


def build_inflow_sector_rank(active_df: pd.DataFrame, topn=10) -> pd.DataFrame:
    if active_df.empty:
        return pd.DataFrame()
    sector_cache = {}
    d = active_df.copy()
    d["chg"] = pd.to_numeric(d.get("chg"), errors="coerce")
    d = d[d["chg"] > 0].head(60).copy()
    if d.empty:
        return pd.DataFrame()
    d["sector"] = [get_sector_for_symbol(s, sector_cache) for s in d["symbol"]]
    g = d.groupby("sector", as_index=False).agg(
        amount_usd=("amount_usd", "sum"),
        leaders=("symbol", lambda x: " / ".join(list(x)[:4])),
        count=("symbol", "count"),
    )
    return g.sort_values("amount_usd", ascending=False).head(topn)


def format_rank_rows(df: pd.DataFrame, strong=True) -> List[str]:
    if df.empty:
        return ["資料暫時不足"]
    rows = []
    for i, row in enumerate(df.itertuples(index=False), 1):
        chg = signed_num(getattr(row, "chg", None), 2)
        rows.append(f"{i}. {row.symbol}｜{short_name(row.name, 16)}｜{chg}")
    return rows


def format_supply_rows(names: List[str]) -> List[str]:
    if not names:
        return ["資料暫時不足"]
    return [f"{i}. {name}" for i, name in enumerate(names[:10], 1)]



CROSS_MARKET_ITEMS = [
    {"label": "BTC", "symbols": ["BTC-USD"]},
    {"label": "貴金屬", "symbols": ["GC=F", "GLD"]},
    {"label": "原油", "symbols": ["CL=F", "USO"]},
    {"label": "美元", "symbols": ["DX-Y.NYB", "UUP"]},
    {"label": "美債殖利率", "symbols": ["^TNX"]},
]


def cross_tone(label: str, chg: Optional[float]) -> str:
    if chg is None or (isinstance(chg, float) and pd.isna(chg)):
        return "資料不足"
    if label == "美債殖利率":
        if chg > 0:
            return "上升"
        if chg < 0:
            return "下降"
        return "持平"
    if chg > 0:
        return "偏強"
    if chg < 0:
        return "偏弱"
    return "持平"


def get_cross_market_rows() -> List[dict]:
    rows = []
    for item in CROSS_MARKET_ITEMS:
        used_symbol, q = get_best_quote(item["symbols"])
        chg = None if not q else q.get("regularMarketChange")
        price = None if not q else q.get("regularMarketPrice")
        rows.append({
            "label": item["label"],
            "symbol": used_symbol,
            "price": price,
            "chg": chg,
            "tone": cross_tone(item["label"], chg),
        })
    return rows


def format_cross_rows(rows: List[dict]) -> List[str]:
    if not rows:
        return ["資料暫時不足"]
    out = []
    for r in rows:
        out.append(f"{r['label']}｜{r['tone']}｜{signed_num(r.get('chg'), 2)}")
    return out

def get_market_data():
    quotes = get_yahoo_most_active(count=120)
    active_df = normalize_active_quotes(quotes)
    strong_top, weak_top = get_strong_weak(active_df, 10)
    inflow_sector_top = build_inflow_sector_rank(active_df, 10)
    cross_rows = get_cross_market_rows()
    return active_df, strong_top, weak_top, inflow_sector_top, cross_rows


def make_report(active_df: pd.DataFrame, strong_top: pd.DataFrame, weak_top: pd.DataFrame, inflow_sector_top: pd.DataFrame, cross_rows: List[dict]) -> str:
    tw = now_tw()
    ny = tw.astimezone(NY_TZ)

    strong_tw = supply_chain_top(list(strong_top["symbol"]) if not strong_top.empty else [], limit=10)
    weak_tw = supply_chain_top(list(weak_top["symbol"]) if not weak_top.empty else [], limit=10)

    lines = []
    lines.append(f"🌙 {APP_NAME}")
    lines.append(f"📅 美股交易日：{ny:%Y-%m-%d}")
    lines.append(f"🕖 台灣更新時間：{tw:%Y-%m-%d %H:%M}")
    lines.append("")

    lines.append("━━━━━━━━━━━━━━")
    lines.append("🔥 美股交易 強勢 Top 10")
    lines.extend(format_rank_rows(strong_top, strong=True))

    lines.append("")
    lines.append("🇹🇼 強勢台股相關供應鏈 Top 10")
    lines.extend(format_supply_rows(strong_tw))

    lines.append("")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("🧊 美股交易 弱勢 Top 10")
    lines.extend(format_rank_rows(weak_top, strong=False))

    lines.append("")
    lines.append("🇹🇼 弱勢台股相關供應鏈 Top 10")
    lines.extend(format_supply_rows(weak_tw))

    lines.append("")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("🧭 資金流入熱門族群 Top 10")
    if inflow_sector_top.empty:
        lines.append("資料暫時不足")
    else:
        for i, row in enumerate(inflow_sector_top.itertuples(index=False), 1):
            lines.append(f"{i}. {row.sector}｜{row.leaders}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("🪙 跨市場追蹤")
    lines.extend(format_cross_rows(cross_rows))

    lines.append("")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("📊 結論")
    strong_symbols = set(strong_top["symbol"]) if not strong_top.empty else set()
    weak_symbols = set(weak_top["symbol"]) if not weak_top.empty else set()
    sectors = list(inflow_sector_top["sector"].head(3)) if not inflow_sector_top.empty else []
    main_axis = " / ".join(sectors) if sectors else "資料不足"

    ai_names = {"AI晶片", "AI伺服器", "AI軟體", "半導體", "記憶體", "網通"}
    ai_hot = any(sec in ai_names for sec in sectors) or bool(strong_symbols & {"NVDA", "AMD", "AVGO", "SMCI", "ARM", "PLTR"})
    semi_positive = bool(strong_symbols & {"NVDA", "AMD", "AVGO", "TSM", "ASML", "ARM", "MU", "QCOM"})
    semi_negative = bool(weak_symbols & {"NVDA", "AMD", "AVGO", "TSM", "ASML", "ARM", "MU", "QCOM"})
    tw_focus = supply_chain_top(list(strong_top["symbol"]) if not strong_top.empty else [], limit=6)

    lines.append(f"主軸：{main_axis}")
    if semi_positive and not semi_negative:
        lines.append("半導體：偏強")
    elif semi_negative and not semi_positive:
        lines.append("半導體：偏弱")
    elif semi_positive and semi_negative:
        lines.append("半導體：分歧")
    else:
        lines.append("半導體：觀察")
    lines.append(f"AI：{'資金集中' if ai_hot else '觀察量能'}")
    cross_map = {r.get("label"): r.get("tone") for r in cross_rows}
    btc_tone = cross_map.get("BTC", "資料不足")
    metal_tone = cross_map.get("貴金屬", "資料不足")
    oil_tone = cross_map.get("原油", "資料不足")
    risk_pref = "偏強" if btc_tone == "偏強" and metal_tone != "偏強" else "避險升溫" if metal_tone == "偏強" else "觀察"
    lines.append(f"風險偏好：{risk_pref}")
    lines.append(f"BTC：{btc_tone}｜貴金屬：{metal_tone}｜原油：{oil_tone}")
    lines.append(f"台股關注：{' / '.join(tw_focus) if tw_focus else '資料不足'}")
    lines.append("說明：台股供應鏈依美股標的直接關聯與族群映射排序，僅供盤後觀察。")
    return "\n".join(lines)

def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[Telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skip sending.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram text limit roughly 4096 chars; split just in case.
    chunks = []
    cur = ""
    for line in text.splitlines():
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + ("\n" if cur else "") + line
    if cur:
        chunks.append(cur)
    for c in chunks:
        r = requests.post(url, data={"chat_id": chat_id, "text": c, "disable_web_page_preview": True}, timeout=20)
        if not r.ok:
            print(f"[Telegram] send failed: {r.status_code} {r.text}")
        else:
            print("[Telegram] sent")
        time.sleep(0.5)


def save_csv(strong_top: pd.DataFrame, weak_top: pd.DataFrame, inflow_sector_top: pd.DataFrame):
    strong_top.to_csv("us_strong_top10_v04.csv", index=False, encoding="utf-8-sig")
    weak_top.to_csv("us_weak_top10_v04.csv", index=False, encoding="utf-8-sig")
    inflow_sector_top.to_csv("us_inflow_sector_top10_v04.csv", index=False, encoding="utf-8-sig")


def main():
    active_df, strong_top, weak_top, inflow_sector_top, cross_rows = get_market_data()
    report = make_report(active_df, strong_top, weak_top, inflow_sector_top, cross_rows)
    print(report)
    save_csv(strong_top, weak_top, inflow_sector_top)
    send_telegram(report)

if __name__ == "__main__":
    main()
