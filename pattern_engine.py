"""
pattern_engine.py
Engine de reconhecimento de padrões estruturais para BTC e S&P500.

Lógica:
  1. compute_fingerprint(closes, vols, window) — "tira um print" do mercado atual:
     range, posição no range, tempo lateral, testes de suporte, RSI, MAs, fase
  2. scan_analogues(closes, current_fp, window) — desliza a janela por todo o histórico,
     pontua cada período e retorna os melhores matches. O score combina dois canais:
       - ESTRUTURA: similarity_score sobre o fingerprint (estado/foto do momento)
       - TRAJETÓRIA: _shape_score sobre a FORMA do gráfico (o desenho esquerda→direita,
         na ordem, com a escala de preço removida) — ver _shape_vector/_shape_score
  3. NAMED_PATTERNS_BTC — biblioteca de padrões históricos nomeados com descrição + fingerprint
  4. identify_named_pattern(fp) — qual padrão nomeado mais se parece com o estado atual
  5. build_pattern_context(btc_closes, btc_vols, sp500_closes, sp500_ts) — monta o bloco para o prompt
"""

import math
from datetime import datetime, timezone, timedelta

# Carimbo de versão — exibido na UI p/ confirmar que o módulo novo foi carregado.
# (Streamlit NÃO recarrega módulos importados; se a UI mostrar versão antiga,
#  o processo precisa ser reiniciado: Ctrl+C e `streamlit run app.py`.)
PATTERN_ENGINE_VERSION = "v4.1 — estrutura de PIVÔS + diversidade de episódio + janelas longas"

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


# ── Trajetória / FORMA do gráfico (o "print" lido da esquerda p/ direita, em ordem) ──
# Diferente do fingerprint (que é uma FOTO de números do momento), isto compara o
# DESENHO inteiro da curva, na ordem temporal, ignorando a escala de preço.

def _resample(seq, m):
    """Reamostra `seq` para exatamente `m` pontos (interpolação linear). Preserva a ORDEM
    esquerda→direita — permite comparar janelas de durações diferentes."""
    n = len(seq)
    if n == 0:
        return [0.0] * m
    if n == m:
        return list(seq)
    out = []
    for j in range(m):
        x = j * (n - 1) / (m - 1) if m > 1 else 0.0
        i0 = int(x)
        i1 = min(i0 + 1, n - 1)
        frac = x - i0
        out.append(seq[i0] * (1 - frac) + seq[i1] * frac)
    return out

def _shape_vector(seg, m=48):
    """
    Converte um trecho de preços na sua FORMA pura (magnitude removida):
      1. log dos preços (o que importa é o movimento %, não o valor absoluto)
      2. reamostra para m pontos (compara janelas de durações diferentes)
      3. normaliza min-max p/ [0,1] — um -60% e um -30% com o MESMO desenho viram a MESMA curva
    Retorna o vetor da forma, na ordem esquerda→direita.
    """
    if not seg or len(seg) < 2:
        return None
    logs = [math.log(v) if v > 0 else 0.0 for v in seg]
    rs = _resample(logs, m)
    lo, hi = min(rs), max(rs)
    if hi - lo < 1e-9:
        return [0.5] * m
    return [(v - lo) / (hi - lo) for v in rs]

