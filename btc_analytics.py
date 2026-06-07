from datetime import datetime, timezone, timedelta


# ── Helpers internos ─────────────────────────────────────────────────────────

def _ema_series(closes, period):
    """Retorna lista de EMAs calculadas ponto a ponto (mesmo tamanho de closes)."""
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
    """Retorna lista de RSIs calculados ponto a ponto."""
    result = [None] * period
    for i in range(period, len(closes)):
        seg = closes[i - period: i + 1]
        gains  = [max(seg[j] - seg[j-1], 0) for j in range(1, len(seg))]
        losses = [max(seg[j-1] - seg[j], 0) for j in range(1, len(seg))]
        ag = sum(gains)  / period
        al = sum(losses) / period
        rsi = 100.0 if al == 0 else round(100 - (100 / (1 + ag / al)), 1)
        result.append(rsi)
    return result

def perf_from(closes, start_i, days):
    end_i = start_i + days
    if end_i < len(closes):
        return round((closes[end_i] - closes[start_i]) / closes[start_i] * 100, 1)
    return None


# ── 1. Support / Resistance por clustering de preço ─────────────────────────

def find_sr_zones(closes, price, lookback=365, n_zones=6):
    """
    Zonas de S/R baseadas em onde o preço ficou mais tempo (price clustering).
    Muito mais preciso que usar max/min de janela.
    """
    recent = closes[-lookback:] if len(closes) > lookback else closes
    if len(recent) < 30:
        return []

    p_min = min(recent)
    p_max = max(recent)
    n_buckets = 60
    bucket_size = (p_max - p_min) / n_buckets

    buckets = [0] * n_buckets
    for c in recent:
        idx = min(int((c - p_min) / bucket_size), n_buckets - 1)
        buckets[idx] += 1

    # Localiza picos no histograma de preços (zonas de alto tráfego)
    zones = []
    for i in range(1, n_buckets - 1):
        if buckets[i] >= buckets[i-1] and buckets[i] >= buckets[i+1] and buckets[i] > len(recent) * 0.025:
            zone_price = p_min + (i + 0.5) * bucket_size
            touches = buckets[i]
            zone_type = "suporte" if zone_price < price else "resistência"
            zones.append((round(zone_price, 0), touches, zone_type))

    zones.sort(key=lambda x: abs(x[0] - price))
    return zones[:n_zones * 2]


# ── 2. Detector de compressão / Bollinger Squeeze ────────────────────────────

def detect_compression(closes, window=20):
    """
    Detecta se o preço está em compressão (Bollinger Squeeze).
    Calcula percentil histórico da largura atual das Bandas e ATR.
    """
    if len(closes) < window + 60:
        return {}

    n = len(closes)
    result = {}

    def bb_width(seg):
        s = sum(seg) / len(seg)
        std = (sum((x - s) ** 2 for x in seg) / len(seg)) ** 0.5
        return (std * 4) / s if s else 0

    current_width = bb_width(closes[-window:])

    historical_widths = [
        bb_width(closes[i - window: i])
        for i in range(window + 30, n - 1)
    ]

    if historical_widths:
        pct_rank = sum(1 for w in historical_widths if w < current_width) / len(historical_widths) * 100
        result["bb_width_pct_rank"] = round(pct_rank, 0)
        result["squeeze_ativo"] = pct_rank < 25  # largura no quartil inferior = squeeze

        # Backtest: após squeeze, o que aconteceu em 14 dias?
        if pct_rank < 25:
            threshold = sorted(historical_widths)[int(len(historical_widths) * 0.25)]
            outcomes = []
            for i in range(window + 30, n - 15):
                if bb_width(closes[i - window: i]) < threshold:
                    fwd = perf_from(closes, i, 14)
                    if fwd is not None:
                        outcomes.append(fwd)
            if outcomes:
                result["apos_squeeze_media_14d"]  = round(sum(outcomes) / len(outcomes), 1)
                result["apos_squeeze_alta_pct"]   = round(sum(1 for x in outcomes if x > 8)  / len(outcomes) * 100, 0)
                result["apos_squeeze_queda_pct"]  = round(sum(1 for x in outcomes if x < -8) / len(outcomes) * 100, 0)
                result["apos_squeeze_amostras"]   = len(outcomes)

    # ATR contraction (7d vs 30d)
    daily_ranges = [abs(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, n)]
    if len(daily_ranges) >= 30:
        atr7  = sum(daily_ranges[-7:])  / 7
        atr30 = sum(daily_ranges[-30:]) / 30
        result["atr_7d"]         = round(atr7, 2)
        result["atr_30d"]        = round(atr30, 2)
        result["atr_contracao"]  = atr7 < atr30 * 0.75
        result["atr_expansao"]   = atr7 > atr30 * 1.40

    return result


