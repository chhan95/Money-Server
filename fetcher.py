"""
Yahoo Finance data fetcher using yfinance.
서버 사이드라 CORS 문제 없이 직접 호출 가능.
"""
import os, shutil, tempfile

# ── SSL 인증서 경로 한글 문제 수정 ─────────────────────────
# venv 경로에 한글이 포함되면 curl_cffi가 cacert.pem을 못 찾음.
# ASCII 경로인 임시 폴더로 복사 후 환경변수로 지정.
try:
    import certifi
    _ca = certifi.where()
    if any(ord(c) > 127 for c in _ca):
        _tmp = os.path.join(tempfile.gettempdir(), "money_cacert.pem")
        if not os.path.exists(_tmp):
            shutil.copy2(_ca, _tmp)
        for _k in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            os.environ.setdefault(_k, _tmp)
except Exception:
    pass

import yfinance as yf
import pandas as pd
import logging
import requests

logger = logging.getLogger(__name__)

# yfinance 버전에 따라 행 이름이 다를 수 있어 복수의 후보를 순서대로 시도
_REVENUE_KEYS    = ["Total Revenue", "Revenue", "TotalRevenue"]
_OPERATING_KEYS  = ["Operating Income", "EBIT", "OperatingIncome"]
_NET_KEYS        = ["Net Income", "Net Income Common Stockholders",
                    "NetIncome", "Net Income Applicable To Common Shares"]
_EQUITY_KEYS     = ["Stockholders Equity", "Total Stockholder Equity",
                    "Common Stock Equity", "StockholdersEquity"]
_ASSETS_KEYS     = ["Total Assets", "TotalAssets"]
_DIL_SHARES_KEYS = ["Diluted Average Shares", "WeightedAverageSharesDiluted",
                    "Basic Average Shares", "WeightedAverageShares"]


def _get_row(df: pd.DataFrame, keys: list[str]) -> pd.Series | None:
    for k in keys:
        if k in df.index:
            return df.loc[k]
    return None


