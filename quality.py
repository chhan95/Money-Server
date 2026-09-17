"""Portfolio Business Quality Dashboard — 관통실적 뷰모델 (순수 함수, I/O 없음).

기존 analytics.analysis_to_dict()와 scoring.score_all()의 결과를 그대로 재사용하고,
그 위에 기간별 비교·자본배분·복리엔진·점수 재그룹·자동 요약을 얹는다.
analytics / scoring 은 수정하지 않는다.

모든 지표 레코드는 다음 필드를 가진다:
  v, na, period, period_type, currency, basis(Reported|Calculated|Estimate), src, as_of, formula
"""
from __future__ import annotations
import statistics

import analytics as A
import scoring as S

SOURCE = "Yahoo Finance (yfinance)"

# Investment Attractiveness = 품질과 가격을 분리해 본 뒤 다시 합친 값 (가중치는 UI에 노출)
ATTR_W_QUALITY, ATTR_W_VALUATION = 0.6, 0.4

# 기존 8개 엔진 항목 → 화면용 하위점수 (엔진 가중치를 그대로 사용해 가중평균)
GROUPS = {
    "growth":             {"label": "Growth",             "keys": ["growth"]},
    "profitability":      {"label": "Profitability",      "keys": ["profitability", "pricing_power"]},
    "capital_efficiency": {"label": "Capital Efficiency", "keys": ["capital_efficiency", "reinvestment"]},
    "moat":               {"label": "Moat",               "keys": ["moat_proxy"]},
    "financial_health":   {"label": "Financial Health",   "keys": ["financial_health"]},
    "valuation":          {"label": "Valuation",          "keys": ["valuation"]},
}
QUALITY_KEYS = [k for k in S.WEIGHTS if k != "valuation"]

RISK_KEYS = {
    "valuation": "Valuation Risk", "growth_deceleration": "Growth Deceleration",
    "competition": "Competition", "customer_concentration": "Customer Concentration",
    "regulatory": "Regulatory Risk", "cyclicality": "Cyclicality",
    "balance_sheet": "Balance Sheet Risk", "technology_disruption": "Technology Disruption",
    "capital_allocation": "Capital Allocation Risk",
}
MOAT_POINTS = {"strong": 2, "moderate": 1}


# ─────────────────────────────────────────────────────────────
#  레코드 / 기초 헬퍼
# ─────────────────────────────────────────────────────────────

def rec(v, *, period="", period_type="FY", currency="", basis="Reported",
        src=SOURCE, as_of="", na=None, formula=None) -> dict:
    return {"v": v, "na": (na or "데이터 없음") if v is None else None,
            "period": period, "period_type": period_type, "currency": currency,
            "basis": basis, "src": src, "as_of": as_of, "formula": formula}


def _n(x):
    return A._num(x)


def _fy(y):
    return f"FY{y.get('fy')}"


def _div(a, b):
    a, b = _n(a), _n(b)
    return None if a is None or b is None or b == 0 else a / b


def eps_of(y):
    """EPS: 손익계산서 Diluted EPS(Reported) 우선, 없으면 순이익÷희석주식수(Calculated)."""
    e = _n(y.get("diluted_eps"))
    if e is not None:
        return e, "Reported"
    v = _div(y.get("net"), y.get("diluted_shares"))
    return (v, "Calculated") if v is not None else (None, None)


def _augment(years):
    out = []
    for y in years:
        e, _ = eps_of(y)
        out.append({**y, "eps_value": e})
    return out


# ─────────────────────────────────────────────────────────────
#  성장 — 1Y/3Y/5Y/10Y (필요한 연수가 없으면 N/A, 구간을 늘려 주장하지 않음)
# ─────────────────────────────────────────────────────────────

def growth_window(years, field, n, currency=""):
    pts = [(y, _n(y.get(field))) for y in years if _n(y.get(field)) is not None]
    label = "1Y YoY" if n == 1 else f"{n}Y CAGR"
    if len(pts) < n + 1:
        return rec(None, period=f"{n}Y", period_type=label, currency=currency,
                   na=f"{label} 계산에 {n + 1}개 회계연도 값 필요 — 값이 있는 연도 {len(pts)}개")
    (y0, v0), (y1, v1) = pts[-(n + 1)], pts[-1]
    if n == 1:
        if not v0:
            return rec(None, period_type=label, na="직전 연도 값이 0")
        return rec((v1 - v0) / abs(v0), period=f"{_fy(y0)}→{_fy(y1)}", period_type=label,
                   currency=currency, basis="Calculated", as_of=y1.get("end", ""),
                   formula="(최근 FY − 직전 FY) ÷ |직전 FY|")
    if v0 <= 0 or v1 <= 0:
        return rec(None, period=f"{_fy(y0)}→{_fy(y1)}", period_type=label,
                   na="시작 또는 종료 값이 0 이하 — CAGR 정의 불가")
    return rec((v1 / v0) ** (1 / n) - 1, period=f"{_fy(y0)}→{_fy(y1)}", period_type=label,
               currency=currency, basis="Calculated", as_of=y1.get("end", ""),
               formula=f"(종료 ÷ 시작)^(1/{n}) − 1")


