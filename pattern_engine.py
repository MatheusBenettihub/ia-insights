"""
pattern_engine.py
Engine de reconhecimento de padrões estruturais para BTC e S&P500.

Lógica:
  1. compute_fingerprint(closes, vols, window) — "tira um print" do mercado atual:
     range, posição no range, tempo lateral, testes de suporte, RSI, MAs, fase
  2. scan_analogues(closes, current_fp, window) — desliza a janela por todo o histórico,
     pontua cada período por similaridade ao fingerprint atual, retorna os melhores matches
  3. NAMED_PATTERNS_BTC — biblioteca de padrões históricos nomeados com descrição + fingerprint
  4. identify_named_pattern(fp) — qual padrão nomeado mais se parece com o estado atual
  5. build_pattern_context(btc_closes, btc_vols, sp500_closes, sp500_ts) — monta o bloco para o prompt
"""

from datetime import datetime, timezone, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def _ts_to_ym(ts):
    try:
        return (_EPOCH + timedelta(seconds=ts)).strftime("%Y-%m")
    except Exception:
        return "?"


# ── Helpers técnicos internos ────────────────────────────────────────────────

def _ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for v in closes[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:])  / period
    al = sum(losses[-period:]) / period
    return round(100 - (100 / (1 + ag / al)), 1) if al else 100.0

def _find_local_extremes(data, w=10):
    tops, bottoms = [], []
    for i in range(w, len(data) - w):
        seg = data[i - w: i + w + 1]
        if data[i] == max(seg): tops.append((i, data[i]))
        elif data[i] == min(seg): bottoms.append((i, data[i]))
    return tops, bottoms


# ── 1. Fingerprint estrutural ────────────────────────────────────────────────