def fetch_stock(ticker: str) -> dict | None:
    """
    yfinance로 연간 손익계산서 + 현재 정보 조회.
    반환값:
        {ticker, name, current_price, shares_outstanding (M),
         years: [{year_key, label, revenue, operating, net, shares}]}
    실패 시 None 반환.
    """
    try:
        t = yf.Ticker(ticker.upper())
        info = t.info or {}

        name  = info.get("shortName") or info.get("longName") or ticker.upper()
        price = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        shares_m = float(info.get("sharesOutstanding") or 0) / 1e6
        dividend_rate  = float(info.get("trailingAnnualDividendRate") or info.get("dividendRate") or 0)

        # 재무제표 통화 → USD 환산 (TSM=TWD, 삼성=KRW 등)
        fin_currency = info.get("financialCurrency") or "USD"
        fin_conv = 1.0
        if fin_currency != "USD":
            try:
                fx_pair = f"{fin_currency}USD=X"
                rate = float(yf.Ticker(fx_pair).fast_info.get("lastPrice") or 0)
                if rate > 0:
                    fin_conv = rate
                    logger.info("[%s] 재무제표 통화 %s→USD 환율: %.6f", ticker, fin_currency, rate)
                else:
                    logger.warning("[%s] %s 환율 0, fin_conv=1 fallback", ticker, fx_pair)
            except Exception as e:
                logger.warning("[%s] 환율 조회 실패 (%s): %s", ticker, fin_currency, e)
        dividend_yield = float(info.get("dividendYield") or info.get("trailingAnnualDividendYield") or 0)
        market_cap     = float(info.get("marketCap")     or 0)   # 시가총액 USD
        trailing_pe    = info.get("trailingPE")                   # TTM P/E
        pb_ratio       = info.get("priceToBook")                  # TTM P/B
        trailing_roe   = info.get("returnOnEquity")               # TTM ROE
        trailing_eps   = info.get("trailingEps")                  # TTM EPS

        # 재무제표 가져오기 (income_stmt 우선, financials 폴백)
        fin: pd.DataFrame | None = None
        for attr in ("income_stmt", "financials"):
            try:
                f = getattr(t, attr, None)
                if f is not None and not f.empty:
                    fin = f
                    break
            except Exception:
                continue

        if fin is None or fin.empty:
            logger.warning("[%s] 재무제표 없음", ticker)
            return None

        rev_row = _get_row(fin, _REVENUE_KEYS)
        op_row  = _get_row(fin, _OPERATING_KEYS)
        net_row = _get_row(fin, _NET_KEYS)
        dil_row = _get_row(fin, _DIL_SHARES_KEYS)

        if rev_row is None or net_row is None:
            logger.warning("[%s] 매출/순이익 행 없음", ticker)
            return None

        # 연간 대차대조표 (ROE, ROI 계산용)
        bs = None
        try:
            bs_raw = getattr(t, "balance_sheet", None)
            if bs_raw is not None and not bs_raw.empty:
                bs = bs_raw
        except Exception:
            pass
        eq_row     = _get_row(bs, _EQUITY_KEYS) if bs is not None else None
        assets_row = _get_row(bs, _ASSETS_KEYS) if bs is not None else None

        def _bs_val(row, fin_col):
            """대차대조표에서 동일 연도 컬럼 값 반환."""
            if row is None or bs is None:
                return float("nan")
            for bc in bs.columns:
                if bc.year == fin_col.year:
                    v = row.get(bc, float("nan"))
                    return float(v) if not pd.isna(v) else float("nan")
            return float("nan")

        years = []
        last_valid_dil_shares = shares_m * 1e6  # 직전 연도까지 유효했던 희석주식수
        for col in reversed(list(fin.columns[:4])):   # 오래된 연도부터
            try:
                rev = rev_row[col]
                op  = op_row[col]  if op_row  is not None else float("nan")
                net = net_row[col]
                if pd.isna(rev) or pd.isna(net):
                    continue

                # EPS: 손익계산서 희석주식수 우선.
                # NaN이면 직전 유효값 사용 (다중 클래스 주식에서 최신 연도가 NaN인 경우 대응).
                dil_shares = float("nan")
                if dil_row is not None:
                    dil_shares = dil_row.get(col, float("nan"))
                if not pd.isna(dil_shares) and float(dil_shares) > 0:
                    eps_shares = float(dil_shares)
                    last_valid_dil_shares = eps_shares
                else:
                    eps_shares = last_valid_dil_shares
                eps = float(net) * fin_conv / eps_shares if eps_shares > 0 else None

                # ROE = 순이익 / 자기자본
                eq = _bs_val(eq_row, col)
                roe = float(net) / eq if not pd.isna(eq) and eq > 0 else None

                # ROI = 순이익 / 총자산 (ROA 방식)
                assets = _bs_val(assets_row, col)
                roi = float(net) / assets if not pd.isna(assets) and assets > 0 else None

                # BVPS = 자기자본 / 희석주식수 (USD 환산)
                bvps = float(eq) * fin_conv / eps_shares if not pd.isna(eq) and eq > 0 and eps_shares > 0 else None

                yr = col.year
                years.append({
                    "year_key":  f"fy{yr}",
                    "label":     f"FY{yr}",
                    "end_date":  col.strftime("%Y-%m"),
                    "revenue":   float(rev) / 1e6 * fin_conv,
                    "operating": float(op)  / 1e6 * fin_conv if not pd.isna(op) else 0.0,
                    "net":       float(net) / 1e6 * fin_conv,
                    "shares":    round(eps_shares / 1e6, 3),  # 희석주식수 우선 (다중 클래스 주식 대응)
                    "eps":       round(eps, 4) if eps is not None else None,
                    "roe":       round(roe, 6) if roe is not None else None,
                    "roi":       round(roi, 6) if roi is not None else None,
                    "bvps":      round(bvps, 4) if bvps is not None else None,
                })
            except Exception as e:
                logger.debug("[%s] %s 열 처리 오류: %s", ticker, col, e)

        if not years:
            return None

        # 예상 순이익 계산용 희석주식수: 루프에서 수집한 마지막 유효값 사용
        forecast_dil_shares = last_valid_dil_shares

        # ── 애널리스트 예상치 (현재 FY / 다음 FY) ─────────────
        forecasts = []
        try:
            ee    = getattr(t, "earnings_estimate", None)
            re_est = getattr(t, "revenue_estimate", None)
            for period, label in [("0y", "현재 FY 예상"), ("+1y", "내년 FY 예상")]:
                if ee is None or period not in ee.index:
                    continue
                eps_avg = ee.loc[period].get("avg")
                if eps_avg is None or pd.isna(eps_avg):
                    continue
                rev_avg = None
                if re_est is not None and period in re_est.index:
                    rv = re_est.loc[period].get("avg")
                    if rv is not None and not pd.isna(rv):
                        rev_avg = float(rv) * fin_conv   # 비USD 통화 환산
                net_est = float(eps_avg) * forecast_dil_shares   # 총 순이익 (USD)
                forecasts.append({
                    "period":  period,
                    "label":   label,
                    "revenue": rev_avg / 1e6 if rev_avg else None,
                    "net":     net_est / 1e6,
                    "eps":     float(eps_avg),
                })
        except Exception as e:
            logger.debug("[%s] 예상 데이터 오류: %s", ticker, e)

        return {
            "ticker":        ticker.upper(),
            "name":          name,
            "current_price": price,
            "shares_m":      shares_m,
            "fin_currency":  fin_currency,
            "dividend_yield": dividend_yield,
            "dividend_rate":  dividend_rate,
            "market_cap":    market_cap,
            "trailing_pe":   float(trailing_pe)  if trailing_pe  is not None else None,
            "pb_ratio":      float(pb_ratio)     if pb_ratio     is not None else None,
            "trailing_roe":  float(trailing_roe) if trailing_roe is not None else None,
            "trailing_eps":  float(trailing_eps) if trailing_eps is not None else None,
            "years":         years,
            "forecasts":     forecasts,
        }

    except Exception as e:
        logger.error("[%s] fetch 실패: %s", ticker, e, exc_info=True)
        return None


