#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
US Market After-hours Radar v0.6
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

APP_NAME = "美股盤後雷達 v0.6"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

# 台股盤前參考用：強弱股採中嚴格篩選，避免低流動性或小漲小跌干擾。
STRICT_PCT = 1.5
STRICT_AMOUNT_USD = 300_000_000
STRICT_VOLUME = 3_000_000
FALLBACK_PCT = 1.0
FALLBACK_AMOUNT_USD = 200_000_000
FALLBACK_VOLUME = 2_000_000


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
    """
    台股盤前版強弱篩選：
    先用中嚴格條件：漲跌幅 ±1.5%、成交金額 3 億美元、成交量 300 萬股。
    若資料不足，再放寬到 ±1%、成交金額 2 億美元、成交量 200 萬股。
    排序以漲跌幅為主，成交金額為輔。
    """
    if active_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    d = active_df.copy()
    d["chg_pct"] = pd.to_numeric(d.get("chg_pct"), errors="coerce")
    d["amount_usd"] = pd.to_numeric(d.get("amount_usd"), errors="coerce")
    d["volume"] = pd.to_numeric(d.get("volume"), errors="coerce")
    d = d.dropna(subset=["chg_pct", "amount_usd", "volume"])

    def pick(pct_th, amt_th, vol_th):
        strong = d[(d["chg_pct"] >= pct_th) & (d["amount_usd"] >= amt_th) & (d["volume"] >= vol_th)]
        weak = d[(d["chg_pct"] <= -pct_th) & (d["amount_usd"] >= amt_th) & (d["volume"] >= vol_th)]
        strong = strong.sort_values(["chg_pct", "amount_usd"], ascending=[False, False]).head(topn)
        weak = weak.sort_values(["chg_pct", "amount_usd"], ascending=[True, False]).head(topn)
        return strong, weak

    strong, weak = pick(STRICT_PCT, STRICT_AMOUNT_USD, STRICT_VOLUME)
    if len(strong) < 3 or len(weak) < 3:
        strong2, weak2 = pick(FALLBACK_PCT, FALLBACK_AMOUNT_USD, FALLBACK_VOLUME)
        if len(strong) < 3:
            strong = strong2
        if len(weak) < 3:
            weak = weak2

    return strong.head(topn), weak.head(topn)


def weighted_supply_chain_top(df: pd.DataFrame, limit=10, weak=False) -> List[dict]:
    """
    台股盤前版供應鏈排序：
    美股漲跌幅 × 成交金額 × 供應鏈關聯順序。
    輸出：台股名稱、關聯等級、主要連動美股。
    """
    if df is None or df.empty:
        return []

    scores = {}
    links = {}
    d = df.copy()
    d["chg_pct"] = pd.to_numeric(d.get("chg_pct"), errors="coerce")
    d["amount_usd"] = pd.to_numeric(d.get("amount_usd"), errors="coerce")
    d = d.dropna(subset=["chg_pct", "amount_usd"])

    for _, r in d.iterrows():
        sym = str(r.get("symbol", "")).upper()
        pct = abs(float(r.get("chg_pct", 0)))
        amt = max(float(r.get("amount_usd", 0)), 1)
        amount_weight = min(3.0, math.log10(amt) / 3.0)

        refs = US_TW_SUPPLY_CHAIN.get(sym)
        if not refs:
            sec = SECTOR_FALLBACK.get(sym, "其他")
            refs = TAIWAN_REFERENCES.get(sec, [])

        for pos, name in enumerate(refs[:10]):
            relation_weight = max(1.0, 10 - pos)
            score = pct * amount_weight * relation_weight
            scores[name] = scores.get(name, 0.0) + score
            links.setdefault(name, [])
            if sym not in links[name]:
                links[name].append(sym)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    out = []
    for name, score in ranked:
        if score >= 80:
            level = "風險連動" if weak else "高關聯"
        elif score >= 35:
            level = "觀察"
        else:
            level = "低關聯"
        out.append({"name": name, "level": level, "sources": links.get(name, [])[:3], "score": round(score, 1)})
    return out


