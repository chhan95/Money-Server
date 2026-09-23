from fastapi import FastAPI, Depends, HTTPException, Request, Form, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone, date
from typing import Optional
import json, logging, uuid
from pathlib import Path

import models, database, fetcher, analytics, scoring, quality
from database import get_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 초기화 ────────────────────────────────────────────────────────────────────
database.create_tables()

app = FastAPI(title="💰 Money Dashboard", docs_url="/docs")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/resources", StaticFiles(directory="resources"), name="resources")

MILESTONE_UPLOAD_DIR = Path("resources") / "milestones"
MILESTONE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory="templates")

CACHE_HOURS = 24


# ════════════════════════════════════════════════════════════
# CRUD 헬퍼
# ════════════════════════════════════════════════════════════

def _now() -> datetime:
    """SQLite naive datetime과 비교 가능한 UTC now."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _today_utc():
    """fetched_at(UTC 저장) 비교용 UTC date."""
    return datetime.now(timezone.utc).date()


def _is_stale(stock: models.Stock | None) -> bool:
    """캐시가 만료됐거나 지표가 누락된 경우 True."""
    if stock is None or stock.fetched_at is None or len(stock.fiscal_years) == 0:
        return True
    if _now() - stock.fetched_at > timedelta(hours=CACHE_HOURS):
        return True
    if stock.forecasts_json is None:
        return True
    if any(f.eps is None and f.roe is None and f.roi is None for f in stock.fiscal_years):
        return True
    return False


def get_or_refresh(ticker: str, db: Session) -> models.Stock | None:
    """DB에서 종목 조회, 24시간 이상 오래됐으면 yfinance로 갱신."""
    ticker = ticker.upper()
    stock  = db.query(models.Stock).filter(models.Stock.ticker == ticker).first()

    missing_metrics = (
        stock is not None
        and (
            any(f.eps is None and f.roe is None and f.roi is None for f in stock.fiscal_years)
            or stock.forecasts_json is None
        )
    )
    stale = (
        stock is None
        or stock.fetched_at is None
        or _now() - stock.fetched_at > timedelta(hours=CACHE_HOURS)
        or len(stock.fiscal_years) == 0
        or missing_metrics
    )

    if stale:
        logger.info("[%s] 데이터 갱신 중...", ticker)
        data = fetcher.fetch_stock(ticker)
        if not data:
            return stock  # 갱신 실패 → 기존 데이터 반환

        if stock is None:
            stock = models.Stock(ticker=ticker)
            db.add(stock)
            db.flush()

        stock.name               = data["name"]
        stock.current_price      = data["current_price"]
        stock.shares_outstanding = data["shares_m"]
        stock.forecasts_json     = json.dumps(data.get("forecasts", []), ensure_ascii=False)
        stock.dividend_yield     = data.get("dividend_yield", 0)
        stock.dividend_rate      = data.get("dividend_rate", 0)
        stock.market_cap         = data.get("market_cap", 0)
        stock.trailing_pe        = data.get("trailing_pe")
        stock.pb_ratio           = data.get("pb_ratio")
        stock.trailing_roe       = data.get("trailing_roe")
        stock.trailing_eps       = data.get("trailing_eps")
        stock.fin_currency       = data.get("fin_currency", "USD")
        if data.get("sector"):
            stock.sector         = data["sector"]
        stock.fetched_at         = _now()

        # 기존 연도 데이터 교체
        db.query(models.FiscalYear).filter(models.FiscalYear.ticker == ticker).delete()
        for y in data["years"]:
            db.add(models.FiscalYear(
                ticker    = ticker,
                year_key  = y["year_key"],
                label     = y["label"],
                end_date  = y.get("end_date"),
                revenue   = y["revenue"],
                operating = y["operating"],
                net       = y["net"],
                shares    = y["shares"],
                eps       = y.get("eps"),
                roe       = y.get("roe"),
                roi       = y.get("roi"),
                bvps      = y.get("bvps"),
            ))

        db.commit()
        db.refresh(stock)

    return stock


def stock_to_dict(stock: models.Stock) -> dict:
    """ORM → JSON 직렬화용 dict."""
    fyears = sorted(stock.fiscal_years, key=lambda f: f.year_key)[-3:]  # 최근 3년
    latest_shares = fyears[-1].shares if fyears else 1.0

    fiscal_data = {
        f.year_key: {
            "label":      f.label,
            "endDate":    f.end_date,
            "revenue":    round(f.revenue   or 0, 3),
            "operating":  round(f.operating or 0, 3),
            "net":        round(f.net       or 0, 3),
            "shares":     round(f.shares    or 0, 3),
            "eps":        round(f.eps, 2)  if f.eps  is not None else None,
            "roe":        round(f.roe, 4)  if f.roe  is not None else None,
            "roi":        round(f.roi, 4)  if f.roi  is not None else None,
            "bvps":       round(f.bvps, 4) if f.bvps is not None else None,
            "isForecast": False,
        }
        for f in fyears
    }

    # 예상치를 fiscalData에 병합 (fc_0y, fc_+1y)
    forecasts = json.loads(stock.forecasts_json or "[]")
    forecast_keys = []
    for fc in forecasts:
        fc_key = f"fc_{fc['period']}"
        fiscal_data[fc_key] = {
            "label":      fc["label"],
            "endDate":    None,
            "revenue":    round(fc["revenue"], 3) if fc.get("revenue") else 0.0,
            "operating":  0.0,
            "net":        round(fc["net"], 3) if fc.get("net") else 0.0,
            "shares":     round(latest_shares, 3),
            "eps":        round(fc["eps"], 2) if fc.get("eps") else None,
            "roe":        None,
            "roi":        None,
            "isForecast": True,
        }
        forecast_keys.append(fc_key)

    return {
        "ticker":         stock.ticker,
        "name":           stock.name or stock.ticker,
        "price":          stock.current_price or 0,
        "updated":        stock.fetched_at.strftime("%Y-%m-%d %H:%M") if stock.fetched_at else "—",
        "fiscalData":     fiscal_data,
        "yearKeys":       [f.year_key for f in fyears],
        "forecastKeys":   forecast_keys,
        "dividendYield":  stock.dividend_yield or 0,
        "dividendRate":   stock.dividend_rate  or 0,
        "marketCap":      stock.market_cap     or 0,
        "trailingPE":     stock.trailing_pe,
        "pbRatio":        stock.pb_ratio,
        "trailingRoe":    stock.trailing_roe,
        "trailingEps":    stock.trailing_eps,
        "sector":         stock.sector or "",
    }


# ════════════════════════════════════════════════════════════
# 일별 스냅샷
# ════════════════════════════════════════════════════════════

def save_daily_snapshot(db: Session) -> None:
    """오늘 스냅샷을 생성(없으면) 또는 갱신(있으면). 홈 로드 시 호출."""
    from datetime import date as _date
    today = _date.today()

    portfolio = db.query(models.Portfolio).all()
    if not portfolio:
        return

    try:
        fx_rate = fetcher.fetch_krw_rate()
    except Exception:
        fx_rate = 1380.0

    total_value_usd = 0.0
    monthly_revenue_usd = 0.0
    monthly_op_usd = 0.0
    monthly_net_usd = 0.0
    unrealized_gain_usd = 0.0

    for p in portfolio:
        stock = db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        if not stock or not stock.fiscal_years:
            continue
        price  = stock.current_price or 0
        shares = p.shares_owned
        avg    = p.avg_price or 0

        total_value_usd += price * shares
        if avg > 0:
            unrealized_gain_usd += (price - avg) * shares

        fyears = sorted(stock.fiscal_years, key=lambda f: f.year_key)
        latest = fyears[-1]
        shares_m = latest.shares or 1
        pct = shares / (shares_m * 1e6)

        # 홈 화면 getMonthlyNet 동일 로직:
        # 현재 FY 예상(forecasts[0])이 있으면 우선 사용, 없으면 최신 확정 FY
        forecasts = json.loads(stock.forecasts_json or "[]")
        if forecasts:
            fc0 = forecasts[0]
            net_val = fc0.get("net") or 0
            rev_val = fc0.get("revenue") or 0
        else:
            net_val = latest.net or 0
            rev_val = latest.revenue or 0

        monthly_revenue_usd += rev_val * pct / 12
        monthly_op_usd      += (latest.operating or 0) * pct / 12
        monthly_net_usd     += net_val * pct / 12

    snap = db.query(models.DailySnapshot).filter(
        models.DailySnapshot.snapshot_date == today
    ).first()
    if snap is None:
        snap = models.DailySnapshot(snapshot_date=today)
        db.add(snap)

    snap.total_value_krw     = total_value_usd * fx_rate
    snap.monthly_revenue_krw = monthly_revenue_usd * 1e6 * fx_rate
    snap.monthly_op_krw      = monthly_op_usd * 1e6 * fx_rate
    snap.monthly_net_krw     = monthly_net_usd * 1e6 * fx_rate
    snap.unrealized_gain_krw = unrealized_gain_usd * fx_rate
    snap.unrealized_gain_usd = unrealized_gain_usd
    snap.fx_rate             = fx_rate
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("스냅샷 저장 실패: %s", e)


# ════════════════════════════════════════════════════════════
# 페이지 라우트 (HTML)
# ════════════════════════════════════════════════════════════

@app.get("/api/refresh-prices")
def api_refresh_prices(db: Session = Depends(get_db)):
    """포트폴리오 종목 현재가 갱신 (하루 1회, 홈/포트폴리오 페이지용)."""
    today = _today_utc()
    portfolio = db.query(models.Portfolio).all()
    updated = []
    for p in portfolio:
        stock = db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        last_date = stock.fetched_at.date() if (stock and stock.fetched_at) else None
        if last_date is None or last_date < today:
            price = fetcher.fetch_current_price(p.ticker)
            if stock:
                if price:
                    stock.current_price = price
                    updated.append(p.ticker)
                    logger.info("[%s] 현재가 갱신: %.2f", p.ticker, price)
                stock.fetched_at = _now()  # 가격 실패해도 오늘 시도 완료 표시
                db.commit()
    try:
        save_daily_snapshot(db)
    except Exception as e:
        logger.warning("스냅샷 저장 실패: %s", e)
    return {"updated": updated}


@app.get("/api/force-refresh-prices")
def api_force_refresh_prices(db: Session = Depends(get_db)):
    """포트폴리오 종목 현재가 강제 갱신 (캐시 무시, 수동 버튼용)."""
    portfolio = db.query(models.Portfolio).all()
    updated = []
    for p in portfolio:
        stock = db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        if not stock:
            continue
        price = fetcher.fetch_current_price(p.ticker)
        if price:
            stock.current_price = price
            stock.fetched_at = _now()
            db.commit()
            updated.append(p.ticker)
            logger.info("[%s] 현재가 강제 갱신: %.2f", p.ticker, price)
    try:
        save_daily_snapshot(db)
    except Exception as e:
        logger.warning("스냅샷 저장 실패: %s", e)
    return {"updated": updated}


@app.get("/api/kr-force-refresh-prices")
def api_kr_force_refresh_prices(db: Session = Depends(get_db)):
    """국내 포트폴리오 종목 현재가 강제 갱신 (캐시 무시, 수동 버튼용)."""
    portfolio = db.query(models.KrPortfolio).all()
    updated = []
    for p in portfolio:
        stock = db.query(models.KrStock).filter(models.KrStock.ticker == p.ticker).first()
        if not stock:
            continue
        price = fetcher.fetch_kr_current_price(p.ticker)
        if price:
            stock.current_price = price
            stock.fetched_at = _now()
            db.commit()
            updated.append(p.ticker)
            logger.info("[%s] 현재가 강제 갱신: %.0f", p.ticker, price)
    try:
        save_kr_daily_snapshot(db)
    except Exception as e:
        logger.warning("국내 스냅샷 저장 실패: %s", e)
    return {"updated": updated}


@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, db: Session = Depends(get_db)):
    portfolio = (
        db.query(models.Portfolio)
        .order_by(models.Portfolio.display_order, models.Portfolio.created_at)
        .all()
    )

    # 오늘 아직 갱신 안 된 종목이 있으면 클라이언트에서 로딩 화면 후 갱신
    today = _today_utc()
    needs_refresh = any(
        (lambda s: s is None or s.fetched_at is None or s.fetched_at.date() < today)(
            db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        )
        for p in portfolio
    )

    items = []
    stale_tickers = []
    for p in portfolio:
        # 24시간 이상 지난 종목은 재무/forecast 데이터도 함께 갱신
        stock = get_or_refresh(p.ticker, db)
        if _is_stale(stock):
            stale_tickers.append(p.ticker)

        if stock and stock.fiscal_years:
            sd = stock_to_dict(stock)
            latest_key = sd["yearKeys"][-1] if sd["yearKeys"] else None
            latest = sd["fiscalData"].get(latest_key, {}) if latest_key else {}
            forecast_keys = sd.get("forecastKeys", [])
            forecast = sd["fiscalData"].get(forecast_keys[-1]) if forecast_keys else None
            items.append({
                "ticker":        p.ticker,
                "name":          sd["name"],
                "shares_owned":  p.shares_owned,
                "avg_price":     p.avg_price or 0,
                "current_price": sd["price"],
                "latest_year":   latest_key,
                "latest":        latest,
                "forecast":      forecast,
                "portfolio_id":  p.id,
                "memo":          p.memo or "",
                "fiscal_data":   sd["fiscalData"],
                "year_keys":     sd["yearKeys"],
                "forecast_keys": sd.get("forecastKeys", []),
                "sector":        sd.get("sector", ""),
            })
        else:
            # 아직 데이터 없음 — 플레이스홀더 (갱신 후 reload)
            items.append({
                "ticker":        p.ticker,
                "name":          p.ticker,
                "shares_owned":  p.shares_owned,
                "avg_price":     p.avg_price or 0,
                "current_price": 0,
                "latest_year":   None,
                "latest":        {},
                "forecast":      None,
                "portfolio_id":  p.id,
                "memo":          p.memo or "",
                "fiscal_data":   {},
                "year_keys":     [],
                "forecast_keys": [],
                "sector":        "",
            })

    try:
        fx_rate = fetcher.fetch_krw_rate()
    except Exception:
        fx_rate = 1380.0

    items.sort(key=lambda x: x["shares_owned"] * x["current_price"], reverse=True)

    # ── 섹터 비중 계산 ────────────────────────────────────────
    _SECTOR_KO = {
        # ── 기술/반도체 ──
        "Semiconductors":                          "반도체",
        "Semiconductor Equipment & Materials":     "반도체 장비/소재",
        "Software—Application":                    "소프트웨어",
        "Software - Application":                  "소프트웨어",
        "Software—Infrastructure":                 "소프트웨어",
        "Software - Infrastructure":               "소프트웨어",
        "Information Technology Services":         "IT서비스",
        "Computer Hardware":                       "컴퓨터 하드웨어",
        "Consumer Electronics":                    "가전/하드웨어",
        "Electronic Components":                   "전자부품",
        "Communication Equipment":                 "통신장비",
        "Technology":                              "기술/IT",
        "information_technology":                  "기술/IT",

        # ── 인터넷/미디어/통신 ──
        "Internet Content & Information":          "인터넷/플랫폼",
        "Internet Retail":                         "이커머스",
        "Advertising Agencies":                    "광고/마케팅",
        "Entertainment":                           "엔터테인먼트",
        "Communication Services":                  "통신/미디어",
        "Telecom Services":                        "통신",
        "communication_services":                  "통신/미디어",

        # ── 헬스케어 ──
        "Drug Manufacturers—General":               "제약",
        "Drug Manufacturers - General":             "제약",
        "Drug Manufacturers—Specialty & Generic":   "제약",
        "Drug Manufacturers - Specialty & Generic": "제약",
        "Biotechnology":                            "바이오테크",
        "Medical Devices":                          "의료기기",
        "Medical Instruments & Supplies":           "의료기기",
        "Healthcare Plans":                         "헬스케어 서비스",
        "Diagnostics & Research":                   "진단/연구",
        "Healthcare":                               "헬스케어",
        "health_care":                              "헬스케어",

        # ── 금융 ──
        "Financial Services":                      "금융",
        "Banks—Diversified":                       "은행",
        "Banks - Diversified":                     "은행",
        "Banks—Regional":                          "은행",
        "Banks - Regional":                        "은행",
        "Asset Management":                        "자산운용",
        "Capital Markets":                         "자본시장",
        "Credit Services":                         "여신금융",
        "Insurance":                               "보험",
        "financials":                              "금융",

        # ── 산업재/방산/자동차 ──
        "Aerospace & Defense":                     "방산",
        "Auto Manufacturers":                      "자동차",
        "Auto Parts":                              "자동차부품",
        "Specialty Industrial Machinery":          "산업재",
        "Industrials":                             "산업재",
        "industrials":                             "산업재",

        # ── 에너지/유틸리티 ──
        "Oil & Gas Integrated":                    "에너지",
        "Oil & Gas E&P":                           "에너지",
        "Oil & Gas Midstream":                     "에너지",
        "Oil & Gas Refining & Marketing":          "에너지",
        "energy":                                  "에너지",
        "Utilities—Regulated Electric":            "유틸리티",
        "Utilities - Regulated Electric":          "유틸리티",
        "utilities":                               "유틸리티",

        # ── 소비재/유통 ──
        "Consumer Cyclical":                       "소비재",
        "consumer_discretionary":                  "소비재",
        "Consumer Defensive":                      "필수소비재",
        "consumer_staples":                        "필수소비재",
        "Discount Stores":                         "유통",
        "Home Improvement Retail":                 "소매유통",
        "Restaurants":                             "외식",
        "Household & Personal Products":           "생활용품",
        "Beverages—Non-Alcoholic":                 "음료",
        "Beverages - Non-Alcoholic":               "음료",
        "Packaged Foods":                          "식품",

        # ── 소재/부동산 ──
        "Basic Materials":                         "소재",
        "materials":                               "소재",
        "Real Estate":                             "부동산",
        "real_estate":                             "부동산",
    }
    _bucket: dict = {}
    for it in items:
        raw = it.get("sector") or ""
        label = _SECTOR_KO.get(raw) or (raw if raw else "기타")
        val = it["shares_owned"] * it["current_price"]
        if label not in _bucket:
            _bucket[label] = {"value": 0.0, "tickers": []}
        _bucket[label]["value"]   += val
        _bucket[label]["tickers"].append(it["ticker"])
    total_val = sum(b["value"] for b in _bucket.values()) or 1.0
    sector_alloc = sorted(
        [{"name": k, "value": round(v["value"], 2),
          "tickers": v["tickers"],
          "pct": round(v["value"] / total_val * 100, 1)}
         for k, v in _bucket.items()],
        key=lambda x: x["value"], reverse=True,
    )

    # 오늘 스냅샷 저장 (최신 가격 기준)
    try:
        save_daily_snapshot(db)
    except Exception as e:
        logger.warning("스냅샷 저장 실패: %s", e)

    resp = templates.TemplateResponse("index.html", {
        "request":        request,
        "items_json":     json.dumps(items, ensure_ascii=False),
        "fx_default":     fx_rate,
        "has_items":      len(portfolio) > 0,
        "stale_tickers":  json.dumps(stale_tickers),
        "needs_refresh":  json.dumps(needs_refresh),
        "sector_alloc":   json.dumps(sector_alloc, ensure_ascii=False),
        "error":          request.query_params.get("error", ""),
        "active":         "home",
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


# 포트폴리오 화면은 홈으로 통합했다. 기존 북마크/링크는 홈으로 넘긴다.
@app.get("/portfolio")
def page_portfolio():
    return RedirectResponse("/", status_code=307)


@app.get("/history", response_class=HTMLResponse)
def page_history(request: Request, db: Session = Depends(get_db)):
    today = _today_utc()
    needs_refresh = any(
        (lambda s: s is None or s.fetched_at is None or s.fetched_at.date() < today)(
            db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        )
        for p in db.query(models.Portfolio).all()
    )
    return templates.TemplateResponse("history.html", {
        "request":       request,
        "active":        "history",
        "needs_refresh": json.dumps(needs_refresh),
    })


@app.get("/kr-history", response_class=HTMLResponse)
def page_kr_history(request: Request, db: Session = Depends(get_db)):
    today = _today_utc()
    needs_refresh = any(
        (lambda s: s is None or s.fetched_at is None or s.fetched_at.date() < today)(
            db.query(models.KrStock).filter(models.KrStock.ticker == p.ticker).first()
        )
        for p in db.query(models.KrPortfolio).all()
    )
    return templates.TemplateResponse("kr_history.html", {
        "request":       request,
        "active":        "kr_history",
        "needs_refresh": json.dumps(needs_refresh),
    })


@app.get("/api/kr-history")
def api_kr_history(db: Session = Depends(get_db)):
    """국내 포트폴리오 월별 스냅샷 데이터 반환."""
    snapshots = (
        db.query(models.KrDailySnapshot)
        .order_by(models.KrDailySnapshot.snapshot_date)
        .all()
    )
    monthly: dict = {}
    for s in snapshots:
        month_key = s.snapshot_date.strftime("%Y-%m")
        monthly[month_key] = s

    result = []
    for month_key in sorted(monthly.keys()):
        s = monthly[month_key]
        result.append({
            "month":              month_key,
            "totalValueKrw":     s.total_value_krw     or 0,
            "monthlyNetKrw":     s.monthly_net_krw     or 0,
            "unrealizedGainKrw": s.unrealized_gain_krw or 0,
        })
    return result


# ════════════════════════════════════════════════════════════
# API 라우트 (JSON)
# ════════════════════════════════════════════════════════════

@app.get("/api/history")
def api_history(granularity: str = "monthly", db: Session = Depends(get_db)):
    """스냅샷 데이터 반환. granularity=monthly(기본, 각 월 마지막 스냅샷) 또는 daily(전체)."""
    snapshots = (
        db.query(models.DailySnapshot)
        .order_by(models.DailySnapshot.snapshot_date)
        .all()
    )
    if granularity == "daily":
        picked = [(s.snapshot_date.strftime("%Y-%m-%d"), s) for s in snapshots]
    else:
        # 월별 마지막 스냅샷만 (덮어쓰기)
        monthly: dict = {}
        for s in snapshots:
            monthly[s.snapshot_date.strftime("%Y-%m")] = s
        picked = sorted(monthly.items())

    result = []
    latest_fx = 0.0
    for key, s in picked:
        fx = s.fx_rate or 0
        if fx > 0:
            latest_fx = fx
        result.append({
            "month":              key,     # daily 모드에서는 YYYY-MM-DD
            "date":              s.snapshot_date.strftime("%Y-%m-%d"),
            "totalValueKrw":     s.total_value_krw     or 0,
            "monthlyRevenueKrw": s.monthly_revenue_krw or 0,
            "monthlyOpKrw":      s.monthly_op_krw      or 0,
            "monthlyNetKrw":     s.monthly_net_krw     or 0,
            "unrealizedGainKrw": s.unrealized_gain_krw or 0,
            "unrealizedGainUsd": s.unrealized_gain_usd or 0,
            "fxRate":            fx,
        })
    return {"latestFxRate": latest_fx, "snapshotCount": len(snapshots), "items": result}


@app.get("/api/stock/{ticker}/quick")
def api_get_stock_quick(ticker: str, db: Session = Depends(get_db)):
    """최신 1개 연도만 빠르게 반환. 캐시 있으면 캐시 사용, 없으면 yfinance quick 조회."""
    ticker = ticker.upper()
    stock  = db.query(models.Stock).filter(models.Stock.ticker == ticker).first()

    if stock and stock.fiscal_years:
        sd = stock_to_dict(stock)
        if sd["yearKeys"]:
            latest = sd["yearKeys"][-1]
            return {**sd,
                    "yearKeys":     [latest],
                    "fiscalData":   {latest: sd["fiscalData"][latest]},
                    "forecastKeys": [],
                    "quick":        True}

    # 캐시 없음 — fast 조회
    data = fetcher.fetch_stock_quick(ticker)
    if not data:
        raise HTTPException(status_code=404, detail=f"'{ticker}' 데이터를 찾을 수 없습니다.")

    if stock is None:
        stock = models.Stock(ticker=ticker)
        db.add(stock)
        db.flush()

    stock.name               = data["name"]
    stock.current_price      = data["current_price"]
    stock.shares_outstanding = data["shares_m"]
    stock.forecasts_json     = "[]"
    stock.fetched_at         = None   # full refresh 미완료 표시

    db.query(models.FiscalYear).filter(models.FiscalYear.ticker == ticker).delete()
    for y in data["years"]:
        db.add(models.FiscalYear(
            ticker=ticker, year_key=y["year_key"], label=y["label"],
            end_date=y.get("end_date"), revenue=y["revenue"],
            operating=y["operating"], net=y["net"], shares=y["shares"],
        ))
    db.commit()
    db.refresh(stock)
    sd = stock_to_dict(stock)
    return {**sd, "quick": True}


@app.get("/api/stock/{ticker}")
def api_get_stock(ticker: str, db: Session = Depends(get_db)):
    stock = get_or_refresh(ticker, db)
    if not stock or not stock.fiscal_years:
        raise HTTPException(status_code=404, detail=f"'{ticker.upper()}' 데이터를 찾을 수 없습니다.")
    return stock_to_dict(stock)


@app.post("/api/stock/{ticker}/refresh")
def api_refresh_stock(ticker: str, db: Session = Depends(get_db)):
    """강제 갱신 (캐시 무효화)."""
    stock = db.query(models.Stock).filter(models.Stock.ticker == ticker.upper()).first()
    if stock:
        stock.fetched_at = None
        db.commit()
    stock = get_or_refresh(ticker, db)
    if not stock or not stock.fiscal_years:
        raise HTTPException(status_code=404, detail=f"'{ticker.upper()}' 갱신 실패.")
    return stock_to_dict(stock)


@app.get("/api/fx")
def api_fx():
    return {"rate": fetcher.fetch_krw_rate()}


# ════════════════════════════════════════════════════════════
# 포트폴리오 폼 핸들러 (POST → Redirect)
# ════════════════════════════════════════════════════════════

@app.post("/portfolio/add")
def portfolio_add(
    ticker:      str   = Form(...),
    shares_owned: float = Form(...),
    avg_price:   float  = Form(0),
    memo:        str    = Form(""),
    next_url:    str    = Form(""),
    db: Session = Depends(get_db),
):
    # 추가한 화면(홈, /quality 등)으로 되돌아간다. 외부 URL은 허용하지 않음.
    dest = next_url if (next_url.startswith("/") and not next_url.startswith("//")) else "/"
    ticker = ticker.strip().upper()
    if not ticker:
        return RedirectResponse(f"{dest}?error=티커를+입력해주세요", status_code=303)

    existing = db.query(models.Portfolio).filter(models.Portfolio.ticker == ticker).first()
    is_new = existing is None
    if existing:
        existing.shares_owned = shares_owned
        existing.avg_price    = avg_price
        existing.memo         = memo
        existing.updated_at   = _now()
    else:
        max_order = db.query(models.Portfolio).count()
        db.add(models.Portfolio(
            ticker=ticker, shares_owned=shares_owned,
            avg_price=avg_price, memo=memo, display_order=max_order,
        ))
    db.commit()

    # 종목 데이터 캐시 — 신규 티커인데 데이터를 못 가져오면 롤백
    stock = get_or_refresh(ticker, db)
    if stock is None and is_new:
        db.query(models.Portfolio).filter(models.Portfolio.ticker == ticker).delete()
        db.commit()
        return RedirectResponse(f"{dest}?error={ticker}+종목을+찾을+수+없습니다", status_code=303)

    return RedirectResponse(dest, status_code=303)


@app.post("/portfolio/delete/{item_id}")
def portfolio_delete(item_id: int, next_url: str = Form(""), db: Session = Depends(get_db)):
    # 보유 목록에서만 제거한다. stocks의 분석 캐시는 남겨 과거 보유 이력에서 조회 가능.
    db.query(models.Portfolio).filter(models.Portfolio.id == item_id).delete()
    db.commit()
    dest = next_url if (next_url.startswith("/") and not next_url.startswith("//")) else "/"
    return RedirectResponse(dest, status_code=303)


# ════════════════════════════════════════════════════════════
# 국내 포트폴리오
# ════════════════════════════════════════════════════════════

def _is_kr_stale(stock: models.KrStock | None) -> bool:
    if stock is None or stock.fetched_at is None:
        return True
    return _now() - stock.fetched_at > timedelta(hours=CACHE_HOURS)


def get_or_refresh_kr(ticker: str, db: Session) -> models.KrStock | None:
    stock = db.query(models.KrStock).filter(models.KrStock.ticker == ticker).first()
    if not _is_kr_stale(stock):
        return stock
    # 전체 재무 데이터 조회 시도, 실패 시 현재가만 조회
    data = fetcher.fetch_kr_full_stock(ticker)
    if data is None:
        data = fetcher.fetch_kr_stock(ticker)
    if data is None:
        return stock
    if stock is None:
        stock = models.KrStock(ticker=data["ticker"])
        db.add(stock)
    stock.name          = data["name"]
    stock.current_price = data["current_price"]
    if "years" in data:
        stock.fiscal_json    = json.dumps(data["years"],     ensure_ascii=False)
        stock.forecasts_json = json.dumps(data["forecasts"], ensure_ascii=False)
    stock.fetched_at    = _now()
    db.commit()
    db.refresh(stock)
    return stock


def save_kr_daily_snapshot(db: Session) -> None:
    """오늘 국내 포트폴리오 스냅샷을 생성(없으면) 또는 갱신(있으면)."""
    from datetime import date as _date
    today = _date.today()

    portfolio = db.query(models.KrPortfolio).all()
    if not portfolio:
        return

    total_value_krw     = 0.0
    monthly_net_krw     = 0.0
    unrealized_gain_krw = 0.0

    for p in portfolio:
        stock = db.query(models.KrStock).filter(models.KrStock.ticker == p.ticker).first()
        if not stock:
            continue
        price  = stock.current_price or 0
        shares = p.shares_owned
        avg    = p.avg_price or 0

        total_value_krw += price * shares
        if avg > 0:
            unrealized_gain_krw += (price - avg) * shares

        fiscal = json.loads(stock.fiscal_json or "[]")
        if fiscal:
            fiscal.sort(key=lambda f: f.get("year_key", ""))
            latest = fiscal[-1]
            shares_m = latest.get("shares") or 1
            pct = shares / (shares_m * 1e6)
            monthly_net_krw += (latest.get("net") or 0) * 1e6 * pct / 12

    snap = db.query(models.KrDailySnapshot).filter(
        models.KrDailySnapshot.snapshot_date == today
    ).first()
    if snap is None:
        snap = models.KrDailySnapshot(snapshot_date=today)
        db.add(snap)

    snap.total_value_krw     = total_value_krw
    snap.monthly_net_krw     = monthly_net_krw
    snap.unrealized_gain_krw = unrealized_gain_krw
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("국내 스냅샷 저장 실패: %s", e)


@app.get("/kr-home", response_class=HTMLResponse)
def page_kr_home(request: Request, db: Session = Depends(get_db)):
    today = _today_utc()
    portfolio = (
        db.query(models.KrPortfolio)
        .order_by(models.KrPortfolio.display_order, models.KrPortfolio.created_at)
        .all()
    )

    needs_refresh = any(
        (lambda s: s is None or s.fetched_at is None or s.fetched_at.date() < today)(
            db.query(models.KrStock).filter(models.KrStock.ticker == p.ticker).first()
        )
        for p in portfolio
    )

    items = []
    stale_tickers = []
    for p in portfolio:
        stock = db.query(models.KrStock).filter(models.KrStock.ticker == p.ticker).first()
        if _is_kr_stale(stock):
            stale_tickers.append(p.ticker)

        fiscal   = json.loads(stock.fiscal_json    or "[]") if stock else []
        forecasts= json.loads(stock.forecasts_json or "[]") if stock else []
        fiscal.sort(key=lambda f: f.get("year_key",""))
        latest_fy   = fiscal[-1]  if fiscal    else None
        forecast_cur= forecasts[0] if forecasts else None
        forecast_nxt= forecasts[1] if len(forecasts) > 1 else None

        items.append({
            "ticker":        p.ticker,
            "name":          stock.name if stock else p.ticker,
            "shares_owned":  p.shares_owned,
            "avg_price":     p.avg_price or 0,
            "current_price": stock.current_price if stock else 0,
            "latest_fy":     latest_fy,
            "forecast_cur":  forecast_cur,
            "forecast_nxt":  forecast_nxt,
        })

    items.sort(key=lambda x: x["shares_owned"] * x["current_price"], reverse=True)

    if not needs_refresh and portfolio:
        try:
            save_kr_daily_snapshot(db)
        except Exception as e:
            logger.warning("국내 스냅샷 저장 실패: %s", e)

    resp = templates.TemplateResponse("kr_home.html", {
        "request":       request,
        "items_json":    json.dumps(items, ensure_ascii=False),
        "has_items":     len(portfolio) > 0,
        "stale_tickers": json.dumps(stale_tickers),
        "needs_refresh": json.dumps(needs_refresh),
        "active":        "kr_home",
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/kr-portfolio", response_class=HTMLResponse)
def page_kr_portfolio(request: Request, db: Session = Depends(get_db)):
    today = _today_utc()
    needs_refresh_kr = any(
        (lambda s: s is None or s.fetched_at is None or s.fetched_at.date() < today)(
            db.query(models.KrStock).filter(models.KrStock.ticker == p.ticker).first()
        )
        for p in db.query(models.KrPortfolio).all()
    )

    portfolio = (
        db.query(models.KrPortfolio)
        .order_by(models.KrPortfolio.display_order, models.KrPortfolio.created_at)
        .all()
    )
    items = []
    stale_tickers = []
    for p in portfolio:
        stock = db.query(models.KrStock).filter(models.KrStock.ticker == p.ticker).first()
        if _is_kr_stale(stock):
            stale_tickers.append(p.ticker)
        items.append({
            "id":            p.id,
            "ticker":        p.ticker,
            "name":          stock.name if stock else p.ticker,
            "shares_owned":  p.shares_owned,
            "avg_price":     p.avg_price or 0,
            "current_price": stock.current_price if stock else 0,
            "memo":          p.memo or "",
        })

    items.sort(key=lambda x: x["shares_owned"] * x["current_price"], reverse=True)

    resp = templates.TemplateResponse("kr_portfolio.html", {
        "request":       request,
        "items":         items,
        "active":        "kr_portfolio",
        "error":         request.query_params.get("error", ""),
        "stale_tickers": json.dumps(stale_tickers),
        "needs_refresh": json.dumps(needs_refresh_kr),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/kr-stock/{ticker}")
def api_kr_stock(ticker: str, db: Session = Depends(get_db)):
    stock = get_or_refresh_kr(ticker, db)
    if stock is None:
        raise HTTPException(status_code=404, detail="종목을 찾을 수 없습니다")
    return {"ticker": stock.ticker, "name": stock.name, "current_price": stock.current_price}


@app.get("/api/kr-refresh-prices")
def api_kr_refresh_prices(db: Session = Depends(get_db)):
    portfolio = db.query(models.KrPortfolio).all()
    for p in portfolio:
        get_or_refresh_kr(p.ticker, db)
    try:
        save_kr_daily_snapshot(db)
    except Exception as e:
        logger.warning("국내 스냅샷 저장 실패: %s", e)
    return {"ok": True}


@app.post("/kr-portfolio/add")
def kr_portfolio_add(
    ticker:       str   = Form(...),
    shares_owned: float = Form(...),
    avg_price:    float = Form(0),
    memo:         str   = Form(""),
    db: Session = Depends(get_db),
):
    ticker = ticker.strip().upper()
    if not ticker:
        return RedirectResponse("/kr-portfolio?error=티커를+입력해주세요", status_code=303)

    # .KS / .KQ 없이 숫자만 입력한 경우도 처리 — fetch에서 suffix 추가
    existing = db.query(models.KrPortfolio).filter(models.KrPortfolio.ticker == ticker).first()
    is_new = existing is None

    if existing:
        existing.shares_owned = shares_owned
        existing.avg_price    = avg_price
        existing.memo         = memo
        existing.updated_at   = _now()
        db.commit()
    else:
        # 실제 티커 확인 (suffix 결정)
        data = fetcher.fetch_kr_stock(ticker)
        if data is None:
            return RedirectResponse(f"/kr-portfolio?error={ticker}+종목을+찾을+수+없습니다", status_code=303)
        real_ticker = data["ticker"]

        # KrStock 업서트
        stock = db.query(models.KrStock).filter(models.KrStock.ticker == real_ticker).first()
        if stock is None:
            stock = models.KrStock(ticker=real_ticker)
            db.add(stock)
        stock.name          = data["name"]
        stock.current_price = data["current_price"]
        stock.fetched_at    = _now()

        max_order = db.query(models.KrPortfolio).count()
        db.add(models.KrPortfolio(
            ticker=real_ticker, shares_owned=shares_owned,
            avg_price=avg_price, memo=memo, display_order=max_order,
        ))
        db.commit()

    return RedirectResponse("/kr-portfolio", status_code=303)


@app.post("/kr-portfolio/delete/{item_id}")
def kr_portfolio_delete(item_id: int, db: Session = Depends(get_db)):
    db.query(models.KrPortfolio).filter(models.KrPortfolio.id == item_id).delete()
    db.commit()
    return RedirectResponse("/kr-portfolio", status_code=303)


# ════════════════════════════════════════════════════════════
# 자산관리
# ════════════════════════════════════════════════════════════

def _asset_to_dict(s: models.AssetSnapshot, custom_accounts: list = None) -> dict:
    pension     = (s.dc or 0) + (s.irp_miraeasset or 0) + (s.irp_samsung or 0) + (s.personal_pension or 0) + (s.pension_cma or 0)
    invest      = (s.isa or 0) + (s.miraeasset or 0) + (s.samsung_trading or 0) + (s.toss_securities or 0)
    savings     = (s.housing_subscription or 0) + (s.fixed_deposit or 0) + (s.hana_salary_savings or 0) + (s.hana_home_savings or 0)
    liquid      = (s.young_hana or 0) + (s.naverpay_hana or 0) + (s.shinhan or 0) + (s.toss_savings or 0)
    realestate  = 0
    loan        = s.hana_loan or 0

    # 커스텀 계좌 집계
    try:
        extra = json.loads(s.extra_json or '{}')
    except Exception:
        extra = {}

    if custom_accounts:
        for acc in custom_accounts:
            val = float(extra.get(str(acc.id), 0) or 0)
            if acc.category == 'pension':      pension     += val
            elif acc.category == 'invest':     invest      += val
            elif acc.category == 'savings':    savings     += val
            elif acc.category == 'liquid':     liquid      += val
            elif acc.category == 'realestate': realestate  += val
            elif acc.category == 'loan':       loan        += val

    total = pension + invest + savings + liquid + realestate - loan
    return {
        "id": s.id, "date": s.snapshot_date, "note": s.note or "",
        "dc": s.dc or 0, "irpMiraeasset": s.irp_miraeasset or 0,
        "irpSamsung": s.irp_samsung or 0, "personalPension": s.personal_pension or 0,
        "pensionCma": s.pension_cma or 0,
        "isa": s.isa or 0, "miraeasset": s.miraeasset or 0,
        "samsungTrading": s.samsung_trading or 0, "tossSecurities": s.toss_securities or 0,
        "hanaSalarySavings": s.hana_salary_savings or 0, "hanaHomeSavings": s.hana_home_savings or 0,
        "housingSubscription": s.housing_subscription or 0, "fixedDeposit": s.fixed_deposit or 0,
        "youngHana": s.young_hana or 0, "naverpayHana": s.naverpay_hana or 0,
        "shinhan": s.shinhan or 0, "tossSavings": s.toss_savings or 0,
        "hanaLoan": s.hana_loan or 0,
        "extra": extra,
        "pensionTotal": pension, "investTotal": invest,
        "savingsTotal": savings, "liquidTotal": liquid,
        "realestateTotal": realestate,
        "totalCapital": total,
    }



@app.get("/assets", response_class=HTMLResponse)
def page_assets(request: Request):
    return templates.TemplateResponse("assets.html", {"request": request, "active": "assets"})


@app.get("/goals", response_class=HTMLResponse)
def page_goals(request: Request):
    return templates.TemplateResponse("goals.html", {"request": request, "active": "goals"})


# ──────────────────────────────────────────────
#  청약
# ──────────────────────────────────────────────

def _cheongyak_to_dict(r: models.Cheongyak) -> dict:
    return {
        "id":            r.id,
        "name":          r.name,
        "region":        r.region or "",
        "supply_type":   r.supply_type or "일반공급",
        "price":         r.price or 0,
        "area_m2":       r.area_m2 or 0,
        "apply_start":   r.apply_start or "",
        "apply_end":     r.apply_end or "",
        "announce_date": r.announce_date or "",
        "move_in_date":  r.move_in_date or "",
        "competition":   r.competition or 0,
        "min_score":     r.min_score or 0,
        "status":        r.status or "관심",
        "memo":          r.memo or "",
    }


@app.get("/api/cheongyak")
def api_cheongyak_list(db: Session = Depends(get_db)):
    rows = db.query(models.Cheongyak).order_by(models.Cheongyak.apply_start, models.Cheongyak.id).all()
    return [_cheongyak_to_dict(r) for r in rows]


@app.post("/api/cheongyak")
async def api_cheongyak_create(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if not (body.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="단지명을 입력해주세요.")
    row = models.Cheongyak(
        name          = body["name"].strip(),
        region        = (body.get("region") or "").strip(),
        supply_type   = body.get("supply_type") or "일반공급",
        price         = float(body.get("price") or 0),
        area_m2       = float(body.get("area_m2") or 0),
        apply_start   = (body.get("apply_start") or "").strip(),
        apply_end     = (body.get("apply_end") or "").strip(),
        announce_date = (body.get("announce_date") or "").strip(),
        move_in_date  = (body.get("move_in_date") or "").strip(),
        competition   = float(body.get("competition") or 0),
        min_score     = int(body.get("min_score") or 0),
        status        = body.get("status") or "관심",
        memo          = (body.get("memo") or "").strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _cheongyak_to_dict(row)


@app.put("/api/cheongyak/{cid}")
async def api_cheongyak_update(cid: int, request: Request, db: Session = Depends(get_db)):
    row = db.query(models.Cheongyak).filter(models.Cheongyak.id == cid).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.json()
    row.name          = (body.get("name") or row.name).strip()
    row.region        = (body.get("region") or "").strip()
    row.supply_type   = body.get("supply_type") or row.supply_type
    row.price         = float(body.get("price") or 0)
    row.area_m2       = float(body.get("area_m2") or 0)
    row.apply_start   = (body.get("apply_start") or "").strip()
    row.apply_end     = (body.get("apply_end") or "").strip()
    row.announce_date = (body.get("announce_date") or "").strip()
    row.move_in_date  = (body.get("move_in_date") or "").strip()
    row.competition   = float(body.get("competition") or 0)
    row.min_score     = int(body.get("min_score") or 0)
    row.status        = body.get("status") or row.status
    row.memo          = (body.get("memo") or "").strip()
    db.commit()
    return _cheongyak_to_dict(row)


@app.delete("/api/cheongyak/{cid}")
def api_cheongyak_delete(cid: int, db: Session = Depends(get_db)):
    db.query(models.Cheongyak).filter(models.Cheongyak.id == cid).delete()
    db.commit()
    return {"ok": True}


# ──────────────────────────────────────────────
#  마일스톤
# ──────────────────────────────────────────────
@app.get("/milestones", response_class=HTMLResponse)
def page_milestones(request: Request):
    return templates.TemplateResponse("milestone.html", {"request": request, "active": "milestones"})


def _milestone_to_dict(m: models.Milestone) -> dict:
    return {
        "id": m.id, "title": m.title, "status": m.status,
        "category": m.category or "", "note": m.note or "",
        "date": m.milestone_date or "", "displayOrder": m.display_order,
        "image": m.image or "",
    }


@app.get("/api/milestones")
def api_milestones_list(db: Session = Depends(get_db)):
    rows = db.query(models.Milestone).order_by(
        models.Milestone.display_order, models.Milestone.id
    ).all()
    return [_milestone_to_dict(r) for r in rows]


@app.post("/api/milestones")
async def api_milestones_save(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    mid = body.get("id")
    if mid:
        row = db.query(models.Milestone).filter(models.Milestone.id == mid).first()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
    else:
        row = models.Milestone()
        max_order = db.query(models.Milestone).count()
        row.display_order = max_order
        db.add(row)
    row.title          = (body.get("title") or "").strip()
    row.status         = body.get("status") or "in_progress"
    row.category       = (body.get("category") or "").strip()
    row.note           = (body.get("note") or "").strip()
    row.milestone_date = (body.get("date") or "").strip() or None
    row.image          = (body.get("image") or "").strip()
    if not row.title:
        raise HTTPException(status_code=400, detail="제목을 입력해주세요.")
    db.commit()
    db.refresh(row)
    return _milestone_to_dict(row)


@app.delete("/api/milestones/{mid}")
def api_milestones_delete(mid: int, db: Session = Depends(get_db)):
    db.query(models.Milestone).filter(models.Milestone.id == mid).delete()
    db.commit()
    return {"ok": True}


@app.post("/api/milestones/upload-image")
async def api_milestones_upload_image(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        raise HTTPException(status_code=400, detail="이미지 파일만 업로드할 수 있습니다.")
    name = f"{uuid.uuid4().hex}{ext}"
    dest = MILESTONE_UPLOAD_DIR / name
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="이미지 용량은 8MB 이하로 업로드해주세요.")
    dest.write_bytes(data)
    return {"url": f"/resources/milestones/{name}"}


@app.post("/api/milestones/import-csv")
async def api_milestones_import(request: Request, db: Session = Depends(get_db)):
    import csv, io
    body = await request.json()
    content = body.get("csv", "")
    reader = csv.DictReader(io.StringIO(content))
    count = 0
    for i, row in enumerate(reader):
        title  = (row.get("인생 목표") or "").strip()
        status_raw = (row.get("Status") or "").strip()
        note   = (row.get("내용") or "").strip()
        date_raw = (row.get("완료된 날") or "").strip()
        if not title:
            continue
        status = "completed" if status_raw == "완료" else "in_progress"
        # 날짜 파싱: "2022년 5월 2일" → "2022-05-02"
        parsed_date = None
        import re
        m = re.match(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", date_raw)
        if m:
            parsed_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        existing = db.query(models.Milestone).filter(models.Milestone.title == title).first()
        if existing:
            existing.status = status
            existing.note   = note
            existing.milestone_date = parsed_date
        else:
            db.add(models.Milestone(
                title=title, status=status, note=note,
                milestone_date=parsed_date, display_order=i,
            ))
        count += 1
    db.commit()
    return {"imported": count}


# ──────────────────────────────────────────────
#  리서치 터미널 (/research)
# ──────────────────────────────────────────────
# 연간 재무는 분기마다 바뀌므로 24h가 아니라 7일 캐시를 쓴다.
# /research 페이지는 캐시만 읽고 절대 네트워크를 타지 않는다 (수집은 명시적 버튼).
ANALYSIS_CACHE_HOURS = 24 * 7
ANALYSIS_VER = 2   # fetcher.ANALYSIS_VER과 맞춘다


def _analysis_stale(stock: models.Stock | None) -> bool:
    if stock is None or not stock.analysis_json or stock.analysis_fetched_at is None:
        return True
    if (stock.analysis_ver or 0) < ANALYSIS_VER:
        return True
    return _now() - stock.analysis_fetched_at > timedelta(hours=ANALYSIS_CACHE_HOURS)


def refresh_analysis(ticker: str, db: Session) -> bool:
    """분석 데이터 수집 후 저장. 실패해도 기존 캐시는 보존한다."""
    ticker = ticker.upper()
    stock = db.query(models.Stock).filter(models.Stock.ticker == ticker).first()
    if stock is None:
        return False
    payload = fetcher.fetch_analysis(ticker)
    if payload is None:
        logger.warning("[%s] 분석 수집 실패 — 기존 캐시 유지", ticker)
        return False
    stock.analysis_json       = json.dumps(payload, ensure_ascii=False)
    stock.analysis_fetched_at = _now()
    stock.analysis_ver        = ANALYSIS_VER
    db.commit()
    return True


def _build_research_row(stock: models.Stock) -> dict:
    """캐시된 분석 데이터 → 화면용 행. 네트워크 호출 없음."""
    base = {
        "ticker": stock.ticker,
        "name":   stock.name or stock.ticker,
        "price":  stock.current_price or 0,
    }
    if not stock.analysis_json:
        base.update({"has_data": False,
                     "na_reason": "분석 데이터 미수집 — '데이터 수집'을 눌러주세요"})
        return base
    try:
        payload = json.loads(stock.analysis_json)
        forecasts = json.loads(stock.forecasts_json or "[]")
        metrics = analytics.analysis_to_dict(payload, forecasts)
        score = scoring.score_all(metrics)
    except Exception as e:
        logger.error("[%s] 분석 계산 실패: %s", stock.ticker, e, exc_info=True)
        base.update({"has_data": False, "na_reason": f"분석 계산 실패: {e}"})
        return base

    base.update({
        "has_data":   True,
        "industry":   metrics["industry"],
        "currency":   metrics["currency"],
        "as_of":      metrics["as_of"],
        "stale":      _analysis_stale(stock),
        "metrics":    metrics,
        "score":      score,
    })
    return base


@app.get("/research", response_class=HTMLResponse)
def page_research(request: Request, db: Session = Depends(get_db)):
    """보유 종목 비교 화면. 캐시만 읽는다 — 페이지 로드 시 yfinance 호출 없음."""
    holdings = (db.query(models.Portfolio)
                  .order_by(models.Portfolio.display_order, models.Portfolio.id).all())
    rows, uncached = [], []
    for h in holdings:
        stock = db.query(models.Stock).filter(models.Stock.ticker == h.ticker).first()
        if stock is None:
            continue
        row = _build_research_row(stock)
        row["shares_owned"] = h.shares_owned
        rows.append(row)
        if not row.get("has_data") or row.get("stale"):
            uncached.append(h.ticker)

    return templates.TemplateResponse("research.html", {
        "request":       request,
        "active":        "research",
        "research_json": json.dumps(rows, ensure_ascii=False, default=str),
        "uncached":      json.dumps(uncached),
        "weights_json":  json.dumps(scoring.WEIGHTS),
    })


@app.post("/api/research/refresh")
async def api_research_refresh(request: Request, db: Session = Depends(get_db)):
    """분석 데이터 수집. yfinance 레이트리밋을 피해 순차 처리 + 지연."""
    import asyncio
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    tickers = body.get("tickers")
    if not tickers:
        tickers = [h.ticker for h in db.query(models.Portfolio).all()]

    results = {}
    for i, t in enumerate(tickers):
        if i:
            await asyncio.sleep(0.5)
        try:
            results[t] = "ok" if refresh_analysis(t, db) else "fail"
        except Exception as e:
            logger.error("[%s] refresh 오류: %s", t, e)
            results[t] = "fail"
    return {"results": results}


# ──────────────────────────────────────────────
#  Portfolio Business Quality Dashboard (/quality)
#  - 캐시만 읽는다. 페이지 로드 시 네트워크 호출 없음 (수집은 /api/research/refresh).
#  - 보유 종목은 portfolio 테이블에서 읽는다. 코드에 종목 하드코딩 없음.
# ──────────────────────────────────────────────
NOTES_PATH = Path("data") / "company_notes.json"


def _load_notes() -> dict:
    """정성 분석 노트. 요청마다 읽어서 파일 편집이 새로고침만으로 반영된다."""
    try:
        with open(NOTES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k.upper(): v for k, v in data.items() if not k.startswith("_")}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error("분석 노트 로드 실패: %s", e)
        return {}


def _quality_company(stock, holding, notes):
    """캐시된 분석 데이터 → 기업 뷰모델. 데이터가 없으면 None."""
    if not stock or not stock.analysis_json:
        return None
    payload = json.loads(stock.analysis_json)
    forecasts = json.loads(stock.forecasts_json or "[]")
    metrics = analytics.analysis_to_dict(payload, forecasts)
    score = scoring.score_all(metrics)
    c = quality.build_company(stock, payload, metrics, score,
                              notes.get(stock.ticker.upper()), holding)
    c["stale"] = _analysis_stale(stock)
    c["payload_ver"] = payload.get("v", 1)
    return c


def _quality_portfolio(db: Session):
    notes = _load_notes()
    holdings = (db.query(models.Portfolio)
                  .order_by(models.Portfolio.display_order, models.Portfolio.id).all())
    held = {h.ticker for h in holdings}
    companies, pending = [], []
    for h in holdings:
        stock = db.query(models.Stock).filter(models.Stock.ticker == h.ticker).first()
        c = None
        try:
            c = _quality_company(stock, {"shares_owned": h.shares_owned}, notes)
        except Exception as e:
            logger.error("[%s] 품질 뷰모델 생성 실패: %s", h.ticker, e, exc_info=True)
        if c is None:
            pending.append({"ticker": h.ticker, "id": h.id,
                            "name": (stock.name if stock else h.ticker)})
        else:
            c["holding"]["id"] = h.id
            companies.append(c)
    built = quality.build_portfolio(companies)
    # 과거 보유 종목: 포트폴리오에서 빠졌지만 분석 캐시가 남아 있는 종목
    history = [{"ticker": s.ticker, "name": s.name,
                "fetched_at": s.analysis_fetched_at.strftime("%Y-%m-%d") if s.analysis_fetched_at else None}
               for s in db.query(models.Stock).filter(models.Stock.analysis_json.isnot(None)).all()
               if s.ticker not in held]
    return built, pending, history


@app.get("/quality", response_class=HTMLResponse)
def page_quality(request: Request, db: Session = Depends(get_db)):
    built, pending, history = _quality_portfolio(db)
    return templates.TemplateResponse("quality.html", {
        "request": request,
        "active":  "quality",
        "quality_json": json.dumps({**built, "pending": pending, "history": history},
                                   ensure_ascii=False, default=str),
        "error": request.query_params.get("error", ""),
    })


@app.get("/quality/{ticker}", response_class=HTMLResponse)
def page_quality_detail(ticker: str, request: Request, db: Session = Depends(get_db)):
    ticker = ticker.upper()
    built, _, _ = _quality_portfolio(db)
    company = next((c for c in built["companies"] if c["ticker"] == ticker), None)
    is_held = company is not None
    if company is None:
        # 과거 보유 종목 — 캐시가 남아 있으면 단독 기준으로 보여준다
        stock = db.query(models.Stock).filter(models.Stock.ticker == ticker).first()
        c = _quality_company(stock, None, _load_notes()) if stock else None
        if c is None:
            return RedirectResponse(f"/quality?error={ticker}+분석+데이터가+없습니다", status_code=303)
        company = quality.build_portfolio([c])["companies"][0]
    peers = [{"ticker": c["ticker"],
              "bq": c["scores"]["business_quality"]["score"],
              "val": c["scores"]["valuation"]["score"],
              "roic": c["roic"]["v"]} for c in built["companies"]]
    return templates.TemplateResponse("quality_detail.html", {
        "request": request,
        "active":  "quality",
        "ticker":  ticker,
        "detail_json": json.dumps({
            "company": company, "is_held": is_held, "peers": peers,
            "medians": built["medians"], "portfolio_quality": built["portfolio_quality"],
            "attr_weights": built["attr_weights"],
        }, ensure_ascii=False, default=str),
    })


@app.get("/api/settings/{key}")
def get_setting(key: str, db: Session = Depends(get_db)):
    s = db.query(models.AppSetting).filter(models.AppSetting.key == key).first()
    return {"value": s.value if s else ""}


@app.post("/api/settings/{key}")
async def set_setting(key: str, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    value = str(body.get("value", ""))
    s = db.query(models.AppSetting).filter(models.AppSetting.key == key).first()
    if s:
        s.value = value
    else:
        db.add(models.AppSetting(key=key, value=value))
    db.commit()
    return {"ok": True}


@app.get("/api/accounts")
def api_accounts_list(db: Session = Depends(get_db)):
    return [
        {"id": a.id, "name": a.name, "category": a.category, "displayOrder": a.display_order}
        for a in db.query(models.CustomAccount).order_by(
            models.CustomAccount.display_order, models.CustomAccount.id
        ).all()
    ]


@app.post("/api/accounts")
async def api_accounts_create(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    name = (body.get("name") or "").strip()
    category = (body.get("category") or "").strip()
    if not name or category not in ("pension", "invest", "savings", "liquid", "realestate", "loan"):
        raise HTTPException(status_code=400, detail="이름과 분류를 확인해주세요.")
    max_order = db.query(models.CustomAccount).count()
    acc = models.CustomAccount(name=name, category=category, display_order=max_order)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return {"id": acc.id, "name": acc.name, "category": acc.category, "displayOrder": acc.display_order}


@app.put("/api/accounts/{acc_id}")
async def api_accounts_update(acc_id: int, request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    acc = db.query(models.CustomAccount).filter(models.CustomAccount.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="계좌를 찾을 수 없습니다")
    if "name" in body:
        acc.name = body["name"].strip()
    if "display_order" in body:
        acc.display_order = int(body["display_order"])
    db.commit()
    return {"id": acc.id, "name": acc.name, "category": acc.category}


@app.delete("/api/accounts/{acc_id}")
def api_accounts_delete(acc_id: int, db: Session = Depends(get_db)):
    db.query(models.CustomAccount).filter(models.CustomAccount.id == acc_id).delete()
    db.commit()
    return {"ok": True}


@app.get("/api/assets")
def api_assets_list(db: Session = Depends(get_db)):
    custom_accounts = db.query(models.CustomAccount).all()
    rows = db.query(models.AssetSnapshot).order_by(models.AssetSnapshot.snapshot_date).all()
    return [_asset_to_dict(r, custom_accounts) for r in rows]


@app.post("/api/assets")
async def api_assets_save(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    date_str = (body.get("date") or "").strip()
    if not date_str:
        raise HTTPException(status_code=400, detail="날짜를 입력해주세요.")

    row = db.query(models.AssetSnapshot).filter(models.AssetSnapshot.snapshot_date == date_str).first()
    if row is None:
        row = models.AssetSnapshot(snapshot_date=date_str)
        db.add(row)

    row.dc                  = float(body.get("dc") or 0)
    row.irp_miraeasset      = float(body.get("irpMiraeasset") or 0)
    row.irp_samsung         = float(body.get("irpSamsung") or 0)
    row.personal_pension    = float(body.get("personalPension") or 0)
    row.pension_cma         = float(body.get("pensionCma") or 0)
    row.isa                 = float(body.get("isa") or 0)
    row.miraeasset          = float(body.get("miraeasset") or 0)
    row.samsung_trading     = float(body.get("samsungTrading") or 0)
    row.toss_securities     = float(body.get("tossSecurities") or 0)
    row.hana_salary_savings = float(body.get("hanaSalarySavings") or 0)
    row.hana_home_savings   = float(body.get("hanaHomeSavings") or 0)
    row.housing_subscription = float(body.get("housingSubscription") or 0)
    row.fixed_deposit       = float(body.get("fixedDeposit") or 0)
    row.young_hana          = float(body.get("youngHana") or 0)
    row.naverpay_hana       = float(body.get("naverpayHana") or 0)
    row.shinhan             = float(body.get("shinhan") or 0)
    row.toss_savings        = float(body.get("tossSavings") or 0)
    row.hana_loan           = float(body.get("hanaLoan") or 0)
    row.extra_json          = json.dumps(body.get("extra") or {}, ensure_ascii=False)
    row.note                = body.get("note") or ""
    db.commit()
    db.refresh(row)
    custom_accounts = db.query(models.CustomAccount).all()
    return _asset_to_dict(row, custom_accounts)


@app.delete("/api/assets/{row_id}")
def api_assets_delete(row_id: int, db: Session = Depends(get_db)):
    db.query(models.AssetSnapshot).filter(models.AssetSnapshot.id == row_id).delete()
    db.commit()
    return {"ok": True}


@app.post("/api/assets/import-csv")
async def api_assets_import_csv(request: Request, db: Session = Depends(get_db)):
    """CSV 텍스트를 받아 asset_snapshots에 일괄 upsert."""
    import csv, io, re

    body = await request.json()
    csv_text = body.get("csv", "")

    def parse_krw(s: str) -> float:
        s = (s or "").strip()
        if not s or s in ("시작 전", "-", "₩"):
            return 0.0
        s = re.sub(r"[₩,\s]", "", s)
        try:
            return float(s)
        except Exception:
            return 0.0

    reader = csv.DictReader(io.StringIO(csv_text))
    col = {
        "날짜": "날짜", "DC": "DC", "IRP(미레에셋)": "irp_miraeasset",
        "IRP(삼성)": "irp_samsung", "ISA": "isa",
        "Young하나통장": "young_hana", "개인연금(미레에셋)": "personal_pension",
        "급여 하나 월복리 적금": "hana_salary_savings",
        "내집마련더블업적금(하나)": "hana_home_savings",
        "네이버페이 머니 하나 통장": "naverpay_hana",
        "신한 주거래 우대통장": "shinhan",
        "연금 CMA(삼성)": "pension_cma",
        "정기예금합": "fixed_deposit",
        "종합(미래에셋증권)": "miraeasset",
        "종합매매(삼성증권)": "samsung_trading",
        "주택청약종합저축": "housing_subscription",
        "토스 자유입출금": "toss_savings",
        "토스증권": "toss_securities",
        "하나은행 대출": "hana_loan",
        "내역": "note",
    }

    imported = 0
    for row in reader:
        date_str = (row.get("날짜") or "").strip()
        if not date_str:
            continue
        snap = db.query(models.AssetSnapshot).filter(models.AssetSnapshot.snapshot_date == date_str).first()
        if snap is None:
            snap = models.AssetSnapshot(snapshot_date=date_str)
            db.add(snap)
        snap.dc                   = parse_krw(row.get("DC"))
        snap.irp_miraeasset       = parse_krw(row.get("IRP(미레에셋)"))
        snap.irp_samsung          = parse_krw(row.get("IRP(삼성)"))
        snap.personal_pension     = parse_krw(row.get("개인연금(미레에셋)"))
        snap.pension_cma          = parse_krw(row.get("연금 CMA(삼성)"))
        snap.isa                  = parse_krw(row.get("ISA"))
        snap.miraeasset           = parse_krw(row.get("종합(미래에셋증권)"))
        snap.samsung_trading      = parse_krw(row.get("종합매매(삼성증권)"))
        snap.toss_securities      = parse_krw(row.get("토스증권"))
        snap.hana_salary_savings  = parse_krw(row.get("급여 하나 월복리 적금"))
        snap.hana_home_savings    = parse_krw(row.get("내집마련더블업적금(하나)"))
        snap.housing_subscription = parse_krw(row.get("주택청약종합저축"))
        snap.fixed_deposit        = parse_krw(row.get("정기예금합"))
        snap.young_hana           = parse_krw(row.get("Young하나통장"))
        snap.naverpay_hana        = parse_krw(row.get("네이버페이 머니 하나 통장"))
        snap.shinhan              = parse_krw(row.get("신한 주거래 우대통장"))
        snap.toss_savings         = parse_krw(row.get("토스 자유입출금"))
        snap.hana_loan            = abs(parse_krw(row.get("하나은행 대출")))
        snap.note                 = (row.get("내역") or "").strip()
        imported += 1

    db.commit()
    return {"imported": imported}