def fetch_rule_of_40(ticker: str) -> dict | None:
    """
    Rule of 40 지표 계산.
    - revenue_growth: 최근 2개 연간 매출 YoY 성장률 (%)
    - profit_margin : 최근 연간 순이익률 (%)
    - score         : growth + margin
    """
    try:
        t    = yf.Ticker(ticker.upper())
        info = t.info or {}
        name = info.get("shortName") or info.get("longName") or ticker.upper()

        fin: pd.DataFrame | None = None
        for attr in ("income_stmt", "financials"):
            try:
                f = getattr(t, attr, None)
                if f is not None and not f.empty:
                    fin = f
                    break
            except Exception:
                continue

        if fin is None or fin.empty:
            logger.warning("[Rule40/%s] 재무제표 없음", ticker)
            return None

        rev_row = _get_row(fin, _REVENUE_KEYS)
        net_row = _get_row(fin, _NET_KEYS)

        if rev_row is None or net_row is None:
            logger.warning("[Rule40/%s] 매출/순이익 행 없음", ticker)
            return None

        # 최신 2개 연도 컬럼 (index 0=최신, 1=직전)
        valid_cols = [c for c in fin.columns[:4]
                      if not pd.isna(rev_row.get(c)) and not pd.isna(net_row.get(c))]
        if len(valid_cols) < 2:
            logger.warning("[Rule40/%s] 유효 연도 부족 (%d개)", ticker, len(valid_cols))
            return None

        rev1 = float(rev_row[valid_cols[0]])   # 최신 매출
        rev0 = float(rev_row[valid_cols[1]])   # 직전 매출
        net1 = float(net_row[valid_cols[0]])   # 최신 순이익

        if rev0 == 0 or rev1 == 0:
            return None

        revenue_growth = (rev1 - rev0) / abs(rev0) * 100
        profit_margin  = net1 / rev1 * 100

        return {
            "ticker":         ticker.upper(),
            "name":           name,
            "revenue_growth": round(revenue_growth, 1),
            "profit_margin":  round(profit_margin, 1),
            "score":          round(revenue_growth + profit_margin, 1),
            "year_latest":    valid_cols[0].year,
            "year_prev":      valid_cols[1].year,
        }

    except Exception as e:
        logger.error("[Rule40/%s] 조회 실패: %s", ticker, e, exc_info=True)
        return None


