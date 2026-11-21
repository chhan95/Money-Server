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


def _fy_label(y: dict) -> str:
    return f"TTM({str(y.get('end', ''))[:7]})" if y.get("fy") == "TTM" else f"FY{y.get('fy')}"


def ttm_point(payload: dict) -> dict | None:
    """최근 4개 분기(TTM)를 연간 행과 같은 모양으로 만든다. 최근 FY보다 새롭지 않으면 None.

    손익·현금흐름은 TTM 합계, 투하자본은 가장 최근 대차대조표(분기말이 FY말보다 새로우면
    분기말, 아니면 FY말). Yahoo의 분기 대차대조표가 손익보다 한 분기 늦는 경우가 있어
    실제 사용한 대차대조표 날짜를 bs_end에 남긴다.
    """
    years = payload.get("years") or []
    ti = payload.get("ttm_income") or {}
    if not years or not ti.get("period_end"):
        return None
    last = years[-1]
    end = ti["period_end"]
    if end <= str(last.get("end", "")):
        return None                      # 회계연도가 막 끝남 — FY와 같은 기간
    if _num(ti.get("revenue")) is None or _num(ti.get("operating_income")) is None:
        return None
    qb = payload.get("ttm_bs") or {}
    fy_bs_end = str(last.get("bs_col") or last.get("end") or "")
    if qb.get("period_end") and qb["period_end"] > fy_bs_end and _num(qb.get("equity")) is not None:
        bs, bs_end = qb, qb["period_end"]
    else:
        bs, bs_end = last, fy_bs_end
    capex = _num(ti.get("capex"))
    return {
        "fy": "TTM", "end": end, "bs_end": bs_end[:10],
        "revenue": ti.get("revenue"), "gross_profit": ti.get("gross_profit"),
        "operating_income": ti.get("operating_income"), "net": ti.get("net"),
        "pretax": ti.get("pretax"), "tax_provision": ti.get("tax_provision"),
        "tax_rate_calc": ti.get("tax_rate_calc"),
        "fcf": ti.get("fcf"), "ocf": ti.get("ocf"), "capex": capex,
        "total_debt": bs.get("total_debt"), "equity": bs.get("equity"),
        "cash": bs.get("cash"), "sti": bs.get("sti"),
    }


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
          f"{_fy_label(first)}→{_fy_label(last)} ΔNOPAT ÷ ΔIC",
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
    pts = [(y, _num(y.get(field))) for y in years]
    pts = [(y, v) for y, v in pts if v is not None and v > 0]
    if len(pts) < 2:
        return NA("데이터 부족")
    (y0, v0), (y1, v1) = pts[0], pts[-1]
    # 끝점이 TTM이면 정수 연수가 아니므로 두 기간 말일 사이의 실제 경과 연수로 연율화한다
    n = _years_between(y0.get("end"), y1.get("end")) if y1.get("fy") == "TTM" else len(pts) - 1
    if not n or n <= 0:
        return NA("계산 불가")
    try:
        g = (v1 / v0) ** (1 / n) - 1
    except (ZeroDivisionError, ValueError):
        return NA("계산 불가")
    n_txt = f"{n:.2f}".rstrip("0").rstrip(".") if y1.get("fy") == "TTM" else str(n)
    r = M(g, f"{n_txt}Y CAGR ({_fy_label(y0)}→{_fy_label(y1)})", _fy_label(y1))
    r["window"] = n
    return r


def _years_between(a, b) -> float | None:
    from datetime import date
    try:
        d0, d1 = date.fromisoformat(str(a)[:10]), date.fromisoformat(str(b)[:10])
    except ValueError:
        return None
    return (d1 - d0).days / 365.25


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
    return M(v, f"{_fy_label(y)} 손익계산서" + (" (최근 4개 분기 합계)" if y.get("fy") == "TTM" else ""),
             str(y.get("end", "")))


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


def _fx(payload: dict) -> float | None:
    """재무제표 금액 → 시세 통화 환산 배율. 같은 통화면 1, 다른데 환율이 없으면 None."""
    if payload.get("fin_currency") == payload.get("quote_currency"):
        return 1.0
    return _num((payload.get("fx") or {}).get("rate"))


