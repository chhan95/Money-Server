"""Investment Score — 순수 함수. I/O·DB·네트워크 없음.

원칙
  1. 모든 점수는 명시적 산식/구간에서 나온다. 임의 부여 금지.
  2. 입력이 없으면 그 항목은 None을 반환하고 총점에서 '제외'된다.
     0점 처리하지 않는다 (데이터 없음 ≠ 나쁨).
  3. 각 항목은 구성요소(raw 값 → 점수 → 가중)를 함께 반환해 화면에서 감사 가능하게 한다.
  4. 계단식이 아니라 구간 선형보간. 14.9% → 15.1% 같은 경계에서 총점이 튀지 않게.

주의: ROIC 계열(자본효율·해자프록시·재투자)이 총점의 약 35%를 좌우한다.
      사용자가 ROIC를 가장 중요하게 본다는 요구에 따른 의도된 가중이며,
      화면에도 이를 명시한다.
"""
from __future__ import annotations

SCORE_VERSION = 1

WEIGHTS = {
    "capital_efficiency": 20,
    "growth":             15,
    "valuation":          15,
    "profitability":      12,
    "moat_proxy":         12,
    "reinvestment":       10,
    "pricing_power":       8,
    "financial_health":    8,
}

LABELS = {
    "capital_efficiency": "자본효율",
    "growth":             "성장성",
    "valuation":          "밸류에이션",
    "profitability":      "수익성",
    "moat_proxy":         "해자 (정량 프록시)",
    "reinvestment":       "재투자",
    "pricing_power":      "가격결정력 (정량 프록시)",
    "financial_health":   "재무건전성",
}

MIN_COVERAGE = 0.60   # 이 미만이면 총점을 내지 않는다


def band(v, points):
    """구간 선형보간. points = [(임계값, 점수)] 오름차순. v가 None이면 None."""
    if v is None:
        return None
    lo_t, lo_s = points[0]
    if v <= lo_t:
        return float(lo_s)
    for i in range(1, len(points)):
        hi_t, hi_s = points[i]
        if v <= hi_t:
            prev_t, prev_s = points[i - 1]
            if hi_t == prev_t:
                return float(hi_s)
            ratio = (v - prev_t) / (hi_t - prev_t)
            return float(prev_s + ratio * (hi_s - prev_s))
    return float(points[-1][1])


def _c(name, raw, score, weight, unit="%", src=""):
    """구성요소 하나 (화면 감사표에 그대로 렌더된다)."""
    return {"name": name, "raw": raw, "score": score, "weight": weight,
            "unit": unit, "src": src}


def _blend(components):
    """구성요소들의 가중평균. 점수가 있는 것만 사용. 전부 없으면 None."""
    avail = [c for c in components if c["score"] is not None]
    if not avail:
        return None, components
    tw = sum(c["weight"] for c in avail)
    if tw <= 0:
        return None, components
    return sum(c["score"] * c["weight"] for c in avail) / tw, components


def _pct(m):
    """analytics의 지표 dict에서 % 단위 숫자를 꺼낸다 (0.25 → 25.0)."""
    if not m or m.get("v") is None:
        return None
    return m["v"] * 100


def _raw(m):
    return None if not m or m.get("v") is None else m["v"]


# ─────────────────────────────────────────────────────────────
#  개별 항목
# ─────────────────────────────────────────────────────────────

GROWTH_CURVE = [(0, 0), (5, 30), (10, 50), (15, 70), (25, 85), (40, 100)]


def score_growth(d):
    rev = _pct(d.get("rev_cagr"))
    net = _pct(d.get("net_cagr"))
    fcf = _pct(d.get("fcf_cagr"))
    comps = [
        _c("매출 CAGR",   rev, band(rev, GROWTH_CURVE), 0.40, src=(d.get("rev_cagr") or {}).get("src", "")),
        _c("순이익 CAGR", net, band(net, GROWTH_CURVE), 0.35, src=(d.get("net_cagr") or {}).get("src", "")),
        _c("FCF CAGR",    fcf, band(fcf, GROWTH_CURVE), 0.25, src=(d.get("fcf_cagr") or {}).get("src", "")),
    ]
    return _blend(comps)