def fetch_stock_quick(ticker: str) -> dict | None:
    """
    최신 1개 회계연도만 빠르게 조회.
    fast_info 사용 (info보다 빠름), balance sheet·예상치 생략.
    """
    try:
        t  = yf.Ticker(ticker.upper())
        fi = t.fast_info
        price    = float(fi.last_price or 0)
        shares_m = float(fi.shares    or 0) / 1e6

        fin = None
        for attr in ("income_stmt", "financials"):
            try:
                f = getattr(t, attr, None)
                if f is not None and not f.empty:
                    fin = f
                    break
            except Exception:
                continue

        if fin is None or fin.empty:
            return None

        rev_row = _get_row(fin, _REVENUE_KEYS)
        net_row = _get_row(fin, _NET_KEYS)
        op_row  = _get_row(fin, _OPERATING_KEYS)
        dil_row = _get_row(fin, _DIL_SHARES_KEYS)

        if rev_row is None or net_row is None:
            return None

        col = fin.columns[0]   # 최신 연도만
        rev = rev_row[col]
        net = net_row[col]
        op  = op_row[col] if op_row is not None else float("nan")

        if pd.isna(rev) or pd.isna(net):
            return None

        # 희석주식수: 손익계산서 우선, 없으면 fast_info 값 (다중 클래스 주식 대응)
        dil_shares = dil_row.get(col, float("nan")) if dil_row is not None else float("nan")
        actual_shares_m = float(dil_shares) / 1e6 \
            if not pd.isna(dil_shares) and float(dil_shares) > 0 else shares_m

        yr = col.year
        return {
            "ticker":        ticker.upper(),
            "name":          ticker.upper(),   # fast_info에 회사명 없음
            "current_price": price,
            "shares_m":      actual_shares_m,
            "years": [{
                "year_key":  f"fy{yr}",
                "label":     f"FY{yr}",
                "end_date":  col.strftime("%Y-%m"),
                "revenue":   float(rev) / 1e6,
                "operating": float(op) / 1e6 if not pd.isna(op) else 0.0,
                "net":       float(net) / 1e6,
                "shares":    round(actual_shares_m, 3),
                "eps": None, "roe": None, "roi": None,
            }],
            "forecasts": [],
        }
    except Exception as e:
        logger.error("[%s] quick fetch 실패: %s", ticker, e, exc_info=True)
        return None


def fetch_current_price(ticker: str) -> float | None:
    """현재가만 빠르게 조회 (fast_info 사용). 실패 시 None."""
    try:
        price = yf.Ticker(ticker.upper()).fast_info.last_price
        return float(price) if price and price > 0 else None
    except Exception:
        return None


def fetch_kr_full_stock(raw_ticker: str) -> dict | None:
    """
    국내 주식 전체 재무 데이터 조회 (KRW 기준).
    years[].revenue/operating/net: KRW 백만원
    years[].eps: KRW 원/주
    forecasts[].net: KRW 백만원, .eps: KRW 원/주
    """
    raw = raw_ticker.strip()
    candidates = [raw] if (raw.endswith(".KS") or raw.endswith(".KQ")) else [raw + ".KS", raw + ".KQ"]
    for t_code in candidates:
        result = _fetch_kr_full(t_code)
        if result:
            return result
    return None


def _fetch_kr_full(ticker: str) -> dict | None:
    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
        price = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        if price <= 0:
            return None

        name     = fetch_kr_name(ticker) or info.get("shortName") or info.get("longName") or ticker
        shares_m = float(info.get("sharesOutstanding") or 0) / 1e6

        fin: pd.DataFrame | None = None
        for attr in ("income_stmt", "financials"):
            try:
                f = getattr(t, attr, None)
                if f is not None and not f.empty:
                    fin = f
                    break
            except Exception:
                continue

        years = []
        if fin is not None and not fin.empty:
            valid_cols = [c for c in fin.columns if not fin[c].isnull().all()][:4]
            rev_row = _get_row(fin, _REVENUE_KEYS)
            op_row  = _get_row(fin, _OPERATING_KEYS)
            net_row = _get_row(fin, _NET_KEYS)
            dil_row = _get_row(fin, _DIL_SHARES_KEYS)
            for col in valid_cols:
                rev = rev_row[col] if rev_row is not None else float("nan")
                op  = op_row[col]  if op_row  is not None else float("nan")
                net = net_row[col] if net_row  is not None else float("nan")
                dil = dil_row[col] if dil_row  is not None else float("nan")
                if pd.isna(rev) or pd.isna(net):
                    continue
                yr       = col.year
                shares_y = float(dil) / 1e6 if (not pd.isna(dil) and float(dil) > 0) else shares_m
                eps_val  = round(float(net) / (shares_y * 1e6)) if shares_y > 0 else None
                years.append({
                    "year_key":  f"fy{yr}",
                    "label":     f"FY{yr}",
                    "end_date":  col.strftime("%Y-%m"),
                    "revenue":   float(rev) / 1e6,
                    "operating": float(op)  / 1e6 if not pd.isna(op) else 0,
                    "net":       float(net) / 1e6,
                    "shares":    round(shares_y, 3),
                    "eps":       eps_val,
                })

        forecasts = []
        try:
            ee = getattr(t, "earnings_estimate", None)
            re = getattr(t, "revenue_estimate",  None)
            if ee is not None and not ee.empty:
                latest_shares = years[-1]["shares"] if years else shares_m
                for period, label in [("0y", "현재 FY"), ("+1y", "내년 FY")]:
                    if period not in ee.index:
                        continue
                    eps_avg = ee.loc[period, "avg"] if "avg" in ee.columns else None
                    if eps_avg is None or pd.isna(eps_avg):
                        continue
                    eps_val = round(float(eps_avg))
                    net_est = float(eps_val) * latest_shares * 1e6 / 1e6
                    rev_avg = None
                    if re is not None and not re.empty and period in re.index:
                        rv = re.loc[period, "avg"] if "avg" in re.columns else None
                        if rv is not None and not pd.isna(rv):
                            rev_avg = float(rv) / 1e6
                    forecasts.append({"period": period, "label": label,
                                      "revenue": rev_avg, "net": net_est, "eps": eps_val})
        except Exception as e:
            logger.debug("[KR full/%s] 예상 오류: %s", ticker, e)

        return {"ticker": ticker, "name": name, "current_price": price,
                "shares_m": shares_m, "years": years, "forecasts": forecasts}
    except Exception as e:
        logger.error("[KR full/%s] 실패: %s", ticker, e, exc_info=True)
        return None


