import streamlit as st
import requests
import json
import os
import re
from datetime import datetime, date, timezone, timedelta
from btc_context import BTC_HISTORICAL_CONTEXT
from btc_analytics import build_analytics_context
import time

st.set_page_config(page_title="Agente BTC", page_icon="₿", layout="wide")

FEEDBACK_FILE = "feedbacks.json"
CG_KEY = ""

for key, val in [("messages", []), ("feedbacks", [])]:
    if key not in st.session_state:
        st.session_state[key] = val

def load_feedbacks():
    try:
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE) as f:
                st.session_state.feedbacks = json.load(f)
    except:
        st.session_state.feedbacks = []

def save_feedbacks():
    try:
        with open(FEEDBACK_FILE, "w") as f:
            json.dump(st.session_state.feedbacks, f, ensure_ascii=False, indent=2)
    except:
        pass

load_feedbacks()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

# ── Indicadores técnicos ─────────────────────────────────────────────────────

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 0)

def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 0)

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    def ema_series(data, period):
        k = 2 / (period + 1)
        ema = sum(data[:period]) / period
        result = [ema]
        for v in data[period:]:
            ema = v * k + ema * (1 - k)
            result.append(ema)
        return result
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len - i)] - ema_slow[-(min_len - i)] for i in range(min_len)]
    signal_line = ema_series(macd_line, signal)
    histogram = macd_line[-1] - signal_line[-1]
    return round(macd_line[-1], 1), round(signal_line[-1], 1), round(histogram, 1)

def calc_bollinger(closes, period=20, std_mult=2):
    if len(closes) < period:
        return None, None, None
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = variance ** 0.5
    return round(sma + std_mult * std, 0), round(sma, 0), round(sma - std_mult * std, 0)

def check_bear_market(closes_w, sma50_w):
    if not sma50_w or len(closes_w) < 3:
        return None
    weeks_below = 0
    for c in reversed(closes_w[-10:]):
        if c < sma50_w:
            weeks_below += 1
        else:
            break
    if weeks_below > 2:
        return f"BEAR MARKET confirmado ({weeks_below} semanas abaixo da SMA50 semanal)"
    elif weeks_below > 0:
        return f"Atenção: {weeks_below} semana(s) abaixo da SMA50 semanal (bear não confirmado ainda)"
    return "Acima da SMA50 semanal (bear não confirmado)"

# ── Histórico completo ───────────────────────────────────────────────────────

def fetch_binance_full_history():
    all_candles = {}
    start_ms = int(datetime(2017, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    next_start = start_ms
    for _ in range(25):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1d",
                        "startTime": next_start, "limit": 200},
                headers=HEADERS, timeout=15
            )
            if r.status_code == 200:
                raw = r.json()
                if isinstance(raw, list) and len(raw) > 1 and isinstance(raw[0], list):
                    for c in raw:
                        all_candles[int(c[0])] = {
                            "open": float(c[1]), "high": float(c[2]),
                            "low":  float(c[3]), "close": float(c[4]),
                            "vol":  float(c[7]),
                        }
                    last_ts = int(raw[-1][0])
                    next_start = last_ts + 86400000
                    if next_start > int(time.time() * 1000):
                        break
                else:
                    break
            else:
                break
        except:
            break
    if len(all_candles) > 100:
        sorted_ts = sorted(all_candles.keys())
        candles_by_date = {
            datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d"): all_candles[ts]
            for ts in sorted_ts
        }
        closes = [all_candles[ts]["close"] for ts in sorted_ts]
        vols   = [all_candles[ts]["vol"]   for ts in sorted_ts]
        return closes, vols, candles_by_date
    return [], [], {}

