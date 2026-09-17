"""리서치 터미널 파생 계산 — 순수 함수만. I/O·DB·네트워크 없음.

fetcher.fetch_analysis()가 만든 원본 payload를 받아 ROIC·성장률·마진·밸류에이션을
계산한다. 값이 없으면 절대 추정하지 않고 사유(na)를 담아 반환한다.

모든 지표는 {"v": 값|None, "src": 출처, "as_of": 기준일, "na": 사유|None} 형태.
"""
from __future__ import annotations
import statistics

# 자본비용 가정 (app_settings로 덮어쓸 수 있음 — main.py에서 주입)
DEFAULT_ERP           = 0.050   # 시장위험프리미엄 (가정)
DEFAULT_CREDIT_SPREAD = 0.015   # 신용스프레드 (가정)
DEFAULT_HURDLE        = 0.10    # 보수적 기준 자본비용

TAX_MIN, TAX_MAX = 0.05, 0.35   # 일회성 항목으로 세율이 튀는 것을 방지


def M(v, src="", as_of="", na=None) -> dict:
    """지표 하나. v가 None이면 na 사유를 반드시 동반한다."""
    return {"v": v, "src": src, "as_of": as_of, "na": na if v is None else None}


def NA(reason: str) -> dict:
    return {"v": None, "src": "", "as_of": "", "na": reason}


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _safe_div(a, b):
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


# ─────────────────────────────────────────────────────────────
#  세율 / NOPAT / 투하자본
# ─────────────────────────────────────────────────────────────

def effective_tax_rate(year: dict) -> tuple[float, str]:
    """실효세율과 그 출처. 없으면 21% 가정(출처에 '가정' 명시)."""
    tr = _num(year.get("tax_rate_calc"))
    if tr is not None and 0 <= tr <= 1:
        return min(max(tr, TAX_MIN), TAX_MAX), "yfinance Tax Rate For Calcs"
    pretax, prov = _num(year.get("pretax")), _num(year.get("tax_provision"))
    if pretax and pretax > 0 and prov is not None:
        return min(max(prov / pretax, TAX_MIN), TAX_MAX), "Tax Provision ÷ Pretax Income"
    return 0.21, "21% (가정 — 세율 데이터 없음)"


def nopat(year: dict) -> float | None:
    ebit = _num(year.get("operating_income"))
    if ebit is None:
        return None
    tr, _ = effective_tax_rate(year)
    return ebit * (1 - tr)


def invested_capital(year: dict) -> float | None:
    """IC = 총부채 + 자기자본 − 현금 − 단기투자.

    현금·단기투자를 초과현금으로 보고 차감한다(사용자 결정). 영업에 실제로
    투입된 자본만 분모에 남긴다. yfinance의 Invested Capital 행은 현금을
    차감하지 않아 현금이 많은 기업의 ROIC를 과소평가한다.
    """
    debt, eq = _num(year.get("total_debt")), _num(year.get("equity"))
    if eq is None:
        return None
    cash = _num(year.get("cash")) or 0.0
    sti  = _num(year.get("sti")) or 0.0
    ic = (debt or 0.0) + eq - cash - sti
    return ic if ic > 0 else None


def roic_series(years: list[dict]) -> list[dict]:
    """연도별 ROIC = NOPAT ÷ 기말 투하자본.

    기초·기말 평균을 쓰는 교과서적 방식도 있으나 기말 기준을 택했다.
    투하자본이 급증하는 기업(예: NVDA)에서 평균 기준은 분모를 낮춰 ROIC를
    과대하게 보이게 하고(145% vs 105%), 첫 해만 기말 기준이 되어 추이 그래프의
    기준이 섞인다. 기말 기준은 모든 연도에 동일하게 적용되고 더 보수적이다.
    """
    out = []
    for y in years:
        n, ic = nopat(y), invested_capital(y)
        if n is None or ic is None:
            out.append({"fy": y.get("fy"), "roic": None, "nopat": n, "ic": ic,
                        "basis": None, "na": "영업이익 또는 투하자본 없음"})
            continue
        out.append({"fy": y.get("fy"), "roic": n / ic, "nopat": n, "ic": ic,
                    "basis": "기말", "na": None})
    return out