def score_profitability(d):
    gm  = _pct(d.get("gross_margin"))
    om  = _pct(d.get("operating_margin"))
    fcm = _pct(d.get("fcf_margin"))
    comps = [
        _c("매출총이익률", gm,  band(gm,  [(20, 20), (35, 45), (50, 65), (65, 85), (75, 100)]), 0.35),
        _c("영업이익률",   om,  band(om,  [(5, 20), (10, 40), (20, 65), (30, 85), (40, 100)]), 0.35),
        _c("FCF 마진",     fcm, band(fcm, [(0, 10), (5, 35), (10, 55), (20, 80), (30, 100)]), 0.30),
    ]
    return _blend(comps)


ROIC_CURVE = [(5, 10), (10, 30), (15, 50), (20, 65), (30, 85), (50, 100)]


def score_capital_efficiency(d):
    roic   = _pct(d.get("roic"))
    median = _pct(d.get("roic_median"))
    trend  = None
    if roic is not None and median is not None:
        trend = roic - median          # pp 단위 추세
    comps = [
        _c("ROIC (최근)",   roic,   band(roic,   ROIC_CURVE), 0.50,
           src=(d.get("roic") or {}).get("src", "")),
        _c("ROIC 중앙값",   median, band(median, ROIC_CURVE), 0.30),
        _c("ROIC 추세(pp)", trend,  band(trend, [(-10, 20), (0, 50), (5, 70), (15, 90), (30, 100)]), 0.20,
           unit="pp"),
    ]
    return _blend(comps)


def score_moat_proxy(d):
    """정량 프록시 — 서술형 해자 평가가 아니다.
    높은 ROIC를 몇 년이나 유지했는가 + 매출총이익률 수준과 안정성.
    """
    series = [r for r in (d.get("roic_series") or []) if r.get("roic") is not None]
    persist = None
    if series:
        persist = sum(1 for r in series if r["roic"] >= 0.15) / len(series) * 100
    gm  = _pct(d.get("gross_margin"))
    std = _pct(d.get("gross_margin_std"))   # pp
    comps = [
        _c("ROIC 15%+ 유지 비율", persist, band(persist, [(0, 0), (50, 50), (80, 80), (100, 100)]), 0.30),
        _c("매출총이익률",        gm,      band(gm, [(20, 20), (35, 45), (50, 65), (65, 85), (75, 100)]), 0.30),
        _c("매출총이익률 변동성", std,
           band(None if std is None else -std,
                [(-10, 0), (-7, 25), (-4, 50), (-2, 75), (-1, 90), (0, 100)]), 0.40, unit="pp"),
    ]
    return _blend(comps)


def score_pricing_power(d):
    """정량 프록시 — 마진을 지키면서 성장하는가."""
    gm_series = [g["v"] for g in (d.get("gross_margin_series") or []) if g.get("v") is not None]
    gm_trend = None
    if len(gm_series) >= 3:
        avg = sum(gm_series) / len(gm_series)
        gm_trend = (gm_series[-1] - avg) * 100      # pp
    rev_g = _pct(d.get("rev_cagr"))
    quality = None
    if rev_g is not None and gm_trend is not None:
        quality = 100 if (rev_g > 10 and gm_trend >= -0.5) else 40
    comps = [
        _c("매출총이익률 추세", gm_trend,
           band(gm_trend, [(-5, 10), (-2, 35), (0, 55), (2, 75), (5, 90), (10, 100)]), 0.60, unit="pp"),
        _c("성장+마진 동시 유지", quality, quality, 0.40, unit=""),
    ]
    return _blend(comps)


def score_reinvestment(d):
    """고ROIC로 자본을 더 투입할수록 좋다. 단, 저ROIC에서의 대규모 재투자는 감점."""
    incr  = _pct(d.get("incremental_roic"))
    rate  = _pct(d.get("reinvestment"))
    roic  = _pct(d.get("roic"))
    rate_score = band(rate, [(0, 20), (20, 50), (50, 80), (100, 100)])
    # 수익률이 자본비용에 못 미치는데 재투자를 많이 하면 가치 파괴 → 점수 반전
    if rate_score is not None and roic is not None and roic < 10:
        rate_score = 100 - rate_score
    comps = [
        _c("증분 ROIC", incr, band(incr, [(0, 15), (10, 40), (20, 60), (30, 80), (50, 100)]), 0.60,
           src=(d.get("incremental_roic") or {}).get("src", "")),
        _c("재투자율",  rate, rate_score, 0.40),
    ]
    return _blend(comps)