def extract_date_from_query(query):
    meses = {
        'janeiro': 1, 'fevereiro': 2, 'março': 3, 'marco': 3,
        'abril': 4, 'maio': 5, 'junho': 6, 'julho': 7,
        'agosto': 8, 'setembro': 9, 'outubro': 10,
        'novembro': 11, 'dezembro': 12
    }
    q = query.lower()
    m = re.search(r'(\d{1,2})\s+de\s+(\w+)\s+(?:de\s+)?(\d{4})', q)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2), int(m.group(3))
        if month_str in meses:
            try:
                return datetime(year, meses[month_str], day, tzinfo=timezone.utc)
            except:
                pass
    m = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', q)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
        except:
            pass
    m = re.search(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', q)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except:
            pass
    return None

def get_candle_for_date(candles_by_date, target_dt):
    key = target_dt.strftime("%Y-%m-%d")
    if key in candles_by_date:
        c = candles_by_date[key]
        return {
            "date": target_dt.strftime("%d/%m/%Y"),
            "open": c["open"], "high": c["high"],
            "low":  c["low"],  "close": c["close"],
            "volume": c["vol"],
        }
    return None

# ── get_indicators ───────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_indicators():
    errors = []
    closes_d, vols_d, candles_by_date = fetch_binance_full_history()

    if len(closes_d) < 50:
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1d", "limit": 366},
                headers=HEADERS, timeout=15
            )
            if r.status_code == 200:
                raw = r.json()
                if isinstance(raw, list) and len(raw) > 0 and isinstance(raw[0], list):
                    closes_d = [float(c[4]) for c in raw]
                    vols_d   = [float(c[7]) for c in raw]
        except Exception as e:
            errors.append(f"Binance fallback: {e}")

    price, change_24h, volume_24h = None, None, None
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"}, headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            price      = float(d["lastPrice"])
            change_24h = round(float(d["priceChangePercent"]), 2)
            volume_24h = round(float(d["quoteVolume"]) / 1e9, 2)
    except Exception as e:
        errors.append(f"Binance price: {e}")

    if price is None:
        try:
            params = {"ids": "bitcoin", "vs_currencies": "usd",
                      "include_24hr_change": "true", "include_24hr_vol": "true"}
            if CG_KEY:
                params["x_cg_demo_api_key"] = CG_KEY
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params=params, headers=HEADERS, timeout=10
            )
            if r.status_code == 200:
                d = r.json()["bitcoin"]
                price      = float(d["usd"])
                change_24h = round(float(d["usd_24h_change"]), 2)
                volume_24h = round(float(d.get("usd_24h_vol", 0)) / 1e9, 2)
        except Exception as e:
            errors.append(f"CoinGecko price: {e}")

    if price is None:
        return {"error": "Não foi possível obter preço."}

    closes_w = []
    if len(closes_d) >= 14:
        now = datetime.now(timezone.utc)
        weekly_map = {}
        for i, close in enumerate(closes_d):
            days_ago = len(closes_d) - 1 - i
            day = now - timedelta(days=days_ago)
            week_key = (day.isocalendar()[0], day.isocalendar()[1])
            weekly_map[week_key] = close
        closes_w = [v for k, v in sorted(weekly_map.items())]

    daily = {}
    if len(closes_d) >= 9:
        daily = {
            "EMA9":   calc_ema(closes_d, 9),
            "EMA21":  calc_ema(closes_d, 21),
            "EMA50":  calc_ema(closes_d, 50),
            "EMA100": calc_ema(closes_d, 100),
            "EMA200": calc_ema(closes_d, 200),
            "SMA50":  calc_sma(closes_d, 50),
            "SMA200": calc_sma(closes_d, 200),
        }

    weekly = {}
    if len(closes_w) >= 9:
        weekly = {
            "EMA9":   calc_ema(closes_w, 9),
            "EMA21":  calc_ema(closes_w, 21),
            "EMA50":  calc_ema(closes_w, 50),
            "EMA100": calc_ema(closes_w, 100),
            "SMA20":  calc_sma(closes_w, 20),
            "SMA50":  calc_sma(closes_w, 50),
            "SMA200": calc_sma(closes_w, 200),
        }

    rsi_14d = calc_rsi(closes_d, 14)
    rsi_14w = calc_rsi(closes_w, 14) if len(closes_w) >= 15 else None
    macd_val, macd_sig, macd_hist = calc_macd(closes_d)
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes_d)

    sma50_w = weekly.get("SMA50")
    bear_status = check_bear_market(closes_w, sma50_w)

    vol_avg_30d = round(sum(vols_d[-30:]) / 30 / 1e9, 2) if len(vols_d) >= 30 else None
    vol_ratio   = round(volume_24h / vol_avg_30d, 2) if (volume_24h and vol_avg_30d) else None

    dominance = None
    try:
        params = {}
        if CG_KEY:
            params["x_cg_demo_api_key"] = CG_KEY
        r = requests.get("https://api.coingecko.com/api/v3/global",
                         params=params, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            dominance = round(r.json().get("data", {}).get(
                "market_cap_percentage", {}).get("btc", 0), 1)
    except:
        pass

    ATH = 126198
    halving = date(2024, 4, 19)
    days_post = (date.today() - halving).days
    dist_ath  = round((price - ATH) / ATH * 100, 1)

    if days_post < 180:   phase = "Pós-halving inicial (0-6m)"
    elif days_post < 365: phase = "Acumulação pré-bull (6-12m)"
    elif days_post < 548: phase = "Bull market histórico (12-18m)"
    else:                 phase = "Fase avançada pós-halving (18m+)"

    ema50d  = daily.get("EMA50")
    ema200d = daily.get("EMA200")
    cross = "Golden Cross ativo" if (ema50d and ema200d and ema50d > ema200d) else \
            "Death Cross ativo"  if (ema50d and ema200d) else "Indefinido"

    return {
        "price": price, "change_24h": change_24h,
        "volume_24h": volume_24h, "vol_avg_30d": vol_avg_30d, "vol_ratio": vol_ratio,
        "ATH": ATH, "dist_ath": dist_ath, "days_post": days_post,
        "phase": phase, "cross": cross, "bear_status": bear_status,
        "daily": daily, "weekly": weekly,
        "rsi_14d": rsi_14d, "rsi_14w": rsi_14w,
        "macd": macd_val, "macd_signal": macd_sig, "macd_hist": macd_hist,
        "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower,
        "candles_d": len(closes_d), "candles_w": len(closes_w),
        "dominance": dominance, "errors": errors,
        "updated": datetime.now().strftime("%H:%M:%S"),
        "_closes": closes_d, "_vols": vols_d,
        "_candles_by_date": candles_by_date,
    }

# ── get_macro_data ───────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_macro_data():
    macro = {}

    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "FEDFUNDS", "api_key": "4a91b29932e776f7d4d73b7d70c37ec5",
                    "file_type": "json", "limit": 6, "sort_order": "desc"},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if obs:
                macro["fed_rate"] = float(obs[0]["value"])
                macro["fed_rate_prev"] = float(obs[1]["value"]) if len(obs) > 1 else None
                macro["fed_rate_6m_ago"] = float(obs[5]["value"]) if len(obs) > 5 else None
    except:
        pass

    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "CPIAUCSL", "api_key": "4a91b29932e776f7d4d73b7d70c37ec5",
                    "file_type": "json", "limit": 14, "sort_order": "desc"},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if len(obs) >= 13:
                macro["cpi_yoy"] = round(
                    (float(obs[0]["value"]) - float(obs[12]["value"])) / float(obs[12]["value"]) * 100, 2)
            if len(obs) >= 2:
                macro["cpi_mom"] = round(
                    (float(obs[0]["value"]) - float(obs[1]["value"])) / float(obs[1]["value"]) * 100, 2)
    except:
        pass

    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "UNRATE", "api_key": "4a91b29932e776f7d4d73b7d70c37ec5",
                    "file_type": "json", "limit": 6, "sort_order": "desc"},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if obs:
                macro["unemployment"] = float(obs[0]["value"])
                macro["unemployment_prev"] = float(obs[1]["value"]) if len(obs) > 1 else None
                macro["unemployment_6m_ago"] = float(obs[5]["value"]) if len(obs) > 5 else None
    except:
        pass

    def fetch_yahoo(symbol, range_="30d"):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": range_}, headers=HEADERS, timeout=10
            )
            if r.status_code == 200:
                result = r.json()["chart"]["result"][0]
                closes = [c for c in result["indicators"]["quote"][0]["close"] if c]
                if closes:
                    return closes
        except:
            pass
        return []

    # DXY
    c = fetch_yahoo("DX-Y.NYB", "90d")
    if c:
        macro["dxy"] = round(c[-1], 2)
        macro["dxy_change_30d"] = round((c[-1] - c[max(0,len(c)-30)]) / c[max(0,len(c)-30)] * 100, 2)
        macro["dxy_change_90d"] = round((c[-1] - c[0]) / c[0] * 100, 2)
        macro["dxy_30d_high"] = round(max(c[-30:]), 2)
        macro["dxy_30d_low"]  = round(min(c[-30:]), 2)

    # Treasury 10Y
    c = fetch_yahoo("%5ETNX", "90d")
    if c:
        macro["treasury_10y"] = round(c[-1], 3)
        macro["treasury_change"] = round(c[-1] - c[max(0,len(c)-30)], 3)
        macro["treasury_90d_high"] = round(max(c), 3)
        macro["treasury_90d_low"]  = round(min(c), 3)

    # S&P500
    c = fetch_yahoo("%5EGSPC", "90d")
    if c:
        macro["sp500"] = round(c[-1], 2)
        macro["sp500_change_30d"] = round((c[-1] - c[max(0,len(c)-30)]) / c[max(0,len(c)-30)] * 100, 2)
        macro["sp500_change_90d"] = round((c[-1] - c[0]) / c[0] * 100, 2)
        macro["sp500_90d_high"] = round(max(c), 2)
        macro["sp500_90d_low"]  = round(min(c), 2)
        macro["sp500_dist_from_high"] = round((c[-1] - max(c)) / max(c) * 100, 1)

    # NASDAQ
    c = fetch_yahoo("%5EIXIC", "90d")
    if c:
        macro["nasdaq"] = round(c[-1], 2)
        macro["nasdaq_change_30d"] = round((c[-1] - c[max(0,len(c)-30)]) / c[max(0,len(c)-30)] * 100, 2)
        macro["nasdaq_change_90d"] = round((c[-1] - c[0]) / c[0] * 100, 2)
        macro["nasdaq_90d_high"] = round(max(c), 2)
        macro["nasdaq_dist_from_high"] = round((c[-1] - max(c)) / max(c) * 100, 1)

    # Ouro
    c = fetch_yahoo("GC%3DF", "90d")
    if c:
        macro["gold"] = round(c[-1], 2)
        macro["gold_change_30d"] = round((c[-1] - c[max(0,len(c)-30)]) / c[max(0,len(c)-30)] * 100, 2)
        macro["gold_change_90d"] = round((c[-1] - c[0]) / c[0] * 100, 2)
        macro["gold_90d_high"] = round(max(c), 2)
        macro["gold_90d_low"]  = round(min(c), 2)
        macro["gold_dist_from_high"] = round((c[-1] - max(c)) / max(c) * 100, 1)

    # Petróleo WTI
    c = fetch_yahoo("CL%3DF", "90d")
    if c:
        macro["oil"] = round(c[-1], 2)
        macro["oil_change_30d"] = round((c[-1] - c[max(0,len(c)-30)]) / c[max(0,len(c)-30)] * 100, 2)
        macro["oil_change_90d"] = round((c[-1] - c[0]) / c[0] * 100, 2)
        macro["oil_90d_high"] = round(max(c), 2)
        macro["oil_90d_low"]  = round(min(c), 2)
        macro["oil_dist_from_high"] = round((c[-1] - max(c)) / max(c) * 100, 1)
        macro["oil_dist_from_low"]  = round((c[-1] - min(c)) / min(c) * 100, 1)

    # VIX
    c = fetch_yahoo("%5EVIX", "30d")
    if c:
        macro["vix"] = round(c[-1], 2)
        macro["vix_30d_high"] = round(max(c), 2)
        macro["vix_30d_low"]  = round(min(c), 2)
        macro["vix_change_30d"] = round(c[-1] - c[0], 2)

    return macro

# ── get_derivatives ──────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def get_derivatives():
    deriv = {}
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": "BTCUSDT", "period": "1h", "limit": 1},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                deriv["long_short_ratio"] = round(float(data[0]["longShortRatio"]), 3)
                deriv["long_pct"]  = round(float(data[0]["longAccount"])  * 100, 1)
                deriv["short_pct"] = round(float(data[0]["shortAccount"]) * 100, 1)
    except:
        pass
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                deriv["funding_rate"] = round(float(data[-1]["fundingRate"]) * 100, 4)
    except:
        pass
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": "BTCUSDT"}, headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            deriv["open_interest"] = round(float(d["openInterest"]) * 81000 / 1e9, 2)
    except:
        pass
    return deriv