def windows(years, field, currency=""):
    return {f"{n}Y": growth_window(years, field, n, currency) for n in (1, 3, 5, 10)}


# ─────────────────────────────────────────────────────────────
#  연도별 지표 / 기간 비교 (TTM, FY, FY-1, FY-3, FY-5)
# ─────────────────────────────────────────────────────────────

def year_metrics(y, prev, currency):
    """한 회계연도의 수익성·효율 지표 레코드 묶음."""
    fy, end = _fy(y), y.get("end", "")
    eps, eps_basis = eps_of(y)
    prev_eps = eps_of(prev)[0] if prev else None

    def ratio(num, den, formula):
        v = _div(y.get(num), y.get(den))
        return rec(v, period=fy, currency="%", basis="Calculated", as_of=end, formula=formula,
                   na=None if v is not None else f"{fy} {formula} 데이터 없음")

    def yoy_rec(cur, pre, what):
        if cur is None or pre is None or pre == 0:
            return rec(None, period=fy, period_type="1Y YoY",
                       na=f"{fy} {what} 직전 연도 비교 불가")
        return rec((cur - pre) / abs(pre), period=f"FY{prev.get('fy')}→{fy}", period_type="1Y YoY",
                   basis="Calculated", as_of=end, formula="(당해 − 직전) ÷ |직전|")

    n, ic = A.nopat(y), A.invested_capital(y)
    roic = rec(n / ic if (n is not None and ic) else None, period=fy, basis="Calculated", as_of=end,
               formula="NOPAT ÷ (총부채 + 자기자본 − 현금 − 단기투자), 기말",
               na=None if (n is not None and ic) else f"{fy} 영업이익 또는 투하자본 없음")

    return {
        "fy": y.get("fy"), "end": end,
        "revenue": rec(_n(y.get("revenue")), period=fy, currency=currency, as_of=end),
        "eps": rec(eps, period=fy, currency=currency, basis=eps_basis or "Reported", as_of=end,
                   formula=None if eps_basis == "Reported" else "순이익 ÷ 희석주식수"),
        "fcf": rec(_n(y.get("fcf")), period=fy, currency=currency, as_of=end),
        "rev_growth": yoy_rec(_n(y.get("revenue")), _n(prev.get("revenue")) if prev else None, "매출"),
        "eps_growth": yoy_rec(eps, prev_eps, "EPS"),
        "fcf_growth": yoy_rec(_n(y.get("fcf")), _n(prev.get("fcf")) if prev else None, "FCF"),
        "gross_margin": ratio("gross_profit", "revenue", "매출총이익 ÷ 매출"),
        "operating_margin": ratio("operating_income", "revenue", "영업이익 ÷ 매출"),
        "net_margin": ratio("net", "revenue", "순이익 ÷ 매출"),
        "fcf_margin": ratio("fcf", "revenue", "FCF ÷ 매출"),
        "roe": ratio("net", "equity", "순이익 ÷ 자기자본"),
        "roa": ratio("net", "total_assets", "순이익 ÷ 총자산"),
        "fcf_conversion": ratio("fcf", "net", "FCF ÷ 순이익"),
        "asset_turnover": ratio("revenue", "total_assets", "매출 ÷ 총자산"),
        "roic": roic,
        "interest_coverage": _coverage(y, fy, end),
    }


def _coverage(y, fy, end):
    op, ie = _n(y.get("operating_income")), _n(y.get("interest_expense"))
    if op is None or ie is None:
        return rec(None, period=fy, na=f"{fy} 이자비용 데이터 없음")
    if ie == 0:
        return rec(None, period=fy, na=f"{fy} 이자비용 0 — 배율 정의 불가")
    return rec(op / abs(ie), period=fy, basis="Calculated", as_of=end, formula="영업이익 ÷ |이자비용|")