def _fx_note(payload: dict) -> str:
    fx = payload.get("fx") or {}
    if payload.get("fin_currency") == payload.get("quote_currency") or not fx:
        return ""
    return f" — 재무 금액을 {fx.get('pair', '')} {fx.get('rate'):.4g}로 환산"


FX_NA = "재무제표 통화와 시가총액 통화 불일치 (환율 없음 — 재수집 필요)"


def ev_ebitda(payload: dict) -> dict:
    """EV/EBITDA. 통화가 같으면 yfinance 값, 다르면 환율로 맞춰 직접 계산한다
    (yfinance 값은 시세 통화 EV ÷ 재무 통화 EBITDA라 틀림)."""
    ttm = payload.get("ttm") or {}
    as_of = payload.get("fetched_at", "")
    fx = _fx(payload)
    if fx is None:
        return NA(FX_NA)
    if fx == 1.0:
        v = _num(ttm.get("ev_ebitda"))
        return M(v, "yfinance enterpriseToEbitda", as_of) if v else NA("EV/EBITDA 없음")
    mc, ebitda = _num(ttm.get("market_cap")), _num(ttm.get("ebitda"))
    debt, cash = _num(ttm.get("total_debt")) or 0.0, _num(ttm.get("total_cash")) or 0.0
    if not mc or not ebitda or ebitda <= 0:
        return NA("시가총액 또는 EBITDA 없음")
    return M((mc + (debt - cash) * fx) / (ebitda * fx),
             "(시가총액 + 순부채) ÷ EBITDA" + _fx_note(payload), as_of)


def ps_ttm(payload: dict) -> dict:
    ttm = payload.get("ttm") or {}
    as_of = payload.get("fetched_at", "")
    fx = _fx(payload)
    if fx is None:
        return NA(FX_NA)
    if fx == 1.0:
        v = _num(ttm.get("ps_ttm"))
        return M(v, "yfinance priceToSalesTrailing12Months", as_of) if v else NA("P/S 없음")
    mc, rev = _num(ttm.get("market_cap")), _num((payload.get("ttm_income") or {}).get("revenue"))
    if not mc or not rev or rev <= 0:
        return NA("시가총액 또는 TTM 매출 없음")
    return M(mc / (rev * fx), "시가총액 ÷ TTM 매출" + _fx_note(payload), as_of)


def fcf_yield(payload: dict) -> dict:
    ttm = payload.get("ttm") or {}
    fcf, mc = _num(ttm.get("fcf")), _num(ttm.get("market_cap"))
    if fcf is None or not mc:
        return NA("FCF 또는 시가총액 없음")
    fx = _fx(payload)
    if fx is None:
        return NA(FX_NA)
    return M(fcf * fx / mc, "yfinance freeCashflow ÷ marketCap" + _fx_note(payload),
             payload.get("fetched_at", ""))


FWD_PE_TURNAROUND_MAX = 100   # 적자 기업의 Forward P/E가 이보다 크면 흑자 전환 기저 효과로 본다