def roic_yf_latest(years: list[dict]) -> dict:
    """대조용 — yfinance Invested Capital 행 기준 ROIC (현금 미차감)."""
    if not years:
        return NA("데이터 없음")
    y = years[-1]
    n, ic = nopat(y), _num(y.get("invested_capital_yf"))
    if n is None or not ic or ic <= 0:
        return NA("yfinance Invested Capital 없음")
    return M(n / ic, "yfinance Invested Capital (현금 미차감)", str(y.get("end", "")))


def incremental_roic(years: list[dict]) -> dict:
    """구간 증분 ROIC = ΔNOPAT / ΔIC (양 끝 기말 기준).

    '추가로 투입한 자본 1달러가 만들어낸 추가 영업이익'. 분모가 줄거나
    미미하면 숫자를 만들지 않고 사유를 반환한다.
    """
    usable = [y for y in years if nopat(y) is not None and invested_capital(y) is not None]
    if len(usable) < 3:
        return NA("데이터 부족 (연속 3개 회계연도 필요)")
    first, last = usable[0], usable[-1]
    n0, n1 = nopat(first), nopat(last)
    ic0, ic1 = invested_capital(first), invested_capital(last)
    d_nopat, d_ic = n1 - n0, ic1 - ic0

    if d_ic <= 0:
        return NA("투하자본 감소 — 증분 ROIC 정의 불가")
    if abs(d_ic) < 0.05 * ic0:
        return NA("투하자본 변화 미미 (5% 미만)")

    r = M(d_nopat / d_ic,
          f"FY{first.get('fy')}→FY{last.get('fy')} ΔNOPAT ÷ ΔIC",
          str(last.get("end", "")))
    r["detail"] = {"fy_from": first.get("fy"), "fy_to": last.get("fy"),
                   "nopat_from": n0, "nopat_to": n1, "ic_from": ic0, "ic_to": ic1,
                   "d_nopat": d_nopat, "d_ic": d_ic}
    return r


def reinvestment_rate(years: list[dict]) -> dict:
    """재투자율 = 구간 ΔIC ÷ 구간 누적 NOPAT.

    '번 돈의 몇 %를 다시 자본으로 투입했는가'. 기초 NOPAT을 분모로 쓰면
    기초 연도가 작은 고성장 기업에서 수백~수천 %가 나와 의미를 잃는다.
    """
    usable = [y for y in years if nopat(y) is not None and invested_capital(y) is not None]
    if len(usable) < 3:
        return NA("데이터 부족")
    # 자본 증가는 기초 이후에 발생하므로 누적 NOPAT도 기초 연도를 제외하고 더한다
    cum_nopat = sum(nopat(y) for y in usable[1:])
    d_ic = invested_capital(usable[-1]) - invested_capital(usable[0])
    if cum_nopat <= 0:
        return NA("누적 NOPAT 없음/음수")
    return M(d_ic / cum_nopat,
             f"FY{usable[0].get('fy')}→FY{usable[-1].get('fy')} ΔIC ÷ 누적 NOPAT",
             str(usable[-1].get("end", "")))


# ─────────────────────────────────────────────────────────────
#  성장률 / 마진
# ─────────────────────────────────────────────────────────────

def cagr(years: list[dict], field: str) -> dict:
    """가능한 구간으로 CAGR 계산. 실제 적용 구간을 라벨에 남긴다.

    종목마다 손익 연도 수가 달라(4~5년) '5Y'를 일괄 주장하면 사실과 달라진다.
    """
    pts = [(y.get("fy"), _num(y.get(field))) for y in years]
    pts = [(fy, v) for fy, v in pts if v is not None and v > 0]
    if len(pts) < 2:
        return NA("데이터 부족")
    (fy0, v0), (fy1, v1) = pts[0], pts[-1]
    n = len(pts) - 1
    try:
        g = (v1 / v0) ** (1 / n) - 1
    except (ZeroDivisionError, ValueError):
        return NA("계산 불가")
    r = M(g, f"{n}Y CAGR (FY{fy0}→FY{fy1})", f"FY{fy1}")
    r["window"] = n
    return r


