from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from typing import Optional
import json, logging

import models, database, fetcher
from database import get_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 초기화 ────────────────────────────────────────────────────────────────────
database.create_tables()

app = FastAPI(title="💰 Money Dashboard", docs_url="/docs")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

CACHE_HOURS = 24


# ════════════════════════════════════════════════════════════
# CRUD 헬퍼
# ════════════════════════════════════════════════════════════

def _now() -> datetime:
    """SQLite naive datetime과 비교 가능한 UTC now."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

        monthly_revenue_usd += (latest.revenue  or 0) * pct / 12
        monthly_op_usd      += (latest.operating or 0) * pct / 12
        monthly_net_usd     += (latest.net       or 0) * pct / 12

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

@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, db: Session = Depends(get_db)):
    portfolio = (
        db.query(models.Portfolio)
        .order_by(models.Portfolio.display_order, models.Portfolio.created_at)
        .all()
    )

    items = []
    stale_tickers = []
    for p in portfolio:
        stock = db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
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
                "fiscal_data":   sd["fiscalData"],
                "year_keys":     sd["yearKeys"],
                "forecast_keys": sd.get("forecastKeys", []),
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
                "fiscal_data":   {},
                "year_keys":     [],
                "forecast_keys": [],
            })

    try:
        fx_rate = fetcher.fetch_krw_rate()
    except Exception:
        fx_rate = 1380.0

    # 오늘 스냅샷 저장 (최신 가격 기준)
    try:
        save_daily_snapshot(db)
    except Exception as e:
        logger.warning("스냅샷 저장 실패: %s", e)

    return templates.TemplateResponse("index.html", {
        "request":       request,
        "items_json":    json.dumps(items, ensure_ascii=False),
        "fx_default":    fx_rate,
        "has_items":     len(portfolio) > 0,
        "stale_tickers": json.dumps(stale_tickers),
        "active":        "home",
    })


@app.get("/calculator", response_class=HTMLResponse)
def page_calculator(
    request: Request,
    ticker: str = "NVDA",
    db: Session = Depends(get_db),
):
    ticker = ticker.upper()
    stock = db.query(models.Stock).filter(models.Stock.ticker == ticker).first()
    stock_dict = stock_to_dict(stock) if (stock and stock.fiscal_years) else None

    try:
        fx_rate = fetcher.fetch_krw_rate()
    except Exception:
        fx_rate = 1380.0

    return templates.TemplateResponse("calculator.html", {
        "request":    request,
        "ticker":     ticker,
        "stock_json": json.dumps(stock_dict, ensure_ascii=False),
        "fx_default": fx_rate,
        "is_stale":   json.dumps(_is_stale(stock)),
        "active":     "calculator",
    })


@app.get("/portfolio", response_class=HTMLResponse)
def page_portfolio(request: Request, db: Session = Depends(get_db)):
    portfolio = (
        db.query(models.Portfolio)
        .order_by(models.Portfolio.display_order, models.Portfolio.created_at)
        .all()
    )
    items = []
    for p in portfolio:
        stock = db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        items.append({
            "id":            p.id,
            "ticker":        p.ticker,
            "name":          stock.name if stock else p.ticker,
            "shares_owned":  p.shares_owned,
            "avg_price":     p.avg_price or 0,
            "current_price": stock.current_price if stock else 0,
            "memo":          p.memo or "",
        })

    return templates.TemplateResponse("portfolio.html", {
        "request": request,
        "items":   items,
        "active":  "portfolio",
    })


@app.get("/history", response_class=HTMLResponse)
def page_history(request: Request):
    return templates.TemplateResponse("history.html", {
        "request": request,
        "active":  "history",
    })


# ════════════════════════════════════════════════════════════
# API 라우트 (JSON)
# ════════════════════════════════════════════════════════════

@app.get("/api/history")
def api_history(db: Session = Depends(get_db)):
    """월별 스냅샷 데이터 반환 (각 월의 마지막 스냅샷 기준)."""
    snapshots = (
        db.query(models.DailySnapshot)
        .order_by(models.DailySnapshot.snapshot_date)
        .all()
    )
    # 월별 마지막 스냅샷만 (덮어쓰기)
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
            "monthlyRevenueKrw": s.monthly_revenue_krw or 0,
            "monthlyOpKrw":      s.monthly_op_krw      or 0,
            "monthlyNetKrw":     s.monthly_net_krw     or 0,
            "unrealizedGainKrw": s.unrealized_gain_krw or 0,
            "unrealizedGainUsd": s.unrealized_gain_usd or 0,
        })
    return result


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
    db: Session = Depends(get_db),
):
    ticker = ticker.strip().upper()
    if not ticker:
        return RedirectResponse("/portfolio?error=티커를+입력해주세요", status_code=303)

    existing = db.query(models.Portfolio).filter(models.Portfolio.ticker == ticker).first()
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

    # 종목 데이터 미리 캐시
    get_or_refresh(ticker, db)

    return RedirectResponse("/portfolio", status_code=303)


@app.post("/portfolio/delete/{item_id}")
def portfolio_delete(item_id: int, db: Session = Depends(get_db)):
    db.query(models.Portfolio).filter(models.Portfolio.id == item_id).delete()
    db.commit()
    return RedirectResponse("/portfolio", status_code=303)