def supply_chain_top(symbols: List[str], limit=10) -> List[str]:
    """保留舊介面，供少數 fallback 使用。"""
    out = []
    for sym in symbols:
        sym = str(sym).upper()
        refs = US_TW_SUPPLY_CHAIN.get(sym)
        if refs:
            candidates = refs
        else:
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
    """強弱排行只顯示簡寫名稱 + 漲幅%，不顯示全名與點數。"""
    if df.empty:
        return ["資料暫時不足"]
    rows = []
    for i, row in enumerate(df.itertuples(index=False), 1):
        pct = fmt_pct(getattr(row, "chg_pct", None))
        rows.append(f"{i}. {row.symbol}｜{pct}")
    return rows


def format_supply_rows(items) -> List[str]:
    """
    精簡版：只顯示台股名稱與關聯等級。
    """
    if not items:
        return ["資料暫時不足"]
    rows = []
    for i, item in enumerate(items[:10], 1):
        if isinstance(item, dict):
            rows.append(f"{i}. {item.get('name')}｜{item.get('level')}")
        else:
            rows.append(f"{i}. {item}")
    return rows


def premarket_bias_lines(strong_top: pd.DataFrame, weak_top: pd.DataFrame, cross_rows: List[dict]) -> List[str]:
    sectors = ["半導體", "AI伺服器", "記憶體", "電動車", "網通", "金融"]
    score = {s: 0.0 for s in sectors}

    def add_scores(df, sign):
        if df is None or df.empty:
            return
        for _, r in df.iterrows():
            sym = str(r.get("symbol", "")).upper()
            sec = SECTOR_FALLBACK.get(sym, "其他")
            pct = abs(float(r.get("chg_pct", 0) or 0))
            # AI 晶片與大型科技對台股科技權值影響較高。
            mapped = []
            if sec in ["AI晶片", "半導體", "手機晶片"]:
                mapped += ["半導體"]
            if sec in ["AI伺服器", "電商雲端", "大型科技", "AI軟體"]:
                mapped += ["AI伺服器"]
            if sec == "記憶體":
                mapped += ["記憶體"]
            if sec in ["電動車", "中概電動車"]:
                mapped += ["電動車"]
            if sec == "網通":
                mapped += ["網通"]
            if sec in ["金融", "金融科技"]:
                mapped += ["金融"]
            for m in set(mapped):
                if m in score:
                    score[m] += sign * pct

    add_scores(strong_top, 1)
    add_scores(weak_top, -1)

    cross_map = {r.get("label"): r.get("tone") for r in cross_rows or []}
    risk_pref = "中性"
    if cross_map.get("BTC") == "偏強" and cross_map.get("貴金屬") != "偏強":
        risk_pref = "偏強"
    elif cross_map.get("貴金屬") == "偏強" or cross_map.get("美債殖利率") == "上升":
        risk_pref = "偏保守"

    out = []
    for sec in sectors:
        v = score.get(sec, 0.0)
        if v >= 2.0:
            tone = "偏強"
        elif v <= -2.0:
            tone = "偏弱"
        elif abs(v) > 0:
            tone = "分歧"
        else:
            tone = "觀察"
        out.append(f"{sec}：{tone}")
    out.append(f"風險偏好：{risk_pref}")
    return out



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


# ============================================================
# 台股族群盤前雷達：廣泛版，輸出仍維持 Top 10
# ============================================================