def _shape_score(vec_a, vec_b):
    """
    Score 0-100 de quão parecido é o DESENHO de duas trajetórias já normalizadas.
    Combina erro ponto-a-ponto (o nível da curva em cada x) com correlação (o
    co-movimento na ordem: sobe quando o outro sobe, desce quando desce).
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    m = len(vec_a)
    # Erro médio absoluto sobre [0,1] — quão alinhadas estão as curvas ponto a ponto
    mae = sum(abs(a - b) for a, b in zip(vec_a, vec_b)) / m
    err_score = max(0.0, 1 - mae / 0.35)            # 0.35 = tolerância de forma
    # Correlação de Pearson — direção do movimento na ordem temporal
    ma = sum(vec_a) / m
    mb = sum(vec_b) / m
    cov = sum((a - ma) * (b - mb) for a, b in zip(vec_a, vec_b))
    va = math.sqrt(sum((a - ma) ** 2 for a in vec_a))
    vb = math.sqrt(sum((b - mb) ** 2 for b in vec_b))
    corr = cov / (va * vb) if va > 1e-9 and vb > 1e-9 else 0.0
    corr_score = max(0.0, corr)                     # só co-movimento positivo conta
    return round((0.55 * err_score + 0.45 * corr_score) * 100, 1)


# ── ESTRUTURA DE PIVÔS (zigzag) — o jeito CERTO de casar padrão gráfico ──────
# Em vez de borrar a curva inteira, identifica os topos/fundos de swing (pivôs) e
# codifica a SEQUÊNCIA de pernas (cada movimento, com direção e tamanho). Assim
# "fez fundo → repicou → rompeu o fundo → voltou" casa entre eras e escalas
# diferentes, que é como um trader lê o gráfico. (Validado: agora ↔ nov/2018.)

def _zigzag(seg, thr=0.10):
    """Pivôs de swing por reversão de `thr` (ex.: 10%). Retorna [(idx, preço, tipo)]
    alternando 'H'/'L', terminando no ponto atual 'C'. Preserva a ordem temporal."""
    if len(seg) < 3:
        return []
    piv = []
    ext_i, ext_p, direc = 0, seg[0], 0
    for i in range(1, len(seg)):
        p = seg[i]
        if direc >= 0 and p > ext_p:
            ext_p, ext_i = p, i; direc = 1
        elif direc <= 0 and p < ext_p:
            ext_p, ext_i = p, i; direc = -1
        if direc == 1 and p <= ext_p * (1 - thr):
            piv.append((ext_i, ext_p, 'H')); ext_p, ext_i = p, i; direc = -1
        elif direc == -1 and p >= ext_p * (1 + thr):
            piv.append((ext_i, ext_p, 'L')); ext_p, ext_i = p, i; direc = 1
    piv.append((len(seg) - 1, seg[-1], 'C'))
    return piv

def _struct_legs(seg, thr=0.10, k=5):
    """Sequência das últimas `k` PERNAS = log-retorno entre pivôs consecutivos.
    Direção (sinal) + magnitude do movimento. É a 'assinatura estrutural' da janela."""
    piv = _zigzag(seg, thr)
    if len(piv) < 3:
        return None
    legs = [math.log(piv[j][1] / piv[j - 1][1]) for j in range(1, len(piv))
            if piv[j - 1][1] > 0 and piv[j][1] > 0]
    return legs[-k:] if len(legs) >= 2 else None

def _leg_similarity(a, b):
    """Score 0-100 entre duas sequências de pernas. Compara magnitude+direção das
    pernas (erro relativo) e recompensa pernas no MESMO sentido, na ordem."""
    if not a or not b:
        return 0.0
    k = min(len(a), len(b))
    if k < 2:
        return 0.0
    a, b = a[-k:], b[-k:]
    num = sum(abs(x - y) for x, y in zip(a, b))
    den = sum(abs(x) + abs(y) for x, y in zip(a, b)) + 1e-9
    mag_score = max(0.0, 1 - num / den)              # pernas do mesmo tamanho/direção
    sign = sum(1 for x, y in zip(a, b) if (x > 0) == (y > 0)) / k
    return round((0.6 * mag_score + 0.4 * sign) * 100, 1)


# ── 1. Fingerprint estrutural ────────────────────────────────────────────────

def compute_fingerprint(closes, vols=None, window=90, full_closes=None, bars_per_year=365):
    """
    Extrai o fingerprint estrutural dos últimos `window` candles.

    full_closes: histórico completo até o ponto atual (para calcular ATH do ciclo).
                 Se None, usa `closes` como proxy.
    bars_per_year: candles por ano (365 p/ diário do BTC, 52 p/ semanal do S&P500).
                   Usado para o lookback do ATH de ciclo (2 anos) e normalização temporal,
                   garantindo comparação válida entre ativos de timeframe diferente.

    Dimensões principais (em ordem de importância para similaridade):
      pct_from_cycle_ath  : % abaixo do ATH dos últimos 2 anos  ← MAIS IMPORTANTE
      years_since_cycle_ath: anos desde o ATH do ciclo          ← 2º MAIS IMPORTANTE (cross-market)
      position_in_range   : onde preço está no range (0=fundo, 100=topo)
      pct_vs_ema50        : % acima/abaixo EMA50
      range_pct           : (max - min) / min * 100 dentro da janela
      support_tests       : quantas vezes o preço esteve a ≤3% do mínimo
      trend_structure     : HH_HL / LH_LL / lateral
      consecutive_lower_highs : quantos topos locais consecutivos mais baixos
      false_breakdown_reclaim : furou o suporte estabelecido e voltou (spring/2018)
      rsi                 : BAIXO PESO — indicador atrasado, aparece igual em bull e bear
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

    # Histórico completo até o ponto atual (p/ EMAs e ATH do ciclo corretos).
    # IMPORTANTE: EMA50/EMA200 precisam de candles ANTERIORES à janela — usar `hist`,
    # nunca o segmento curto `seg`, senão EMA200 fica None e ma_cross vira 0.
    # PERFORMANCE: calcular o EMA sobre uma CAUDA LIMITADA (~260 candles) e não sobre
    # todo o histórico — senão o scan de janela deslizante vira O(n²) e trava o app.
    hist = full_closes if full_closes else closes
    ema_tail = hist[-max(260, window + 10):]

    # MAs e RSI (RSI tem baixo peso no scoring — aparece igual em bull e bear)
    rsi_val  = _rsi(ema_tail[-50:]) if len(ema_tail) >= 15 else None
    ema50    = _ema(ema_tail, 50)
    ema200   = _ema(ema_tail, 200)
    pct_e50  = round((price - ema50)  / ema50  * 100, 1) if ema50  else None
    pct_e200 = round((price - ema200) / ema200 * 100, 1) if ema200 else None
    # Death/Golden Cross: -1 = death cross (bearish), +1 = golden cross (bullish)
    ma_cross = (1 if (ema50 and ema200 and ema50 > ema200)
                else -1 if (ema50 and ema200) else 0)

    # ATH do ciclo (últimos 2 anos) — dimensão mais importante.
    # Lookback em CANDLES = 2 anos × bars_per_year (730 p/ diário, 104 p/ semanal).
    lookback_ath = min(len(hist), int(2 * bars_per_year))
    cycle_ath = max(hist[-lookback_ath:])
    ath_idx_in_slice = hist[-lookback_ath:].index(cycle_ath)
    days_since_cycle_ath = lookback_ath - 1 - ath_idx_in_slice
    # Normaliza para ANOS — unidade comum entre BTC (diário) e S&P500 (semanal)
    years_since_cycle_ath = round(days_since_cycle_ath / bars_per_year, 2)
    pct_from_cycle_ath = round((price - cycle_ath) / cycle_ath * 100, 1)

    # Falsa ruptura + reclaim (spring / padrão 2018 em $6k):
    # o preço furou o mínimo ESTABELECIDO (primeiros 70% da janela) durante os
    # últimos 30% da janela, e o fechamento atual voltou ACIMA desse mínimo.
    false_breakdown_reclaim = False
    if n_seg >= 30:
        split = int(n_seg * 0.70)
        established_low = min(seg[:split])
        tail = seg[split:]
        dipped_below = any(c < established_low for c in tail)
        # furou de leve (não colapsou): mínimo da cauda no máx -12% do suporte
        marginal = min(tail) >= established_low * 0.88 if tail else False
        reclaimed = price > established_low
        false_breakdown_reclaim = bool(dipped_below and marginal and reclaimed)

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
        "years_since_cycle_ath":   years_since_cycle_ath,
        "position_in_range":       position,
        "range_pct":               range_pct,
        "pct_vs_ema50":            pct_e50,
        "support_tests":           support_tests,
        "consecutive_lower_highs": consecutive_lower_highs,
        "trend_structure":         trend_structure,
        "ma_cross":                ma_cross,
        "false_breakdown_reclaim": false_breakdown_reclaim,
        # Dimensões secundárias (baixo peso)
        "days_since_top":          days_since_top,
        "days_since_bottom":       days_since_bottom,
        "resistance_tests":        resistance_tests,
        "rsi":                     rsi_val,   # baixo peso — só tiebreaker
        "pct_vs_ema200":           pct_e200,
        "vol_trend":               vol_trend,
        "phase":                   phase,
        "window":                  window,
        "bars_per_year":           bars_per_year,
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
    # ESTRUTURA e CENÁRIO acima de tudo. A % de queda do ATH pesa POUCO: o mercado
    # era muito menor e mais volátil no passado (um -60% de 2018 ≈ um -30% de hoje).
    # O que de fato repete entre eras é a SEQUÊNCIA estrutural:
    # bear → fundo → reclaim de média → furo do fundo → recuperação → média seguinte.
    "trend_structure":           0.16,  # ← formato: HH_HL (bull) / LH_LL (bear) / acumulação
    "position_in_range":         0.14,  # onde no range (fundo? meio? topo?)
    "pct_vs_ema50":              0.12,  # relação com a média (reclaim/rejeição)
    "support_tests":             0.12,  # quantas vezes testou/segurou o fundo
    "consecutive_lower_highs":   0.10,  # estrutura de topos
    "pct_vs_ema200":             0.06,  # relação com a média longa
    "days_since_top_ratio":      0.05,  # sequência temporal do movimento
    "days_since_bottom_ratio":   0.05,
    "pct_from_cycle_ath":        0.10,  # ← peso baixo: profundidade importa pouco
    "days_since_cycle_ath_norm": 0.05,  # tempo desde o topo do ciclo
    "range_pct_norm":            0.05,  # amplitude (volatilidade varia por era)
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

    # scaled(): ajusta a MAGNITUDE do candidato p/ cross-market (BTC vs S&P500).
    # Ex.: um -50% do BTC equivale a ~-14% do S&P (×3.5). Para BTC-BTC scale=1.0 (sem efeito).
    def scaled(v):
        return v * scale_range if v is not None else None

    def trend_pen(a, b):
        """Penalidade categórica de estrutura de topos/fundos (o formato gráfico)."""
        if a is None or b is None:
            return 0.5
        if a == b:
            return 0.0
        if "lateral" in (a, b):   # lateral é transição — meia penalidade
            return 0.5
        return 1.0

    penalties = {}

    # ── ESTRUTURA / CENÁRIO (peso alto) — o que de fato repete entre eras ───────
    # Estrutura de topos/fundos: o formato (bear LH_LL, acumulação LH_HL, bull HH_HL)
    penalties["trend_structure"] = trend_pen(
        fp_a.get("trend_structure"), fp_b.get("trend_structure")
    )
    # Posição no range (fundo / meio / topo da janela)
    penalties["position_in_range"] = pnorm(
        fp_a.get("position_in_range"), fp_b.get("position_in_range"), 18
    )
    # Relação com as médias (reclaim/rejeição da EMA50/EMA200) — escalada p/ cross-market
    penalties["pct_vs_ema50"]  = pnorm(fp_a.get("pct_vs_ema50"),  scaled(fp_b.get("pct_vs_ema50")),  12)
    penalties["pct_vs_ema200"] = pnorm(fp_a.get("pct_vs_ema200"), scaled(fp_b.get("pct_vs_ema200")), 14)
    # Testes de suporte — quantas vezes segurou/furou o fundo
    penalties["support_tests"] = pnorm(
        fp_a.get("support_tests"), fp_b.get("support_tests"), 4
    )
    # Topos consecutivos mais baixos (estrutura bear/bull)
    penalties["consecutive_lower_highs"] = pnorm(
        fp_a.get("consecutive_lower_highs"), fp_b.get("consecutive_lower_highs"), 2
    )
    # Dias desde topo/fundo (ratio da janela) — sequência temporal do movimento
    w = fp_a.get("window", 90)
    rat_top_a = (fp_a.get("days_since_top") or 0)    / w
    rat_top_b = (fp_b.get("days_since_top") or 0)    / fp_b.get("window", w)
    rat_bot_a = (fp_a.get("days_since_bottom") or 0) / w
    rat_bot_b = (fp_b.get("days_since_bottom") or 0) / fp_b.get("window", w)
    penalties["days_since_top_ratio"]    = pnorm(rat_top_a, rat_top_b, 0.25)
    penalties["days_since_bottom_ratio"] = pnorm(rat_bot_a, rat_bot_b, 0.25)

    # ── CONTEXTO (peso baixo) — magnitude/profundidade importa pouco ────────────
    # % abaixo do ATH do ciclo — magnitude escalada p/ cross-market, tolerância larga
    penalties["pct_from_cycle_ath"] = pnorm(
        fp_a.get("pct_from_cycle_ath"), scaled(fp_b.get("pct_from_cycle_ath")), 25
    )
    # Tempo desde o ATH do ciclo — em ANOS (unidade comum entre timeframes)
    ath_yr_a = fp_a.get("years_since_cycle_ath")
    ath_yr_b = fp_b.get("years_since_cycle_ath")
    if ath_yr_a is None:
        ath_yr_a = (fp_a.get("days_since_cycle_ath") or 0) / 365.0
    if ath_yr_b is None:
        ath_yr_b = (fp_b.get("days_since_cycle_ath") or 0) / 365.0
    penalties["days_since_cycle_ath_norm"] = pnorm(ath_yr_a, ath_yr_b, 0.4)
    # Range normalizado (amplitude) — volatilidade varia por era, peso baixo
    rng_a = fp_a.get("range_pct") or 0
    rng_b = (fp_b.get("range_pct") or 0) * scale_range
    penalties["range_pct_norm"] = pnorm(rng_a, rng_b, 25)

    # RSI: NÃO entra no scoring — é lagging e aparece igual em contextos opostos

    total_penalty = sum(WEIGHTS[k] * penalties[k] for k in WEIGHTS)
    score = (1 - total_penalty) * 100

    # Bônus de forma: mesma assinatura "furou o fundo e recuperou" (spring/2018).
    # +12 pts — é exatamente o tipo de CENÁRIO que define semelhança estrutural.
    if fp_a.get("false_breakdown_reclaim") and fp_b.get("false_breakdown_reclaim"):
        score += 12

    return max(round(score, 1), 0.0)