# ── 3. Detector de padrões de gráfico ────────────────────────────────────────

def _find_extremes(data, w=10):
    tops, bottoms = [], []
    for i in range(w, len(data) - w):
        seg = data[i - w: i + w + 1]
        if data[i] == max(seg):
            tops.append((i, data[i]))
        elif data[i] == min(seg):
            bottoms.append((i, data[i]))
    return tops, bottoms

def detect_patterns(closes):
    """
    Detecta: double top/bottom, triângulo simétrico/ascendente/descendente,
    bull/bear flag, head & shoulders (simplificado).
    """
    if len(closes) < 60:
        return {}

    result = {}
    price = closes[-1]
    recent = closes[-200:]
    tops, bottoms = _find_extremes(recent, w=10)

    # ── Double Top ────────────────────────────────────────────────────────────
    if len(tops) >= 2:
        t1_i, t1_p = tops[-2]
        t2_i, t2_p = tops[-1]
        diff_pct = abs(t1_p - t2_p) / t1_p * 100
        if diff_pct < 3 and t2_i > t1_i:
            neck_range = recent[t1_i: t2_i + 1] if t2_i > t1_i else []
            neckline = min(neck_range) if neck_range else None
            if neckline:
                alvo = neckline - (t1_p - neckline)
                result["double_top"] = {
                    "detectado":   True,
                    "topo1":       round(t1_p, 0),
                    "topo2":       round(t2_p, 0),
                    "neckline":    round(neckline, 0),
                    "alvo_queda":  round(alvo, 0),
                    "confirmado":  price < neckline,
                }

    # ── Double Bottom ─────────────────────────────────────────────────────────
    if len(bottoms) >= 2:
        b1_i, b1_p = bottoms[-2]
        b2_i, b2_p = bottoms[-1]
        diff_pct = abs(b1_p - b2_p) / b1_p * 100
        if diff_pct < 3 and b2_i > b1_i:
            neck_range = recent[b1_i: b2_i + 1] if b2_i > b1_i else []
            neckline = max(neck_range) if neck_range else None
            if neckline:
                alvo = neckline + (neckline - b1_p)
                result["double_bottom"] = {
                    "detectado":   True,
                    "fundo1":      round(b1_p, 0),
                    "fundo2":      round(b2_p, 0),
                    "neckline":    round(neckline, 0),
                    "alvo_alta":   round(alvo, 0),
                    "confirmado":  price > neckline,
                }

    # ── Triângulo (últimos 60 candles) ────────────────────────────────────────
    seg60 = closes[-60:]
    tops60, bots60 = _find_extremes(seg60, w=5)
    if len(tops60) >= 2 and len(bots60) >= 2:
        tops_desc = tops60[-1][1] < tops60[-2][1]   # topos caindo
        bots_asc  = bots60[-1][1] > bots60[-2][1]   # fundos subindo
        if tops_desc and bots_asc:
            result["triangulo"] = "SIMÉTRICO — compressão, breakout iminente em qualquer direção"
        elif tops_desc and not bots_asc:
            result["triangulo"] = "DESCENDENTE — viés bearish, fundos não suportam"
        elif not tops_desc and bots_asc:
            result["triangulo"] = "ASCENDENTE — viés bullish, comprador absorvendo oferta"

    # ── Flag / Pennant ────────────────────────────────────────────────────────
    if len(closes) >= 25:
        move_pole = (closes[-11] - closes[-21]) / closes[-21] * 100
        range_flag = ((max(closes[-7:]) - min(closes[-7:])) / closes[-8]) * 100 if len(closes) >= 8 else 999

        if abs(move_pole) > 12 and range_flag < 5:
            tipo = "BULL FLAG" if move_pole > 0 else "BEAR FLAG"
            continuacao = "alta" if move_pole > 0 else "queda"
            result["flag"] = {
                "tipo":         tipo,
                "mastro_move":  round(move_pole, 1),
                "range_flag":   round(range_flag, 1),
                "bias":         f"continuação de {continuacao} — em ~65-70% dos casos históricos do BTC",
                "alvo_medido":  round(closes[-1] * (1 + move_pole / 100), 0),
            }

    # ── Head & Shoulders (simplificado) ──────────────────────────────────────
    if len(tops) >= 3:
        left, head, right = tops[-3], tops[-2], tops[-1]
        shoulder_diff = abs(left[1] - right[1]) / left[1] * 100
        if head[1] > left[1] and head[1] > right[1] and shoulder_diff < 5:
            # Neckline = média dos fundos entre ombros
            bt = [b for b in bottoms if left[0] < b[0] < right[0]]
            if len(bt) >= 2:
                neckline = sum(b[1] for b in bt) / len(bt)
                alvo = neckline - (head[1] - neckline)
                result["head_and_shoulders"] = {
                    "detectado":    True,
                    "ombro_esq":    round(left[1], 0),
                    "cabeca":       round(head[1], 0),
                    "ombro_dir":    round(right[1], 0),
                    "neckline":     round(neckline, 0),
                    "alvo_queda":   round(alvo, 0),
                    "confirmado":   price < neckline,
                }

    return result