def yoy(years: list[dict], field: str) -> dict:
    pts = [_num(y.get(field)) for y in years]
    pts = [p for p in pts if p is not None]
    if len(pts) < 2 or not pts[-2]:
        return NA("데이터 부족")
    return M((pts[-1] - pts[-2]) / abs(pts[-2]), "최근 회계연도 YoY",
             f"FY{years[-1].get('fy')}")


def margin_series(years: list[dict], numer: str) -> list[dict]:
    out = []
    for y in years:
        out.append({"fy": y.get("fy"), "v": _safe_div(y.get(numer), y.get("revenue"))})
    return out


def latest_margin(years: list[dict], numer: str, label: str) -> dict:
    if not years:
        return NA("데이터 없음")
    y = years[-1]
    v = _safe_div(y.get(numer), y.get("revenue"))
    if v is None:
        return NA(f"{label} 데이터 없음")
    return M(v, f"FY{y.get('fy')} 손익계산서", str(y.get("end", "")))


# ─────────────────────────────────────────────────────────────
#  밸류에이션
# ─────────────────────────────────────────────────────────────

def multiple_history(payload: dict) -> dict:
    """월별 종가 ÷ 당시 최근 발표 연간 EPS(및 SPS)로 멀티플 이력을 근사한다.

    근사치임이 중요하다: 분기 실적 반영 지연 때문에 고성장 기업은 실제 당시
    P/E보다 높게 나온다. 현재값도 동일한 방식으로 계산해 사과 대 사과 비교를
    보장한다.
    """
    years = payload.get("years") or []
    prices = payload.get("prices_monthly") or []
    if not years or not prices:
        return {"pe": [], "ps": [], "prefer": None, "na": "데이터 부족"}

    anchors = []
    for y in years:
        end, sh = y.get("end"), _num(y.get("diluted_shares"))
        net, rev = _num(y.get("net")), _num(y.get("revenue"))
        if not end or not sh or sh <= 0:
            continue
        anchors.append({"end": end[:7], "eps": (net / sh) if net else None,
                        "sps": (rev / sh) if rev else None})
    if not anchors:
        return {"pe": [], "ps": [], "prefer": None, "na": "주당 지표 산출 불가"}

    pe, ps = [], []
    for p in prices:
        d, c = p.get("d"), _num(p.get("c"))
        if not d or not c:
            continue
        avail = [a for a in anchors if a["end"] <= d]
        if not avail:
            continue
        a = avail[-1]
        if a["eps"] and a["eps"] > 0:
            pe.append({"d": d, "v": c / a["eps"]})
        if a["sps"] and a["sps"] > 0:
            ps.append({"d": d, "v": c / a["sps"]})

    eps_vals = [a["eps"] for a in anchors if a["eps"] is not None]
    prefer = "pe" if (eps_vals and all(e > 0 for e in eps_vals)
                      and min(eps_vals) >= 0.25 * max(eps_vals)) else "ps"
    return {"pe": pe, "ps": ps, "prefer": prefer, "na": None}


def multiple_vs_history(payload: dict) -> dict:
    """현재 멀티플이 자기 과거 평균 대비 어디인지. 양쪽 모두 동일 방식으로 계산."""
    h = multiple_history(payload)
    if h.get("na"):
        return NA(h["na"])
    series = h["pe"] if h["prefer"] == "pe" else h["ps"]
    if len(series) < 12:
        return NA("이력 부족 (12개월 미만)")
    vals = [s["v"] for s in series]
    cur, avg = vals[-1], statistics.mean(vals)
    if not avg:
        return NA("평균 산출 불가")
    label = "P/E" if h["prefer"] == "pe" else "P/S"
    r = M(cur / avg, f"{label} 현재 ÷ 5년 평균 (근사 — 월말 종가 ÷ 직전 FY 주당지표)",
          series[-1]["d"])
    r["detail"] = {"metric": label, "current": cur, "avg": avg,
                   "pct_rank": sum(1 for v in vals if v <= cur) / len(vals)}
    return r