# ── 3. Scanner de janelas deslizantes ────────────────────────────────────────

def scan_analogues(closes, current_fp, vols=None, timestamps=None,
                   window=90, top_n=5, scale_range=1.0, min_score=55.0,
                   bars_per_year=365, regime_filter=None,
                   current_struct=None, shape_weight=0.0, exclude_recent=0):
    """
    Desliza a janela de `window` candles por todo o histórico de `closes`,
    computa o fingerprint de cada janela e pontua a similaridade com `current_fp`.
    Retorna os `top_n` melhores matches com o que aconteceu depois.

    scale_range:   1.0 para BTC vs BTC; 3.5 para BTC vs S&P500.
    bars_per_year: 365 (diário) ou 52 (semanal) — passado ao fingerprint.
    regime_filter: "bear" → só mantém janelas estruturalmente bear
                   (pct_from_cycle_ath <= -35 e death cross). None = liberal.
    current_struct: sequência de PERNAS atual (de _struct_legs). Se dada e shape_weight>0,
                   o score final combina ESTRUTURA DE PIVÔS (o padrão gráfico real) +
                   fingerprint. É o canal que casa "fundo→repique→furo→reclaim".
    shape_weight:  0..1 — quanto a estrutura de pivôs pesa no score (vs fingerprint).
    exclude_recent: nº de candles ao FINAL a ignorar — evita casar o presente com o
                   passado RECENTE do MESMO ciclo (ex.: comparar o bear de agora com
                   ele mesmo 4 meses atrás, o que não prevê nada). Só p/ self-scan.
    """
    n = len(closes)
    if n < window + 90:
        return []

    # Janela de histórico passada ao fingerprint: o bastante p/ ATH de ciclo (2 anos)
    # + EMA200. LIMITADA para o scan ser O(n) e não O(n²) (senão trava o app).
    hist_window = int(2 * bars_per_year) + window + 10
    last_allowed = n - 1 - exclude_recent      # índice máximo permitido p/ um match

    results = []
    for i in range(window, n - 30):
        if exclude_recent and i > last_allowed:
            continue
        seg  = closes[i - window: i + 1]
        vseg = vols[i - window: i + 1] if vols and len(vols) > i else None
        lo   = max(0, i + 1 - hist_window)
        full_up_to_i = closes[lo: i + 1]

        fp_i = compute_fingerprint(seg, vseg, window,
                                   full_closes=full_up_to_i,
                                   bars_per_year=bars_per_year)
        if not fp_i:
            continue

        # Filtro de regime (só BTC em bear): exclui bull/correção rasa
        if regime_filter == "bear":
            pct_ath = fp_i.get("pct_from_cycle_ath")
            if pct_ath is None or pct_ath > -35 or fp_i.get("ma_cross") != -1:
                continue
            # RETROSPECTIVA: um bear ESTRUTURAL não se recupera para perto do ATH
            # logo depois. Correções dentro de bull (ex.: jul/2021 caiu -54% e fez novo
            # ATH em ~3 meses) são FALSOS bears — descarta olhando o que veio depois.
            lookback_ath = min(len(full_up_to_i), int(2 * bars_per_year))
            cyc_ath_i = max(full_up_to_i[-lookback_ath:]) if lookback_ath else None
            fwd_horizon = int(bars_per_year / 3)          # ~4 meses
            fwd_max = max(closes[i: min(i + fwd_horizon, n)])
            if cyc_ath_i and fwd_max >= cyc_ath_i * 0.85:  # voltou a -15% do ATH = era bull
                continue

        # FINGERPRINT pontual — o "estado/regime" do mercado (confirma contexto)
        struct = similarity_score(current_fp, fp_i, scale_range)

        # ESTRUTURA DE PIVÔS — o padrão gráfico real (pernas: fundo→repique→furo→...)
        shape = None
        if shape_weight > 0 and current_struct:
            legs_i = _struct_legs(seg)
            shape = _leg_similarity(current_struct, legs_i)
            score = round((1 - shape_weight) * struct + shape_weight * shape, 1)
        else:
            score = struct

        if score < min_score:
            continue

        # Forward returns (escala pelo timeframe: 30/60/90 candles)
        fwd30 = round((closes[min(i + 30, n-1)] - closes[i]) / closes[i] * 100, 1)
        fwd60 = round((closes[min(i + 60, n-1)] - closes[i]) / closes[i] * 100, 1)
        fwd90 = round((closes[min(i + 90, n-1)] - closes[i]) / closes[i] * 100, 1)

        ts  = timestamps[i] if timestamps and i < len(timestamps) else None
        era = "jovem" if (ts and ts < 410227200) else "moderno"

        results.append({
            "score":    score,
            "struct":   struct,
            "shape":    shape,
            "idx":      i,
            "window":   window,
            "periodo":  _ts_to_ym(ts) if ts else f"candle {i}",
            "era":      era,
            "fp":       fp_i,
            "fwd30":    fwd30,
            "fwd60":    fwd60,
            "fwd90":    fwd90,
            "named":    None,
        })

    # Ordena por score, remove duplicatas muito próximas (dentro de `window` candles)
    results.sort(key=lambda x: -x["score"])
    filtered = []
    used_idx = []
    for r in results:
        if all(abs(r["idx"] - u) > max(30, window) for u in used_idx):
            filtered.append(r)
            used_idx.append(r["idx"])
        if len(filtered) >= top_n:
            break

    return filtered