# ── 4. Similar Structure Finder ──────────────────────────────────────────────

def similar_structure_finder(closes):
    """
    Varre TODO o histórico disponível e encontra períodos com estrutura
    ESTRUTURAL parecida com a atual.

    Filtro principal: distância do ATH do ciclo (±15pp)
    Filtro secundário: posição vs EMA50 (±10pp)
    RSI: NÃO é usado como filtro — é lagging e aparece igual em bull e bear.

    Isso garante que -50% do ATH só casa com outros períodos de -50% do ATH,
    nunca com bull markets que acidentalmente tenham o mesmo RSI.
    """
    if len(closes) < 250:
        return {}

    n = len(closes)
    price = closes[-1]

    # Pré-computa séries de EMAs em O(n)
    ema50_all  = _ema_series(closes, 50)
    ema200_all = _ema_series(closes, 200)

    e50_now  = ema50_all[-1]
    e200_now = ema200_all[-1]
    if not e50_now or not e200_now:
        return {}

    pct_vs_e50 = (price - e50_now) / e50_now * 100

    # ATH do ciclo atual (últimos 2 anos)
    lookback_ath = min(n, 730)
    cycle_ath_now = max(closes[-lookback_ath:])
    pct_from_ath_now = (price - cycle_ath_now) / cycle_ath_now * 100

    # Death/Golden cross atual
    cross_now = 1 if e50_now > e200_now else -1

    # Pré-computa RSI (só para exibição — não é critério de filtro)
    rsi_all = _rsi_series(closes, 14)

    matches = []
    for i in range(200, n - 91):
        e50_i  = ema50_all[i]
        e200_i = ema200_all[i]
        if e50_i is None or e200_i is None:
            continue

        p_i = closes[i]

        # ATH do ciclo no ponto i (últimos 2 anos a partir de i)
        lb_i  = min(i, 730)
        ath_i = max(closes[i - lb_i: i + 1])
        pct_ath_i = (p_i - ath_i) / ath_i * 100

        pp_e50_i = (p_i - e50_i) / e50_i * 100
        cross_i  = 1 if e50_i > e200_i else -1

        # FILTRO PRIMÁRIO: distância do ATH ±15pp (separa bear de bull)
        if abs(pct_ath_i - pct_from_ath_now) > 15:
            continue

        # FILTRO SECUNDÁRIO: posição vs EMA50 ±10pp
        if abs(pp_e50_i - pct_vs_e50) > 10:
            continue

        same_cross = cross_i == cross_now

        fwd30 = perf_from(closes, i, 30)
        fwd60 = perf_from(closes, i, 60)
        fwd90 = perf_from(closes, i, 90)
        if fwd30 is not None:
            matches.append({
                "dias_atras":  n - 1 - i,
                "preco":       round(p_i, 0),
                "pct_ath":     round(pct_ath_i, 1),
                "pct_e50":     round(pp_e50_i, 1),
                "rsi":         round(rsi_all[i], 1) if rsi_all[i] is not None else None,
                "same_cross":  same_cross,
                "fwd30":       fwd30,
                "fwd60":       fwd60,
                "fwd90":       fwd90,
            })

    if not matches:
        return {}

    # Prioriza matches com mesmo cross (bear=bear, bull=bull)
    same_cross_matches = [m for m in matches if m["same_cross"]]
    primary = same_cross_matches if len(same_cross_matches) >= 3 else matches

    fwd30s = [m["fwd30"] for m in primary]
    fwd60s = [m["fwd60"] for m in primary if m["fwd60"] is not None]
    fwd90s = [m["fwd90"] for m in primary if m["fwd90"] is not None]

    result = {
        "analogos_encontrados": len(primary),
        "criterio":             f"ATH do ciclo ±15pp (atual: {round(pct_from_ath_now,1)}%), EMA50 ±10pp",
        "ultimos_3_analogos":   primary[-3:],
    }
    if fwd30s:
        result["media_30d"]        = round(sum(fwd30s) / len(fwd30s), 1)
        result["positivo_30d_pct"] = round(sum(1 for x in fwd30s if x > 0)  / len(fwd30s) * 100, 0)
        result["acima_10pct_30d"]  = round(sum(1 for x in fwd30s if x > 10) / len(fwd30s) * 100, 0)
        result["abaixo_10pct_30d"] = round(sum(1 for x in fwd30s if x < -10) / len(fwd30s) * 100, 0)
    if fwd60s:
        result["media_60d"]        = round(sum(fwd60s) / len(fwd60s), 1)
        result["positivo_60d_pct"] = round(sum(1 for x in fwd60s if x > 0)  / len(fwd60s) * 100, 0)
    if fwd90s:
        result["media_90d"]        = round(sum(fwd90s) / len(fwd90s), 1)
        result["positivo_90d_pct"] = round(sum(1 for x in fwd90s if x > 0)  / len(fwd90s) * 100, 0)

    return result