TAIWAN_GROUP_RADAR = {
    "半導體權值": {
        "stocks": ["台積電", "聯發科", "日月光投控", "聯電", "創意", "世芯-KY"],
        "drivers": ["TSM", "NVDA", "AMD", "AVGO", "ASML", "ARM", "QCOM", "INTC", "^SOX"],
    },
    "IC設計": {
        "stocks": ["聯發科", "創意", "世芯-KY", "智原", "瑞昱", "祥碩"],
        "drivers": ["NVDA", "AMD", "ARM", "QCOM", "MRVL", "AVGO", "INTC"],
    },
    "晶圓代工 / 封測": {
        "stocks": ["台積電", "聯電", "力積電", "日月光投控", "京元電子", "矽格"],
        "drivers": ["TSM", "INTC", "ASML", "AMD", "NVDA", "AVGO", "^SOX"],
    },
    "半導體設備 / 材料": {
        "stocks": ["家登", "辛耘", "弘塑", "帆宣", "漢唐", "京鼎", "旺矽"],
        "drivers": ["ASML", "TSM", "AMAT", "LRCX", "KLAC", "TER", "^SOX"],
    },
    "AI伺服器": {
        "stocks": ["緯穎", "廣達", "緯創", "英業達", "神達", "營邦"],
        "drivers": ["NVDA", "SMCI", "DELL", "HPE", "MSFT", "GOOGL", "META", "AMZN"],
    },
    "伺服器代工": {
        "stocks": ["廣達", "緯創", "英業達", "仁寶", "神達", "緯穎"],
        "drivers": ["SMCI", "DELL", "HPE", "MSFT", "AMZN", "GOOGL", "META"],
    },
    "散熱": {
        "stocks": ["奇鋐", "雙鴻", "健策", "建準", "力致", "尼得科超眾"],
        "drivers": ["NVDA", "SMCI", "DELL", "AMD", "AVGO", "MSFT"],
    },
    "電源 / UPS": {
        "stocks": ["台達電", "光寶科", "群電", "康舒", "崇越電", "飛宏"],
        "drivers": ["NVDA", "SMCI", "DELL", "HPE", "TSLA", "MSFT", "AMZN"],
    },
    "PCB / CCL": {
        "stocks": ["台光電", "聯茂", "欣興", "金像電", "臻鼎-KY", "健鼎"],
        "drivers": ["AVGO", "NVDA", "AMD", "MRVL", "ANET", "AAPL", "TSM"],
    },
    "ABF / 載板": {
        "stocks": ["欣興", "景碩", "南電", "臻鼎-KY"],
        "drivers": ["NVDA", "AMD", "AVGO", "INTC", "TSM", "^SOX"],
    },
    "記憶體 / NAND": {
        "stocks": ["南亞科", "華邦電", "群聯", "威剛", "創見", "十銓"],
        "drivers": ["MU", "WDC", "STX", "NVDA", "AMD"],
    },
    "網通": {
        "stocks": ["智邦", "啟碁", "中磊", "明泰", "正文", "台光電"],
        "drivers": ["ANET", "CSCO", "MRVL", "AVGO", "AMZN", "GOOGL", "META"],
    },
    "光通訊": {
        "stocks": ["聯亞", "上詮", "華星光", "波若威", "眾達-KY", "光聖"],
        "drivers": ["ANET", "MRVL", "AVGO", "CSCO", "NVDA", "META", "GOOGL"],
    },
    "蘋概": {
        "stocks": ["鴻海", "大立光", "玉晶光", "台郡", "臻鼎-KY", "和碩"],
        "drivers": ["AAPL", "QCOM", "TSM", "AVGO"],
    },
    "電動車 / 車用": {
        "stocks": ["貿聯-KY", "台達電", "和大", "胡連", "致茂", "同致"],
        "drivers": ["TSLA", "RIVN", "LCID", "NIO", "XPEV", "LI"],
    },
    "機器人 / 自動化": {
        "stocks": ["上銀", "亞德客-KY", "盟立", "所羅門", "羅昇", "穎漢"],
        "drivers": ["TSLA", "NVDA", "ROK", "ISRG", "TER", "SYM"],
    },
    "航太 / 軍工": {
        "stocks": ["漢翔", "寶一", "千附精密", "雷虎", "長榮航太"],
        "drivers": ["BA", "LMT", "RTX", "NOC", "GD"],
    },
    "金融": {
        "stocks": ["富邦金", "國泰金", "中信金", "兆豐金", "元大金", "玉山金"],
        "drivers": ["JPM", "BAC", "WFC", "C", "GS", "MS", "^TNX"],
    },
    "航運": {
        "stocks": ["長榮", "陽明", "萬海", "長榮航", "華航", "裕民"],
        "drivers": ["FDX", "UPS", "DAL", "UAL", "AAL", "ZIM", "CL=F"],
    },
    "鋼鐵 / 原物料": {
        "stocks": ["中鋼", "中鴻", "燁輝", "大成鋼", "榮剛"],
        "drivers": ["X", "NUE", "CLF", "VALE", "BHP", "FCX"],
    },
    "塑化 / 能源": {
        "stocks": ["台塑", "南亞", "台化", "台塑化", "中石化"],
        "drivers": ["XOM", "CVX", "OXY", "COP", "CL=F"],
    },
    "重電 / 電網": {
        "stocks": ["華城", "士電", "中興電", "亞力", "大同", "東元"],
        "drivers": ["GE", "ETN", "ABB", "TSLA", "NVDA", "NEE"],
    },
    "綠能 / 儲能": {
        "stocks": ["元晶", "聯合再生", "茂迪", "中興電", "華城", "台達電"],
        "drivers": ["ENPH", "SEDG", "FSLR", "TSLA", "NEE"],
    },
    "營建 / 資產": {
        "stocks": ["華固", "長虹", "興富發", "國建", "潤泰新", "遠雄"],
        "drivers": ["^TNX", "VNQ", "JPM", "BAC"],
    },
    "生技醫療": {
        "stocks": ["保瑞", "藥華藥", "美時", "中裕", "台康生技", "智擎"],
        "drivers": ["LLY", "NVO", "UNH", "PFE", "MRNA", "BIIB"],
    },
    "觀光餐飲": {
        "stocks": ["王品", "瓦城", "雄獅", "鳳凰", "晶華", "雲品"],
        "drivers": ["BKNG", "ABNB", "MAR", "HLT", "MCD", "SBUX"],
    },
    "零售通路": {
        "stocks": ["統一超", "全家", "寶雅", "富邦媒", "潤泰全"],
        "drivers": ["WMT", "COST", "TGT", "HD", "AMZN"],
    },
    "遊戲 / 軟體": {
        "stocks": ["智冠", "橘子", "鈊象", "精誠", "凌群", "零壹"],
        "drivers": ["MSFT", "ADBE", "CRM", "NOW", "EA", "TTWO", "PLTR"],
    },
    "加密概念": {
        "stocks": ["技嘉", "微星", "華擎", "撼訊", "映泰"],
        "drivers": ["BTC-USD", "COIN", "MSTR", "MARA", "RIOT"],
    },
    "高股息 / ETF": {
        "stocks": ["0056", "00878", "00919", "00929", "00713"],
        "drivers": ["^TNX", "DX-Y.NYB", "SPY", "QQQ"],
    },
}