def scan_analogues_multiwindow(closes, vols=None, timestamps=None,
                               windows=(60, 90, 120, 180), top_n=5,
                               scale_range=1.0, min_score=55.0,
                               bars_per_year=365, regime_filter=None,
                               current_fps=None, current_segs=None,
                               shape_weight=0.0, exclude_recent=0,
                               min_separation=45):
    """
    Roda scan_analogues em várias janelas e mescla os resultados, priorizando o
    maior score — acha a forma gráfica mais parecida independente da duração exata.

    current_fps:  dict {window: fingerprint_atual_dessa_janela}. O fingerprint atual
                  precisa ser calculado na MESMA janela de cada scan para casar a forma.
    current_segs: dict {window: trecho_de_precos_atual} para a comparação de TRAJETÓRIA.
                  Em self-scan (BTC vs BTC) usa os últimos `w` candles de `closes`.
                  Em cross-market (BTC vs S&P500) passe o trecho do BTC aqui.
    shape_weight: 0..1 — peso do desenho do gráfico (trajetória) no score final.
    exclude_recent: candles ao final a ignorar (não casar o presente com o passado recente).
    min_separation: candles mínimos entre dois análogos retornados — DIVERSIDADE DE
                   EPISÓDIO. Evita o top-N virar 2-3 fatias do MESMO bear (ex.: 2022-05 e
                   2022-08). Com ~180 (6 meses), cada análogo é um período distinto.
    """
    all_results = []
    for w in windows:
        cur_fp = (current_fps or {}).get(w)
        if cur_fp is None:
            cur_fp = compute_fingerprint(closes, vols, w,
                                         full_closes=closes,
                                         bars_per_year=bars_per_year)
        if not cur_fp:
            continue
        cur_seg = (current_segs or {}).get(w)
        if cur_seg is None:
            cur_seg = closes[-w:]            # self-scan: estrutura atual = últimos w candles
        cur_struct = _struct_legs(cur_seg) if shape_weight > 0 else None
        res = scan_analogues(
            closes, cur_fp, vols=vols, timestamps=timestamps,
            window=w, top_n=top_n, scale_range=scale_range,
            min_score=min_score, bars_per_year=bars_per_year,
            regime_filter=regime_filter,
            current_struct=cur_struct, shape_weight=shape_weight,
            exclude_recent=exclude_recent,
        )
        all_results.extend(res)

    # Dedup global por proximidade temporal (diversidade de episódio), maior score 1º
    all_results.sort(key=lambda x: -x["score"])
    merged = []
    used_idx = []
    for r in all_results:
        if all(abs(r["idx"] - u) > min_separation for u in used_idx):
            merged.append(r)
            used_idx.append(r["idx"])
        if len(merged) >= top_n:
            break
    return merged