def ttm_metrics(payload, currency):
    t = payload.get("ttm_income") or {}
    ttm_info = payload.get("ttm") or {}
    if not t:
        na = rec(None, period="TTM", period_type="TTM", na="TTM 데이터 없음")
        return {k: na for k in ("revenue", "eps", "fcf", "gross_margin", "operating_margin",
                                "net_margin", "fcf_margin", "roe", "roic", "rev_growth", "eps_growth")}
    pe, cpe = t.get("period_end", ""), t.get("cf_period_end") or t.get("period_end", "")
    label = f"TTM ({pe})"

    def r(num, den, formula, as_of=pe):
        v = _div(t.get(num), t.get(den))
        return rec(v, period=label, period_type="TTM", basis="Calculated", as_of=as_of,
                   formula=formula, na=None if v is not None else "TTM 데이터 없음")

    return {
        "revenue": rec(_n(t.get("revenue")), period=label, period_type="TTM", currency=currency, as_of=pe),
        "eps": rec(_n(t.get("diluted_eps")), period=label, period_type="TTM", currency=currency, as_of=pe),
        "fcf": rec(_n(t.get("fcf")), period=f"TTM ({cpe})", period_type="TTM", currency=currency, as_of=cpe),
        "gross_margin": r("gross_profit", "revenue", "매출총이익 ÷ 매출"),
        "operating_margin": r("operating_income", "revenue", "영업이익 ÷ 매출"),
        "net_margin": r("net", "revenue", "순이익 ÷ 매출"),
        "fcf_margin": rec(_div(t.get("fcf"), t.get("revenue")), period=label, period_type="TTM",
                          basis="Calculated", as_of=cpe, formula="TTM FCF ÷ TTM 매출",
                          na="TTM FCF 또는 매출 없음"),
        "roe": rec(_n(ttm_info.get("roe")), period="TTM", period_type="TTM",
                   src=f"{SOURCE} returnOnEquity", as_of=payload.get("fetched_at", "")[:10]),
        # 투하자본은 연간 대차대조표 기준이라 TTM ROIC를 만들지 않는다
        "roic": rec(None, period="TTM", period_type="TTM", na="투하자본이 연간 기준이라 TTM ROIC 미산출"),
        "rev_growth": rec(None, period="TTM", period_type="TTM", na="직전 TTM 비교 데이터 없음"),
        "eps_growth": rec(None, period="TTM", period_type="TTM", na="직전 TTM 비교 데이터 없음"),
    }


def period_table(payload, years, currency):
    """TTM / FY(최근) / FY-1 / FY-3 / FY-5. 해당 연도가 없으면 전 항목 N/A."""
    out = {"TTM": ttm_metrics(payload, currency)}
    for label, k in (("FY", 0), ("FY-1", 1), ("FY-3", 3), ("FY-5", 5)):
        idx = len(years) - 1 - k
        if idx < 0:
            na = rec(None, period=label, na=f"{label} 데이터 없음 — 보유 {len(years)}개 회계연도")
            out[label] = {key: na for key in out["TTM"]}
            continue
        out[label] = year_metrics(years[idx], years[idx - 1] if idx > 0 else None, currency)
    return out


# ─────────────────────────────────────────────────────────────
#  자본배분
# ─────────────────────────────────────────────────────────────

ALLOC_USES = ["acquisitions", "buyback", "dividends", "debt_repayment"]


def capital_allocation(years, currency):
    """현금흐름표의 유출(음수)을 사용액(양수)으로 바꿔 연도별·누적으로 정리."""
    rows = []
    for y in years:
        def use(k):
            v = _n(y.get(k))
            return None if v is None else -v
        rows.append({
            "fy": y.get("fy"), "end": y.get("end"),
            "ocf": _n(y.get("ocf")), "capex": use("capex"), "fcf": _n(y.get("fcf")),
            "rnd": _n(y.get("rnd")),
            "acquisitions": use("acquisitions"), "buyback": use("buyback"),
            "dividends": use("dividends_paid"), "debt_repayment": use("debt_repayment"),
            "change_in_cash": _n(y.get("change_in_cash")),
        })
    keys = ["ocf", "capex", "fcf", "rnd", *ALLOC_USES, "change_in_cash"]
    cum = {}
    for k in keys:
        vals = [r[k] for r in rows if r[k] is not None]
        cum[k] = sum(vals) if vals else None
    fcf = cum.get("fcf")
    share = {k: (cum[k] / fcf if (cum[k] is not None and fcf and fcf > 0) else None) for k in ALLOC_USES}
    window = f"FY{rows[0]['fy']}→FY{rows[-1]['fy']}" if rows else ""
    return {
        "rows": rows, "cumulative": cum, "fcf_share": share, "window": window, "currency": currency,
        "basis": "Reported", "src": SOURCE,
        "note": ("CAPEX·R&D는 FCF를 계산하기 전에 이미 차감된 '영업 재투자'입니다. "
                 "M&A·자사주 매입·배당·부채 상환은 FCF의 사용처이며, 차입·주식 발행 등 다른 "
                 "현금흐름이 섞이므로 합계가 FCF와 일치하지 않을 수 있습니다. "
                 "FCF 대비 비율이 100%를 넘으면 해당 구간에 FCF보다 많은 금액을 차입·보유현금으로 "
                 "충당해 지출했다는 뜻입니다."),
    }