def fcf_yield(payload: dict) -> dict:
    ttm = payload.get("ttm") or {}
    fcf, mc = _num(ttm.get("fcf")), _num(ttm.get("market_cap"))
    if fcf is None or not mc:
        return NA("FCF 또는 시가총액 없음")
    if payload.get("fin_currency") != payload.get("quote_currency"):
        return NA("재무제표 통화와 시가총액 통화 불일치")
    return M(fcf / mc, "yfinance freeCashflow ÷ marketCap", payload.get("fetched_at", ""))


def peg(payload: dict, forecasts: list | None = None) -> dict:
    """PEG. yfinance 값 우선, 없으면 애널리스트 예상 EPS 성장률로 계산."""
    ttm = payload.get("ttm") or {}
    v = _num(ttm.get("trailing_peg"))
    if v is not None and v > 0:
        return M(v, "yfinance trailingPegRatio", payload.get("fetched_at", ""))
    fpe = _num(ttm.get("forward_pe"))
    g = None
    if forecasts and len(forecasts) >= 2:
        e0, e1 = _num(forecasts[0].get("eps")), _num(forecasts[1].get("eps"))
        if e0 and e1 and e0 > 0:
            g = (e1 / e0 - 1) * 100
    if fpe and g and g > 3:
        return M(fpe / g, "Forward P/E ÷ 애널리스트 예상 EPS 성장률", payload.get("fetched_at", ""))
    return NA("성장률 3% 이하 또는 예상치 없음 — PEG 의미 없음")


# ─────────────────────────────────────────────────────────────
#  자본비용
# ─────────────────────────────────────────────────────────────

def wacc(payload: dict, erp=DEFAULT_ERP, credit_spread=DEFAULT_CREDIT_SPREAD) -> dict:
    """CAPM 기반 WACC 추정치. 가정이 섞이므로 반드시 '추정'으로 표기해 쓴다."""
    ttm = payload.get("ttm") or {}
    years = payload.get("years") or []

    if payload.get("fin_currency") != payload.get("quote_currency"):
        return NA("재무제표 통화와 시가총액 통화 불일치 — WACC 산출 불가")

    beta = _num(ttm.get("beta"))
    if beta is None or beta <= 0 or beta > 3:
        return NA("베타 없음 또는 비정상")
    rf_obj = payload.get("rf") or {}
    rf = _num(rf_obj.get("value"))
    if rf is None:
        return NA("무위험수익률 없음")

    e = _num(ttm.get("market_cap"))
    d = _num(ttm.get("total_debt")) or 0.0
    if not e or e <= 0:
        return NA("시가총액 없음")

    tax = effective_tax_rate(years[-1])[0] if years else 0.21
    ke = rf + beta * erp
    kd = rf + credit_spread
    w = e + d
    val = (e / w) * ke + (d / w) * kd * (1 - tax)

    r = M(val, "CAPM 추정 (가정 포함)", rf_obj.get("as_of", ""))
    r["detail"] = {"rf": rf, "rf_src": rf_obj.get("src", ""), "beta": beta,
                   "erp": erp, "kd": kd, "credit_spread": credit_spread,
                   "tax": tax, "equity": e, "debt": d, "ke": ke}
    return r


# ─────────────────────────────────────────────────────────────
#  뷰모델
# ─────────────────────────────────────────────────────────────