# ── 4. Biblioteca de padrões históricos nomeados do BTC ──────────────────────

NAMED_PATTERNS_BTC = {
    "2018_bear_lateral": {
        "nome":    "Bear 2018 — acumulação em $6k",
        "periodo": "Mar/2018 – Nov/2018",
        "regime":  "bear",
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
    "2018_falsa_ruptura_6k": {
        "nome":    "Bear 2018 — falsa ruptura + reclaim de $6k",
        "periodo": "Jun/2018 – Out/2018",
        "regime":  "bear",
        "desc": (
            "Dentro do bear de 2018, o BTC firmou suporte em ~$6k testado várias vezes. "
            "Após repiques que falharam, rompeu LEVEMENTE abaixo de $6k (ex.: ~$5.8k) "
            "em mais de uma ocasião e RECLAMOU o nível, voltando para a faixa $6.2k-$6.8k. "
            "Estrutura: lateral longa com furos rápidos do suporte (springs) seguidos de volta. "
            "Cada falsa ruptura parecia o início da queda final — mas o nível segurou por meses."
        ),
        "desfecho": (
            "As primeiras falsas rupturas reverteram (reclaim), MAS a ruptura final de nov/2018 "
            "com volume alto confirmou o breakdown → capitulação para $3.1k (-48% adicional)."
        ),
        "licao": (
            "Falsa ruptura + reclaim em bear pode reverter no curto prazo (spring), porém o "
            "suporte muito testado tende a romper de vez quando vem com volume. "
            "Distinguir reclaim (volume baixo no furo) de breakdown real (volume alto)."
        ),
        "fingerprint_aprox": {
            "pct_from_cycle_ath": -68, "days_since_cycle_ath": 240,
            "position_in_range": 18, "pct_vs_ema50": -6,
            "consecutive_lower_highs": 2, "support_tests": 9,
            "false_breakdown_reclaim": True, "phase": "bear_lateral",
        },
    },
    "2019_recuperacao": {
        "nome":    "Recuperação 2019 — bull de $3.1k a $14k",
        "periodo": "Dez/2018 – Jun/2019",
        "regime":  "bull",
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
        "regime":  "bull",
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
        "regime":  "bull",
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
        "regime":  "bear",
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
        "regime":  "acumulacao",
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
        "regime":  "acumulacao",
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


def identify_named_pattern(current_fp, regime_filter=None):
    """
    Compara o fingerprint atual com os padrões nomeados e retorna
    o mais similar com o score de confiança.

    regime_filter: "bear" → só considera padrões com regime=="bear"
                   (exclui COVID/2019/2021/2024). None = todos.
    """
    if not current_fp:
        return None, 0

    best_name  = None
    best_score = 0

    bpy = current_fp.get("bars_per_year", 365)
    for name, pat in NAMED_PATTERNS_BTC.items():
        if regime_filter == "bear" and pat.get("regime") != "bear":
            continue
        fp_ref = pat["fingerprint_aprox"]
        dsca = fp_ref.get("days_since_cycle_ath", current_fp.get("days_since_cycle_ath"))
        # Monta fingerprint temporário compatível
        fp_temp = {
            "pct_from_cycle_ath":      fp_ref.get("pct_from_cycle_ath",
                                            current_fp.get("pct_from_cycle_ath")),
            "days_since_cycle_ath":    dsca,
            "years_since_cycle_ath":   (dsca / 365.0) if dsca is not None else None,
            "position_in_range":       fp_ref.get("position_in_range", 50),
            "pct_vs_ema50":            fp_ref.get("pct_vs_ema50", 0),
            "range_pct":               current_fp.get("range_pct"),
            "days_since_top":          current_fp.get("days_since_top"),
            "days_since_bottom":       current_fp.get("days_since_bottom"),
            "support_tests":           fp_ref.get("support_tests", current_fp.get("support_tests")),
            "consecutive_lower_highs": fp_ref.get("consecutive_lower_highs", 0),
            "false_breakdown_reclaim": fp_ref.get("false_breakdown_reclaim", False),
            "window":                  current_fp.get("window", 90),
            "bars_per_year":           bpy,
        }
        score = similarity_score(current_fp, fp_temp)
        if score > best_score:
            best_score = score
            best_name  = name

    return best_name, best_score


# ── 5. Contexto completo para o prompt ───────────────────────────────────────

def build_pattern_context(btc_closes, btc_vols=None,
                           sp500_closes=None, sp500_timestamps=None,
                           window_btc=90, window_sp500=13,
                           btc_timestamps=None, regime=None):
    """
    Gera o bloco de análise de padrões para injetar no system prompt.

    window_btc     : janela base em dias para o fingerprint BTC
    window_sp500   : janela base em semanas para o S&P500 (≈90 dias)
    btc_timestamps : timestamps (segundos) alinhados a btc_closes → datas reais
    regime         : "bear"/"bull"/None — filtra análogos BTC e padrões nomeados
    """
    if len(btc_closes) < window_btc + 90:
        return ""

    # ── Fingerprint BTC atual (janela base) ───────────────────────────────────
    current_fp = compute_fingerprint(
        btc_closes, btc_vols, window_btc, full_closes=btc_closes, bars_per_year=365
    )
    if not current_fp:
        return ""

    # ── Padrão nomeado mais próximo (filtrado por regime) ─────────────────────
    named_match, named_score = identify_named_pattern(current_fp, regime_filter=regime)

    # ── Análogos BTC — scan MULTI-JANELA p/ máxima semelhança gráfica ──────────
    # Janelas curtas E longas: 90d capta o movimento recente; 270/365d capta o ARCO
    # inteiro de um bear (ex.: 2018 levou ~12 meses — janela curta não enxerga o arco).
    btc_windows = (90, 180, 270, 365)
    btc_analogues = scan_analogues_multiwindow(
        btc_closes, vols=btc_vols, timestamps=btc_timestamps,
        windows=btc_windows, top_n=3, scale_range=1.0,
        min_score=50.0, bars_per_year=365, regime_filter=regime,
        shape_weight=0.65,   # trajetória (desenho do gráfico) domina, estrutura confirma
        exclude_recent=270,  # ignora os últimos ~9 meses (não casar o bear atual consigo mesmo)
        min_separation=180,  # diversidade: cada análogo é um episódio distinto (não 2x o mesmo bear)
    )

    # ── Análogos S&P500 — liberal (qualquer era/regime), timeframe semanal ─────
    sp500_analogues = []
    if sp500_closes and len(sp500_closes) > window_sp500 + 60:
        sp_windows = (window_sp500, window_sp500 * 2, window_sp500 * 3)  # ~3m, 6m, 9m
        sp500_analogues = scan_analogues_multiwindow(
            sp500_closes, timestamps=sp500_timestamps,
            windows=sp_windows, top_n=3, scale_range=3.5,
            min_score=48.0, bars_per_year=52, regime_filter=None,
            current_fps={
                w: compute_fingerprint(btc_closes, btc_vols, _btc_equiv_days(w),
                                       full_closes=btc_closes, bars_per_year=365)
                for w in sp_windows
            },
            # Trajetória é IDEAL p/ cross-market: a forma ignora a escala de preço,
            # então o desenho do BTC casa direto com o do S&P (sem o hack de 3.5x).
            current_segs={w: btc_closes[-_btc_equiv_days(w):] for w in sp_windows},
            shape_weight=0.7,
            min_separation=26,   # ~6 meses em semanas — episódios distintos no S&P
        )

    # ── Monta o bloco de texto ────────────────────────────────────────────────
    fbr = current_fp.get("false_breakdown_reclaim")
    lines = [
        "",
        "================================================================",
        "RECONHECIMENTO DE PADROES ESTRUTURAIS (Pattern Engine)",
        "================================================================",
        "",
        f"FINGERPRINT DO MERCADO ATUAL (janela {window_btc} candles):",
        f"  Fase detectada:        {current_fp['phase']}",
        f"  % abaixo do ATH ciclo: {current_fp['pct_from_cycle_ath']}% (ha {current_fp.get('years_since_cycle_ath','?')} anos)",
        f"  Range da janela:       ${current_fp['range_low']:,.0f} - ${current_fp['range_high']:,.0f} ({current_fp['range_pct']}%)",
        f"  Posicao no range:      {current_fp['position_in_range']}% (0=fundo, 100=topo)",
        f"  Dias desde o topo:     {current_fp['days_since_top']}",
        f"  Dias desde o fundo:    {current_fp['days_since_bottom']}",
        f"  Estrutura topos/fundos:{current_fp['trend_structure']}",
        f"  Topos mais baixos consec.: {current_fp['consecutive_lower_highs']}",
        f"  Testes de suporte:     {current_fp['support_tests']} (vezes proximas ao minimo)",
        f"  Testes de resistencia: {current_fp['resistance_tests']}",
        f"  FALSA RUPTURA + RECLAIM: {'SIM — furou o suporte e voltou (padrao spring / 2018 em $6k)' if fbr else 'nao'}",
        f"  RSI:                   {current_fp.get('rsi','N/A')} (baixo peso)",
        f"  Pos. vs EMA50:         {current_fp['pct_vs_ema50']}%",
        f"  Pos. vs EMA200:        {current_fp['pct_vs_ema200']}%",
        f"  Death/Golden cross:    {'Death Cross' if current_fp.get('ma_cross')==-1 else 'Golden Cross' if current_fp.get('ma_cross')==1 else 'N/A'}",
        f"  Volume:                {current_fp['vol_trend'] or 'N/A'}",
        "",
    ]

    if regime == "bear":
        lines += [
            "REGIME = BEAR: analogos BTC restritos a outros bear markets reais",
            "  (so periodos com -35% ou mais do ATH e death cross — ex.: 2014, 2018, 2022).",
            "",
        ]

    # Match mais semelhante em destaque (maior score de todos os análogos BTC)
    if btc_analogues:
        top = btc_analogues[0]
        fp_t = top["fp"]
        _sh = f" (estrutura de pivos {top['shape']:.0f}/100 + fingerprint {top['struct']:.0f}/100)" if top.get("shape") is not None else ""
        lines += [
            f"PADRAO REAL MAIS SEMELHANTE (BTC) — score {top['score']:.0f}/100{_sh}:",
            f"  Periodo: {top['periodo']} | janela {top['window']}d | fase={fp_t.get('phase','?')}",
            f"  Estrutura: pos={fp_t.get('position_in_range','?')}% ATHdist={fp_t.get('pct_from_cycle_ath','?')}% "
            f"falsa_ruptura={'SIM' if fp_t.get('false_breakdown_reclaim') else 'nao'}",
            f"  O que veio depois (retorno real): 30d={top['fwd30']}% 60d={top['fwd60']}% 90d={top['fwd90']}%",
            "",
        ]

    # Padrão nomeado mais próximo
    if named_match and named_score >= 55:
        pat = NAMED_PATTERNS_BTC[named_match]
        lines += [
            f"PADRAO HISTORICO NOMEADO MAIS PROXIMO (score {named_score:.0f}/100):",
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
            f"ANALOGOS HISTORICOS BTC — TOP {len(btc_analogues)} MAIS SIMILARES (scan multi-janela):",
            "  (Score = ESTRUTURA de pivos/pernas do grafico + fingerprint; ignora escala de preco; datas reais)",
        ]
        for a in btc_analogues:
            fp_a = a["fp"]
            _sh = f"estrut={a['shape']:.0f} " if a.get("shape") is not None else ""
            lines.append(
                f"  Score {a['score']:.0f}/100 | {_sh}| {a['periodo']} (jan {a['window']}d) | "
                f"fase={fp_a.get('phase','?')} pos={fp_a.get('position_in_range','?')}% "
                f"ATHdist={fp_a.get('pct_from_cycle_ath','?')}% | "
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
    elif regime == "bear":
        lines += [
            "ANALOGOS HISTORICOS BTC: nenhum bear real casou com a janela atual no filtro estrito.",
            "  Use a BIBLIOTECA de padroes bear (2014/2018/2022) abaixo como referencia.",
            "",
        ]

    # Análogos S&P500 (cross-market — sempre presente)
    if sp500_analogues:
        lines += [
            f"ANALOGOS CROSS-MARKET S&P500 — TOP {len(sp500_analogues)} (match por ESTRUTURA, datas reais):",
            "  Mesma SEQUENCIA de pivos em OUTRO mercado (estrutura ignora escala). Multiplicar retorno por ~3.5x p/ equiv BTC.",
        ]
        for a in sp500_analogues:
            fp_a = a["fp"]
            era_label = "[ERA JOVEM <1983 - mais analogo ao BTC]" if a["era"] == "jovem" else "[moderno]"
            _sh = f"estrut={a['shape']:.0f} " if a.get("shape") is not None else ""
            lines.append(
                f"  Score {a['score']:.0f}/100 | {_sh}| {a['periodo']} (jan {a['window']}sem) {era_label} | "
                f"fase={fp_a.get('phase','?')} pos={fp_a.get('position_in_range','?')}% "
                f"ATHdist={fp_a.get('pct_from_cycle_ath','?')}% | "
                f"S&P500 real: 30sem={a['fwd30']}% 90sem={a['fwd90']}% "
                f"| equiv BTC ~{round(a['fwd30']*3.5,0)}% / {round(a['fwd90']*3.5,0)}%"
            )
        lines.append("")
    else:
        lines += [
            "ANALOGOS CROSS-MARKET S&P500: dados insuficientes ou nenhum match no momento.",
            "  Use os ciclos do S&P500 jovem / Ouro 1970s / NASDAQ jovem do contexto narrativo.",
            "",
        ]

    # Biblioteca de padrões nomeados (filtrada por regime quando bear)
    lib_items = [(n, p) for n, p in NAMED_PATTERNS_BTC.items()
                 if not (regime == "bear" and p.get("regime") != "bear")]
    lib_title = ("BIBLIOTECA DE PADROES BEAR DO BTC (referencia — regime atual = bear):"
                 if regime == "bear"
                 else "BIBLIOTECA DE PADROES BTC (referencia para a IA usar):")
    lines += [lib_title]
    for name, pat in lib_items:
        lines.append(f"  [{pat['nome']} | {pat['periodo']} | regime={pat.get('regime','?')}]")
        lines.append(f"    Desc: {pat['desc']}")
        lines.append(f"    Desfecho: {pat['desfecho']}")
        lines.append(f"    Licao: {pat['licao']}")

    lines += [
        "",
        "INSTRUCOES:",
        "  1. Use o FINGERPRINT ATUAL para descrever objetivamente a estrutura presente",
        "  2. PRIORIZE o PADRAO REAL MAIS SEMELHANTE (maior score). O score = ESTRUTURA DE PIVOS",
        "     (a sequencia de pernas/swings do grafico, ignorando a escala de preco) + fingerprint.",
        "     Descreva a SEQUENCIA que se repete: fundo -> repique -> furo do fundo -> reclaim -> etc.",
        "  3. Cite os ANALOGOS por DATA REAL (a engine ja calculou) — NUNCA invente anos",
        "  4. Em regime bear, so cite bear markets reais do BTC (2014/2018/2022)",
        "  5. Se FALSA RUPTURA + RECLAIM = SIM, compare explicitamente com o $6k de 2018",
        "  6. S&P500 / Ouro / NASDAQ jovem = analogos cross-market validos se a estrutura casar",
        "  7. Nunca diga 'identico a X' — diga 'em X de Y casos similares, o resultado foi Z'",
        "================================================================",
    ]

    return "\n".join(lines)


def _btc_equiv_days(sp500_weeks):
    """Converte janela do S&P500 (semanas) p/ janela BTC equivalente (dias)."""
    return max(40, int(sp500_weeks * 7))