# ─────────────────────────────────────────────────────────────
#  재투자 · 복리엔진
# ─────────────────────────────────────────────────────────────

def compounding(metrics, reinvest_median):
    roic, rr = metrics["roic"]["v"], metrics["reinvestment"]["v"]
    wacc = metrics["wacc"]["v"]
    implied = roic * rr if (roic is not None and rr is not None) else None

    if wacc is not None:
        bar, bar_label = wacc, f"WACC 추정 {wacc * 100:.1f}%"
    else:
        bar, bar_label = A.DEFAULT_HURDLE, f"기준 자본비용 {A.DEFAULT_HURDLE * 100:.0f}% (가정 — WACC N/A)"

    quadrant, text = None, "ROIC 또는 재투자율 데이터가 없어 판정하지 않습니다."
    if roic is not None and rr is not None and reinvest_median is not None:
        hi_r, hi_rr = roic > bar, rr >= reinvest_median
        quadrant = {(True, True): "high_high", (True, False): "high_low",
                    (False, True): "low_high", (False, False): "low_low"}[(hi_r, hi_rr)]
        text = {
            "high_high": "ROIC가 자본비용을 웃돌고 재투자율도 보유 기업 중앙값 이상 — 높은 성장 지속 가능성 신호",
            "high_low":  "ROIC는 자본비용을 웃돌지만 재투자율은 중앙값 미만 — 성숙 기업 또는 재투자 기회 제한 가능성",
            "low_high":  "재투자율은 중앙값 이상이나 ROIC가 자본비용에 못 미침 — 재투자 효율 추가 확인 필요",
            "low_low":   "ROIC와 재투자율 모두 기준 미만 — 복리 엔진이 약한 상태일 가능성",
        }[quadrant]

    return {
        "roic": metrics["roic"], "reinvestment": metrics["reinvestment"],
        "incremental_roic": metrics["incremental_roic"],
        "implied_growth": rec(implied, period=metrics["reinvestment"].get("src", ""), basis="Calculated",
                              formula="ROIC × 재투자율 (기본 성장 산식 g = ROIC × Reinvestment Rate)",
                              na="ROIC 또는 재투자율 없음"),
        "reinvestment_formula": "재투자율 = 구간 ΔIC ÷ 구간 누적 NOPAT (번 돈 중 다시 영업자본으로 투입한 비율)",
        "bar": bar_label, "reinvest_median": reinvest_median,
        "quadrant": quadrant, "text": text,
    }


# ─────────────────────────────────────────────────────────────
#  점수 재그룹 — 엔진은 그대로, 표시만 묶는다
# ─────────────────────────────────────────────────────────────

def group_scores(score):
    subs = score["subs"]
    groups = {}
    for g, cfg in GROUPS.items():
        av = [subs[k] for k in cfg["keys"] if subs[k]["score"] is not None]
        tw = sum(s["weight"] for s in av)
        groups[g] = {
            "label": cfg["label"],
            "score": round(sum(s["score"] * s["weight"] for s in av) / tw, 1) if av else None,
            "from": [f"{subs[k]['label']} (엔진 가중 {subs[k]['weight']})" for k in cfg["keys"]],
            "excluded": [subs[k]["label"] for k in cfg["keys"] if subs[k]["score"] is None],
        }

    qs = [subs[k] for k in QUALITY_KEYS]
    av = [s for s in qs if s["score"] is not None]
    total_w = sum(s["weight"] for s in qs)
    tw = sum(s["weight"] for s in av)
    cov = tw / total_w if total_w else 0
    bq = round(sum(s["score"] * s["weight"] for s in av) / tw, 1) if (av and cov >= S.MIN_COVERAGE) else None
    val = groups["valuation"]["score"]
    attr = (round(ATTR_W_QUALITY * bq + ATTR_W_VALUATION * val, 1)
            if (bq is not None and val is not None) else None)

    return {
        "groups": groups,
        "business_quality": {
            "score": bq, "coverage": round(cov * 100, 1),
            "formula": "밸류에이션을 제외한 7개 엔진 항목의 가중평균 (결측 항목은 제외 후 재정규화)",
            "na": None if bq is not None else f"커버리지 {round(cov * 100, 1)}% — 산출 불가",
        },
        "valuation": {"score": val, "na": None if val is not None else "Valuation 데이터 없음"},
        "attractiveness": {
            "score": attr,
            "formula": f"{ATTR_W_QUALITY} × Business Quality + {ATTR_W_VALUATION} × Valuation",
            "na": None if attr is not None else
                  ("Business Quality 산출 불가" if bq is None else "Valuation 산출 불가"),
        },
        "investment_score": score["total"],
    }