# ── Interpretação macro completa ─────────────────────────────────────────────

def build_macro_interpretation(macro):
    lines = []
    bullish_points = 0
    bearish_points = 0

    sp500        = macro.get("sp500")
    sp500_chg30  = macro.get("sp500_change_30d", 0) or 0
    sp500_chg90  = macro.get("sp500_change_90d", 0) or 0
    sp500_hi     = macro.get("sp500_90d_high")
    sp500_lo     = macro.get("sp500_90d_low")
    sp500_dfh    = macro.get("sp500_dist_from_high", 0) or 0

    nasdaq       = macro.get("nasdaq")
    nasdaq_chg30 = macro.get("nasdaq_change_30d", 0) or 0
    nasdaq_chg90 = macro.get("nasdaq_change_90d", 0) or 0
    nasdaq_dfh   = macro.get("nasdaq_dist_from_high", 0) or 0

    gold         = macro.get("gold")
    gold_chg30   = macro.get("gold_change_30d", 0) or 0
    gold_chg90   = macro.get("gold_change_90d", 0) or 0
    gold_dfh     = macro.get("gold_dist_from_high", 0) or 0

    oil          = macro.get("oil")
    oil_chg30    = macro.get("oil_change_30d", 0) or 0
    oil_chg90    = macro.get("oil_change_90d", 0) or 0
    oil_hi       = macro.get("oil_90d_high")
    oil_lo       = macro.get("oil_90d_low")
    oil_dfh      = macro.get("oil_dist_from_high", 0) or 0
    oil_dfl      = macro.get("oil_dist_from_low",  0) or 0

    vix          = macro.get("vix", 0) or 0
    vix_hi       = macro.get("vix_30d_high", 0) or 0
    vix_lo       = macro.get("vix_30d_low",  0) or 0

    dxy          = macro.get("dxy")
    dxy_chg30    = macro.get("dxy_change_30d", 0) or 0
    dxy_chg90    = macro.get("dxy_change_90d", 0) or 0

    treasury     = macro.get("treasury_10y")
    treas_chg    = macro.get("treasury_change", 0) or 0
    treas_hi     = macro.get("treasury_90d_high")
    treas_lo     = macro.get("treasury_90d_low")

    fed          = macro.get("fed_rate")
    fed_prev     = macro.get("fed_rate_prev")
    fed_6m       = macro.get("fed_rate_6m_ago")

    cpi          = macro.get("cpi_yoy")
    cpi_mom      = macro.get("cpi_mom")

    unemp        = macro.get("unemployment")
    unemp_prev   = macro.get("unemployment_prev")
    unemp_6m     = macro.get("unemployment_6m_ago")

    # ════════════════════════════════════════════════════════════════
    # S&P500
    # ════════════════════════════════════════════════════════════════
    if sp500:
        lines.append("── S&P500 ──────────────────────────────────────────────────")
        lines.append(f"Preço atual: {sp500:,.0f} | 30d: {sp500_chg30:+.1f}% | 90d: {sp500_chg90:+.1f}%")
        if sp500_hi and sp500_lo:
            lines.append(f"Máxima 90d: {sp500_hi:,.0f} | Mínima 90d: {sp500_lo:,.0f} | Dist. máxima: {sp500_dfh:+.1f}%")

        if sp500_dfh >= -1:
            lines.append("STATUS: S&P500 em ou próximo da MÁXIMA HISTÓRICA/90d.")
            lines.append("RISCO CRÍTICO: Quando S&P500 está em máxima histórica, qualquer evento negativo")
            lines.append("gera realização de lucros em cascata. BTC historicamente cai junto:")
            lines.append("  - Mar/2020: S&P500 caiu -34% do topo → BTC caiu -51% em 48h")
            lines.append("  - Jan/2022: S&P500 em topo → correção -12% → BTC caiu -22%")
            lines.append("  - Set/2022: S&P500 bateu topo parcial → reverteu -10% → BTC caiu -20%")
            lines.append("PROBABILIDADE: se S&P500 cair >5% nos próximos 30d, BTC segue em 80% dos casos.")
            bearish_points += 2
        elif sp500_dfh >= -5:
            lines.append("STATUS: S&P500 próximo da máxima (dentro de 5%). Vulnerável a reversão.")
            lines.append("Historicamente: S&P500 dentro de 5% da máxima = zona de distribuição.")
            lines.append("  - Em 2021: S&P500 ficou 3 meses dentro de 5% do topo antes de cair -25%")
            lines.append("  - BTC seguiu o S&P500 em cada queda >5% desde 2020 (correlação 0.65)")
            bearish_points += 1
        elif sp500_dfh <= -15:
            lines.append("STATUS: S&P500 em correção significativa. Modo risk-off ativo.")
            lines.append("Correlação BTC-SP500 aumenta para 0.75+ em correções prolongadas.")
            lines.append("  - Jun/2022: S&P500 -25% em 6 meses → BTC caiu -75% no mesmo período")
            lines.append("  - Out/2022: S&P500 fundo → BTC ainda levou 3 semanas para tocar fundo")
            bearish_points += 2

        if sp500_chg30 > 8:
            lines.append(f"ALERTA: Rally forte em 30d ({sp500_chg30:+.1f}%). Ganhos elevados aumentam risco de realização.")
            lines.append("Historicamente quando S&P500 sobe >8% em 30d, reversão parcial em 60d ocorre 70% das vezes.")
            bearish_points += 1
        elif sp500_chg30 < -8:
            lines.append(f"QUEDA FORTE 30d ({sp500_chg30:+.1f}%): risk-off dominante. BTC raramente sobe quando S&P500 cai >8%/mês.")
            bearish_points += 2
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # NASDAQ
    # ════════════════════════════════════════════════════════════════
    if nasdaq:
        lines.append("── NASDAQ ──────────────────────────────────────────────────")
        lines.append(f"Preço atual: {nasdaq:,.0f} | 30d: {nasdaq_chg30:+.1f}% | 90d: {nasdaq_chg90:+.1f}%")
        if nasdaq_dfh is not None:
            lines.append(f"Dist. máxima 90d: {nasdaq_dfh:+.1f}%")

        lines.append("CORRELAÇÃO HISTÓRICA NASDAQ-BTC:")
        lines.append("  - 2020-2022: correlação NASDAQ-BTC atingiu 0.70 (maior da história)")
        lines.append("  - 2022: NASDAQ caiu -33% → BTC caiu -65% no mesmo período")
        lines.append("  - 2023: NASDAQ subiu +43% → BTC subiu +155% (BTC amplificou o movimento)")
        lines.append("  - 2024: NASDAQ subiu +29% → BTC subiu +125%")
        lines.append("  PADRÃO: BTC amplifica movimentos do NASDAQ (beta ~2x em rallies, ~1.5x em quedas)")

        if nasdaq_dfh >= -2:
            lines.append(f"STATUS: NASDAQ em ou perto da máxima. Mesmo risco do S&P500 — zona de distribuição.")
            bearish_points += 1
        elif nasdaq_chg30 < -10:
            lines.append(f"NASDAQ queda forte ({nasdaq_chg30:+.1f}% 30d): liquidação de tech. BTC historicamente cai 1.5-2x isso.")
            bearish_points += 2
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # OURO
    # ════════════════════════════════════════════════════════════════
    if gold:
        lines.append("── OURO ────────────────────────────────────────────────────")
        lines.append(f"Preço atual: ${gold:,.0f} | 30d: {gold_chg30:+.1f}% | 90d: {gold_chg90:+.1f}%")
        if macro.get("gold_90d_high") and macro.get("gold_90d_low"):
            lines.append(f"Máxima 90d: ${macro['gold_90d_high']:,.0f} | Mínima 90d: ${macro['gold_90d_low']:,.0f} | Dist. máxima: {gold_dfh:+.1f}%")

        lines.append("CORRELAÇÃO HISTÓRICA OURO-BTC:")
        lines.append("  - Correlação média ouro-BTC: baixa (0.1-0.3), mas aumenta em crises")
        lines.append("  - Quando DXY cai + ouro sobe: capital foge do dólar → favorável ao BTC")
        lines.append("  - 2020 (pós-covid): ouro subiu +25%, BTC subiu +300% (mesmo catalisador: dólar fraco)")
        lines.append("  - 2022: ouro caiu -5%, BTC caiu -65% (correlação inversa — ambos sofreram com Fed hawkish)")
        lines.append("  - 2023-2024: ouro subiu +30%, BTC subiu +200% (narrativa de reserva de valor)")
        lines.append("  PADRÃO: ouro subindo + DXY caindo = ambiente historicamente favorável ao BTC")
        lines.append("  PADRÃO: ouro caindo + DXY subindo = ambiente desfavorável ao BTC")

        if gold_chg30 > 5 and dxy_chg30 < -1:
            lines.append(f"SINAL BULLISH CONFIRMADO: Ouro +{gold_chg30:.1f}% + DXY {dxy_chg30:+.1f}%. Combinação historicamente positiva para BTC.")
            bullish_points += 2
        elif gold_chg30 > 3:
            lines.append(f"Ouro subindo ({gold_chg30:+.1f}% 30d): possível fuga para reserva de valor. Verificar se DXY está caindo para confirmar sinal bullish para BTC.")
            bullish_points += 1
        elif gold_chg30 < -3:
            lines.append(f"Ouro caindo ({gold_chg30:+.1f}% 30d): redução de demanda por hedge. Levemente negativo para narrativa de reserva de valor do BTC.")
            bearish_points += 1

        if gold_dfh >= -1:
            lines.append("Ouro em máxima histórica/90d: pode indicar medo macro elevado OU euforia especulativa.")
            lines.append("Se for medo macro: ruim para BTC (risk-off). Se for dólar fraco: bom para BTC.")
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # PETRÓLEO
    # ════════════════════════════════════════════════════════════════
    if oil:
        lines.append("── PETRÓLEO WTI ────────────────────────────────────────────")
        lines.append(f"Preço atual: ${oil:.1f} | 30d: {oil_chg30:+.1f}% | 90d: {oil_chg90:+.1f}%")
        if oil_hi and oil_lo:
            lines.append(f"Máxima 90d: ${oil_hi:.1f} | Mínima 90d: ${oil_lo:.1f}")
            lines.append(f"Dist. máxima 90d: {oil_dfh:+.1f}% | Dist. mínima 90d: {oil_dfl:+.1f}%")

        lines.append("MECANISMO DE IMPACTO PETRÓLEO → BTC:")
        lines.append("  Canal 1 (inflação): petróleo sobe → inflação sobe → Fed hawkish → juros sobem → BTC cai")
        lines.append("  Canal 2 (crescimento): petróleo cai muito → recessão → risk-off → BTC cai junto com tudo")
        lines.append("  Canal 3 (mineração): petróleo caro = energia cara = custo de mineração sobe = pressão de venda de miners")
        lines.append("HISTÓRICO:")
        lines.append("  - 2022: petróleo foi de $75 para $120 (+60%) → inflação 9% → Fed subiu 4.5% → BTC -77%")
        lines.append("  - 2020: petróleo colapsou para -$37 (covid) → recessão → BTC caiu -51% em março")
        lines.append("  - 2023: petróleo caiu de $95 para $70 (-26%) → inflação controlou → Fed parou de subir → BTC +155%")
        lines.append("  - 2024: petróleo estável $70-80 → ambiente neutro → BTC subiu por fatores próprios (halving/ETF)")

        if oil_dfl <= 3:
            lines.append(f"STATUS: PETRÓLEO PRÓXIMO DA MÍNIMA 90d (dist. mínima: {oil_dfl:+.1f}%)")
            lines.append("RISCO DE REVERSÃO: petróleo na mínima tende a reverter. Se subir:")
            lines.append("  - Pressão inflacionária retorna → Fed pode adiar cortes → bearish BTC")
            lines.append("  - Cada +10% no petróleo a partir de mínimas históricas adicionou ~0.3-0.5% ao CPI")
            lines.append("  - Se petróleo voltar para máxima 90d de " + (f"${oil_hi:.1f}" if oil_hi else "N/A") + f", seria {oil_dfh*-1:.1f}% de alta")
            lines.append("  - Monitorar petróleo é monitorar risco inflacionário futuro")
            bearish_points += 1
        elif oil_dfh >= -2:
            lines.append(f"STATUS: PETRÓLEO NA MÁXIMA 90d. Pressão inflacionária máxima no período.")
            lines.append("Fed menos propenso a cortar juros com petróleo alto. Ambiente desfavorável ao BTC.")
            bearish_points += 2
        elif oil_chg30 > 10:
            lines.append(f"ALTA FORTE DO PETRÓLEO ({oil_chg30:+.1f}% 30d): risco inflacionário crescente.")
            lines.append("Historicamente: petróleo +10% em 30d → CPI sobe 0.2-0.4% nos 2 meses seguintes → Fed reage.")
            bearish_points += 1
        elif oil_chg30 < -10:
            lines.append(f"QUEDA FORTE DO PETRÓLEO ({oil_chg30:+.1f}% 30d): dois cenários possíveis:")
            lines.append("  Positivo: inflação cai → Fed corta → bullish BTC")
            lines.append("  Negativo: queda por recessão → risk-off → bearish BTC")
            lines.append("  Verificar: se desemprego subindo junto = recessão (bearish). Se isolado = desinflação (bullish).")
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # VIX
    # ════════════════════════════════════════════════════════════════
    if vix:
        lines.append("── VIX (VOLATILIDADE DO MERCADO) ───────────────────────────")
        lines.append(f"Atual: {vix} | Máxima 30d: {vix_hi} | Mínima 30d: {vix_lo}")
        lines.append("INTERPRETAÇÃO DO VIX PARA BTC:")
        lines.append("  VIX < 13: complacência extrema → spike de volatilidade iminente em 4-8 semanas (histórico)")
        lines.append("  VIX 13-18: mercado calmo → BTC pode se mover por fatores próprios")
        lines.append("  VIX 18-25: cautela → correlação BTC-SP500 começa a subir")
        lines.append("  VIX 25-35: medo elevado → correlação BTC-SP500 sobe para 0.70+ → BTC cai junto com equities")
        lines.append("  VIX > 35: pânico → correlação BTC-SP500 vai para 0.80+ → BTC caiu -30% a -51% em todos os eventos VIX>35")
        lines.append("HISTÓRICO VIX E BTC:")
        lines.append("  - Mar/2020: VIX atingiu 85 → BTC caiu -51% em 48h")
        lines.append("  - Jun/2022: VIX em 34 → BTC caiu -38% no mês")
        lines.append("  - Nov/2022 (FTX): VIX em 25 → BTC caiu -25% em uma semana")
        lines.append("  - Jan/2024: VIX em 14 → BTC subiu +70% no trimestre (correlação baixa, narrativa própria)")

        if vix > 35:
            lines.append(f"STATUS: VIX={vix} PÂNICO EXTREMO. Probabilidade de queda adicional do BTC muito alta.")
            bearish_points += 3
        elif vix > 25:
            lines.append(f"STATUS: VIX={vix} MEDO ELEVADO. Correlação BTC-SP500 em 0.70+. Risco de queda conjunta.")
            bearish_points += 2
        elif vix < 13:
            lines.append(f"STATUS: VIX={vix} COMPLACÊNCIA EXTREMA.")
            lines.append("Mercado ignorando riscos. Historicamente precede picos de volatilidade:")
            lines.append("  - Em 100% dos casos onde VIX ficou <13 por >3 semanas, houve spike para >20 em 2 meses")
            lines.append("  - Spike do VIX derruba BTC mesmo que o trigger inicial não seja crypto")
            bearish_points += 1
        elif vix < 18:
            lines.append(f"STATUS: VIX={vix} mercado calmo. BTC pode se mover por fatores próprios. Neutro.")
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # DXY
    # ════════════════════════════════════════════════════════════════
    if dxy:
        lines.append("── DXY (ÍNDICE DO DÓLAR) ───────────────────────────────────")
        lines.append(f"Atual: {dxy} | 30d: {dxy_chg30:+.1f}% | 90d: {dxy_chg90:+.1f}%")
        lines.append("CORRELAÇÃO HISTÓRICA DXY-BTC: -0.60 a -0.80 (fortemente inversa)")
        lines.append("HISTÓRICO:")
        lines.append("  - 2022: DXY foi de 95 para 114 (+20%) → BTC caiu -77%")
        lines.append("  - 2023: DXY caiu de 114 para 100 (-12%) → BTC subiu +155%")
        lines.append("  - 2024: DXY oscilou 100-107 → BTC subiu +125% (ETFs superaram o efeito DXY)")
        lines.append("  - Jan/2025: DXY >109 → BTC corrigiu de $108k para $89k")
        lines.append("  REGRA: DXY acima de 105 = pressão. DXY acima de 108 = pressão forte. DXY abaixo de 100 = alívio.")

        if dxy > 108:
            lines.append(f"STATUS: DXY={dxy} NÍVEL CRÍTICO (>108). Pressão máxima sobre BTC.")
            lines.append("Historicamente: DXY>108 coincidiu com quedas significativas do BTC em 3 de 3 casos (2015, 2022, 2025).")
            bearish_points += 2
        elif dxy > 104:
            lines.append(f"STATUS: DXY={dxy} (>104). Pressão moderada. BTC pode subir mas com resistência.")
            bearish_points += 1
        elif dxy < 100:
            lines.append(f"STATUS: DXY={dxy} (<100). Dólar fraco. Historicamente favorável ao BTC.")
            bullish_points += 2
        elif dxy < 102:
            lines.append(f"STATUS: DXY={dxy} (100-102). Zona de alívio. Levemente favorável ao BTC.")
            bullish_points += 1

        if dxy_chg30 > 2:
            lines.append(f"DXY FORTALECENDO ({dxy_chg30:+.1f}% 30d): pressão crescente sobre BTC.")
            bearish_points += 1
        elif dxy_chg30 < -2:
            lines.append(f"DXY ENFRAQUECENDO ({dxy_chg30:+.1f}% 30d): alívio para BTC.")
            bullish_points += 1
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # TREASURY 10Y
    # ════════════════════════════════════════════════════════════════
    if treasury:
        lines.append("── TREASURY 10Y (JUROS LONGOS) ─────────────────────────────")
        lines.append(f"Atual: {treasury}% | Variação 30d: {treas_chg:+.3f}%")
        if treas_hi and treas_lo:
            lines.append(f"Máxima 90d: {treas_hi}% | Mínima 90d: {treas_lo}%")
        lines.append("MECANISMO DE IMPACTO TREASURY → BTC:")
        lines.append("  Yield sobe → custo de oportunidade sobe → capital sai de ativos de risco → BTC cai")
        lines.append("  Yield cai → dinheiro busca retorno maior → ativos de risco sobem → BTC sobe")
        lines.append("HISTÓRICO:")
        lines.append("  - 2022: yield foi de 1.5% para 4.2% (+2.7pp) → BTC caiu -77%")
        lines.append("  - 2023: yield oscilou 3.5-5% mas BTC subiu +155% (antecipação de cortes do Fed)")
        lines.append("  - 2024: yield caiu de 5% para 3.6% → BTC atingiu ATH $108k")
        lines.append("  - Jan/2025: yield voltou a 4.8% → BTC corrigiu de $108k para $89k")
        lines.append("  ZONAS CRÍTICAS: <3.5% = bullish | 3.5-4.0% = neutro | 4.0-4.5% = pressão | >4.5% = pressão forte")

        if treasury > 4.5:
            lines.append(f"STATUS: {treasury}% ACIMA DE 4.5%. Custo de oportunidade alto. Ambiente desfavorável ao BTC.")
            lines.append("Historicamente BTC performa abaixo da média com yield >4.5% por mais de 60 dias.")
            bearish_points += 2
        elif treasury > 4.0:
            lines.append(f"STATUS: {treasury}% (4.0-4.5%). Zona de pressão moderada.")
            bearish_points += 1
        elif treasury < 3.5:
            lines.append(f"STATUS: {treasury}% ABAIXO DE 3.5%. Favorável ao BTC.")
            bullish_points += 2
        elif treasury < 4.0:
            lines.append(f"STATUS: {treasury}% (3.5-4.0%). Zona neutra.")

        if treas_chg > 0.3:
            lines.append(f"YIELD SUBINDO RÁPIDO ({treas_chg:+.3f}pp 30d): pressão crescente sobre ativos de risco.")
            bearish_points += 1
        elif treas_chg < -0.3:
            lines.append(f"YIELD CAINDO ({treas_chg:+.3f}pp 30d): alívio para ativos de risco incluindo BTC.")
            bullish_points += 1
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # FED RATE
    # ════════════════════════════════════════════════════════════════
    if fed is not None:
        lines.append("── TAXA DO FED ─────────────────────────────────────────────")
        lines.append(f"Atual: {fed}%")
        if fed_prev:
            lines.append(f"Anterior: {fed_prev}% | Variação: {fed - fed_prev:+.2f}pp")
        if fed_6m:
            lines.append(f"6 meses atrás: {fed_6m}% | Tendência 6m: {fed - fed_6m:+.2f}pp")
        lines.append("IMPACTO HISTÓRICO DA TAXA DO FED NO BTC:")
        lines.append("  - 2017: Fed em 0.5-1.5% (baixo) → BTC subiu +1.900%")
        lines.append("  - 2018: Fed subiu de 1.5% para 2.5% → BTC caiu -84%")
        lines.append("  - 2020: Fed zerou por covid → BTC subiu +300% em 8 meses")
        lines.append("  - 2021: Fed em 0% → BTC subiu +700% no ciclo")
        lines.append("  - 2022: Fed subiu de 0% para 4.5% (ciclo mais agressivo em 40 anos) → BTC -77%")
        lines.append("  - 2023: Fed parou de subir → BTC antecipou cortes → subiu +155%")
        lines.append("  - Set/2024: Fed cortou 0.5pp → BTC atingiu novo ATH em novembro")
        lines.append("  PADRÃO: BTC sobe ANTES dos cortes (antecipação) e DURANTE cortes.")
        lines.append("  PADRÃO: BTC cai quando Fed sobe juros agressivamente (>1.5pp em 12 meses).")

        if fed_prev and fed < fed_prev:
            lines.append(f"STATUS: FED CORTANDO JUROS ({fed_prev}% → {fed}%). Historicamente bullish para BTC.")
            bullish_points += 2
        elif fed_prev and fed > fed_prev:
            lines.append(f"STATUS: FED SUBINDO JUROS ({fed_prev}% → {fed}%). Historicamente bearish para BTC.")
            bearish_points += 2
        else:
            lines.append(f"STATUS: FED ESTÁVEL em {fed}%. Neutro no curto prazo.")
            if fed >= 4.5:
                lines.append("Taxa em patamar alto. BTC historicamente underperforma com Fed >4.5% por >6 meses.")
                bearish_points += 1
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # CPI / INFLAÇÃO
    # ════════════════════════════════════════════════════════════════
    if cpi is not None:
        lines.append("── INFLAÇÃO (CPI) ──────────────────────────────────────────")
        lines.append(f"CPI YoY: {cpi}%")
        if cpi_mom is not None:
            lines.append(f"CPI MoM: {cpi_mom:+.2f}%")
        lines.append("MECANISMO: CPI alto → Fed hawkish → juros sobem → BTC cai")
        lines.append("MECANISMO: CPI baixo → Fed dovish/corta → juros caem → BTC sobe")
        lines.append("HISTÓRICO:")
        lines.append("  - 2021: CPI subiu de 2% para 7% → Fed começou a subir juros → BTC caiu -65% em 2022")
        lines.append("  - 2022: CPI chegou a 9.1% (máxima 40 anos) → Fed hawkish máximo → BTC no fundo")
        lines.append("  - 2023: CPI caiu de 9% para 3% → Fed parou de subir → BTC subiu +155%")
        lines.append("  - 2024: CPI ficou entre 3-3.5% → Fed cortou em set/2024 → BTC ATH $108k")
        lines.append("  ZONA CRÍTICA: CPI >4% = Fed não corta. CPI 2.5-3.5% = Fed pode cortar. CPI <2.5% = cortes prováveis.")

        if cpi > 4:
            lines.append(f"STATUS: CPI={cpi}% ACIMA DE 4%. Fed provavelmente não corta. Ambiente desfavorável.")
            bearish_points += 2
        elif cpi > 3:
            lines.append(f"STATUS: CPI={cpi}% (3-4%). Zona de incerteza. Fed hesita em cortar.")
            bearish_points += 1
        elif cpi < 2.5:
            lines.append(f"STATUS: CPI={cpi}% ABAIXO DE 2.5%. Espaço para cortes. Favorável ao BTC.")
            bullish_points += 2
        else:
            lines.append(f"STATUS: CPI={cpi}% (2.5-3%). Zona de transição. Fed pode cortar com cautela.")
            bullish_points += 1

        if cpi_mom and cpi_mom > 0.4:
            lines.append(f"ALERTA: CPI MoM={cpi_mom:+.2f}% (acima de 0.4%). Inflação acelerando no mês. Fed pode pausar cortes.")
            bearish_points += 1
        elif cpi_mom and cpi_mom < 0:
            lines.append(f"CPI MoM={cpi_mom:+.2f}% (deflação mensal). Desinflação em curso. Favorável.")
            bullish_points += 1
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # DESEMPREGO
    # ════════════════════════════════════════════════════════════════
    if unemp is not None:
        lines.append("── DESEMPREGO EUA ──────────────────────────────────────────")
        lines.append(f"Atual: {unemp}%")
        if unemp_prev:
            lines.append(f"Mês anterior: {unemp_prev}% | Variação: {unemp - unemp_prev:+.1f}pp")
        if unemp_6m:
            lines.append(f"6 meses atrás: {unemp_6m}% | Tendência 6m: {unemp - unemp_6m:+.1f}pp")
        lines.append("MECANISMO DE IMPACTO:")
        lines.append("  Desemprego SUBINDO: Fed corta juros para estimular → bullish BTC via juros menores")
        lines.append("  Desemprego SUBINDO MUITO: risco de recessão → risk-off → bearish BTC no curto prazo")
        lines.append("  Desemprego BAIXO: economia forte → Fed mantém juros altos → pressão no BTC")
        lines.append("  REGRA DE SAHM: aumento de 0.5pp na média de 3 meses = recessão historicamente")
        lines.append("HISTÓRICO:")
        lines.append("  - 2020: desemprego foi de 3.5% para 14.7% em 2 meses (covid) → BTC caiu -51%, depois Fed zerou → BTC +300%")
        lines.append("  - 2022: desemprego baixo (3.5%) → Fed subiu juros agressivamente → BTC -77%")
        lines.append("  - 2023: desemprego estável 3.4-3.7% → economia forte mas Fed parou → BTC +155%")
        lines.append("  - 2024: desemprego subiu para 4.3% (ago/2024) → Fed cortou 0.5pp em set → BTC subiu")

        delta_unemp = (unemp - unemp_prev) if unemp_prev else 0
        delta_6m = (unemp - unemp_6m) if unemp_6m else 0

        if unemp > 5:
            lines.append(f"STATUS: {unemp}% DESEMPREGO ALTO. Risco de recessão. Fed provavelmente corta, mas recessão = risk-off inicial.")
            bearish_points += 1
            bullish_points += 1  # contraditório — neutro
        elif delta_6m >= 0.5:
            lines.append(f"ALERTA: Desemprego subiu {delta_6m:+.1f}pp em 6 meses. Próximo da Regra de Sahm.")
            lines.append("Fed deve cortar juros → bullish BTC médio prazo. Mas risco de recessão no curto prazo.")
            bullish_points += 1
        elif delta_unemp >= 0.3:
            lines.append(f"Desemprego subindo ({delta_unemp:+.1f}pp no mês). Fed pode antecipar cortes. Levemente bullish BTC.")
            bullish_points += 1
        elif unemp < 4:
            lines.append(f"STATUS: {unemp}% DESEMPREGO BAIXO. Economia forte → Fed menos propenso a cortar juros.")
            bearish_points += 1
        lines.append("")

    # ════════════════════════════════════════════════════════════════
    # SCORE MACRO FINAL
    # ════════════════════════════════════════════════════════════════
    lines.append("══════════════════════════════════════════════════════════════")
    lines.append(f"SCORE MACRO: {bullish_points} pontos bullish | {bearish_points} pontos bearish")
    if bearish_points >= bullish_points + 3:
        lines.append("VEREDICTO MACRO: 🔴 FORTEMENTE BEARISH — múltiplos fatores negativos alinhados")
    elif bearish_points > bullish_points:
        lines.append("VEREDICTO MACRO: 🟠 BEARISH — mais fatores negativos que positivos")
    elif bullish_points >= bearish_points + 3:
        lines.append("VEREDICTO MACRO: 🟢 FORTEMENTE BULLISH — múltiplos fatores positivos alinhados")
    elif bullish_points > bearish_points:
        lines.append("VEREDICTO MACRO: 🟡 LEVEMENTE BULLISH — leve vantagem para fatores positivos")
    else:
        lines.append("VEREDICTO MACRO: ⚪ NEUTRO — forças conflitantes sem direção clara")
    lines.append("REGRA: Este score deve ser cruzado com os indicadores técnicos do BTC.")
    lines.append("Macro favorável + técnico bullish = maior convicção.")
    lines.append("Macro desfavorável + técnico bearish = maior convicção de baixa.")
    lines.append("Divergência (macro bearish + técnico bullish) = incerteza, reduzir tamanho das posições.")
    lines.append("══════════════════════════════════════════════════════════════")

    return "\n".join(lines)

