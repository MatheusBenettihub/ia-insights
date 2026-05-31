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
CG_KEY = ""  # Cole sua key do CoinGecko aqui se tiver

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

# ── Helpers de indicadores ───────────────────────────────────────────────────

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
    upper = round(sma + std_mult * std, 0)
    lower = round(sma - std_mult * std, 0)
    return round(upper, 0), round(sma, 0), round(lower, 0)

# ── Histórico completo + candle exato ───────────────────────────────────────

def fetch_binance_full_history():
    """Busca histórico completo da Binance desde 2017 em lotes de 200 dias."""
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
                            "open":  float(c[1]),
                            "high":  float(c[2]),
                            "low":   float(c[3]),
                            "close": float(c[4]),
                            "vol":   float(c[7]),
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
        candles_by_date = {}
        for ts in sorted_ts:
            dt = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
            candles_by_date[dt] = all_candles[ts]
        closes = [all_candles[ts]["close"] for ts in sorted_ts]
        vols   = [all_candles[ts]["vol"]   for ts in sorted_ts]
        return closes, vols, candles_by_date
    return [], [], {}

def extract_date_from_query(query):
    """Detecta se a pergunta menciona uma data específica."""
    meses = {
        'janeiro': 1, 'fevereiro': 2, 'março': 3, 'marco': 3,
        'abril': 4, 'maio': 5, 'junho': 6,
        'julho': 7, 'agosto': 8, 'setembro': 9,
        'outubro': 10, 'novembro': 11, 'dezembro': 12
    }
    q = query.lower()

    # "4 de maio de 2018" ou "4 de maio 2018"
    m = re.search(r'(\d{1,2})\s+de\s+(\w+)\s+(?:de\s+)?(\d{4})', q)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2), int(m.group(3))
        if month_str in meses:
            try:
                return datetime(year, meses[month_str], day, tzinfo=timezone.utc)
            except:
                pass

    # "04/05/2018" ou "04-05-2018"
    m = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', q)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
        except:
            pass

    # "2018-05-04"
    m = re.search(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', q)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except:
            pass

    return None

def get_candle_for_date(candles_by_date, target_dt):
    """Busca candle exato do histórico já carregado."""
    key = target_dt.strftime("%Y-%m-%d")
    if key in candles_by_date:
        c = candles_by_date[key]
        return {
            "date":   target_dt.strftime("%d/%m/%Y"),
            "open":   c["open"],
            "high":   c["high"],
            "low":    c["low"],
            "close":  c["close"],
            "volume": c["vol"],
        }
    return None

# ── get_indicators ───────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def get_indicators():
    errors = []
    closes_d, vols_d, candles_by_date = [], [], {}

    closes_d, vols_d, candles_by_date = fetch_binance_full_history()

    # Fallback simples
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

    # Preço atual
    price, change_24h, volume_24h = None, None, None
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            price      = float(d["lastPrice"])
            change_24h = round(float(d["priceChangePercent"]), 2)
            volume_24h = round(float(d["quoteVolume"]) / 1e9, 2)
    except Exception as e:
        errors.append(f"Binance price: {e}")

    # Fallback CoinGecko
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

    # Semanais a partir dos diários
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

    # Indicadores diários
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

    # Indicadores semanais
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

    # RSI, MACD, Bollinger
    rsi_14d = calc_rsi(closes_d, 14)
    rsi_14w = calc_rsi(closes_w, 14) if len(closes_w) >= 15 else None
    macd_val, macd_sig, macd_hist = calc_macd(closes_d)
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes_d)

    vol_avg_30d = round(sum(vols_d[-30:]) / 30 / 1e9, 2) if len(vols_d) >= 30 else None
    vol_ratio   = round(volume_24h / vol_avg_30d, 2) if (volume_24h and vol_avg_30d) else None

    # Dominância
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
            "Death Cross ativo" if (ema50d and ema200d) else "Indefinido"

    return {
        "price": price, "change_24h": change_24h,
        "volume_24h": volume_24h, "vol_avg_30d": vol_avg_30d, "vol_ratio": vol_ratio,
        "ATH": ATH, "dist_ath": dist_ath, "days_post": days_post,
        "phase": phase, "cross": cross,
        "daily": daily, "weekly": weekly,
        "rsi_14d": rsi_14d, "rsi_14w": rsi_14w,
        "macd": macd_val, "macd_signal": macd_sig, "macd_hist": macd_hist,
        "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower,
        "candles_d": len(closes_d), "candles_w": len(closes_w),
        "dominance": dominance,
        "errors": errors,
        "updated": datetime.now().strftime("%H:%M:%S"),
        # Dados brutos para analytics e busca histórica
        "_closes": closes_d,
        "_vols":   vols_d,
        "_candles_by_date": candles_by_date,
    }