def score_financial_health(d):
    nde = _raw(d.get("net_debt_ebitda"))
    fcf_pos = d.get("fcf_positive_years") or {}
    n, total = fcf_pos.get("v"), fcf_pos.get("total")
    fcf_ratio = (n / total * 100) if (n is not None and total) else None
    comps = [
        _c("순부채/EBITDA", nde,
           band(None if nde is None else -nde,
                [(-5, 0), (-4, 25), (-3, 40), (-2, 55), (-1, 70), (0, 85), (1, 100)]), 0.60, unit="x"),
        _c("FCF 흑자 연도 비율", fcf_ratio, band(fcf_ratio, [(0, 0), (50, 50), (80, 80), (100, 100)]), 0.40),
    ]
    return _blend(comps)


def score_valuation(d):
    """성장 대비 멀티플과 자기 과거 대비 위치로 평가.
    고성장 기업이 멀티플이 높다는 이유만으로 자동 감점되지 않게 설계.
    """
    peg_v = _raw(d.get("peg"))
    rel   = _raw(d.get("mult_vs_hist"))       # 현재 ÷ 5년 평균
    fcfy  = _pct(d.get("fcf_yield"))
    comps = [
        _c("PEG", peg_v,
           band(None if peg_v is None else -peg_v,
                [(-5, 10), (-4, 25), (-3, 40), (-2, 50), (-1.5, 70), (-1.0, 85), (-0.5, 100)]),
           0.50, unit="x", src=(d.get("peg") or {}).get("src", "")),
        _c("5년 평균 대비", rel,
           band(None if rel is None else -rel,
                [(-2.0, 10), (-1.6, 25), (-1.3, 40), (-1.15, 55), (-1.0, 70), (-0.85, 85), (-0.70, 100)]),
           0.30, unit="x", src=(d.get("mult_vs_hist") or {}).get("src", "")),
        _c("FCF 수익률", fcfy, band(fcfy, [(1, 20), (2, 40), (4, 60), (6, 80), (8, 100)]), 0.20),
    ]
    return _blend(comps)


SCORERS = {
    "capital_efficiency": score_capital_efficiency,
    "growth":             score_growth,
    "valuation":          score_valuation,
    "profitability":      score_profitability,
    "moat_proxy":         score_moat_proxy,
    "reinvestment":       score_reinvestment,
    "pricing_power":      score_pricing_power,
    "financial_health":   score_financial_health,
}


def score_all(d: dict) -> dict:
    """analytics.analysis_to_dict() 결과 → 항목별 점수 + 총점.

    총점 = Σ(점수×가중) ÷ Σ(가중, 산출 가능한 항목만).
    커버리지가 60% 미만이면 총점을 내지 않는다.
    """
    subs, excluded = {}, []
    for key, fn in SCORERS.items():
        try:
            score, comps = fn(d)
        except Exception:
            score, comps = None, []
        subs[key] = {
            "key": key, "label": LABELS[key], "weight": WEIGHTS[key],
            "score": None if score is None else round(score, 1),
            "components": comps,
        }
        if score is None:
            excluded.append(LABELS[key])

    avail = [s for s in subs.values() if s["score"] is not None]
    tw = sum(s["weight"] for s in avail)
    coverage = tw / sum(WEIGHTS.values())

    if not avail or coverage < MIN_COVERAGE:
        total = None
    else:
        total = round(sum(s["score"] * s["weight"] for s in avail) / tw, 1)

    return {
        "version":  SCORE_VERSION,
        "total":    total,
        "coverage": round(coverage * 100, 1),
        "excluded": excluded,
        "subs":     subs,
        "na_reason": None if total is not None else
                     f"데이터 커버리지 {round(coverage*100,1)}% — 총점 산출 불가",
    }