def compute_fingerprint(closes, vols=None, window=90, full_closes=None):
    """
    Extrai o fingerprint estrutural dos últimos `window` candles.

    full_closes: histórico completo até o ponto atual (para calcular ATH do ciclo).
                 Se None, usa `closes` como proxy.

    Dimensões principais (em ordem de importância para similaridade):
      pct_from_cycle_ath : % abaixo do ATH dos últimos 2 anos  ← MAIS IMPORTANTE
      days_since_cycle_ath: dias desde o ATH do ciclo           ← 2º MAIS IMPORTANTE
      position_in_range  : onde preço está no range (0=fundo, 100=topo)
      pct_vs_ema50       : % acima/abaixo EMA50
      range_pct          : (max - min) / min * 100 dentro da janela
      support_tests      : quantas vezes o preço esteve a ≤3% do mínimo
      trend_structure    : HH_HL / LH_LL / lateral
      consecutive_lower_highs : quantos topos locais consecutivos mais baixos
      rsi                : BAIXO PESO — indicador atrasado, aparece igual em bull e bear
    """
    if len(closes) < max(window, 20):
        return None

    seg   = closes[-window:]
    n_seg = len(seg)
    price = seg[-1]

    high  = max(seg)
    low   = min(seg)
    hi    = seg.index(high)
    li    = seg.index(low)

    range_pct = round((high - low) / low * 100, 1)
    position  = round((price - low) / (high - low) * 100, 1) if high != low else 50.0

    days_since_top    = n_seg - 1 - hi
    days_since_bottom = n_seg - 1 - li

    # Testes de suporte e resistência (quantas vezes preço se aproximou ±3%)
    support_zone    = low  * 1.03
    resistance_zone = high * 0.97
    support_tests    = sum(1 for c in seg if c <= support_zone)
    resistance_tests = sum(1 for c in seg if c >= resistance_zone)

    # Estrutura de topos e fundos locais
    tops, bottoms = _find_local_extremes(seg, w=max(5, n_seg // 12))

    trend_structure = "lateral"
    consecutive_lower_highs = 0
    if len(tops) >= 2 and len(bottoms) >= 2:
        hh = tops[-1][1]    > tops[-2][1]
        hl = bottoms[-1][1] > bottoms[-2][1]
        if hh and hl:     trend_structure = "HH_HL"   # bull
        elif not hh and not hl: trend_structure = "LH_LL"  # bear
        elif hh and not hl:     trend_structure = "HH_LL"  # expansão
        else:                   trend_structure = "LH_HL"  # acumulação

        # Conta topos consecutivos mais baixos (bear market clássico)
        if len(tops) >= 2:
            count = 1
            for k in range(len(tops) - 1, 0, -1):
                if tops[k][1] < tops[k-1][1]: count += 1
                else: break
            consecutive_lower_highs = count - 1

    # MAs e RSI (RSI tem baixo peso no scoring — aparece igual em bull e bear)
    rsi_val  = _rsi(closes[-50:]) if len(closes) >= 15 else None
    ema50    = _ema(closes, 50)
    ema200   = _ema(closes, 200)
    pct_e50  = round((price - ema50)  / ema50  * 100, 1) if ema50  else None
    pct_e200 = round((price - ema200) / ema200 * 100, 1) if ema200 else None
    # Death/Golden Cross: -1 = death cross (bearish), +1 = golden cross (bullish)
    ma_cross = (1 if (ema50 and ema200 and ema50 > ema200)
                else -1 if (ema50 and ema200) else 0)

    # ATH do ciclo (últimos 2 anos = ~730 candles) — dimensão mais importante
    hist = full_closes if full_closes else closes
    lookback_ath = min(len(hist), 730)
    cycle_ath = max(hist[-lookback_ath:])
    ath_idx_in_slice = hist[-lookback_ath:].index(cycle_ath)
    days_since_cycle_ath = lookback_ath - 1 - ath_idx_in_slice
    pct_from_cycle_ath = round((price - cycle_ath) / cycle_ath * 100, 1)

    # Volume trend (14d vs janela)
    vol_trend = None
    if vols and len(vols) >= window:
        vseg = vols[-window:]
        v14  = sum(vseg[-14:]) / 14
        vavg = sum(vseg) / len(vseg)
        if vavg > 0:
            vr = v14 / vavg
            vol_trend = "crescente" if vr > 1.15 else "decrescente" if vr < 0.85 else "estavel"

    # Classificação de fase
    phase = _classify_phase(
        price, high, low, days_since_top, days_since_bottom,
        trend_structure, rsi_val, pct_e50, pct_e200, position
    )

    return {
        # Dimensões estruturais (alto peso)
        "pct_from_cycle_ath":      pct_from_cycle_ath,
        "days_since_cycle_ath":    days_since_cycle_ath,
        "position_in_range":       position,
        "range_pct":               range_pct,
        "pct_vs_ema50":            pct_e50,
        "support_tests":           support_tests,
        "consecutive_lower_highs": consecutive_lower_highs,
        "trend_structure":         trend_structure,
        "ma_cross":                ma_cross,
        # Dimensões secundárias (baixo peso)
        "days_since_top":          days_since_top,
        "days_since_bottom":       days_since_bottom,
        "resistance_tests":        resistance_tests,
        "rsi":                     rsi_val,   # baixo peso — só tiebreaker
        "pct_vs_ema200":           pct_e200,
        "vol_trend":               vol_trend,
        "phase":                   phase,
        "window":                  window,
        "price":                   round(price, 0),
        "range_high":              round(high, 0),
        "range_low":               round(low, 0),
    }


def _classify_phase(price, high, low, dst, dsb, trend, rsi, pct_e50, pct_e200, pos):
    """Classifica a fase de mercado com base nas dimensões do fingerprint."""
    if trend == "LH_LL" and pos < 35:
        return "bear_capitulacao"
    if trend == "LH_LL" and pos < 60:
        return "bear_lateral"
    if trend == "LH_LL":
        return "bear_pullback"
    if trend == "HH_HL" and pos > 70:
        return "bull_extensao"
    if trend == "HH_HL" and pos > 40:
        return "bull_pullback"
    if trend == "LH_HL" and dsb > dst:
        return "acumulacao"
    if trend == "LH_HL":
        return "compressao"
    if trend == "HH_LL":
        return "expansao_volatil"
    if dst < 20 and pos > 75:
        return "distribuicao"
    if dsb < 20 and pos < 30:
        return "fundo_recente"
    return "indefinido"


# ── 2. Score de similaridade entre dois fingerprints ────────────────────────

WEIGHTS = {
    # Dimensões estruturais — o que realmente distingue bull de bear de acumulação
    "pct_from_cycle_ath":        0.28,  # ← mais importante: 50% abaixo ATH = bear, 5% = topo
    "days_since_cycle_ath_norm": 0.15,  # tempo desde o topo do ciclo
    "position_in_range":         0.15,  # onde no range da janela
    "pct_vs_ema50":              0.10,  # relação com EMA50
    "range_pct_norm":            0.10,  # amplitude do range (ajustada por escala)
    "consecutive_lower_highs":   0.08,  # estrutura de topos — bear clássico
    "support_tests":             0.07,  # quantas vezes testou o fundo
    "days_since_top_ratio":      0.04,
    "days_since_bottom_ratio":   0.03,
    # RSI: peso ZERO no scoring — aparece igual em bull e bear, é lagging
    # (mantido no fingerprint só para a IA ler, não para comparar)
}

def similarity_score(fp_a, fp_b, scale_range=1.0):
    """
    Computa score de similaridade [0-100] entre dois fingerprints.
    scale_range: fator de escala para normalizar range_pct (1.0=mesmo ativo, 3.5=BTC vs SP500).
    Score alto = mais similar.
    """
    if not fp_a or not fp_b:
        return 0.0

    def pnorm(a, b, tolerance):
        """Penalidade normalizada: 0 se dentro da tolerância, sobe linearmente até 1."""
        if a is None or b is None:
            return 0.5  # incerteza = penalidade moderada
        diff = abs(a - b)
        return min(diff / tolerance, 1.0)

    penalties = {}

    # 1. % abaixo do ATH do ciclo — CRITÉRIO PRINCIPAL
    # Tolerância ±12pp: 50% abaixo ATH não pode casar com 5% abaixo ATH
    penalties["pct_from_cycle_ath"] = pnorm(
        fp_a.get("pct_from_cycle_ath"), fp_b.get("pct_from_cycle_ath"), 12
    )

    # 2. Tempo desde o ATH do ciclo (normalizado pela janela)
    w = fp_a.get("window", 90)
    # normaliza: 365 dias desde ATH = 4 janelas de 90d
    ath_norm_a = (fp_a.get("days_since_cycle_ath") or 0) / 365.0
    ath_norm_b = (fp_b.get("days_since_cycle_ath") or 0) / 365.0
    penalties["days_since_cycle_ath_norm"] = pnorm(ath_norm_a, ath_norm_b, 0.4)

    # 3. Posição no range da janela
    penalties["position_in_range"] = pnorm(
        fp_a.get("position_in_range"), fp_b.get("position_in_range"), 18
    )

    # 4. Posição vs EMA50
    penalties["pct_vs_ema50"] = pnorm(fp_a.get("pct_vs_ema50"), fp_b.get("pct_vs_ema50"), 10)

    # 5. Range normalizado (amplitude)
    rng_a = fp_a.get("range_pct") or 0
    rng_b = (fp_b.get("range_pct") or 0) * scale_range
    penalties["range_pct_norm"] = pnorm(rng_a, rng_b, 25)

    # 6. Topos consecutivos mais baixos (estrutura bear/bull)
    penalties["consecutive_lower_highs"] = pnorm(
        fp_a.get("consecutive_lower_highs"), fp_b.get("consecutive_lower_highs"), 2
    )

    # 7. Testes de suporte
    penalties["support_tests"] = pnorm(
        fp_a.get("support_tests"), fp_b.get("support_tests"), 4
    )

    # 8. Dias desde topo/fundo (ratio da janela)
    rat_top_a = (fp_a.get("days_since_top") or 0)    / w
    rat_top_b = (fp_b.get("days_since_top") or 0)    / fp_b.get("window", w)
    rat_bot_a = (fp_a.get("days_since_bottom") or 0) / w
    rat_bot_b = (fp_b.get("days_since_bottom") or 0) / fp_b.get("window", w)
    penalties["days_since_top_ratio"]    = pnorm(rat_top_a, rat_top_b, 0.25)
    penalties["days_since_bottom_ratio"] = pnorm(rat_bot_a, rat_bot_b, 0.25)

    # RSI: NÃO entra no scoring — é lagging e aparece igual em contextos opostos
    # (mantido no fingerprint só para leitura, não para comparação)

    total_penalty = sum(WEIGHTS[k] * penalties[k] for k in WEIGHTS)
    score = round((1 - total_penalty) * 100, 1)
    return max(score, 0.0)


# ── 3. Scanner de janelas deslizantes ────────────────────────────────────────

def scan_analogues(closes, current_fp, vols=None, timestamps=None,
                   window=90, top_n=5, scale_range=1.0, min_score=55.0):
    """
    Desliza a janela de `window` candles por todo o histórico de `closes`,
    computa o fingerprint de cada janela e pontua a similaridade com `current_fp`.
    Retorna os `top_n` melhores matches com o que aconteceu depois.

    scale_range: 1.0 para BTC vs BTC; 3.5 para BTC vs S&P500.
    """
    n = len(closes)
    if n < window + 90:
        return []

    results = []
    # Deixa pelo menos 90 candles à frente para forward returns
    for i in range(window, n - 90):
        seg  = closes[i - window: i + 1]
        vseg = vols[i - window: i + 1] if vols and len(vols) > i else None
        # Passa histórico completo até i para calcular ATH do ciclo corretamente
        full_up_to_i = closes[:i + 1]

        fp_i = compute_fingerprint(seg, vseg, window, full_closes=full_up_to_i)
        if not fp_i:
            continue

        score = similarity_score(current_fp, fp_i, scale_range)
        if score < min_score:
            continue

        # Forward returns
        fwd30  = round((closes[min(i + 30,  n-1)] - closes[i]) / closes[i] * 100, 1)
        fwd60  = round((closes[min(i + 60,  n-1)] - closes[i]) / closes[i] * 100, 1)
        fwd90  = round((closes[min(i + 90,  n-1)] - closes[i]) / closes[i] * 100, 1)

        ts  = timestamps[i] if timestamps and i < len(timestamps) else None
        era = "jovem" if (ts and ts < 410227200) else "moderno"

        results.append({
            "score":       score,
            "idx":         i,
            "periodo":     _ts_to_ym(ts) if ts else f"candle {i}",
            "era":         era,
            "fp":          fp_i,
            "fwd30":       fwd30,
            "fwd60":       fwd60,
            "fwd90":       fwd90,
            "named":       None,   # preenchido depois
        })

    # Ordena por score, remove duplicatas muito próximas (dentro de 30 candles)
    results.sort(key=lambda x: -x["score"])
    filtered = []
    used_idx = []
    for r in results:
        if all(abs(r["idx"] - u) > 30 for u in used_idx):
            filtered.append(r)
            used_idx.append(r["idx"])
        if len(filtered) >= top_n:
            break

    return filtered


# ── 4. Biblioteca de padrões históricos nomeados do BTC ──────────────────────

NAMED_PATTERNS_BTC = {
    "2018_bear_lateral": {
        "nome":    "Bear 2018 — acumulação em $6k",
        "periodo": "Mar/2018 – Nov/2018",
        "desc": (
            "Após ATH de $20k (dez/2017), BTC entrou em bear com lower highs consecutivos. "
            "Formou suporte forte em $6k testado 3x entre mar e out/2018. "
            "Estrutura: LH_LL mas fundo estável. RSI lateral entre 35-50. "
            "Terminou com breakdown final para $3.1k (nov/2018) após romper $6k."
        ),
        "desfecho": "Breakdown do suporte → -48% adicional antes do fundo real ($3.1k dez/2018)",
        "licao":    "Suporte testado muitas vezes eventualmente rompe. Volume decrescente no suporte = fraqueza.",
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -55, "days_since_cycle_ath": 180,
            "position_in_range": 20, "pct_vs_ema50": -8,
            "consecutive_lower_highs": 3, "support_tests": 8, "phase": "bear_lateral",
        },
    },
    "2019_recuperacao": {
        "nome":    "Recuperação 2019 — bull de $3.1k a $14k",
        "periodo": "Dez/2018 – Jun/2019",
        "desc": (
            "Fundo em $3.1k (dez/2018). Rally de 350% em 6 meses até $14k. "
            "Fase inicial: acumulação silenciosa jan-mar/2019 ($3.1k-$4k). "
            "Breakout em abr/2019 acima de $5.3k com volume explosivo. "
            "Sem pull-back significativo até $14k. RSI semanal rompeu 60+ no breakout."
        ),
        "desfecho": "Correção de $14k para $6.5k (out/2019), depois lateral antes do halving 2020",
        "licao":    "Fundo de bear tem período de acumulação de 3-4 meses antes do breakout.",
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -85, "days_since_cycle_ath": 90,
            "position_in_range": 15, "pct_vs_ema50": -5,
            "consecutive_lower_highs": 0, "support_tests": 2, "phase": "acumulacao",
        },
    },
    "2020_covid_v": {
        "nome":    "Crash COVID mar/2020 — V-shape",
        "periodo": "Mar/2020",
        "desc": (
            "Crash de -63% em 48h (12-13/mar/2020): de $8k para $3.8k. "
            "Recuperação completa em 60 dias. Estrutura: fundo pontual, não lateral. "
            "Volume de capitulação extremo. Funding rate fortemente negativo. "
            "Diferente de bear normal: causado por crise de liquidez externa (macro), não por ciclo BTC."
        ),
        "desfecho": "Recuperação total para $8k em 60 dias, depois bull para $60k em 12 meses",
        "licao":    "Crashes de liquidez macro (não de ciclo) têm recuperação em V. Distinguir pelo contexto.",
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -55, "days_since_cycle_ath": 7,
            "position_in_range": 5, "pct_vs_ema50": -25,
            "consecutive_lower_highs": 1, "support_tests": 1, "phase": "bear_capitulacao",
        },
    },
    "2021_distribuicao": {
        "nome":    "Distribuição maio/2021 — topo intermediário $64k",
        "periodo": "Abr/2021 – Jul/2021",
        "desc": (
            "Primeiro topo do ciclo 2021 em $64k (abr/2021). Correção de -53% para $29k em maio. "
            "Período: lateral $29k-$40k por 3 meses. Lower highs: $64k → $40k → $35k. "
            "RSI semanal caiu de 87 para 45. Volume decrescente na lateral. "
            "Parecia bear; era distribuição antes do segundo bull para $69k."
        ),
        "desfecho": "Rally final de $29k para $69k (out-nov/2021) antes do bear 2022",
        "licao":    "Correções de 50%+ dentro de ciclos bull podem ser continuação, não reversão.",
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -55, "days_since_cycle_ath": 60,
            "position_in_range": 35, "pct_vs_ema50": -5,
            "consecutive_lower_highs": 2, "support_tests": 4, "phase": "bear_lateral",
        },
    },
    "2022_bear_estrutural": {
        "nome":    "Bear estrutural 2022 — $69k a $15.5k",
        "periodo": "Nov/2021 – Nov/2022",
        "desc": (
            "Bear mais longo do ciclo: 13 meses, -77%. Fases: "
            "$69k → $33k (jan/2022, -52%), rally morto para $48k (mar/2022), "
            "quebra de $30k em junho (Luna/UST), fundo em $17.6k. "
            "FTX collapse (nov/2022) causou capitulação final para $15.5k. "
            "Cada rally foi vendido. RSI semanal nunca rompeu 50 durante o bear."
        ),
        "desfecho": "Fundo $15.5k nov/2022 → acumulação até jan/2023 → bull 2023-2024",
        "licao":    "Rally dentro de bear com RSI semanal abaixo de 50 = dead cat bounce.",
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -75, "days_since_cycle_ath": 300,
            "position_in_range": 10, "pct_vs_ema50": -12,
            "consecutive_lower_highs": 4, "support_tests": 2, "phase": "bear_capitulacao",
        },
    },
    "2022_2023_acumulacao": {
        "nome":    "Acumulação pós-bear 2022/2023 — $15.5k a $25k",
        "periodo": "Nov/2022 – Jan/2023",
        "desc": (
            "Fundo em $15.5k (nov/2022). Lateral $15.5k-$25k por ~8 semanas. "
            "Higher lows silenciosos. Volume baixo e estável. RSI semanal saindo de 32 para 45. "
            "Breakout em jan/2023 acima de $21k com volume 2x média. "
            "Estrutura: LH_HL transitando para HH_HL."
        ),
        "desfecho": "Rally de $15.5k para $31k em 60 dias (+100%), depois lateral $25k-$31k",
        "licao":    "Acumulação pós-bear: volume baixo + higher lows + RSI subindo de <35 = setup de breakout.",
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -78, "days_since_cycle_ath": 14,
            "position_in_range": 40, "pct_vs_ema50": 2,
            "consecutive_lower_highs": 0, "support_tests": 3, "phase": "acumulacao",
        },
    },
    "2024_halving_pre_bull": {
        "nome":    "Pré-bull halving 2024 — consolidação $55k-$72k",
        "periodo": "Abr/2024 – Out/2024",
        "desc": (
            "Após ATH local de $73k (mar/2024), correção para $55k (-25%). "
            "Lateral $55k-$72k por ~5 meses. Halving em abr/2024 (dia 19). "
            "Estrutura: HH_HL após o halving. Volume decrescente na lateral. "
            "RSI semanal entre 50-60. EMA50 semanal suporte em $58k."
        ),
        "desfecho": "Breakout acima de $73k (out/2024) → rally para $109k (jan/2025)",
        "licao":    "Pós-halving: 6-12 meses de acumulação antes da fase bull acelerada é o padrão.",
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -16, "days_since_cycle_ath": 120,
            "position_in_range": 45, "pct_vs_ema50": 3,
            "consecutive_lower_highs": 1, "support_tests": 3, "phase": "acumulacao",
        },
    },
}