def fetch_kr_name(ticker: str) -> str | None:
    """
    NAVER Finance에서 한글 종목명 조회.
    ticker: "005930.KS" 또는 "005930" 형식
    실패 시 None 반환.
    """
    code = ticker.replace(".KS", "").replace(".KQ", "").strip()
    try:
        url = f"https://m.stock.naver.com/api/stock/{code}/basic"
        resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            data = resp.json()
            name = data.get("stockName") or data.get("name")
            if name:
                return name
    except Exception as e:
        logger.debug("[KR name/%s] NAVER 조회 실패: %s", code, e)
    return None


def fetch_kr_rule_of_40(raw_ticker: str) -> dict | None:
    """
    국내 주식 Rule of 40 지표 계산.
    raw_ticker: "035420" 또는 "035420.KS" / "035420.KQ"
    반환값: {ticker, name, revenue_growth, profit_margin, score}
    실패 시 None 반환.
    """
    raw = raw_ticker.strip()
    if raw.endswith(".KS") or raw.endswith(".KQ"):
        candidates = [raw]
    else:
        candidates = [raw + ".KS", raw + ".KQ"]

    for t_code in candidates:
        result = fetch_rule_of_40(t_code)
        if result:
            result["ticker"] = t_code
            kr_name = fetch_kr_name(t_code)
            if kr_name:
                result["name"] = kr_name
            return result

    return None


def fetch_kr_stock(raw_ticker: str) -> dict | None:
    """
    yfinance로 한국 주식 현재가 조회.
    raw_ticker: "005930" 또는 "005930.KS" / "005930.KQ" 형식
    반환값: {ticker, name, current_price (KRW)}
    실패 시 None 반환.
    """
    raw = raw_ticker.strip().upper()
    if raw.endswith(".KS") or raw.endswith(".KQ"):
        candidates = [raw]
    else:
        candidates = [raw + ".KS", raw + ".KQ"]

    for t_code in candidates:
        try:
            t = yf.Ticker(t_code)
            info = t.info or {}
            price = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
            if price > 0:
                name = fetch_kr_name(t_code) or info.get("shortName") or info.get("longName") or t_code
                return {"ticker": t_code, "name": name, "current_price": price}
        except Exception as e:
            logger.warning("[KR/%s] 조회 실패: %s", t_code, e)

    return None


def fetch_kr_current_price(ticker: str) -> float | None:
    """국내 주식 현재가만 빠르게 조회. 실패 시 None."""
    try:
        price = yf.Ticker(ticker).fast_info.last_price
        return float(price) if price and price > 0 else None
    except Exception:
        return None


def fetch_krw_rate() -> float:
    """현재 USD/KRW 환율 조회. 실패 시 1380 반환."""
    for sym in ("USDKRW=X", "KRW=X"):
        try:
            val = yf.Ticker(sym).fast_info.last_price
            if val and val > 0:
                return float(val) if val > 100 else round(1.0 / val, 2)
        except Exception:
            continue
    return 1380.0