def build_core_driver_df(active_df: pd.DataFrame, watch_rows: List[dict], cross_rows: List[dict]) -> pd.DataFrame:
    """
    建立美股核心驅動資料。
    active_df 有資料就先用；不足的 driver 再用 Yahoo quote 補。
    """
    rows = []

    if active_df is not None and not active_df.empty:
        for _, r in active_df.iterrows():
            rows.append({
                "symbol": str(r.get("symbol", "")).upper(),
                "chg_pct": r.get("chg_pct"),
                "amount_usd": r.get("amount_usd", 0),
            })

    # watchlist 補：NQ / SOX / TSM / NVDA
    for r in watch_rows or []:
        rows.append({
            "symbol": str(r.get("symbol", "")).upper(),
            "chg_pct": r.get("pct"),
            "amount_usd": 5_000_000_000,
        })

    # 跨市場補：BTC / 原油 / 美債 / 美元
    for r in cross_rows or []:
        sym = str(r.get("symbol", "")).upper()
        chg = r.get("chg")
        rows.append({
            "symbol": sym,
            "chg_pct": chg,
            "amount_usd": 3_000_000_000,
        })

    have = {r["symbol"] for r in rows if r.get("symbol")}
    need = sorted({s.upper() for g in TAIWAN_GROUP_RADAR.values() for s in g.get("drivers", []) if s.upper() not in have})

    # 補固定觀察池，避免盤勢平淡時沒資料
    if need:
        q = yahoo_quote(need[:80])
        for sym, item in q.items():
            rows.append({
                "symbol": str(sym).upper(),
                "chg_pct": item.get("regularMarketChangePercent"),
                "amount_usd": (item.get("regularMarketPrice") or 0) * (item.get("regularMarketVolume") or 0),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["chg_pct"] = pd.to_numeric(df["chg_pct"], errors="coerce")
    df["amount_usd"] = pd.to_numeric(df["amount_usd"], errors="coerce").fillna(0)
    df = df.dropna(subset=["symbol", "chg_pct"])
    df = df.drop_duplicates(subset=["symbol"], keep="first")
    return df


def build_tw_group_radar(active_df: pd.DataFrame, watch_rows: List[dict], cross_rows: List[dict], topn=10) -> List[dict]:
    """
    台股族群盤前雷達：
    候選族群很多，但輸出維持 Top 10。
    分數 = 美股/跨市場驅動漲跌幅 × 成交金額權重 × 驅動關聯權重。
    """
    driver_df = build_core_driver_df(active_df, watch_rows, cross_rows)

    if driver_df is None or driver_df.empty:
        # 完全沒資料時仍給固定觀察族群
        fallback = list(TAIWAN_GROUP_RADAR.items())[:topn]
        return [
            {
                "group": k,
                "tone": "觀察",
                "drivers": "-",
                "stocks": "、".join(v.get("stocks", [])[:5]),
                "score": 0,
            }
            for k, v in fallback
        ]

    driver_map = {str(r["symbol"]).upper(): r for _, r in driver_df.iterrows()}
    out = []

    for group, cfg in TAIWAN_GROUP_RADAR.items():
        score = 0.0
        signed = 0.0
        used = []

        for pos, sym in enumerate(cfg.get("drivers", [])):
            sym = str(sym).upper()
            r = driver_map.get(sym)
            if r is None:
                continue

            pct = float(r.get("chg_pct", 0) or 0)
            amt = float(r.get("amount_usd", 0) or 0)
            amount_weight = min(3.0, max(1.0, math.log10(max(amt, 1)) / 3.5))
            relation_weight = max(1.0, 8 - pos)
            s = pct * amount_weight * relation_weight

            signed += s
            score += abs(s)
            used.append(sym)

        if signed >= 8:
            tone = "偏強"
        elif signed <= -8:
            tone = "偏弱"
        elif abs(signed) >= 3:
            tone = "分歧"
        else:
            tone = "觀察"

        out.append({
            "group": group,
            "tone": tone,
            "drivers": " / ".join(used[:4]) if used else "-",
            "stocks": "、".join(cfg.get("stocks", [])[:5]),
            "score": round(score, 1),
            "signed": round(signed, 1),
        })

    # 先看有動的族群，再以分數排序；仍維持 Top 10
    out = sorted(out, key=lambda x: (x["score"], abs(x["signed"])), reverse=True)
    return out[:topn]


def format_tw_group_radar(items: List[dict]) -> List[str]:
    """
    精簡版輸出：
    只顯示 族群｜偏向｜代表股，避免 Telegram 太亂。
    """
    if not items:
        return ["資料暫時不足"]
    rows = []
    for i, item in enumerate(items[:10], 1):
        stocks = str(item.get("stocks", ""))
        # 只保留前 3 檔代表股，避免太長
        stock_short = "、".join([x for x in stocks.split("、") if x][:3])
        rows.append(f"{i}. {item.get('group')}｜{item.get('tone')}｜{stock_short}")
    return rows



def expected_us_trade_day(tw=None) -> str:
    """以台灣 07:00 排程來看，預期為紐約前一個美股交易日。"""
    tw = tw or now_tw()
    ny_date = tw.astimezone(NY_TZ).date()
    # 若落在週末，往前推到週五。
    while ny_date.weekday() >= 5:
        ny_date -= dt.timedelta(days=1)
    return ny_date.strftime("%Y-%m-%d")


def data_update_status(active_df: pd.DataFrame, strong_top: pd.DataFrame, weak_top: pd.DataFrame, tw=None):
    tw = tw or now_tw()
    expected = expected_us_trade_day(tw)
    if active_df is None or active_df.empty:
        return "❌ 資料狀態：抓取失敗", expected
    if (strong_top is not None and not strong_top.empty) or (weak_top is not None and not weak_top.empty):
        return "✅ 資料狀態：已更新", expected
    return "⚠️ 資料狀態：資料不足", expected

def get_market_data():
    quotes = get_yahoo_most_active(count=120)
    active_df = normalize_active_quotes(quotes)
    strong_top, weak_top = get_strong_weak(active_df, 10)
    inflow_sector_top = build_inflow_sector_rank(active_df, 10)
    cross_rows = get_cross_market_rows()
    watch_rows = get_watchlist_rows()
    tw_group_radar = build_tw_group_radar(active_df, watch_rows, cross_rows, topn=10)
    return active_df, strong_top, weak_top, inflow_sector_top, cross_rows, watch_rows, tw_group_radar


def make_report(active_df: pd.DataFrame, strong_top: pd.DataFrame, weak_top: pd.DataFrame, inflow_sector_top: pd.DataFrame, cross_rows: List[dict], watch_rows: List[dict], tw_group_radar: List[dict]) -> str:
    tw = now_tw()
    ny = tw.astimezone(NY_TZ)

    strong_tw = weighted_supply_chain_top(strong_top, limit=10, weak=False)
    weak_tw = weighted_supply_chain_top(weak_top, limit=10, weak=True)

    lines = []
    status_text, expected_day = data_update_status(active_df, strong_top, weak_top, tw)

    lines.append(f"🌙 {APP_NAME}")
    lines.append(status_text)
    lines.append(f"📅 預期美股交易日：{expected_day}")
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
    lines.append("📌 台股盤前偏向")
    lines.extend(premarket_bias_lines(strong_top, weak_top, cross_rows))

    lines.append("")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("🇹🇼 台股族群雷達 Top 10")
    lines.extend(format_tw_group_radar(tw_group_radar))

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
    lines.append("🪙 跨市場風險")
    lines.extend(format_cross_rows(cross_rows))

    lines.append("")
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
    strong_top.to_csv("us_strong_top10_v06.csv", index=False, encoding="utf-8-sig")
    weak_top.to_csv("us_weak_top10_v06.csv", index=False, encoding="utf-8-sig")
    inflow_sector_top.to_csv("us_inflow_sector_top10_v06.csv", index=False, encoding="utf-8-sig")


def main():
    active_df, strong_top, weak_top, inflow_sector_top, cross_rows, watch_rows, tw_group_radar = get_market_data()
    report = make_report(active_df, strong_top, weak_top, inflow_sector_top, cross_rows, watch_rows, tw_group_radar)
    print(report)
    save_csv(strong_top, weak_top, inflow_sector_top)
    send_telegram(report)

if __name__ == "__main__":
    main()