# ── build_prompt ─────────────────────────────────────────────────────────────

def build_prompt(ind, macro, deriv, historical_candle=None):
    if not ind or "error" in ind:
        return "Dados indisponíveis."

    p = ind["price"]
    d = ind["daily"]
    w = ind["weekly"]

    def fmt(name, val):
        if not val: return ""
        diff = round((p - val) / val * 100, 1)
        pos = "suporte" if p > val else "resistência"
        return f"  {name}: ${val:,.0f} ({pos}, {diff:+.1f}%)"

    daily_lines  = "\n".join([fmt(k, v) for k, v in d.items() if v])
    weekly_lines = "\n".join([fmt(k, v) for k, v in w.items() if v])

    bear_ctx = f"\nStatus de tendência: {ind.get('bear_status', 'N/A')}"

    rsi_ctx = ""
    if ind.get("rsi_14d"):
        rsi_d = ind["rsi_14d"]
        rsi_zone = ("sobrecomprado — topo próximo" if rsi_d > 70
                    else "sobrevendido — fundo próximo" if rsi_d < 30 else "neutro")
        rsi_ctx = f"\nRSI 14 diário: {rsi_d} ({rsi_zone})"
    if ind.get("rsi_14w"):
        rsi_ctx += f" | RSI 14 semanal: {ind['rsi_14w']}"

    macd_ctx = ""
    if ind.get("macd") is not None:
        hist = ind["macd_hist"]
        macd_dir = "bullish (histograma positivo)" if hist and hist > 0 else "bearish (histograma negativo)"
        macd_ctx = f"\nMACD: {ind['macd']} | Signal: {ind['macd_signal']} | Hist: {hist} ({macd_dir})"

    bb_ctx = ""
    if ind.get("bb_upper"):
        bb_pos = ("acima da banda superior — sobrecomprado" if p > ind["bb_upper"]
                  else "abaixo da banda inferior — sobrevendido" if p < ind["bb_lower"]
                  else "dentro das bandas — neutro")
        bb_ctx = (f"\nBollinger Bands (20,2): Upper ${ind['bb_upper']:,.0f} | "
                  f"Mid ${ind['bb_mid']:,.0f} | Lower ${ind['bb_lower']:,.0f} | Preço: {bb_pos}")

    vol_ctx = ""
    if ind.get("vol_ratio"):
        vr = ind["vol_ratio"]
        vs = ("muito acima da média" if vr > 1.5 else "acima da média" if vr > 1.2
              else "muito abaixo da média" if vr < 0.6 else "abaixo da média" if vr < 0.8
              else "dentro da média")
        vol_ctx = f"\nVolume 24h: ${ind['volume_24h']}B | Ratio 30d: {vr}x ({vs})"

    dom_ctx = ""
    if ind.get("dominance"):
        dom = ind["dominance"]
        ds = ("muito alta" if dom > 60 else "alta" if dom > 55
              else "neutra-alta" if dom > 50 else "baixa — altseason ativa" if dom < 45 else "neutra")
        dom_ctx = f"\nDominância BTC: {dom}% ({ds})"

    deriv_lines = []
    if deriv.get("long_short_ratio") is not None:
        ls = deriv["long_short_ratio"]
        ls_s = ("excesso de longs" if ls > 1.5 else "excesso de shorts" if ls < 0.67 else "equilibrado")
        deriv_lines.append(f"  Long/Short: {ls} (L:{deriv['long_pct']}% S:{deriv['short_pct']}%) — {ls_s}")
    if deriv.get("funding_rate") is not None:
        fr = deriv["funding_rate"]
        fr_s = ("positivo alto — correção provável" if fr > 0.05
                else "negativo — short squeeze provável" if fr < -0.01 else "neutro")
        deriv_lines.append(f"  Funding Rate: {fr}% ({fr_s})")
    if deriv.get("open_interest") is not None:
        deriv_lines.append(f"  Open Interest: ${deriv['open_interest']}B")
    deriv_ctx = "\nDERIVATIVOS:\n" + "\n".join(deriv_lines) if deriv_lines else ""

    macro_interp = build_macro_interpretation(macro)

    analytics_ctx = build_analytics_context(
        ind.get("_closes", []),
        ind.get("_vols", [])
    )

    hist_ctx = ""
    if historical_candle:
        hist_ctx = f"""
CANDLE HISTÓRICO EXATO (Binance BTCUSDT — dados reais):
Data:   {historical_candle['date']}
Open:   ${historical_candle['open']:,.2f}
High:   ${historical_candle['high']:,.2f}
Low:    ${historical_candle['low']:,.2f}
Close:  ${historical_candle['close']:,.2f}
Volume: ${historical_candle['volume']:,.0f} USD
REGRA: Use esses valores exatos ao falar sobre essa data. Nunca estime."""

    fb = ""
    if st.session_state.feedbacks:
        fb = "\n\nFEEDBACKS ANTERIORES:\n"
        for f in st.session_state.feedbacks[-15:]:
            s = "ACERTOU" if f["result"] == "correct" else "ERROU"
            fb += f"- {f['date']}: \"{f['query']}\" → {s}: {f['note']}\n"

    data_ctx = f"""DADOS EM TEMPO REAL DO BTC:

Preço: ${p:,.0f} | Variação 24h: {ind['change_24h']}%
ATH: $126.198 (out/2025) | Distância: {ind['dist_ath']}%
Dias pós-halving abr/2024: {ind['days_post']} | Fase: {ind['phase']}
Tendência MA: {ind['cross']}{bear_ctx}{rsi_ctx}{macd_ctx}{bb_ctx}{vol_ctx}{dom_ctx}
{deriv_ctx}

MÉDIAS DIÁRIAS ({ind['candles_d']} candles reais):
{daily_lines if daily_lines else "  Dados insuficientes"}

MÉDIAS SEMANAIS ({ind['candles_w']} semanas reais):
{weekly_lines if weekly_lines else "  Dados insuficientes"}"""

    return f"""Você é um agente quantitativo especializado em Bitcoin com conhecimento histórico completo desde 2011.

{data_ctx}

{analytics_ctx}

{hist_ctx}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANÁLISE MACRO COMPLETA — MERCADOS GLOBAIS E CONTEXTO ECONÔMICO
(Dados reais + histórico de impacto no BTC calculado automaticamente)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{macro_interp}

{BTC_HISTORICAL_CONTEXT}

REGRAS:
1. Use os dados em tempo real — nunca invente valores de médias ou indicadores
2. Use os padrões do ANÁLISE QUANTITATIVA AUTOMÁTICA para embasar probabilidades com frequência histórica real
3. Quando houver CANDLE HISTÓRICO EXATO, use esses valores — nunca estime datas históricas
4. Compare com análogos históricos com datas e números exatos
5. Probabilidades numéricas baseadas em frequência histórica real dos dados
6. Cruze BTC + S&P500 + NASDAQ + Ouro + Petróleo + VIX + DXY + Juros + Desemprego + CPI simultaneamente
7. Seja direto — diga o que os dados sugerem
8. PROIBIDO afirmar contexto qualitativo (ETFs, regulação, instituições, sentimento) sem que esse dado esteja explicitamente nos dados fornecidos. Se não tem dado, diga "sem dado disponível para confirmar"
9. BEAR MARKET = preço abaixo da SMA50 semanal por mais de 2 semanas consecutivas (já calculado no campo bear_status). BULL MARKET = acima da SMA50 semanal. Fora disso: "tendência indefinida"
10. Nunca use linguagem de certeza — use frequência histórica: "em X de Y casos similares, foi Y. Em Z de Y, foi W"
11. Ao analisar probabilidades de preço, sempre considerar o SCORE MACRO calculado acima e cruzar com o técnico
12. Petróleo na mínima = risco de reversão inflacionária. S&P500 na máxima = risco de realização. Sempre mencionar quando relevante{fb}"""