@st.cache_data(ttl=86400)
def get_macro_data():
    macro = {}
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "FEDFUNDS", "api_key": "4a91b29932e776f7d4d73b7d70c37ec5",
                    "file_type": "json", "limit": 2, "sort_order": "desc"},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if obs:
                macro["fed_rate"] = float(obs[0]["value"])
                macro["fed_rate_prev"] = float(obs[1]["value"]) if len(obs) > 1 else None
    except:
        pass
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "CPIAUCSL", "api_key": "4a91b29932e776f7d4d73b7d70c37ec5",
                    "file_type": "json", "limit": 13, "sort_order": "desc"},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if len(obs) >= 13:
                macro["cpi_yoy"] = round((float(obs[0]["value"]) - float(obs[12]["value"])) / float(obs[12]["value"]) * 100, 2)
    except:
        pass
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "UNRATE", "api_key": "4a91b29932e776f7d4d73b7d70c37ec5",
                    "file_type": "json", "limit": 2, "sort_order": "desc"},
            headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            obs = r.json().get("observations", [])
            if obs:
                macro["unemployment"] = float(obs[0]["value"])
                macro["unemployment_prev"] = float(obs[1]["value"]) if len(obs) > 1 else None
    except:
        pass
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
            params={"interval": "1d", "range": "5d"}, headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
            if closes:
                macro["dxy"] = round(closes[-1], 2)
                macro["dxy_change"] = round(closes[-1] - closes[0], 2)
    except:
        pass
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX",
            params={"interval": "1d", "range": "5d"}, headers=HEADERS, timeout=10
        )
        if r.status_code == 200:
            closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
            if closes:
                macro["treasury_10y"] = round(closes[-1], 3)
                macro["treasury_change"] = round(closes[-1] - closes[0], 3)
    except:
        pass
    return macro

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
                deriv["long_pct"] = round(float(data[0]["longAccount"]) * 100, 1)
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

    # RSI
    rsi_ctx = ""
    if ind.get("rsi_14d"):
        rsi_d = ind["rsi_14d"]
        rsi_zone = ("sobrecomprado — topo próximo" if rsi_d > 70
                    else "sobrevendido — fundo próximo" if rsi_d < 30
                    else "neutro")
        rsi_ctx = f"\nRSI 14 diário: {rsi_d} ({rsi_zone})"
    if ind.get("rsi_14w"):
        rsi_w = ind["rsi_14w"]
        rsi_ctx += f" | RSI 14 semanal: {rsi_w}"

    # MACD
    macd_ctx = ""
    if ind.get("macd") is not None:
        hist = ind["macd_hist"]
        macd_dir = "bullish (histograma positivo)" if hist and hist > 0 else "bearish (histograma negativo)"
        macd_ctx = f"\nMACD: {ind['macd']} | Signal: {ind['macd_signal']} | Hist: {hist} ({macd_dir})"

    # Bollinger
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
              else "neutra-alta" if dom > 50 else "baixa — altseason ativa" if dom < 45
              else "neutra")
        dom_ctx = f"\nDominância BTC: {dom}% ({ds})"

    macro_lines = []
    if macro.get("fed_rate") is not None:
        prev = macro.get("fed_rate_prev")
        trend = (" (subindo — hawkish)" if prev and macro["fed_rate"] > prev
                 else " (caindo — dovish)" if prev and macro["fed_rate"] < prev
                 else " (estável)")
        macro_lines.append(f"  Fed Rate: {macro['fed_rate']}%{trend}")
    if macro.get("cpi_yoy") is not None:
        macro_lines.append(f"  Inflação CPI YoY: {macro['cpi_yoy']}%")
    if macro.get("unemployment") is not None:
        macro_lines.append(f"  Desemprego EUA: {macro['unemployment']}%")
    if macro.get("treasury_10y") is not None:
        tc = macro.get("treasury_change", 0)
        macro_lines.append(f"  Treasury 10Y: {macro['treasury_10y']}% ({'subindo' if tc > 0 else 'caindo'})")
    if macro.get("dxy") is not None:
        dc = macro.get("dxy_change", 0)
        macro_lines.append(f"  DXY: {macro['dxy']} ({'fortalecendo' if dc > 0 else 'enfraquecendo'})")

    deriv_lines = []
    if deriv.get("long_short_ratio") is not None:
        ls = deriv["long_short_ratio"]
        ls_s = ("excesso de longs" if ls > 1.5 else "excesso de shorts" if ls < 0.67
                else "equilibrado")
        deriv_lines.append(f"  Long/Short: {ls} (L:{deriv['long_pct']}% S:{deriv['short_pct']}%) — {ls_s}")
    if deriv.get("funding_rate") is not None:
        fr = deriv["funding_rate"]
        fr_s = ("positivo alto — correção provável" if fr > 0.05
                else "negativo — short squeeze provável" if fr < -0.01
                else "neutro")
        deriv_lines.append(f"  Funding Rate: {fr}% ({fr_s})")
    if deriv.get("open_interest") is not None:
        deriv_lines.append(f"  Open Interest: ${deriv['open_interest']}B")

    macro_ctx = "\nMACRO EUA:\n" + "\n".join(macro_lines) if macro_lines else ""
    deriv_ctx = "\nDERIVATIVOS:\n" + "\n".join(deriv_lines) if deriv_lines else ""

    data_ctx = f"""DADOS EM TEMPO REAL:

Preço: ${p:,.0f} | Variação 24h: {ind['change_24h']}%
ATH: $126.198 (out/2025) | Distância: {ind['dist_ath']}%
Dias pós-halving abr/2024: {ind['days_post']} | Fase: {ind['phase']}
Tendência MA: {ind['cross']}{rsi_ctx}{macd_ctx}{bb_ctx}{vol_ctx}{dom_ctx}
{macro_ctx}
{deriv_ctx}

MÉDIAS DIÁRIAS ({ind['candles_d']} candles reais):
{daily_lines if daily_lines else "  Dados insuficientes"}

MÉDIAS SEMANAIS ({ind['candles_w']} semanas reais):
{weekly_lines if weekly_lines else "  Dados insuficientes"}

REGRA: Nunca cite valores de médias que não estejam listados acima."""

    # Analytics automático sobre histórico completo
    analytics_ctx = build_analytics_context(
        ind.get("_closes", []),
        ind.get("_vols", [])
    )

    # Candle histórico exato (se pergunta mencionar data)
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

    return f"""Você é um agente quantitativo especializado em Bitcoin com conhecimento histórico completo desde 2011.

{data_ctx}

{analytics_ctx}

{hist_ctx}

{BTC_HISTORICAL_CONTEXT}

REGRAS:
1. Use os dados em tempo real — nunca invente valores de médias ou indicadores
2. Use os padrões do ANÁLISE QUANTITATIVA AUTOMÁTICA para embasar probabilidades com frequência histórica real
3. Quando houver CANDLE HISTÓRICO EXATO, use esses valores — nunca estime datas históricas
4. Compare com análogos históricos com datas e números exatos
5. Probabilidades numéricas baseadas em frequência histórica real dos dados
6. Cruze BTC + macro + derivativos + RSI + MACD + Bollinger simultaneamente
7. Seja direto — diga o que os dados sugerem{fb}"""

