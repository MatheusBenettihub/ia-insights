"""
sp500_analytics.py
Busca histórico real do S&P500 (^GSPC desde ~1950) e roda análise estrutural
cross-market para comparar com a estrutura atual do BTC.

Lógica central:
  - Normaliza tudo em % (posição vs EMAs, RSI, volatilidade) — nunca compara preço absoluto
  - Varre o histórico inteiro e acha períodos do S&P500 com estrutura similar ao BTC atual
  - Destaca períodos "jovens" (pré-1983) como mais análogos
  - Retorna o que o S&P500 fez depois (em %) — a IA converte para BTC aplicando fator de escala 3-5x
"""

import requests
from datetime import datetime, timezone, timedelta

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://finance.yahoo.com",
}

ERA_JOVEM_FIM_TS = 410227200   # unix timestamp de 1983-01-01
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def _ts_to_date(ts: int) -> str:
    """Converte timestamp Unix (positivo ou negativo) para string YYYY-MM-DD."""
    try:
        return (_EPOCH + timedelta(seconds=ts)).strftime("%Y-%m-%d")
    except Exception:
        return "?"


# ── Fetch ────────────────────────────────────────────────────────────────────

def fetch_sp500_history():
    """
    Busca histórico semanal do S&P500 via Yahoo Finance desde ~1950.
    Usa period1 explícito para contornar o limite de range=max.
    Retorna (closes: list[float], timestamps: list[int], dates: list[str]).
    """
    try:
        period1 = int(datetime(1950, 1, 1, tzinfo=timezone.utc).timestamp())
        period2 = int(datetime.now(tz=timezone.utc).timestamp())

        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
            params={"interval": "1wk", "period1": period1, "period2": period2},
            headers=HEADERS,
            timeout=30,
        )
        if r.status_code != 200:
            return [], [], []

        data   = r.json()
        result = data["chart"]["result"][0]
        tss    = result["timestamp"]
        raw_c  = result["indicators"]["quote"][0]["close"]

        pairs = [(t, c) for t, c in zip(tss, raw_c) if c is not None]
        if not pairs:
            return [], [], []

        timestamps = [p[0] for p in pairs]
        closes     = [p[1] for p in pairs]
        dates      = [_ts_to_date(t) for t in timestamps]
        return closes, timestamps, dates

    except Exception:
        return [], [], []


# ── Helpers técnicos (sem dependência de btc_analytics) ──────────────────────