# ── 5. Level Breakout Probability ────────────────────────────────────────────

def level_breakout_probability(closes, target_price):
    """
    Probabilidade histórica de atingir um nível-alvo em 14/30 dias,
    a partir de situações onde o preço estava na mesma distância relativa
    de uma resistência ou suporte local.
    """
    if len(closes) < 120:
        return {}

    n = len(closes)
    price = closes[-1]
    dist_pct = (target_price - price) / price * 100  # + = alvo acima, - = abaixo

    outcomes_14, outcomes_30 = [], []

    for i in range(30, n - 31):
        p_i = closes[i]
        if dist_pct > 0:
            local_ref = max(closes[i - 30: i])  # resistência local
            dist_i = (local_ref - p_i) / p_i * 100
        else:
            local_ref = min(closes[i - 30: i])  # suporte local
            dist_i = (p_i - local_ref) / p_i * 100 * -1

        if abs(dist_i - dist_pct) < 2.5:  # situação similar (±2.5pp)
            fwd14 = closes[i + 14]
            fwd30 = closes[i + 30]
            hit14 = fwd14 >= local_ref if dist_pct > 0 else fwd14 <= local_ref
            hit30 = fwd30 >= local_ref if dist_pct > 0 else fwd30 <= local_ref
            outcomes_14.append(hit14)
            outcomes_30.append(hit30)

    if len(outcomes_14) < 5:
        return {}

    taxa14 = round(sum(outcomes_14) / len(outcomes_14) * 100, 0)
    taxa30 = round(sum(outcomes_30) / len(outcomes_30) * 100, 0)

    return {
        "alvo":          round(target_price, 0),
        "distancia_pct": round(dist_pct, 1),
        "direcao":       "acima" if dist_pct > 0 else "abaixo",
        "taxa_hit_14d":  taxa14,
        "taxa_hit_30d":  taxa30,
        "amostras":      len(outcomes_14),
        "resumo": (
            f"Em {len(outcomes_14)} situações históricas similares: "
            f"atingiu o nível em 14 dias em {taxa14}% dos casos, "
            f"em 30 dias em {taxa30}% dos casos."
        ),
    }


