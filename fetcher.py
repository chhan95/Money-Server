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

        name  = info.get("longName") or info.get("shortName") or ticker.upper()
        price = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        shares_m = float(info.get("sharesOutstanding") or 0) / 1e6

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
        for col in reversed(list(fin.columns[:4])):   # 오래된 연도부터
            try:
                rev = rev_row[col]
                op  = op_row[col]  if op_row  is not None else float("nan")
                net = net_row[col]
                if pd.isna(rev) or pd.isna(net):
                    continue

                # EPS: 희석 주식수 우선, 없으면 현재 발행주식수 사용
                dil_shares = float("nan")
                if dil_row is not None:
                    dil_shares = dil_row.get(col, float("nan"))
                eps_shares = float(dil_shares) if not pd.isna(dil_shares) and dil_shares > 0 \
                             else shares_m * 1e6
                eps = float(net) / eps_shares if eps_shares > 0 else None

                # ROE = 순이익 / 자기자본
                eq = _bs_val(eq_row, col)
                roe = float(net) / eq if not pd.isna(eq) and eq > 0 else None

                # ROI = 순이익 / 총자산 (ROA 방식)
                assets = _bs_val(assets_row, col)
                roi = float(net) / assets if not pd.isna(assets) and assets > 0 else None

                yr = col.year
                years.append({
                    "year_key":  f"fy{yr}",
                    "label":     f"FY{yr}",
                    "end_date":  col.strftime("%Y-%m"),
                    "revenue":   float(rev) / 1e6,
                    "operating": float(op)  / 1e6 if not pd.isna(op) else 0.0,
                    "net":       float(net) / 1e6,
                    "shares":    shares_m,
                    "eps":       round(eps, 4) if eps is not None else None,
                    "roe":       round(roe, 6) if roe is not None else None,
                    "roi":       round(roi, 6) if roi is not None else None,
                })
            except Exception as e:
                logger.debug("[%s] %s 열 처리 오류: %s", ticker, col, e)

        if not years:
            return None

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
                        rev_avg = float(rv)
                net_est = float(eps_avg) * shares_m * 1e6   # 총 순이익 (USD)
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
            "years":         years,
            "forecasts":     forecasts,
        }

    except Exception as e:
        logger.error("[%s] fetch 실패: %s", ticker, e, exc_info=True)
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

        if rev_row is None or net_row is None:
            return None

        col = fin.columns[0]   # 최신 연도만
        rev = rev_row[col]
        net = net_row[col]
        op  = op_row[col] if op_row is not None else float("nan")

        if pd.isna(rev) or pd.isna(net):
            return None

        yr = col.year
        return {
            "ticker":        ticker.upper(),
            "name":          ticker.upper(),   # fast_info에 회사명 없음
            "current_price": price,
            "shares_m":      shares_m,
            "years": [{
                "year_key":  f"fy{yr}",
                "label":     f"FY{yr}",
                "end_date":  col.strftime("%Y-%m"),
                "revenue":   float(rev) / 1e6,
                "operating": float(op) / 1e6 if not pd.isna(op) else 0.0,
                "net":       float(net) / 1e6,
                "shares":    shares_m,
                "eps": None, "roe": None, "roi": None,
            }],
            "forecasts": [],
        }
    except Exception as e:
        logger.error("[%s] quick fetch 실패: %s", ticker, e, exc_info=True)
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