def identify_named_pattern(current_fp):
    """
    Compara o fingerprint atual com os padrões nomeados e retorna
    o mais similar com o score de confiança.
    """
    if not current_fp:
        return None, 0

    best_name  = None
    best_score = 0

    for name, pat in NAMED_PATTERNS_BTC.items():
        fp_ref = pat["fingerprint_aprox"]
        # Monta fingerprint temporário compatível
        fp_temp = {
            "pct_from_cycle_ath":      fp_ref.get("pct_from_cycle_ath",
                                            current_fp.get("pct_from_cycle_ath")),
            "days_since_cycle_ath":    fp_ref.get("days_since_cycle_ath",
                                            current_fp.get("days_since_cycle_ath")),
            "position_in_range":       fp_ref.get("position_in_range", 50),
            "pct_vs_ema50":            fp_ref.get("pct_vs_ema50", 0),
            "range_pct":               current_fp.get("range_pct"),
            "days_since_top":          current_fp.get("days_since_top"),
            "days_since_bottom":       current_fp.get("days_since_bottom"),
            "support_tests":           fp_ref.get("support_tests", current_fp.get("support_tests")),
            "consecutive_lower_highs": fp_ref.get("consecutive_lower_highs", 0),
            "window":                  current_fp.get("window", 90),
        }
        score = similarity_score(current_fp, fp_temp)
        if score > best_score:
            best_score = score
            best_name  = name

    return best_name, best_score