# ── 6. Padrões históricos (existente) ────────────────────────────────────────

def identify_patterns(closes, vols):
    if len(closes) < 200:
        return {}

    results = {}
    n = len(closes)
    price = closes[-1]

    # Drawdown atual
    all_time_high = max(closes)
    ath_i = closes.index(all_time_high)
    results["ath"]          = round(all_time_high, 0)
    results["ath_days_ago"] = n - 1 - ath_i
    results["current_dd"]   = round((price - all_time_high) / all_time_high * 100, 1)

    # Sequência de candles
    direction = "alta" if closes[-1] > closes[-2] else "baixa"
    streak = 1
    for i in range(n - 2, 0, -1):
        if direction == "alta" and closes[i] > closes[i-1]: streak += 1
        elif direction == "baixa" and closes[i] < closes[i-1]: streak += 1
        else: break
    results["streak"] = f"{streak} dias consecutivos de {direction}"

    # Volatilidade histórica
    returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, n)]
    vol_30d  = (sum(r**2 for r in returns[-30:])  / 30)  ** 0.5
    vol_90d  = (sum(r**2 for r in returns[-90:])  / 90)  ** 0.5
    vol_365d = (sum(r**2 for r in returns[-365:]) / 365) ** 0.5
    results["vol_30d"]  = round(vol_30d, 2)
    results["vol_90d"]  = round(vol_90d, 2)
    results["vol_365d"] = round(vol_365d, 2)
    results["vol_status"] = ("ALTA" if vol_30d > vol_90d * 1.2
                              else "BAIXA" if vol_30d < vol_90d * 0.8
                              else "NORMAL")

    # Maiores movimentos
    sorted_r = sorted(enumerate(returns), key=lambda x: x[1])
    results["top5_quedas"] = [(i+1, round(r, 1)) for i, r in sorted_r[:5]]
    results["top5_altas"]  = [(i+1, round(r, 1)) for i, r in sorted_r[-5:][::-1]]

    # Performance por período
    def perf(d):
        return round((closes[-1] - closes[-d]) / closes[-d] * 100, 1) if len(closes) > d else None

    for label, d in [("perf_7d", 7), ("perf_30d", 30), ("perf_90d", 90),
                     ("perf_180d", 180), ("perf_365d", 365)]:
        results[label] = perf(d)

    # Topos e fundos locais
    tops, bottoms = _find_extremes(closes, w=20)
    recent_tops    = tops[-3:]    if len(tops)    >= 3 else tops
    recent_bottoms = bottoms[-3:] if len(bottoms) >= 3 else bottoms
    results["recent_tops"]    = [(n - 1 - i, round(p, 0)) for i, p in recent_tops]
    results["recent_bottoms"] = [(n - 1 - i, round(p, 0)) for i, p in recent_bottoms]

    if len(recent_tops) >= 2:
        results["structure_tops"] = ("Higher Highs — bullish" if recent_tops[-1][1] > recent_tops[-2][1]
                                      else "Lower Highs — bearish")
    if len(recent_bottoms) >= 2:
        results["structure_bottoms"] = ("Higher Lows — bullish" if recent_bottoms[-1][1] > recent_bottoms[-2][1]
                                         else "Lower Lows — bearish")

    # Volume
    if vols:
        va7  = sum(vols[-7:])  / 7
        va30 = sum(vols[-30:]) / 30
        va90 = sum(vols[-90:]) / 90
        results["vol_trend"]  = ("CRESCENTE" if va7 > va30 * 1.15
                                  else "DECRESCENTE" if va7 < va30 * 0.85
                                  else "ESTÁVEL")
        results["vol_vs_90d"] = round(va30 / va90, 2)

    # Backtesting: após quedas >5%
    big_drops = []
    for i in range(1, n):
        ret = returns[i-1]
        if ret < -5:
            fwd7  = perf_from(closes, i, 7)
            fwd30 = perf_from(closes, i, 30)
            if fwd7 is not None:
                big_drops.append((ret, fwd7, fwd30))

    if big_drops:
        results["after_5pct_drop_avg_7d"]  = round(sum(x[1] for x in big_drops) / len(big_drops), 1)
        results["after_5pct_drop_avg_30d"] = round(sum(x[2] for x in big_drops if x[2]) / len(big_drops), 1)
        results["big_drops_count"] = len(big_drops)

    # Níveis simples (compatibilidade)
    results["price_levels"] = {
        "resistance_1": round(max(closes[-30:]), 0),
        "resistance_2": round(max(closes[-90:]), 0),
        "support_1":    round(min(closes[-30:]), 0),
        "support_2":    round(min(closes[-90:]), 0),
        "median_90d":   round(sorted(closes[-90:])[45], 0),
    }

    # Correlação preço × volume
    if vols and len(vols) >= 30:
        pr30 = closes[-30:]; vl30 = vols[-30:]
        mp = sum(pr30)/30;    mv = sum(vl30)/30
        cov   = sum((pr30[i]-mp)*(vl30[i]-mv) for i in range(30)) / 30
        std_p = (sum((x-mp)**2 for x in pr30)/30)**0.5
        std_v = (sum((x-mv)**2 for x in vl30)/30)**0.5
        results["price_vol_corr_30d"] = round(cov/(std_p*std_v), 2) if std_p and std_v else None

    return results