# ─────────────────────────────────────────────────────────────
#  분석 노트 (사람이 쓴 정성 콘텐츠) — 작성일 없는 항목은 쓰지 않는다
# ─────────────────────────────────────────────────────────────

def usable_note(note):
    return note if (isinstance(note, dict) and note.get("written_at")) else None


def moat_from_note(note):
    note = usable_note(note)
    if not note:
        return None
    factors = [f for f in ((note.get("moat") or {}).get("factors") or [])
               if f.get("evidence") and f.get("rating") in MOAT_POINTS]
    if not factors:
        return None
    pts = sum(MOAT_POINTS[f["rating"]] for f in factors)
    return {
        "stars": min(5, (pts + 1) // 2), "points": pts, "factors": factors,
        "rule": "근거가 적힌 요인만 집계 · strong 2점 / moderate 1점 · 별 = ⌈점수 ÷ 2⌉ (최대 5)",
        "written_at": note["written_at"], "author": note.get("author", ""),
    }


# ─────────────────────────────────────────────────────────────
#  데이터 기반 리스크 신호 (노트와 별개)
# ─────────────────────────────────────────────────────────────

def data_risk_flags(metrics, groups, period, val_median):
    flags = []
    incr, wacc = metrics["incremental_roic"]["v"], metrics["wacc"]["v"]
    if incr is not None and wacc is not None and incr < wacc:
        flags.append({"key": "capital_allocation",
                      "text": f"증분 ROIC {incr * 100:.1f}%가 WACC 추정 {wacc * 100:.1f}%보다 낮음"})
    fy = period["FY"]
    g1, g3 = fy["rev_growth"]["v"], None
    if metrics.get("_rev3") and metrics["_rev3"]["v"] is not None:
        g3 = metrics["_rev3"]["v"]
    if g1 is not None and g3 is not None and g1 < g3:
        flags.append({"key": "growth_deceleration",
                      "text": f"최근 매출 성장률 {g1 * 100:.1f}%가 3Y CAGR {g3 * 100:.1f}%보다 낮음"})
    nde = metrics["net_debt_ebitda"]["v"]
    if nde is not None and nde > 0:
        flags.append({"key": "balance_sheet", "text": f"순부채/EBITDA {nde:.2f}x (순현금이 아닌 순부채 상태)"})
    val = groups["valuation"]["score"]
    if val is not None and val_median is not None and val < val_median:
        flags.append({"key": "valuation",
                      "text": f"Valuation 점수 {val}가 보유 기업 중앙값 {val_median:.1f}보다 낮음"})
    gms = [g["v"] for g in metrics.get("gross_margin_series", []) if g.get("v") is not None]
    if len(gms) >= 3 and gms[-1] < gms[0]:
        flags.append({"key": "competition",
                      "text": f"매출총이익률 {gms[0] * 100:.1f}% → {gms[-1] * 100:.1f}%로 하락"})
    return flags


# ─────────────────────────────────────────────────────────────
#  기업 단위 뷰모델
# ─────────────────────────────────────────────────────────────

def build_company(stock_row, payload, metrics, score, note, holding=None):
    years = _augment(payload.get("years") or [])
    currency = payload.get("fin_currency") or ""
    quote_cur = payload.get("quote_currency") or ""
    profile = payload.get("profile") or {}
    period = period_table(payload, years, currency)
    grouped = group_scores(score)

    rev3 = growth_window(years, "revenue", 3, currency)
    eps3 = growth_window(years, "eps_value", 3, currency)
    metrics = {**metrics, "_rev3": rev3}

    series = [year_metrics(y, years[i - 1] if i > 0 else None, currency) for i, y in enumerate(years)]
    price = _n(profile.get("price")) or _n(getattr(stock_row, "current_price", None))
    shares = _n((holding or {}).get("shares_owned"))

    return {
        "ticker": payload.get("ticker"),
        "name": profile.get("name") or getattr(stock_row, "name", "") or payload.get("ticker"),
        "sector": payload.get("sector") or "", "industry": payload.get("industry") or "",
        "country": profile.get("country"), "exchange": profile.get("exchange"),
        "currency": currency, "quote_currency": quote_cur,
        "fetched_at": payload.get("fetched_at", ""),
        "missing": payload.get("missing") or [],
        "price": rec(price, period="현재", period_type="Quote", currency=quote_cur,
                     as_of=payload.get("fetched_at", "")[:10]),
        "market_cap": {**metrics["market_cap"], "currency": quote_cur, "basis": "Reported"},
        "earnings_dates": payload.get("earnings_dates") or {},
        "summary": profile.get("summary"),
        "holding": {"shares": shares,
                    "value": (shares * price) if (shares and price) else None,
                    "currency": quote_cur},

        "period": period,
        "series": series,
        "windows": {
            "revenue": windows(years, "revenue", currency),
            "eps": windows(years, "eps_value", currency),
            "fcf": windows(years, "fcf", currency),
        },
        "rev_cagr3": rev3, "eps_cagr3": eps3,

        "roic_series": metrics["roic_series"],
        "roic": metrics["roic"], "roic_yf": metrics["roic_yf"],
        "wacc": metrics["wacc"], "roic_spread": metrics["roic_spread"],
        "incremental_roic": metrics["incremental_roic"],
        "reinvestment": metrics["reinvestment"],
        "valuation_metrics": {k: metrics[k] for k in
                              ("peg", "forward_pe", "trailing_pe", "ev_ebitda", "ps_ttm",
                               "fcf_yield", "mult_vs_hist")},
        "net_debt_ebitda": metrics["net_debt_ebitda"],
        "fcf_positive_years": metrics["fcf_positive_years"],
        "earnings_history": metrics["earnings_history"],
        "gross_margin_series": metrics["gross_margin_series"],

        "capital_allocation": capital_allocation(years, currency),
        "scores": grouped,
        "engine": score,
        "note": usable_note(note),
        "moat_note": moat_from_note(note),
        "_metrics": metrics,
    }


# ─────────────────────────────────────────────────────────────
#  포트폴리오 단위 — 순위·평균·요약 문장·8대 질문
# ─────────────────────────────────────────────────────────────

def _val(c, path):
    cur = c
    for p in path:
        if cur is None:
            return None
        cur = cur.get(p) if isinstance(cur, dict) else None
    if isinstance(cur, dict):
        return cur.get("v") if "v" in cur else cur.get("score")
    return cur


SUMMARY_METRICS = [
    ("rev_growth", "평균 매출 성장률", ("period", "FY", "rev_growth"), "pct"),
    ("eps_growth", "평균 EPS 성장률", ("period", "FY", "eps_growth"), "pct"),
    ("op_margin", "평균 영업이익률", ("period", "FY", "operating_margin"), "pct"),
    ("roe", "평균 ROE", ("period", "FY", "roe"), "pct"),
    ("roic", "평균 ROIC", ("roic",), "pct"),
    ("fcf_margin", "평균 FCF Margin", ("period", "FY", "fcf_margin"), "pct"),
    ("rev_cagr3", "평균 Revenue CAGR (3Y)", ("rev_cagr3",), "pct"),
    ("eps_cagr3", "평균 EPS CAGR (3Y)", ("eps_cagr3",), "pct"),
    ("quality", "Business Quality", ("scores", "business_quality"), "score"),
]


def _avg(companies, path, weight_fn=None):
    pairs = []
    for c in companies:
        v = _val(c, path)
        if v is None:
            continue
        w = weight_fn(c) if weight_fn else 1.0
        if w is None or w <= 0:
            continue
        pairs.append((v, w))
    if not pairs:
        return None, 0
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw, len(pairs)


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def portfolio_summary(companies):
    hold_w = lambda c: c["holding"]["value"]
    cap_w = lambda c: c["market_cap"]["v"]
    has_holding = any(c["holding"]["value"] for c in companies)
    rows = []
    for key, label, path, kind in SUMMARY_METRICS:
        simple, n = _avg(companies, path)
        weighted, nw = _avg(companies, path, hold_w) if has_holding else (None, 0)
        capw, nc = _avg(companies, path, cap_w)
        rows.append({"key": key, "label": label, "kind": kind,
                     "simple": simple, "n": n, "holding_weighted": weighted, "n_holding": nw,
                     "cap_weighted": capw, "n_cap": nc})
    return {"rows": rows, "has_holding": has_holding,
            "weight_note": ("보유금액 = 보유 수량 × 현재가 (포트폴리오 입력값 기준)"
                            if has_holding else "보유 수량이 없어 기업별 단순 평균만 제공")}


def rank_labels(companies):
    """보유 기업 내 상대 위치. 임의 임계값 대신 순위를 사용한다."""
    def rank(path):
        vals = [(c["ticker"], _val(c, path)) for c in companies]
        vals = [x for x in vals if x[1] is not None]
        vals.sort(key=lambda x: x[1], reverse=True)
        n = len(vals)
        return {t: {"rank": i + 1, "of": n} for i, (t, _) in enumerate(vals)}
    return {
        "growth": rank(("period", "FY", "rev_growth")),
        "eps": rank(("eps_cagr3",)),
        "roic": rank(("roic",)),
        "fcf": rank(("period", "FY", "fcf_margin")),
    }


def _tier(r):
    if not r:
        return None
    third = max(1, r["of"] // 3)
    if r["rank"] <= third:
        return "top"
    if r["rank"] > r["of"] - third:
        return "bottom"
    return "mid"


def one_liner(c, ranks, flags):
    """보유 기업 내 상대 순위 + 실제 수치로 문장을 만든다. 임의 임계값을 쓰지 않는다."""
    t = c["ticker"]
    g, e = _tier(ranks["growth"].get(t)), _tier(ranks["eps"].get(t))
    f = _tier(ranks["fcf"].get(t))
    if g == "top" and e == "top":
        growth = "매출과 EPS 성장이 보유 기업 중 상위권이고"
    elif g == "top":
        growth = "매출 성장이 보유 기업 중 상위권이고"
    elif g == "bottom":
        growth = "매출 성장은 보유 기업 중 낮은 편이지만"
    else:
        growth = "성장 속도는 보유 기업 중 중간 수준이며"

    roic, sp = c["roic"]["v"], c["roic_spread"]["v"]
    if roic is not None and sp is not None:
        roic_c = (f"ROIC는 {roic * 100:.1f}%로 추정 자본비용보다 {abs(sp) * 100:.1f}pp "
                  f"{'높습니다' if sp > 0 else '낮습니다'}.")
    elif roic is not None:
        roic_c = f"ROIC는 {roic * 100:.1f}%입니다(WACC 비교 불가)."
    else:
        roic_c = "ROIC는 산출되지 않았습니다."
    fcf_c = {"top": " FCF Margin도 보유 기업 중 상위권입니다.",
             "bottom": " FCF Margin은 보유 기업 중 낮은 편입니다."}.get(f, "")
    data_part = f"{growth} {roic_c}{fcf_c}"

    note = c.get("note")
    note_part = None
    if note and note.get("key_risks"):
        note_part = "장기 성장률의 주요 변수: " + ", ".join(note["key_risks"][:3]) + "."
    elif flags:
        note_part = "추가 확인 신호: " + "; ".join(fl["text"] for fl in flags[:2]) + "."
    return {"data_part": data_part, "note_part": note_part,
            "note_source": ("분석 노트 · " + note["written_at"]) if (note and note.get("key_risks")) else
                           ("데이터 신호" if flags else None)}


def quality_price_label(c, bq_med, val_med):
    bq, val = c["scores"]["business_quality"]["score"], c["scores"]["valuation"]["score"]
    if bq is None or val is None or bq_med is None or val_med is None:
        return None
    return {(True, True): "좋은 기업 · 가격 매력",
            (True, False): "좋은 기업 · 가격 부담",
            (False, True): "상대적 저평가 · 품질 확인 필요",
            (False, False): "품질·가격 모두 상대 열위"}[(bq >= bq_med, val >= val_med)]


def eight_questions(companies):
    def best(path, label, fmt="pct", filt=None):
        pool = [c for c in companies if _val(c, path) is not None and (filt is None or filt(c))]
        if not pool:
            return {"q": label, "ticker": None, "value": None, "na": "비교 가능한 데이터 없음"}
        c = max(pool, key=lambda x: _val(x, path))
        return {"q": label, "ticker": c["ticker"], "value": _val(c, path), "fmt": fmt}

    # Q4: 자본비용을 넘는 ROIC와 중앙값 이상의 성장을 '동시에' 갖춘 기업 중 두 순위 합이 가장 좋은 기업
    growth_of = lambda c: _val(c, ("period", "FY", "rev_growth"))
    bar_of = lambda c: c["wacc"]["v"] if c["wacc"]["v"] is not None else A.DEFAULT_HURDLE
    g_med = _median([growth_of(c) for c in companies])
    qual = [c for c in companies
            if c["roic"]["v"] is not None and growth_of(c) is not None and g_med is not None
            and c["roic"]["v"] > bar_of(c) and growth_of(c) >= g_med]
    q4_label = "높은 ROIC를 유지하면서 성장하는 기업"
    if qual:
        rk_r = {x["ticker"]: i for i, x in enumerate(sorted(qual, key=lambda x: -x["roic"]["v"]))}
        rk_g = {x["ticker"]: i for i, x in enumerate(sorted(qual, key=lambda x: -growth_of(x)))}
        pick = min(qual, key=lambda x: (rk_r[x["ticker"]] + rk_g[x["ticker"]], -x["roic"]["v"]))
        q4 = {"q": q4_label, "ticker": pick["ticker"], "value": pick["roic"]["v"], "fmt": "pct"}
    else:
        q4 = {"q": q4_label, "ticker": None, "value": None, "na": "조건을 만족하는 기업 없음"}
    q4["basis"] = ("ROIC > 자본비용(WACC 추정, 없으면 10% 가정) 이면서 매출 성장률 ≥ 보유 기업 중앙값인 "
                   "기업 중 ROIC·성장률 순위 합이 가장 좋은 기업 (표시값: ROIC)")

    q7 = best(("scores", "business_quality"), "좋은 기업인데 현재 가격이 비싼 기업", fmt="score",
              filt=lambda c: c.get("qp_label") == "좋은 기업 · 가격 부담")
    q7["basis"] = "Business Quality ≥ 중앙값이면서 Valuation < 중앙값인 기업 중 품질 최고"

    moat = best(("scores", "groups", "moat"), "경제적 해자가 가장 강한 기업 (정량 프록시)", fmt="score")
    moat["basis"] = "Moat 정량 프록시 점수 기준 — 분석 노트의 별점은 상세 페이지에서 확인"

    return [
        {**best(("period", "FY", "operating_margin"), "가장 돈을 잘 버는 기업"), "basis": "최근 FY 영업이익률"},
        {**best(("roic",), "자본을 가장 효율적으로 쓰는 기업"), "basis": "ROIC (현금·단기투자 차감 투하자본 기준)"},
        {**best(("period", "FY", "rev_growth"), "가장 빠르게 성장하는 기업"), "basis": "최근 FY 매출 성장률"},
        q4,
        {**best(("period", "FY", "fcf_margin"), "현금흐름이 가장 좋은 기업"), "basis": "최근 FY FCF Margin"},
        moat,
        q7,
        {**best(("scores", "attractiveness"), "가격까지 고려했을 때 가장 매력적인 기업", fmt="score"),
         "basis": f"Investment Attractiveness = {ATTR_W_QUALITY}×품질 + {ATTR_W_VALUATION}×가격"},
    ]


def headline(companies, summary):
    if not companies:
        return "보유 기업의 분석 데이터가 없습니다."
    cmp_ = [c for c in companies if c["roic"]["v"] is not None and c["wacc"]["v"] is not None]
    above = sum(1 for c in cmp_ if c["roic"]["v"] > c["wacc"]["v"])
    rows = {r["key"]: r for r in summary["rows"]}
    parts = []
    if cmp_:
        ratio = above / len(cmp_)
        tone = ("높은 자본효율성을 가진 기업 중심으로" if ratio >= 0.8 else
                "자본효율성이 엇갈리는 기업들로" if ratio >= 0.5 else
                "자본비용 대비 수익성이 약한 기업 비중이 큰 상태로")
        parts.append(f"현재 포트폴리오는 {tone} 구성되어 있습니다 "
                     f"(WACC 비교 가능 {len(cmp_)}개 중 {above}개가 ROIC > WACC).")
    g, om = rows["rev_growth"]["simple"], rows["op_margin"]["simple"]
    if g is not None and om is not None:
        parts.append(f"기업별 단순 평균 매출 성장률 {g * 100:.1f}%, 영업이익률 {om * 100:.1f}%.")
    pricey = [c["ticker"] for c in companies if c.get("qp_label") == "좋은 기업 · 가격 부담"]
    if pricey:
        parts.append(f"사업 품질은 상대적으로 높지만 가격 부담이 있는 기업: {', '.join(pricey)}.")
    return " ".join(parts)


def build_portfolio(companies):
    """기업 뷰모델 목록 → 포트폴리오 공통 요소를 채워 넣는다."""
    rr_med = _median([c["reinvestment"]["v"] for c in companies])
    bq_med = _median([c["scores"]["business_quality"]["score"] for c in companies])
    val_med = _median([c["scores"]["valuation"]["score"] for c in companies])

    for c in companies:
        c["compounding"] = compounding(c["_metrics"], rr_med)
        c["qp_label"] = quality_price_label(c, bq_med, val_med)
        c["risk_flags"] = data_risk_flags(c["_metrics"], c["scores"]["groups"], c["period"], val_med)

    ranks = rank_labels(companies)
    for c in companies:
        c["one_liner"] = one_liner(c, ranks, c["risk_flags"])

    summary = portfolio_summary(companies)
    qs = [c["scores"]["business_quality"]["score"] for c in companies]
    qs = [q for q in qs if q is not None]

    for c in companies:
        c.pop("_metrics", None)

    dates = sorted({(c.get("fetched_at") or "")[:10] for c in companies if c.get("fetched_at")})
    return {
        "companies": companies,
        "summary": summary,
        "headline": headline(companies, summary),
        "questions": eight_questions(companies),
        "portfolio_quality": round(sum(qs) / len(qs), 1) if qs else None,
        "medians": {"business_quality": bq_med, "valuation": val_med, "reinvestment": rr_med},
        "as_of": (dates[0] if len(dates) == 1 else f"{dates[0]} ~ {dates[-1]}") if dates else None,
        "mixed_dates": len(dates) > 1,
        "attr_weights": {"quality": ATTR_W_QUALITY, "valuation": ATTR_W_VALUATION},
    }