# ── send_message ─────────────────────────────────────────────────────────────

def send_message(api_key, user_msg, ind, macro, deriv, historical_candle=None):
    try:
        msgs = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
        msgs.append({"role": "user", "content": user_msg})
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2500,
                  "system": build_prompt(ind, macro, deriv, historical_candle),
                  "messages": msgs},
            timeout=60
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"], None
        elif r.status_code == 401:
            return None, "API key inválida."
        elif r.status_code == 429:
            return None, "Limite atingido. Aguarde."
        else:
            return None, f"Erro {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return None, f"Erro: {e}"

# ── Interface ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Configuração")
    api_key = st.text_input("API Key da Anthropic", type="password", placeholder="sk-ant-...")
    if api_key and not api_key.startswith("sk-ant-"):
        st.error("Deve começar com sk-ant-")
        api_key = ""
    elif api_key:
        st.success("✓ Key configurada")
    st.divider()
    st.markdown("""**Como usar:**
1. Cole sua API key acima
2. Pergunte qualquer coisa sobre BTC
3. Avalie acertou/errou
4. O agente aprende com os feedbacks""")
    st.divider()
    if st.button("🔄 Atualizar dados"):
        st.cache_data.clear()
        st.rerun()
    if st.button("🗑️ Limpar conversa"):
        st.session_state.messages = []
        st.rerun()