def _ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for v in closes[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def _ema_series(closes, period):
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    ema = sum(closes[:period]) / period
    result.append(ema)
    for v in closes[period:]:
        ema = v * k + ema * (1 - k)
        result.append(ema)
    return result

def _rsi_series(closes, period=14):
    result = [None] * period
    for i in range(period, len(closes)):
        seg    = closes[i - period: i + 1]
        gains  = [max(seg[j] - seg[j-1], 0) for j in range(1, len(seg))]
        losses = [max(seg[j-1] - seg[j], 0) for j in range(1, len(seg))]
        ag = sum(gains)  / period
        al = sum(losses) / period
        result.append(round(100 - (100 / (1 + ag / al)), 1) if al else 100.0)
    return result

def _vol_series(closes, period=30):
    """Retorna volatilidade anualizada diária em cada ponto."""
    result = [None] * period
    for i in range(period, len(closes)):
        rets = [(closes[j] - closes[j-1]) / closes[j-1] * 100 for j in range(i - period + 1, i + 1)]
        v = (sum(r**2 for r in rets) / period) ** 0.5
        result.append(round(v, 3))
    return result

def _perf_from(closes, i, days):
    end = i + days
    if end < len(closes):
        return round((closes[end] - closes[i]) / closes[i] * 100, 2)
    return None


# ── Similar Structure Finder (cross-market) ──────────────────────────────────

def sp500_similar_structure(sp500_closes, sp500_timestamps,
                             btc_pct_vs_ema50, btc_pct_vs_ema200,
                             btc_rsi, btc_vol_30d):
    """
    Varre o histórico completo do S&P500 buscando períodos com estrutura
    técnica normalizada similar à estrutura atual do BTC.

    Parâmetros BTC (já calculados em app.py):
      btc_pct_vs_ema50  : % do preço BTC em relação à sua EMA50
      btc_pct_vs_ema200 : % do preço BTC em relação à sua EMA200
      btc_rsi           : RSI 14 diário atual do BTC
      btc_vol_30d       : volatilidade 30d atual do BTC (desvio padrão diário em %)

    Retorna dict com análise agregada + exemplos separados por era (jovem vs moderno).
    """
    n = len(sp500_closes)
    if n < 250:
        return {}

    # Pré-computa séries em O(n)
    ema50_s  = _ema_series(sp500_closes, 50)
    ema200_s = _ema_series(sp500_closes, 200)
    rsi_s    = _rsi_series(sp500_closes, 14)
    vol_s    = _vol_series(sp500_closes, 30)

    # Percentil de volatilidade atual do S&P500 (para mapear regime)
    vols_valid = [v for v in vol_s if v is not None]
    vol_sp500_now = vol_s[-1] if vol_s[-1] else (sum(vols_valid[-30:]) / 30 if len(vols_valid) >= 30 else None)

    matches_jovem   = []  # pré-1983
    matches_moderno = []  # 1983+

    for i in range(200, n - 91):
        e50  = ema50_s[i]
        e200 = ema200_s[i]
        rsi  = rsi_s[i]
        vol  = vol_s[i]

        if e50 is None or e200 is None or rsi is None or vol is None:
            continue

        p_i    = sp500_closes[i]
        pp_e50  = (p_i - e50)  / e50  * 100
        pp_e200 = (p_i - e200) / e200 * 100

        # Tolerâncias: ±10pp em MAs, ±18pt em RSI (S&P500 menos volátil → afrouxar um pouco)
        if (abs(pp_e50  - btc_pct_vs_ema50)  < 10 and
            abs(pp_e200 - btc_pct_vs_ema200) < 10 and
            abs(rsi     - btc_rsi)           < 18):

            ts_i = sp500_timestamps[i] if i < len(sp500_timestamps) else None

            fwd30  = _perf_from(sp500_closes, i, 30)
            fwd60  = _perf_from(sp500_closes, i, 60)
            fwd90  = _perf_from(sp500_closes, i, 90)
            fwd180 = _perf_from(sp500_closes, i, 180)

            if fwd30 is None:
                continue

            era = "jovem" if (ts_i and ts_i < ERA_JOVEM_FIM_TS) else "moderno"
            record = {
                "data":       _ts_to_date(ts_i)[:7] if ts_i else "?",
                "era":        era,
                "pp_ema50":   round(pp_e50, 1),
                "pp_ema200":  round(pp_e200, 1),
                "rsi":        round(rsi, 1),
                "fwd30":      fwd30,
                "fwd60":      fwd60,
                "fwd90":      fwd90,
                "fwd180":     fwd180,
            }

            if era == "jovem":
                matches_jovem.append(record)
            else:
                matches_moderno.append(record)

    def aggregate(matches, label):
        if not matches:
            return {}
        f30  = [m["fwd30"]  for m in matches]
        f60  = [m["fwd60"]  for m in matches if m["fwd60"]  is not None]
        f90  = [m["fwd90"]  for m in matches if m["fwd90"]  is not None]
        f180 = [m["fwd180"] for m in matches if m["fwd180"] is not None]
        return {
            "label":           label,
            "amostras":        len(matches),
            "media_30d":       round(sum(f30)  / len(f30),  2) if f30  else None,
            "media_60d":       round(sum(f60)  / len(f60),  2) if f60  else None,
            "media_90d":       round(sum(f90)  / len(f90),  2) if f90  else None,
            "media_180d":      round(sum(f180) / len(f180), 2) if f180 else None,
            "pos_30d_pct":     round(sum(1 for x in f30 if x > 0)  / len(f30)  * 100, 0) if f30  else None,
            "pos_90d_pct":     round(sum(1 for x in f90 if x > 0)  / len(f90)  * 100, 0) if f90  else None,
            "forte_alta_30d":  round(sum(1 for x in f30 if x > 8)  / len(f30)  * 100, 0) if f30  else None,
            "forte_queda_30d": round(sum(1 for x in f30 if x < -8) / len(f30)  * 100, 0) if f30  else None,
            "exemplos":        matches[-4:],   # 4 mais recentes
        }

    result = {
        "jovem":   aggregate(matches_jovem,   "S&P500 jovem 1950-1982"),
        "moderno": aggregate(matches_moderno, "S&P500 moderno 1983-hoje"),
        "total_analogos": len(matches_jovem) + len(matches_moderno),
        "escala_nota": (
            "IMPORTANTE: S&P500 é 3-5x menos volátil que BTC. "
            "Multiplicar retornos do S&P500 por 3-4x para estimar equivalente BTC. "
            "Ajustar duração: S&P500 completa em ~6 meses o que BTC faz em ~2 meses."
        ),
    }
    return result


# ── Level Analog cross-market ────────────────────────────────────────────────

def sp500_level_analog(sp500_closes, sp500_timestamps,
                        btc_dist_pct, btc_rsi, btc_pct_vs_ema50,
                        scale_factor=3.5):
    """
    Para uma pergunta do tipo "BTC vai testar 70k?" (dist_pct = distância do alvo):
    acha casos no S&P500 onde o índice estava a distância equivalente
    (ajustada pelo fator de escala) de uma resistência/suporte local,
    em estrutura técnica similar (RSI, posição vs EMA50).

    scale_factor: quanto o BTC se move a mais que o S&P500 (padrão 3.5x).
    A distância BTC é dividida por scale_factor para encontrar o equivalente S&P500.
    """
    n = len(sp500_closes)
    if n < 120 or btc_dist_pct is None:
        return {}

    # Distância equivalente no S&P500 (ajustada pela escala)
    sp500_dist_equiv = btc_dist_pct / scale_factor
    direcao = "acima" if btc_dist_pct > 0 else "abaixo"

    ema50_s = _ema_series(sp500_closes, 50)
    rsi_s   = _rsi_series(sp500_closes, 14)

    outcomes_jovem   = []  # pré-1983
    outcomes_moderno = []

    for i in range(30, n - 31):
        e50 = ema50_s[i]
        rsi = rsi_s[i]
        if e50 is None or rsi is None:
            continue

        p_i = sp500_closes[i]
        pp_e50_i = (p_i - e50) / e50 * 100

        # RSI similar (±15pt) e posição vs EMA50 similar (±10pp)
        if abs(rsi - btc_rsi) > 15 or abs(pp_e50_i - btc_pct_vs_ema50) > 10:
            continue

        # Distância relativa ao nível local (resistência se acima, suporte se abaixo)
        if btc_dist_pct > 0:
            local_ref = max(sp500_closes[max(0, i - 20): i])
            dist_i = (local_ref - p_i) / p_i * 100
        else:
            local_ref = min(sp500_closes[max(0, i - 20): i])
            dist_i = (p_i - local_ref) / p_i * 100 * -1

        # Tolerância: ±2pp na distância equivalente
        if abs(dist_i - sp500_dist_equiv) > 2.0:
            continue

        fwd_14d = sp500_closes[i + 14] if i + 14 < n else None
        fwd_30d = sp500_closes[i + 30] if i + 30 < n else None

        if fwd_14d is None:
            continue

        hit_14 = (fwd_14d >= local_ref if btc_dist_pct > 0 else fwd_14d <= local_ref)
        hit_30 = (fwd_30d >= local_ref if btc_dist_pct > 0 else fwd_30d <= local_ref) if fwd_30d else None
        ret_30 = round((fwd_30d - p_i) / p_i * 100, 2) if fwd_30d else None
        ts_i   = sp500_timestamps[i] if i < len(sp500_timestamps) else None
        era    = "jovem" if (ts_i and ts_i < ERA_JOVEM_FIM_TS) else "moderno"

        record = {
            "data":    _ts_to_date(ts_i)[:7] if ts_i else "?",
            "era":     era,
            "rsi":     round(rsi, 1),
            "dist_sp500": round(dist_i, 1),
            "hit_14":  hit_14,
            "hit_30":  hit_30,
            "ret_30":  ret_30,
        }

        if era == "jovem":
            outcomes_jovem.append(record)
        else:
            outcomes_moderno.append(record)

    def agg_level(records, label):
        if not records:
            return {}
        h14 = [r["hit_14"] for r in records]
        h30 = [r["hit_30"] for r in records if r["hit_30"] is not None]
        r30 = [r["ret_30"] for r in records if r["ret_30"] is not None]
        taxa14 = round(sum(h14) / len(h14) * 100, 0)
        taxa30 = round(sum(h30) / len(h30) * 100, 0) if h30 else None
        med_r30 = round(sum(r30) / len(r30), 2) if r30 else None
        return {
            "label":        label,
            "amostras":     len(records),
            "taxa_hit_14d": taxa14,
            "taxa_hit_30d": taxa30,
            "retorno_medio_30d_sp500": med_r30,
            "retorno_equiv_btc_30d":   round(med_r30 * scale_factor, 1) if med_r30 else None,
            "exemplos":     records[-3:],
        }

    total = len(outcomes_jovem) + len(outcomes_moderno)
    if total == 0:
        return {}

    return {
        "direcao":        direcao,
        "dist_btc_pct":   round(btc_dist_pct, 1),
        "dist_sp500_equiv": round(sp500_dist_equiv, 1),
        "scale_factor":   scale_factor,
        "total_analogos": total,
        "jovem":   agg_level(outcomes_jovem,   "S&P500 jovem 1950-1982"),
        "moderno": agg_level(outcomes_moderno, "S&P500 moderno 1983-hoje"),
        "nota": (
            f"BTC a {abs(round(btc_dist_pct,1))}% do alvo = equivalente a "
            f"{abs(round(sp500_dist_equiv,1))}% no S&P500 (fator {scale_factor}x). "
            f"Casos encontrados no S&P500 com distância similar e RSI/EMA análogos: {total}."
        ),
    }


# ── Análise de padrões macro do S&P500 atual ─────────────────────────────────

def sp500_current_analysis(sp500_closes, sp500_timestamps):
    """
    Análise da estrutura atual do S&P500 (últimos candles):
    tendência, posição vs MAs, RSI, distância do ATH.
    Serve para cruzar com BTC e avaliar correlação/divergência.
    """
    if len(sp500_closes) < 200:
        return {}

    n     = len(sp500_closes)
    price = sp500_closes[-1]

    ema50  = _ema(sp500_closes, 50)
    ema200 = _ema(sp500_closes, 200)
    rsi    = _rsi_series(sp500_closes[-50:], 14)[-1]

    ath = max(sp500_closes[-252:])  # ATH do último ano
    dd_1y = round((price - ath) / ath * 100, 1)

    perf_30d  = _perf_from(sp500_closes, n - 31, 30)
    perf_90d  = _perf_from(sp500_closes, n - 91, 90)
    perf_365d = _perf_from(sp500_closes, n - 366, 365) if n > 366 else None

    cross = None
    if ema50 and ema200:
        cross = "Golden Cross (bullish)" if ema50 > ema200 else "Death Cross (bearish)"

    # Volatilidade 30d
    rets  = [(sp500_closes[i] - sp500_closes[i-1]) / sp500_closes[i-1] * 100 for i in range(n - 30, n)]
    vol30 = round((sum(r**2 for r in rets) / 30) ** 0.5, 2)

    # Bear market status (abaixo da SMA50 por >10 dias)
    sma50 = sum(sp500_closes[-50:]) / 50
    days_below_sma50 = sum(1 for c in sp500_closes[-15:] if c < sma50)
    bear_status = ("bear" if days_below_sma50 >= 10
                   else "bull" if days_below_sma50 <= 3
                   else "indefinido")

    return {
        "price":      round(price, 2),
        "ema50":      round(ema50, 2)  if ema50  else None,
        "ema200":     round(ema200, 2) if ema200 else None,
        "rsi":        rsi,
        "cross":      cross,
        "dd_1y_ath":  dd_1y,
        "perf_30d":   perf_30d,
        "perf_90d":   perf_90d,
        "perf_365d":  perf_365d,
        "vol_30d":    vol30,
        "bear_status": bear_status,
        "pct_vs_ema50":  round((price - ema50)  / ema50  * 100, 1) if ema50  else None,
        "pct_vs_ema200": round((price - ema200) / ema200 * 100, 1) if ema200 else None,
    }


# ── Formata bloco de contexto para injetar no prompt ─────────────────────────

def build_sp500_context(sp500_closes, sp500_timestamps,
                         btc_pct_vs_ema50, btc_pct_vs_ema200,
                         btc_rsi, btc_vol_30d,
                         btc_dist_to_target=None):
    """
    Monta o bloco de texto que vai no system prompt do Claude.
    btc_dist_to_target: % de distância do preço BTC ao nível-alvo mencionado
                        (positivo = alvo acima, negativo = abaixo). None se não houver.
    """
    if not sp500_closes:
        return ""

    cur = sp500_current_analysis(sp500_closes, sp500_timestamps)
    ssf = sp500_similar_structure(
        sp500_closes, sp500_timestamps,
        btc_pct_vs_ema50, btc_pct_vs_ema200,
        btc_rsi, btc_vol_30d
    )
    # Level analog só roda quando há alvo de preço na pergunta
    la = (sp500_level_analog(sp500_closes, sp500_timestamps,
                              btc_dist_to_target, btc_rsi, btc_pct_vs_ema50)
          if btc_dist_to_target is not None else {})

    lines = [
        "",
        "════════════════════════════════════════════════════════════════",
        f"S&P500 — ANÁLISE CROSS-MARKET ({len(sp500_closes)} candles reais desde ~1950)",
        "════════════════════════════════════════════════════════════════",
        "",
    ]

    # Situação atual do S&P500
    if cur:
        lines += [
            "SITUAÇÃO ATUAL DO S&P500:",
            f"  Preço: {cur['price']:,.2f} | RSI 14d: {cur['rsi']} | {cur.get('cross','N/A')}",
            f"  Distância do ATH 1 ano: {cur['dd_1y_ath']}%",
            f"  Performance: 30d={cur['perf_30d']}% | 90d={cur['perf_90d']}% | 365d={cur['perf_365d']}%",
            f"  Volatilidade 30d: {cur['vol_30d']}% (BTC costuma ser 3-5x maior)",
            f"  Tendência: {cur['bear_status'].upper()} | Pos. vs EMA50: {cur.get('pct_vs_ema50','N/A')}% | EMA200: {cur.get('pct_vs_ema200','N/A')}%",
            "",
        ]

    # Similar structure cross-market
    if ssf and ssf.get("total_analogos", 0) > 0:
        lines += [
            f"ESTRUTURAS ANÁLOGAS NO S&P500 COM ESTRUTURA TÉCNICA SIMILAR AO BTC ATUAL:",
            f"  (busca normalizada: posição vs EMAs ±10pp, RSI ±18pt)",
            f"  Total de análogos encontrados: {ssf['total_analogos']}",
            f"  {ssf['escala_nota']}",
            "",
        ]

        # ERA JOVEM (mais relevante)
        j = ssf.get("jovem", {})
        if j and j.get("amostras", 0) > 0:
            lines += [
                f"  ── S&P500 JOVEM 1950-1982 (era mais análoga ao BTC) — {j['amostras']} casos ──",
                f"  Retorno médio S&P500: 30d={j.get('media_30d','?')}% | 60d={j.get('media_60d','?')}% | 90d={j.get('media_90d','?')}% | 180d={j.get('media_180d','?')}%",
                f"  Positivo: 30d={j.get('pos_30d_pct','?')}% dos casos | 90d={j.get('pos_90d_pct','?')}% dos casos",
                f"  Alta forte >8% em 30d: {j.get('forte_alta_30d','?')}% | Queda forte >8%: {j.get('forte_queda_30d','?')}%",
                f"  Equivalente BTC estimado (×3.5x): 30d={round(j['media_30d']*3.5,1) if j.get('media_30d') else '?'}% | 90d={round(j['media_90d']*3.5,1) if j.get('media_90d') else '?'}%",
            ]
            if j.get("exemplos"):
                lines.append("  Exemplos (S&P500 % real):")
                for ex in j["exemplos"]:
                    lines.append(
                        f"    {ex['data']} | RSI {ex['rsi']} | pos EMA50: {ex['pp_ema50']}% "
                        f"→ 30d: {ex['fwd30']}% | 90d: {ex.get('fwd90','?')}%"
                    )
            lines.append("")

        # ERA MODERNA (referência secundária)
        m = ssf.get("moderno", {})
        if m and m.get("amostras", 0) > 0:
            lines += [
                f"  ── S&P500 MODERNO 1983-hoje (referência secundária) — {m['amostras']} casos ──",
                f"  Retorno médio: 30d={m.get('media_30d','?')}% | 90d={m.get('media_90d','?')}% | 180d={m.get('media_180d','?')}%",
                f"  Positivo: 30d={m.get('pos_30d_pct','?')}% | 90d={m.get('pos_90d_pct','?')}%",
                f"  Alta forte >8% em 30d: {m.get('forte_alta_30d','?')}% | Queda forte: {m.get('forte_queda_30d','?')}%",
            ]
            if m.get("exemplos"):
                lines.append("  Exemplos recentes (S&P500 % real):")
                for ex in m["exemplos"][-2:]:
                    lines.append(
                        f"    {ex['data']} | RSI {ex['rsi']} | pos EMA50: {ex['pp_ema50']}% "
                        f"→ 30d: {ex['fwd30']}% | 90d: {ex.get('fwd90','?')}%"
                    )
            lines.append("")

    else:
        lines += [
            "  Nenhum análogo S&P500 encontrado para a estrutura atual do BTC.",
            "",
        ]

    # ── Level Analog (só aparece quando há alvo de preço na pergunta) ─────────
    if la and la.get("total_analogos", 0) > 0:
        dir_label = "acima" if la["direcao"] == "acima" else "abaixo"
        lines += [
            f"PROBABILIDADE DE NÍVEL — ANÁLOGO S&P500 ({la['total_analogos']} casos):",
            f"  BTC a {abs(la['dist_btc_pct'])}% do alvo ({dir_label})",
            f"  Equivalente S&P500: {abs(la['dist_sp500_equiv'])}% (fator escala {la['scale_factor']}x)",
            f"  {la['nota']}",
            "",
        ]
        lj = la.get("jovem", {})
        if lj and lj.get("amostras", 0) > 0:
            lines += [
                f"  S&P500 JOVEM ({lj['amostras']} casos, era mais análoga):",
                f"    Taxa de rompimento em 14 dias: {lj['taxa_hit_14d']}%",
                f"    Taxa de rompimento em 30 dias: {lj.get('taxa_hit_30d','?')}%",
                f"    Retorno médio 30d S&P500: {lj.get('retorno_medio_30d_sp500','?')}% "
                f"→ equiv. BTC: {lj.get('retorno_equiv_btc_30d','?')}%",
            ]
            if lj.get("exemplos"):
                lines.append("    Exemplos:")
                for ex in lj["exemplos"]:
                    hit = "ROMPEU" if ex.get("hit_30") else "nao rompeu"
                    lines.append(
                        f"      {ex['data']} | RSI {ex['rsi']} | dist {ex['dist_sp500']}% "
                        f"→ 30d: {ex.get('ret_30','?')}% ({hit})"
                    )
            lines.append("")

        lm = la.get("moderno", {})
        if lm and lm.get("amostras", 0) > 0:
            lines += [
                f"  S&P500 MODERNO ({lm['amostras']} casos, referência secundária):",
                f"    Taxa de rompimento em 14 dias: {lm['taxa_hit_14d']}%",
                f"    Taxa de rompimento em 30 dias: {lm.get('taxa_hit_30d','?')}%",
                f"    Retorno médio 30d S&P500: {lm.get('retorno_medio_30d_sp500','?')}% "
                f"→ equiv. BTC: {lm.get('retorno_equiv_btc_30d','?')}%",
                "",
            ]

    lines += [
        "COMO USAR ESSES DADOS:",
        "  1. S&P500 e analise SECUNDARIA — o historico proprio do BTC tem prioridade",
        "  2. Quando BTC tem <5 analogos proprios, o S&P500 jovem e a melhor expansao de amostra",
        "  3. Para nivel especifico: taxa de rompimento do S&P500 em situacao equivalente ja esta calculada acima",
        "  4. Multiplicar retornos do S&P500 por 3-5x para estimar magnitude equivalente no BTC",
        "  5. Ajustar duracao: dividir por 1.5-2x (BTC se move mais rapido)",
        "  6. Correlacao BTC x S&P500 atual tende a ser alta (0.5-0.7)",
        "════════════════════════════════════════════════════════════════",
    ]

    return "\n".join(lines)