# ── 5. Contexto completo para o prompt ───────────────────────────────────────

def build_pattern_context(btc_closes, btc_vols=None,
                           sp500_closes=None, sp500_timestamps=None,
                           window_btc=90, window_sp500=52):
    """
    Gera o bloco de análise de padrões para injetar no system prompt.

    window_btc    : janela em dias para fingerprint BTC
    window_sp500  : janela em semanas para fingerprint S&P500 (≈90 dias)
    """
    if len(btc_closes) < window_btc + 90:
        return ""

    # ── Fingerprint BTC atual ─────────────────────────────────────────────────
    current_fp = compute_fingerprint(
        btc_closes, btc_vols, window_btc, full_closes=btc_closes
    )
    if not current_fp:
        return ""

    # ── Padrão nomeado mais próximo ───────────────────────────────────────────
    named_match, named_score = identify_named_pattern(current_fp)

    # ── Análogos históricos no BTC (top 5) ────────────────────────────────────
    btc_analogues = scan_analogues(
        btc_closes, current_fp,
        vols=btc_vols,
        window=window_btc,
        top_n=5,
        scale_range=1.0,
        min_score=55.0,
    )

    # ── Análogos históricos no S&P500 (top 5) ────────────────────────────────
    sp500_analogues = []
    if sp500_closes and len(sp500_closes) > window_sp500 + 90:
        sp500_analogues = scan_analogues(
            sp500_closes, current_fp,
            timestamps=sp500_timestamps,
            window=window_sp500,
            top_n=5,
            scale_range=3.5,   # BTC ~3.5x mais volátil que S&P500
            min_score=52.0,
        )

    # ── Monta o bloco de texto ────────────────────────────────────────────────
    lines = [
        "",
        "================================================================",
        "RECONHECIMENTO DE PADROES ESTRUTURAIS (Pattern Engine)",
        "================================================================",
        "",
        "FINGERPRINT DO MERCADO ATUAL (janela " + str(window_btc) + " candles):",
        f"  Fase detectada:        {current_fp['phase']}",
        f"  Range da janela:       ${current_fp['range_low']:,.0f} - ${current_fp['range_high']:,.0f} ({current_fp['range_pct']}%)",
        f"  Posicao no range:      {current_fp['position_in_range']}% (0=fundo, 100=topo)",
        f"  Dias desde o topo:     {current_fp['days_since_top']}",
        f"  Dias desde o fundo:    {current_fp['days_since_bottom']}",
        f"  Estrutura topos/fundos:{current_fp['trend_structure']}",
        f"  Topos mais baixos consec.: {current_fp['consecutive_lower_highs']}",
        f"  Testes de suporte:     {current_fp['support_tests']} (vezes proximas ao minimo)",
        f"  Testes de resistencia: {current_fp['resistance_tests']}",
        f"  RSI:                   {current_fp['rsi']}",
        f"  Pos. vs EMA50:         {current_fp['pct_vs_ema50']}%",
        f"  Pos. vs EMA200:        {current_fp['pct_vs_ema200']}%",
        f"  Volume:                {current_fp['vol_trend'] or 'N/A'}",
        "",
    ]

    # Padrão nomeado mais próximo
    if named_match and named_score >= 55:
        pat = NAMED_PATTERNS_BTC[named_match]
        lines += [
            f"PADRAO HISTORICO MAIS PROXIMO (score {named_score:.0f}/100):",
            f"  Nome:     {pat['nome']}",
            f"  Periodo:  {pat['periodo']}",
            f"  Contexto: {pat['desc']}",
            f"  Desfecho: {pat['desfecho']}",
            f"  Licao:    {pat['licao']}",
            "",
        ]

    # Análogos BTC (janelas históricas reais mais similares)
    if btc_analogues:
        lines += [
            f"ANALOGOS HISTORICOS BTC — TOP {len(btc_analogues)} MAIS SIMILARES (scan janela deslizante):",
            "  (Score = similaridade estrutural 0-100, calculado sobre fingerprint real)",
        ]
        for a in btc_analogues:
            fp_a = a["fp"]
            lines.append(
                f"  Score {a['score']:.0f}/100 | {a['periodo']} | "
                f"fase={fp_a['phase']} pos={fp_a['position_in_range']}% RSI={fp_a['rsi']} "
                f"EMA50={fp_a['pct_vs_ema50']}% | "
                f"retorno real: 30d={a['fwd30']}% 60d={a['fwd60']}% 90d={a['fwd90']}%"
            )
        fwd30s = [a["fwd30"] for a in btc_analogues]
        fwd90s = [a["fwd90"] for a in btc_analogues]
        lines += [
            f"  Media dos {len(btc_analogues)} analogos: 30d={round(sum(fwd30s)/len(fwd30s),1)}% "
            f"| 90d={round(sum(fwd90s)/len(fwd90s),1)}%",
            f"  Positivos em 30d: {sum(1 for x in fwd30s if x>0)}/{len(fwd30s)} casos",
            f"  Positivos em 90d: {sum(1 for x in fwd90s if x>0)}/{len(fwd90s)} casos",
            "",
        ]

    # Análogos S&P500
    if sp500_analogues:
        lines += [
            f"ANALOGOS S&P500 — TOP {len(sp500_analogues)} (escala ajustada 3.5x):",
            "  SECUNDARIO: S&P500 confirma ou diverge do padrao BTC.",
            "  Multiplicar retornos por 3.5x para estimar equivalente BTC.",
        ]
        for a in sp500_analogues:
            fp_a = a["fp"]
            era_label = "[ERA JOVEM - mais relevante]" if a["era"] == "jovem" else "[moderno]"
            lines.append(
                f"  Score {a['score']:.0f}/100 | {a['periodo']} {era_label} | "
                f"fase={fp_a['phase']} pos={fp_a['position_in_range']}% RSI={fp_a['rsi']} | "
                f"S&P500 real: 30d={a['fwd30']}% 90d={a['fwd90']}% "
                f"| equiv BTC: 30d~{round(a['fwd30']*3.5,0)}% 90d~{round(a['fwd90']*3.5,0)}%"
            )
        fwd30s = [a["fwd30"] for a in sp500_analogues]
        fwd90s = [a["fwd90"] for a in sp500_analogues]
        lines += [
            f"  Media S&P500: 30d={round(sum(fwd30s)/len(fwd30s),1)}% "
            f"| equiv BTC 30d~{round(sum(fwd30s)/len(fwd30s)*3.5,1)}%",
            f"  Positivos em 30d: {sum(1 for x in fwd30s if x>0)}/{len(fwd30s)} casos",
            "",
        ]

    # Todos os padrões nomeados — biblioteca para referência
    lines += [
        "BIBLIOTECA DE PADROES BTC (referencia para a IA usar nas respostas):",
    ]
    for name, pat in NAMED_PATTERNS_BTC.items():
        lines.append(f"  [{pat['nome']} | {pat['periodo']}]")
        lines.append(f"    Desc: {pat['desc']}")
        lines.append(f"    Desfecho: {pat['desfecho']}")
        lines.append(f"    Licao: {pat['licao']}")

    lines += [
        "",
        "INSTRUCOES:",
        "  1. Use o FINGERPRINT ATUAL para descrever objetivamente a estrutura presente",
        "  2. O PADRAO NOMEADO MAIS PROXIMO e o ponto de partida para analogias historicas",
        "  3. Os ANALOGOS HISTORICOS sao as janelas reais mais parecidas — cite os periodos e retornos",
        "  4. S&P500 e analise secundaria — reforça ou questiona o cenario BTC",
        "  5. Nunca diga 'identico a X' — diga 'em X de Y casos similares, o resultado foi Z'",
        "================================================================",
    ]

    return "\n".join(lines)