def forward_pe(payload: dict) -> dict:
    """Forward P/E. 아직 적자(TTM EPS ≤ 0)인데 예상 EPS가 0에 가까워 P/E가 폭증하면 의미 없는 값이므로 N/A.
    (예: 적자에서 막 흑자 전환이 예상되는 상장 초기 기업 — 주가 ÷ 거의 0인 EPS)"""
    ttm = payload.get("ttm") or {}
    as_of = payload.get("fetched_at", "")
    v = _num(ttm.get("forward_pe"))
    if not v:
        return NA("Forward P/E 없음")
    ttm_eps = _num((payload.get("ttm_income") or {}).get("diluted_eps"))
    if ttm_eps is not None and ttm_eps <= 0 and v > FWD_PE_TURNAROUND_MAX:
        return NA(f"아직 적자(TTM EPS {ttm_eps:.2f})이고 예상 EPS가 0에 가까워 P/E {v:,.0f}배 — "
                  "흑자 전환 기저 효과로 의미 없음")
    return M(v, "yfinance forwardPE", as_of)


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
    if g is not None and g > 100:
        return NA("예상 EPS 성장률 100% 초과 (기저 효과) — PEG 의미 없음")
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

    fx = _fx(payload)
    if fx is None:
        return NA(FX_NA + " — WACC 산출 불가")

    beta = _num(ttm.get("beta"))
    if beta is None or beta <= 0 or beta > 3:
        return NA("베타 없음 또는 비정상")
    rf_obj = payload.get("rf") or {}
    rf = _num(rf_obj.get("value"))
    if rf is None:
        return NA("무위험수익률 없음")

    e = _num(ttm.get("market_cap"))
    d = (_num(ttm.get("total_debt")) or 0.0) * fx   # 부채는 재무 통화 → 시세 통화
    if not e or e <= 0:
        return NA("시가총액 없음")

    tax = effective_tax_rate(years[-1])[0] if years else 0.21
    ke = rf + beta * erp
    kd = rf + credit_spread
    w = e + d
    val = (e / w) * ke + (d / w) * kd * (1 - tax)

    r = M(val, "CAPM 추정 (가정 포함)" + _fx_note(payload), rf_obj.get("as_of", ""))
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
    rs = roic_series(years)                  # 연도별 추이·중앙값·안정성은 회계연도 기준 유지
    roic_vals = [r["roic"] for r in rs if r["roic"] is not None]

    # 최신값(ROIC·마진·성장률 끝점·증분 ROIC)은 최근 4개 분기(TTM)를 우선 쓴다.
    # 연간 재무제표만 쓰면 회계연도 종료 후 최대 1년 가까이 지난 숫자로 남기 때문.
    tp = ttm_point(payload)
    latest_y = tp or (years[-1] if years else {})
    eff = years + [tp] if tp else years     # 끝점 계산용 (FY … + TTM)

    latest_roic = NA("데이터 없음")
    if tp:
        n_t, ic_t = nopat(tp), invested_capital(tp)
        if n_t is not None and ic_t is not None:
            latest_roic = M(n_t / ic_t,
                            f"TTM NOPAT ÷ 투하자본({tp['bs_end']} 대차대조표) — 현금·단기투자 차감",
                            f"TTM {tp['end']}")
            latest_roic["detail"] = {"nopat": n_t, "ic": ic_t, "bs_end": tp["bs_end"], "period_end": tp["end"]}
    if latest_roic["v"] is None and rs and rs[-1]["roic"] is not None:
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
        "basis":           ({"type": "TTM", "end": tp["end"], "bs_end": tp["bs_end"]} if tp
                            else {"type": "FY", "end": str((years[-1] if years else {}).get("end", ""))}),
        "roic_yf":         roic_yf_latest(years),
        "roic_median":     M(statistics.median(roic_vals), "연도별 ROIC 중앙값", as_of)
                           if roic_vals else NA("데이터 없음"),
        "incremental_roic": incremental_roic(eff),
        "reinvestment":     reinvestment_rate(years),   # 누적 NOPAT 구간이 겹치지 않도록 FY 기준
        "wacc":             w,
        "roic_spread":      spread,

        "rev_cagr":   cagr(eff, "revenue"),
        "rev_yoy":    yoy(years, "revenue"),
        "net_cagr":   cagr(eff, "net"),
        "fcf_cagr":   cagr(eff, "fcf"),

        "gross_margin":     latest_margin([latest_y], "gross_profit", "매출총이익"),
        "operating_margin": latest_margin([latest_y], "operating_income", "영업이익"),
        "net_margin":       latest_margin([latest_y], "net", "순이익"),
        "fcf_margin":       latest_margin([latest_y], "fcf", "FCF"),
        "gross_margin_series": gm,
        "gross_margin_std": M(statistics.pstdev(gm_vals), "매출총이익률 표준편차(5년)", as_of)
                            if len(gm_vals) >= 3 else NA("데이터 부족"),

        "peg":            peg(payload, forecasts),
        "forward_pe":     forward_pe(payload),
        "trailing_pe":    M(_num(ttm.get("trailing_pe")), "yfinance trailingPE", as_of)
                          if ttm.get("trailing_pe") else NA("P/E 없음"),
        # yfinance의 EV/EBITDA·P/S는 시가총액(시세 통화) ÷ 재무제표(보고 통화)라 통화가 다르면 환산해서 다시 계산
        "ev_ebitda":      ev_ebitda(payload),
        "ps_ttm":         ps_ttm(payload),
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