def analysis_to_dict(payload: dict, forecasts: list | None = None,
                     erp=DEFAULT_ERP, credit_spread=DEFAULT_CREDIT_SPREAD) -> dict:
    """원본 payload → 화면/스코어링이 쓰는 파생 지표 묶음."""
    years = payload.get("years") or []
    ttm = payload.get("ttm") or {}
    as_of = payload.get("fetched_at", "")
    rs = roic_series(years)
    roic_vals = [r["roic"] for r in rs if r["roic"] is not None]

    latest_roic = NA("데이터 없음")
    if rs and rs[-1]["roic"] is not None:
        last = rs[-1]
        latest_roic = M(last["roic"],
                        f"NOPAT ÷ 투하자본({last['basis']}) — 현금·단기투자 차감",
                        f"FY{last['fy']}")

    w = wacc(payload, erp, credit_spread)
    spread = NA("ROIC 또는 WACC 없음")
    if latest_roic["v"] is not None and w["v"] is not None:
        spread = M(latest_roic["v"] - w["v"], "ROIC − WACC(추정)", latest_roic["as_of"])

    gm = margin_series(years, "gross_profit")
    gm_vals = [g["v"] for g in gm if g["v"] is not None]

    return {
        "ticker":   payload.get("ticker"),
        "industry": payload.get("industry") or payload.get("sector") or "",
        "sector":   payload.get("sector") or "",
        "currency": payload.get("fin_currency"),
        "as_of":    as_of,
        "missing":  payload.get("missing") or [],

        "roic":            latest_roic,
        "roic_series":     rs,
        "roic_yf":         roic_yf_latest(years),
        "roic_median":     M(statistics.median(roic_vals), "연도별 ROIC 중앙값", as_of)
                           if roic_vals else NA("데이터 없음"),
        "incremental_roic": incremental_roic(years),
        "reinvestment":     reinvestment_rate(years),
        "wacc":             w,
        "roic_spread":      spread,

        "rev_cagr":   cagr(years, "revenue"),
        "rev_yoy":    yoy(years, "revenue"),
        "net_cagr":   cagr(years, "net"),
        "fcf_cagr":   cagr(years, "fcf"),

        "gross_margin":     latest_margin(years, "gross_profit", "매출총이익"),
        "operating_margin": latest_margin(years, "operating_income", "영업이익"),
        "net_margin":       latest_margin(years, "net", "순이익"),
        "fcf_margin":       latest_margin(years, "fcf", "FCF"),
        "gross_margin_series": gm,
        "gross_margin_std": M(statistics.pstdev(gm_vals), "매출총이익률 표준편차(5년)", as_of)
                            if len(gm_vals) >= 3 else NA("데이터 부족"),

        "peg":            peg(payload, forecasts),
        "forward_pe":     M(_num(ttm.get("forward_pe")), "yfinance forwardPE", as_of)
                          if ttm.get("forward_pe") else NA("Forward P/E 없음"),
        "trailing_pe":    M(_num(ttm.get("trailing_pe")), "yfinance trailingPE", as_of)
                          if ttm.get("trailing_pe") else NA("P/E 없음"),
        "ev_ebitda":      M(_num(ttm.get("ev_ebitda")), "yfinance enterpriseToEbitda", as_of)
                          if ttm.get("ev_ebitda") else NA("EV/EBITDA 없음"),
        "ps_ttm":         M(_num(ttm.get("ps_ttm")), "yfinance priceToSalesTrailing12Months", as_of)
                          if ttm.get("ps_ttm") else NA("P/S 없음"),
        "fcf_yield":      fcf_yield(payload),
        "mult_vs_hist":   multiple_vs_history(payload),

        "roe":            M(_num(ttm.get("roe")), "yfinance returnOnEquity", as_of)
                          if ttm.get("roe") is not None else NA("ROE 없음"),
        "market_cap":     M(_num(ttm.get("market_cap")), "yfinance marketCap", as_of)
                          if ttm.get("market_cap") else NA("시가총액 없음"),
        "net_debt_ebitda": _net_debt_ebitda(payload),
        "fcf_positive_years": _fcf_positive_years(years),
        "earnings_history": payload.get("earnings_history") or [],
        "years": years,
    }


def _net_debt_ebitda(payload: dict) -> dict:
    ttm = payload.get("ttm") or {}
    debt, cash, ebitda = _num(ttm.get("total_debt")), _num(ttm.get("total_cash")), _num(ttm.get("ebitda"))
    if debt is None or cash is None:
        return NA("부채/현금 데이터 없음")
    if not ebitda or ebitda <= 0:
        return NA("EBITDA 없음 또는 음수")
    return M((debt - cash) / ebitda, "(총부채 − 현금) ÷ EBITDA", payload.get("fetched_at", ""))


def _fcf_positive_years(years: list[dict]) -> dict:
    vals = [_num(y.get("fcf")) for y in years]
    vals = [v for v in vals if v is not None]
    if not vals:
        return NA("FCF 데이터 없음")
    r = M(sum(1 for v in vals if v > 0), f"FCF 흑자 연도 수 / {len(vals)}년", "")
    r["total"] = len(vals)
    return r