# ── 7. build_analytics_context — injeta tudo no prompt ───────────────────────

def build_analytics_context(closes, vols):
    if len(closes) < 50:
        return ""

    p = identify_patterns(closes, vols)
    if not p:
        return ""

    n     = len(closes)
    price = closes[-1]

    # ── Performance ──────────────────────────────────────────────────────────
    perf_lines = []
    for label, key in [("7d","perf_7d"),("30d","perf_30d"),("90d","perf_90d"),
                        ("180d","perf_180d"),("365d","perf_365d")]:
        v = p.get(key)
        if v is not None:
            perf_lines.append(f"  {label}: {'▲' if v>0 else '▼'}{v}%")

    tops_str = " | ".join([f"${pr:,.0f} ({d}d atrás)" for d,pr in p.get("recent_tops",[])])
    bots_str = " | ".join([f"${pr:,.0f} ({d}d atrás)" for d,pr in p.get("recent_bottoms",[])])
    quedas_s = " | ".join([f"dia {d}: {r}%"  for d,r in p.get("top5_quedas",[])])
    altas_s  = " | ".join([f"dia {d}: +{r}%" for d,r in p.get("top5_altas",[])])

    # ── S/R por clustering ────────────────────────────────────────────────────
    sr_zones = find_sr_zones(closes, price)
    sr_lines = []
    for zp, touches, ztype in sr_zones:
        dist = round((zp - price) / price * 100, 1)
        sr_lines.append(f"  ${zp:,.0f} ({ztype}, {touches} toques, dist: {dist:+.1f}%)")

    # ── Compressão ────────────────────────────────────────────────────────────
    comp = detect_compression(closes)
    comp_lines = []
    if comp.get("bb_width_pct_rank") is not None:
        sq = "⚠️ SQUEEZE ATIVO" if comp.get("squeeze_ativo") else "normal"
        comp_lines.append(f"  Largura BB (percentil histórico): {comp['bb_width_pct_rank']}% — {sq}")
    if comp.get("apos_squeeze_amostras"):
        comp_lines.append(
            f"  Após squeezes similares ({comp['apos_squeeze_amostras']} casos): "
            f"média 14d = {comp['apos_squeeze_media_14d']}% | "
            f"alta >8%: {comp['apos_squeeze_alta_pct']}% das vezes | "
            f"queda >8%: {comp['apos_squeeze_queda_pct']}% das vezes"
        )
    if comp.get("atr_7d"):
        atr_s = "CONTRAINDO" if comp.get("atr_contracao") else "EXPANDINDO" if comp.get("atr_expansao") else "estável"
        comp_lines.append(f"  ATR diário — 7d: {comp['atr_7d']}% | 30d: {comp['atr_30d']}% ({atr_s})")

    # ── Padrões de gráfico ────────────────────────────────────────────────────
    patterns = detect_patterns(closes)
    pat_lines = []
    if "triangulo" in patterns:
        pat_lines.append(f"  Triângulo: {patterns['triangulo']}")
    if "flag" in patterns:
        f = patterns["flag"]
        pat_lines.append(f"  {f['tipo']} — mastro: {f['mastro_move']}% | range: {f['range_flag']}% | {f['bias']} | alvo: ${f['alvo_medido']:,.0f}")
    if "double_top" in patterns:
        dt = patterns["double_top"]
        conf = "CONFIRMADO" if dt["confirmado"] else "não confirmado"
        pat_lines.append(f"  Double Top {conf} — topos ${dt['topo1']:,.0f}/${dt['topo2']:,.0f} | neckline ${dt['neckline']:,.0f} | alvo queda ${dt['alvo_queda']:,.0f}")
    if "double_bottom" in patterns:
        db = patterns["double_bottom"]
        conf = "CONFIRMADO" if db["confirmado"] else "não confirmado"
        pat_lines.append(f"  Double Bottom {conf} — fundos ${db['fundo1']:,.0f}/${db['fundo2']:,.0f} | neckline ${db['neckline']:,.0f} | alvo alta ${db['alvo_alta']:,.0f}")
    if "head_and_shoulders" in patterns:
        hs = patterns["head_and_shoulders"]
        conf = "CONFIRMADO" if hs["confirmado"] else "não confirmado"
        pat_lines.append(f"  H&S {conf} — cabeça ${hs['cabeca']:,.0f} | neckline ${hs['neckline']:,.0f} | alvo queda ${hs['alvo_queda']:,.0f}")

    # ── Similar Structure Finder ──────────────────────────────────────────────
    ssf = similar_structure_finder(closes)
    ssf_lines = []
    if ssf.get("analogos_encontrados"):
        k = ssf["analogos_encontrados"]
        criterio = ssf.get("criterio", "distância ATH ±15pp + EMA50 ±10pp")
        ssf_lines.append(f"  Análogos encontrados ({criterio}): {k} casos")
        if ssf.get("media_30d") is not None:
            ssf_lines.append(
                f"  Retorno médio 30d: {ssf['media_30d']}% | "
                f"positivo: {ssf['positivo_30d_pct']}% | "
                f">+10%: {ssf['acima_10pct_30d']}% | "
                f"<-10%: {ssf['abaixo_10pct_30d']}%"
            )
        if ssf.get("media_60d") is not None:
            ssf_lines.append(
                f"  Retorno médio 60d: {ssf['media_60d']}% | positivo: {ssf['positivo_60d_pct']}%"
            )
        if ssf.get("media_90d") is not None:
            ssf_lines.append(
                f"  Retorno médio 90d: {ssf['media_90d']}% | positivo: {ssf['positivo_90d_pct']}%"
            )
        for a in ssf.get("ultimos_3_analogos", []):
            rsi_str = f"RSI {a['rsi']}" if a.get('rsi') is not None else ""
            ssf_lines.append(
                f"  → {a['dias_atras']}d atrás | ${a['preco']:,.0f} | "
                f"ATH dist: {a.get('pct_ath','?')}% | {rsi_str} "
                f"| 30d={a['fwd30']}% 60d={a.get('fwd60','?')}% 90d={a.get('fwd90','?')}%"
            )

    # ── Monta bloco final ─────────────────────────────────────────────────────
    lines = [
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"ANÁLISE QUANTITATIVA AUTOMÁTICA — {n} CANDLES REAIS",
        f"(Calculado sobre histórico real Binance — atualizado a cada hora)",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"PERFORMANCE REAL:",
        *perf_lines,
        f"",
        f"ATH HISTÓRICO: ${p['ath']:,.0f} ({p['ath_days_ago']}d atrás) | Drawdown atual: {p['current_dd']}%",
        f"Sequência: {p.get('streak','N/A')}",
        f"",
        f"ESTRUTURA DE MERCADO (topos/fundos locais, janela 20d):",
        f"  Últimos topos:   {tops_str or 'N/A'}",
        f"  Últimos fundos:  {bots_str or 'N/A'}",
        f"  Estrutura topos: {p.get('structure_tops','N/A')}",
        f"  Estrutura fundos:{p.get('structure_bottoms','N/A')}",
        f"",
        f"SUPORTE / RESISTÊNCIA POR CLUSTERING DE PREÇO ({len(sr_zones)} zonas, 365d):",
        *(sr_lines if sr_lines else ["  N/A"]),
        f"  (zonas baseadas em onde o preço mais operou, não em max/min de janela)",
        f"",
        f"COMPRESSÃO / VOLATILIDADE:",
        *(comp_lines if comp_lines else ["  N/A"]),
        f"  Volatilidade: 30d={p.get('vol_30d')}% | 90d={p.get('vol_90d')}% | 365d={p.get('vol_365d')}% — status: {p.get('vol_status')}",
        f"",
        f"PADRÕES DE GRÁFICO DETECTADOS:",
        *(pat_lines if pat_lines else ["  Nenhum padrão claro detectado no momento"]),
        f"",
        f"ESTRUTURAS HISTÓRICAS ANÁLOGAS (similar structure finder):",
        *(ssf_lines if ssf_lines else ["  Amostras insuficientes para o filtro atual"]),
        f"",
        f"VOLUME:",
        f"  Tendência 7d vs 30d: {p.get('vol_trend','N/A')} | Ratio 30d/90d: {p.get('vol_vs_90d','N/A')}x",
        f"  Correlação preço×volume 30d: {p.get('price_vol_corr_30d','N/A')} (>0.5=confirma | <0=diverge)",
        f"",
        f"BACKTESTING — APÓS QUEDAS >5% EM 1 DIA ({p.get('big_drops_count',0)} ocorrências):",
        f"  Retorno médio 7d:  {p.get('after_5pct_drop_avg_7d','N/A')}%",
        f"  Retorno médio 30d: {p.get('after_5pct_drop_avg_30d','N/A')}%",
        f"",
        f"TOP 5 QUEDAS DIÁRIAS HISTÓRICAS: {quedas_s}",
        f"TOP 5 ALTAS DIÁRIAS HISTÓRICAS:  {altas_s}",
        f"",
        f"REGRA: Use esses dados para fundamentar probabilidades — são calculados sobre histórico real.",
        f"Para perguntas sobre nível específico (ex: 'rompe $X?'), use a função level_breakout_probability.",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)