st.title("₿ Agente BTC")
st.caption("Análise quantitativa — histórico completo desde 2017 + mercados globais + macro detalhado + derivativos")

with st.spinner("Carregando dados..."):
    ind   = get_indicators()
    macro = get_macro_data()
    deriv = get_derivatives()

if ind and "error" not in ind:

    st.markdown("**Bitcoin**")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Preço", f"${ind['price']:,.0f}", f"{ind['change_24h']}% 24h")
    c2.metric("Dist. ATH", f"{ind['dist_ath']}%", "de $126.198")
    c3.metric("Pós-halving", f"{ind['days_post']}d", "abr/2024")
    c4.metric("MA Trend", ind['cross'].split()[0], ind['cross'].split()[-1])
    if ind.get("dominance"):
        c5.metric("Dominância", f"{ind['dominance']}%", "BTC/mercado")

    if ind.get("bear_status"):
        color = "🔴" if "BEAR" in ind["bear_status"] else "🟡" if "Atenção" in ind["bear_status"] else "🟢"
        st.caption(f"{color} {ind['bear_status']}")

    st.markdown("**Indicadores Técnicos**")
    t1, t2, t3, t4 = st.columns(4)
    if ind.get("rsi_14d"):
        rsi_d = ind["rsi_14d"]
        rc = "🔴" if rsi_d > 70 else "🟢" if rsi_d < 30 else "🟡"
        t1.metric("RSI 14D", f"{rc} {rsi_d}")
    if ind.get("rsi_14w"):
        rsi_w = ind["rsi_14w"]
        rc = "🔴" if rsi_w > 70 else "🟢" if rsi_w < 30 else "🟡"
        t2.metric("RSI 14W", f"{rc} {rsi_w}")
    if ind.get("macd_hist") is not None:
        hist = ind["macd_hist"]
        t3.metric("MACD Hist", f"{hist}", "bullish" if hist > 0 else "bearish")
    if ind.get("bb_upper") and ind.get("bb_lower"):
        bb_range = ind["bb_upper"] - ind["bb_lower"]
        bb_pct = round((ind["price"] - ind["bb_lower"]) / bb_range * 100, 0) if bb_range else 0
        t4.metric("Bollinger %", f"{bb_pct}%", "posição nas bandas")

    st.markdown("**Mercados Globais**")
    g1, g2, g3, g4, g5 = st.columns(5)
    if macro.get("sp500"):
        dfh = macro.get("sp500_dist_from_high", 0)
        g1.metric("S&P500", f"{macro['sp500']:,.0f}",
                  f"{macro.get('sp500_change_30d', 0):+.1f}% 30d | {dfh:+.1f}% max")
    if macro.get("nasdaq"):
        dfh = macro.get("nasdaq_dist_from_high", 0)
        g2.metric("NASDAQ", f"{macro['nasdaq']:,.0f}",
                  f"{macro.get('nasdaq_change_30d', 0):+.1f}% 30d")
    if macro.get("gold"):
        g3.metric("Ouro", f"${macro['gold']:,.0f}",
                  f"{macro.get('gold_change_30d', 0):+.1f}% 30d")
    if macro.get("oil"):
        dfl = macro.get("oil_dist_from_low", 0)
        g4.metric("Petróleo WTI", f"${macro['oil']:.1f}",
                  f"{macro.get('oil_change_30d', 0):+.1f}% 30d | {dfl:+.1f}% min")
    if macro.get("vix"):
        vix = macro["vix"]
        vc = "🔴" if vix > 25 else "🟡" if vix > 18 else "🟢"
        g5.metric("VIX", f"{vc} {vix}", f"max30d: {macro.get('vix_30d_high', '-')}")

    st.markdown("**Macro EUA**")
    m1, m2, m3, m4, m5 = st.columns(5)
    if macro.get("fed_rate") is not None:
        delta = round(macro["fed_rate"] - macro["fed_rate_prev"], 2) if macro.get("fed_rate_prev") else None
        m1.metric("Fed Rate", f"{macro['fed_rate']}%", f"{delta:+.2f}pp" if delta else None)
    if macro.get("cpi_yoy") is not None:
        m2.metric("CPI YoY", f"{macro['cpi_yoy']}%",
                  f"MoM: {macro['cpi_mom']:+.2f}%" if macro.get("cpi_mom") is not None else "YoY")
    if macro.get("unemployment") is not None:
        delta_ue = round(macro["unemployment"] - macro["unemployment_prev"], 1) if macro.get("unemployment_prev") else None
        m3.metric("Desemprego", f"{macro['unemployment']}%", f"{delta_ue:+.1f}pp" if delta_ue else None)
    if macro.get("treasury_10y") is not None:
        m4.metric("Treasury 10Y", f"{macro['treasury_10y']}%",
                  f"{macro['treasury_change']:+.3f}pp 30d" if macro.get("treasury_change") else None)
    if macro.get("dxy") is not None:
        m5.metric("DXY", str(macro["dxy"]),
                  f"{macro.get('dxy_change_30d', 0):+.1f}% 30d")

    st.markdown("**Derivativos**")
    d1, d2, d3 = st.columns(3)
    if deriv.get("long_short_ratio"):
        d1.metric("Long/Short", str(deriv["long_short_ratio"]),
                  f"L:{deriv['long_pct']}% S:{deriv['short_pct']}%")
    if deriv.get("funding_rate") is not None:
        d2.metric("Funding Rate", f"{deriv['funding_rate']}%")
    if deriv.get("open_interest"):
        d3.metric("Open Interest", f"${deriv['open_interest']}B")

    with st.expander("📊 Médias móveis reais"):
        col1, col2 = st.columns(2)
        p = ind["price"]
        with col1:
            st.markdown("**Diárias**")
            for name, val in ind["daily"].items():
                if val:
                    diff = round((p - val) / val * 100, 1)
                    icon = "🟢" if p > val else "🔴"
                    st.markdown(f"{icon} **{name}:** ${val:,.0f} ({'suporte' if p > val else 'resistência'}, {diff:+.1f}%)")
        with col2:
            st.markdown("**Semanais**")
            for name, val in ind["weekly"].items():
                if val:
                    diff = round((p - val) / val * 100, 1)
                    icon = "🟢" if p > val else "🔴"
                    st.markdown(f"{icon} **{name}:** ${val:,.0f} ({'suporte' if p > val else 'resistência'}, {diff:+.1f}%)")

    st.caption(f"Atualizado às {ind['updated']} | {ind['candles_d']} candles diários, {ind['candles_w']} semanais")

