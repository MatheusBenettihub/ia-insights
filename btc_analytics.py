from datetime import datetime, timezone, timedelta

def identify_patterns(closes, vols):
    """Identifica padrões automáticos no histórico completo."""
    if len(closes) < 200:
        return {}
    
    results = {}
    n = len(closes)
    price = closes[-1]

    # ── Drawdowns históricos ──────────────────────────────────────────────────
    peak = closes[0]
    peak_i = 0
    drawdowns = []
    for i, c in enumerate(closes):
        if c > peak:
            peak = c
            peak_i = i
        dd = (c - peak) / peak * 100
        if dd < -20:
            drawdowns.append((i, dd, peak, c))

    # Drawdown atual
    all_time_high = max(closes)
    ath_i = closes.index(all_time_high)
    current_dd = (price - all_time_high) / all_time_high * 100

    results["ath"] = round(all_time_high, 0)
    results["ath_days_ago"] = n - 1 - ath_i
    results["current_dd"] = round(current_dd, 1)

    # ── Sequências de alta/baixa ──────────────────────────────────────────────
    current_streak = 1
    direction = "alta" if closes[-1] > closes[-2] else "baixa"
    for i in range(n - 2, 0, -1):
        if direction == "alta" and closes[i] > closes[i-1]:
            current_streak += 1
        elif direction == "baixa" and closes[i] < closes[i-1]:
            current_streak += 1
        else:
            break
    results["streak"] = f"{current_streak} dias consecutivos de {direction}"

    # ── Volatilidade histórica ────────────────────────────────────────────────
    returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, n)]
    vol_30d  = (sum(r**2 for r in returns[-30:])  / 30)  ** 0.5
    vol_90d  = (sum(r**2 for r in returns[-90:])  / 90)  ** 0.5
    vol_365d = (sum(r**2 for r in returns[-365:]) / 365) ** 0.5
    results["vol_30d"]  = round(vol_30d, 2)
    results["vol_90d"]  = round(vol_90d, 2)
    results["vol_365d"] = round(vol_365d, 2)
    vol_status = "ALTA" if vol_30d > vol_90d * 1.2 else "BAIXA" if vol_30d < vol_90d * 0.8 else "NORMAL"
    results["vol_status"] = vol_status

    # ── Maiores movimentos históricos ─────────────────────────────────────────
    sorted_returns = sorted(enumerate(returns), key=lambda x: x[1])
    results["top5_quedas"]  = [(i+1, round(r, 1)) for i, r in sorted_returns[:5]]
    results["top5_altas"]   = [(i+1, round(r, 1)) for i, r in sorted_returns[-5:][::-1]]

    # ── Performance por período ───────────────────────────────────────────────
    def perf(days):
        if len(closes) > days:
            return round((closes[-1] - closes[-days]) / closes[-days] * 100, 1)
        return None

    results["perf_7d"]   = perf(7)
    results["perf_30d"]  = perf(30)
    results["perf_90d"]  = perf(90)
    results["perf_180d"] = perf(180)
    results["perf_365d"] = perf(365)

    # ── Detecção de ciclos (topos e fundos locais) ────────────────────────────
    def find_local_extremes(data, window=30):
        tops, bottoms = [], []
        for i in range(window, len(data) - window):
            segment = data[i-window:i+window+1]
            if data[i] == max(segment):
                tops.append((i, data[i]))
            elif data[i] == min(segment):
                bottoms.append((i, data[i]))
        return tops, bottoms

    tops, bottoms = find_local_extremes(closes, window=20)

    # Últimos 3 topos e fundos locais
    recent_tops    = tops[-3:]    if len(tops)    >= 3 else tops
    recent_bottoms = bottoms[-3:] if len(bottoms) >= 3 else bottoms

    results["recent_tops"]    = [(n-1-i, round(p, 0)) for i, p in recent_tops]
    results["recent_bottoms"] = [(n-1-i, round(p, 0)) for i, p in recent_bottoms]

    # ── Higher Highs / Lower Lows (estrutura de mercado) ────────────────────
    if len(recent_tops) >= 2:
        hh = recent_tops[-1][1] > recent_tops[-2][1]
        results["market_structure_tops"] = "Higher Highs (bullish)" if hh else "Lower Highs (bearish)"
    if len(recent_bottoms) >= 2:
        hl = recent_bottoms[-1][1] > recent_bottoms[-2][1]
        results["market_structure_bottoms"] = "Higher Lows (bullish)" if hl else "Lower Lows (bearish)"

    # ── Volume analysis ───────────────────────────────────────────────────────
    if vols:
        vol_avg_7d   = sum(vols[-7:])  / 7
        vol_avg_30d  = sum(vols[-30:]) / 30
        vol_avg_90d  = sum(vols[-90:]) / 90
        results["vol_trend"] = (
            "CRESCENTE (bullish confirma)" if vol_avg_7d > vol_avg_30d * 1.15
            else "DECRESCENTE (cautela)"   if vol_avg_7d < vol_avg_30d * 0.85
            else "ESTÁVEL"
        )
        results["vol_vs_90d"] = round(vol_avg_30d / vol_avg_90d, 2)

    # ── Padrões de retorno após quedas (backtesting simples) ─────────────────
    big_drops = []
    for i in range(1, n):
        ret = returns[i-1]
        if ret < -5:  # quedas de mais de 5% em um dia
            fwd_7  = perf_from(closes, i, 7)
            fwd_30 = perf_from(closes, i, 30)
            big_drops.append((ret, fwd_7, fwd_30))

    if big_drops:
        avg_fwd7  = sum(x[1] for x in big_drops if x[1] is not None) / len(big_drops)
        avg_fwd30 = sum(x[2] for x in big_drops if x[2] is not None) / len(big_drops)
        results["after_5pct_drop_avg_7d"]  = round(avg_fwd7, 1)
        results["after_5pct_drop_avg_30d"] = round(avg_fwd30, 1)
        results["big_drops_count"] = len(big_drops)

    # ── Correlação preço vs volume (últimos 30d) ──────────────────────────────
    if vols and len(vols) >= 30:
        pr30  = closes[-30:]
        vl30  = vols[-30:]
        mean_p = sum(pr30) / 30
        mean_v = sum(vl30) / 30
        cov    = sum((pr30[i]-mean_p)*(vl30[i]-mean_v) for i in range(30)) / 30
        std_p  = (sum((x-mean_p)**2 for x in pr30) / 30) ** 0.5
        std_v  = (sum((x-mean_v)**2 for x in vl30) / 30) ** 0.5
        results["price_vol_corr_30d"] = round(cov / (std_p * std_v), 2) if std_p and std_v else None

    # ── Resumo dos níveis-chave ───────────────────────────────────────────────
    results["price_levels"] = {
        "resistance_1": round(max(closes[-30:]), 0),
        "resistance_2": round(max(closes[-90:]), 0),
        "support_1":    round(min(closes[-30:]), 0),
        "support_2":    round(min(closes[-90:]), 0),
        "median_90d":   round(sorted(closes[-90:])[45], 0),
    }

    return results