# ── send_message ─────────────────────────────────────────────────────────────

def send_message(api_key, user_msg, ind, macro, deriv, historical_candle=None):
    try:
        msgs = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
        msgs.append({"role": "user", "content": user_msg})
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1500,
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
st.caption("Análise quantitativa — histórico completo desde 2017 + RSI + MACD + Bollinger + macro + derivativos")

with st.spinner("Carregando dados históricos..."):
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

    # RSI + MACD + Bollinger na UI
    st.markdown("**Indicadores Técnicos**")
    t1, t2, t3, t4 = st.columns(4)
    if ind.get("rsi_14d"):
        rsi_d = ind["rsi_14d"]
        rsi_color = "🔴" if rsi_d > 70 else "🟢" if rsi_d < 30 else "🟡"
        t1.metric("RSI 14D", f"{rsi_color} {rsi_d}")
    if ind.get("rsi_14w"):
        rsi_w = ind["rsi_14w"]
        rsi_color = "🔴" if rsi_w > 70 else "🟢" if rsi_w < 30 else "🟡"
        t2.metric("RSI 14W", f"{rsi_color} {rsi_w}")
    if ind.get("macd_hist") is not None:
        hist = ind["macd_hist"]
        t3.metric("MACD Hist", f"{hist}", "bullish" if hist > 0 else "bearish")
    if ind.get("bb_upper"):
        p_now = ind["price"]
        bb_pct = round((p_now - ind["bb_lower"]) / (ind["bb_upper"] - ind["bb_lower"]) * 100, 0)
        t4.metric("Bollinger %", f"{bb_pct}%", "posição nas bandas")

    st.markdown("**Macro EUA**")
    m1, m2, m3, m4, m5 = st.columns(5)
    if macro.get("fed_rate") is not None:
        delta = round(macro["fed_rate"] - macro["fed_rate_prev"], 2) if macro.get("fed_rate_prev") else None
        m1.metric("Fed Rate", f"{macro['fed_rate']}%", f"{delta:+.2f}%" if delta else None)
    if macro.get("cpi_yoy") is not None:
        m2.metric("Inflação CPI", f"{macro['cpi_yoy']}%", "YoY")
    if macro.get("unemployment") is not None:
        delta_ue = round(macro["unemployment"] - macro["unemployment_prev"], 1) if macro.get("unemployment_prev") else None
        m3.metric("Desemprego", f"{macro['unemployment']}%", f"{delta_ue:+.1f}%" if delta_ue else None)
    if macro.get("treasury_10y") is not None:
        m4.metric("Treasury 10Y", f"{macro['treasury_10y']}%",
                  f"{macro['treasury_change']:+.3f}%" if macro.get("treasury_change") else None)
    if macro.get("dxy") is not None:
        m5.metric("DXY", str(macro["dxy"]),
                  f"{macro['dxy_change']:+.2f}" if macro.get("dxy_change") else None)

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
                    pos  = "suporte" if p > val else "resistência"
                    st.markdown(f"{icon} **{name}:** ${val:,.0f} ({pos}, {diff:+.1f}%)")
        with col2:
            st.markdown("**Semanais**")
            for name, val in ind["weekly"].items():
                if val:
                    diff = round((p - val) / val * 100, 1)
                    icon = "🟢" if p > val else "🔴"
                    pos  = "suporte" if p > val else "resistência"
                    st.markdown(f"{icon} **{name}:** ${val:,.0f} ({pos}, {diff:+.1f}%)")

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

    # Detecta data na pergunta e busca candle exato do histórico já carregado
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