elif ind and "error" in ind:
    st.error(f"Erro: {ind['error']}")

st.divider()

st.markdown("**Perguntas rápidas:**")
perguntas = [
    "Qual a situação atual do BTC?",
    "O macro atual é favorável ou desfavorável?",
    "Os derivativos indicam alta ou queda?",
    "Esse drawdown lembra qual período histórico?",
    "Onde estamos no ciclo do halving?",
    "É um bom momento para comprar?"
]
cols = st.columns(3)
triggered = None
for i, q in enumerate(perguntas):
    if cols[i % 3].button(q, key=f"q{i}", use_container_width=True):
        triggered = q

for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
    if msg["role"] == "assistant" and i == len(st.session_state.messages) - 1:
        ca, cb = st.columns([1, 1])
        if ca.button("✓ Acertou", key=f"ok{i}"):
            uq = st.session_state.messages[i-1]["content"][:80] if i > 0 else ""
            st.session_state.feedbacks.append({
                "date": datetime.now().strftime("%d/%m/%Y"),
                "query": uq, "result": "correct", "note": "confirmado"
            })
            save_feedbacks()
            st.success("Registrado!")
        if cb.button("✗ Errou", key=f"no{i}"):
            st.session_state[f"fb{i}"] = True
        if st.session_state.get(f"fb{i}"):
            note = st.text_input("O que aconteceu diferente?", key=f"note{i}")
            if st.button("Salvar", key=f"sv{i}"):
                uq = st.session_state.messages[i-1]["content"][:80] if i > 0 else ""
                st.session_state.feedbacks.append({
                    "date": datetime.now().strftime("%d/%m/%Y"),
                    "query": uq, "result": "wrong", "note": note or "sem detalhe"
                })
                save_feedbacks()
                st.session_state[f"fb{i}"] = False