def perf_from(closes, start_i, days):
    end_i = start_i + days
    if end_i < len(closes):
        return round((closes[end_i] - closes[start_i]) / closes[start_i] * 100, 1)
    return None


def build_analytics_context(closes, vols):
    """Gera o bloco de contexto analítico para injetar no prompt."""
    if len(closes) < 50:
        return ""

    p = identify_patterns(closes, vols)
    if not p:
        return ""

    n = len(closes)
    price = closes[-1]

    # Performance
    perf_lines = []
    for label, key in [("7d", "perf_7d"), ("30d", "perf_30d"),
                        ("90d", "perf_90d"), ("180d", "perf_180d"), ("365d", "perf_365d")]:
        v = p.get(key)
        if v is not None:
            icon = "▲" if v > 0 else "▼"
            perf_lines.append(f"  {label}: {icon}{v}%")

    # Topos e fundos recentes (em dias atrás)
    tops_str = " | ".join([f"${pr:,.0f} ({d}d atrás)" for d, pr in p.get("recent_tops", [])])
    bots_str = " | ".join([f"${pr:,.0f} ({d}d atrás)" for d, pr in p.get("recent_bottoms", [])])

    # Maiores quedas/altas históricas
    quedas_str = " | ".join([f"dia {d}: {r}%" for d, r in p.get("top5_quedas", [])])
    altas_str  = " | ".join([f"dia {d}: +{r}%" for d, r in p.get("top5_altas", [])])

    ctx = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANÁLISE QUANTITATIVA AUTOMÁTICA — {n} CANDLES REAIS
(Calculado sobre dados reais da Binance, atualizado a cada hora)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PERFORMANCE REAL:
{chr(10).join(perf_lines)}

ATH HISTÓRICO (Binance): ${p['ath']:,.0f} ({p['ath_days_ago']} dias atrás)
Drawdown atual do ATH: {p['current_dd']}%
Sequência atual: {p.get('streak', 'N/A')}

ESTRUTURA DE MERCADO (topos/fundos locais):
  Últimos topos:  {tops_str or 'N/A'}
  Últimos fundos: {bots_str or 'N/A'}
  Estrutura topos:  {p.get('market_structure_tops', 'N/A')}
  Estrutura fundos: {p.get('market_structure_bottoms', 'N/A')}

VOLATILIDADE (desvio padrão diário):
  30d: {p.get('vol_30d')}% | 90d: {p.get('vol_90d')}% | 365d: {p.get('vol_365d')}%
  Status: {p.get('vol_status')} (30d vs 90d)

VOLUME:
  Tendência 7d vs 30d: {p.get('vol_trend', 'N/A')}
  Ratio 30d/90d: {p.get('vol_vs_90d', 'N/A')}x
  Correlação preço×volume (30d): {p.get('price_vol_corr_30d', 'N/A')}
  (>0.5 = volume confirmando direção | <0 = divergência)

NÍVEIS-CHAVE (calculados):
  Resistências: ${p['price_levels']['resistance_1']:,.0f} (30d) | ${p['price_levels']['resistance_2']:,.0f} (90d)
  Suportes:     ${p['price_levels']['support_1']:,.0f} (30d)    | ${p['price_levels']['support_2']:,.0f} (90d)
  Mediana 90d:  ${p['price_levels']['median_90d']:,.0f}

BACKTESTING AUTOMÁTICO:
  Após quedas >5% em 1 dia ({p.get('big_drops_count', 0)} ocorrências históricas):
    Retorno médio 7d depois:  {p.get('after_5pct_drop_avg_7d', 'N/A')}%
    Retorno médio 30d depois: {p.get('after_5pct_drop_avg_30d', 'N/A')}%

TOP 5 MAIORES QUEDAS DIÁRIAS (histórico completo):
  {quedas_str}

TOP 5 MAIORES ALTAS DIÁRIAS (histórico completo):
  {altas_str}

REGRA: Esses dados são calculados automaticamente sobre o histórico real.
Use-os para embasar análises com frequência histórica real, não estimativas.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    return ctx