if st.session_state.feedbacks:
    with st.expander(f"🧠 Memória — {len(st.session_state.feedbacks)} feedbacks"):
        for fb in reversed(st.session_state.feedbacks[-8:]):
            icon = "✓" if fb["result"] == "correct" else "✗"
            cor = "green" if fb["result"] == "correct" else "red"
            st.markdown(f":{cor}[{icon}] **{fb['date']}** — _{fb['query']}_ → {fb['note']}")

prompt = st.chat_input("Pergunte qualquer coisa sobre o BTC...")
final_prompt = triggered or prompt

if final_prompt:
    if not api_key:
        st.error("Cole sua API key da Anthropic na barra lateral.")
        st.stop()

    historical_candle = None
    target_dt = extract_date_from_query(final_prompt)
    if target_dt:
        candles_by_date = ind.get("_candles_by_date", {})
        historical_candle = get_candle_for_date(candles_by_date, target_dt)

    with st.chat_message("user"):
        st.markdown(final_prompt)
    with st.chat_message("assistant"):
        with st.spinner("Analisando..."):
            reply, err = send_message(api_key, final_prompt, ind, macro, deriv, historical_candle)
        if err:
            st.error(err)
        else:
            st.markdown(reply)
            st.session_state.messages.append({"role": "user", "content": final_prompt})
            st.session_state.messages.append({"role": "assistant", "content": reply})
            st.rerun()