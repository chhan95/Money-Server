from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone, date
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
app.mount("/resources", StaticFiles(directory="resources"), name="resources")
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
        stock.fin_currency       = data.get("fin_currency", "USD")
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

@app.get("/api/refresh-prices")
def api_refresh_prices(db: Session = Depends(get_db)):
    """포트폴리오 종목 현재가 갱신 (하루 1회, 홈/포트폴리오 페이지용)."""
    today = date.today()
    portfolio = db.query(models.Portfolio).all()
    updated = []
    for p in portfolio:
        stock = db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        last_date = stock.fetched_at.date() if (stock and stock.fetched_at) else None
        if last_date is None or last_date < today:
            price = fetcher.fetch_current_price(p.ticker)
            if price and stock:
                stock.current_price = price
                stock.fetched_at = _now()
                db.commit()
                updated.append(p.ticker)
                logger.info("[%s] 현재가 갱신: %.2f", p.ticker, price)
    try:
        save_daily_snapshot(db)
    except Exception as e:
        logger.warning("스냅샷 저장 실패: %s", e)
    return {"updated": updated}


@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, db: Session = Depends(get_db)):
    portfolio = (
        db.query(models.Portfolio)
        .order_by(models.Portfolio.display_order, models.Portfolio.created_at)
        .all()
    )

    # 오늘 아직 갱신 안 된 종목이 있으면 클라이언트에서 로딩 화면 후 갱신
    today = date.today()
    needs_refresh = any(
        (lambda s: s is None or s.fetched_at is None or s.fetched_at.date() < today)(
            db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        )
        for p in portfolio
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

    items.sort(key=lambda x: x["shares_owned"] * x["current_price"], reverse=True)

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
        "active":         "home",
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    # 오늘 아직 갱신 안 된 종목이 있으면 클라이언트에서 로딩 화면 후 갱신
    today = date.today()
    needs_refresh_pf = any(
        (lambda s: s is None or s.fetched_at is None or s.fetched_at.date() < today)(
            db.query(models.Stock).filter(models.Stock.ticker == p.ticker).first()
        )
        for p in db.query(models.Portfolio).all()
    )

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

    try:
        fx_rate = fetcher.fetch_krw_rate()
    except Exception:
        fx_rate = 1380.0

    resp = templates.TemplateResponse("portfolio.html", {
        "request":        request,
        "items":          items,
        "active":         "portfolio",
        "fx_default":     fx_rate,
        "error":          request.query_params.get("error", ""),
        "stale_tickers":  json.dumps(stale_tickers),
        "needs_refresh":  json.dumps(needs_refresh_pf),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/history", response_class=HTMLResponse)
def page_history(request: Request, db: Session = Depends(get_db)):
    today = date.today()
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
    today = date.today()
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


R40_COLORS = [
    '#667eea', '#f59e0b', '#10b981', '#ef4444', '#e879f9',
    '#22d3ee', '#f97316', '#84cc16', '#ec4899', '#14b8a6',
]

R40_SAMPLE_DEFS = [
    {"ticker": "CRM",  "color": "#00A1E0"},   # Salesforce
    {"ticker": "PLTR", "color": "#1a1a1a"},   # Palantir
]
# 버전 키: SAMPLE_DEFS 변경 시 올려서 DB 재동기화
_R40_SAMPLE_VERSION = "v3"


def _r40_item_dict(r: models.Rule40Ticker) -> dict:
    return {
        "ticker":         r.ticker,
        "name":           r.name,
        "revenue_growth": r.revenue_growth,
        "profit_margin":  r.profit_margin,
        "score":          r.score,
        "color":          r.color,
        "fetched_at":     r.fetched_at.strftime("%Y-%m-%d") if r.fetched_at else None,
    }


def _ensure_r40_samples(db: Session):
    """SAMPLE_DEFS 버전 기준으로 동기화. 정의에서 제거된 샘플은 삭제, 새 샘플은 추가."""
    ver_key = f"r40_samples_seeded_{_R40_SAMPLE_VERSION}"
    if db.query(models.AppSetting).filter_by(key=ver_key).first():
        return

    current_tickers = {d["ticker"] for d in R40_SAMPLE_DEFS}

    # 더 이상 참고 종목이 아닌 샘플 삭제
    old_samples = db.query(models.Rule40Ticker).filter_by(is_sample=True).all()
    for r in old_samples:
        if r.ticker not in current_tickers:
            db.delete(r)

    # 새 샘플 추가
    for i, defn in enumerate(R40_SAMPLE_DEFS):
        existing = db.query(models.Rule40Ticker).filter_by(ticker=defn["ticker"]).first()
        if existing:
            existing.is_sample     = True
            existing.color         = defn["color"]
            existing.display_order = -(len(R40_SAMPLE_DEFS) - i)
        else:
            db.add(models.Rule40Ticker(
                ticker=defn["ticker"], color=defn["color"],
                is_sample=True, display_order=-(len(R40_SAMPLE_DEFS) - i),
            ))

    # 이전 버전 키 정리 후 새 버전 키 저장
    db.query(models.AppSetting).filter(
        models.AppSetting.key.like("r40_samples_seeded_%")
    ).delete(synchronize_session=False)
    db.add(models.AppSetting(key=ver_key, value="1"))
    db.commit()


@app.get("/rule-of-40", response_class=HTMLResponse)
def page_rule_of_40(request: Request, db: Session = Depends(get_db)):
    _ensure_r40_samples(db)

    portfolio = (
        db.query(models.Portfolio)
        .join(models.Stock, models.Portfolio.ticker == models.Stock.ticker, isouter=True)
        .order_by(models.Portfolio.display_order)
        .all()
    )
    all_items = (
        db.query(models.Rule40Ticker)
        .order_by(models.Rule40Ticker.display_order)
        .all()
    )

    cutoff = datetime.utcnow() - timedelta(hours=CACHE_HOURS)
    stale  = [r.ticker for r in all_items if r.fetched_at is None or r.fetched_at < cutoff]

    portfolio_tickers = [
        {"ticker": p.ticker, "name": (p.stock.name if p.stock else None) or p.ticker}
        for p in portfolio
    ]
    sample_tickers = {d["ticker"] for d in R40_SAMPLE_DEFS}
    r40_samples = [_r40_item_dict(r) for r in all_items if r.ticker in sample_tickers and r.revenue_growth is not None]
    r40_data    = [_r40_item_dict(r) for r in all_items if r.ticker not in sample_tickers and r.revenue_growth is not None]

    return templates.TemplateResponse("rule_of_40.html", {
        "request":           request,
        "active":            "rule_of_40",
        "portfolio_tickers": json.dumps(portfolio_tickers),
        "r40_samples":       json.dumps(r40_samples),
        "r40_data":          json.dumps(r40_data),
        "stale_r40":         json.dumps(stale),
    })


@app.post("/api/rule-of-40/add")
async def api_r40_add(
    request: Request,
    ticker: str = Form(...),
    db: Session = Depends(get_db),
):
    ticker = ticker.upper().strip()
    existing = db.query(models.Rule40Ticker).filter(models.Rule40Ticker.ticker == ticker).first()
    if existing and existing.revenue_growth is not None:
        return JSONResponse({"ok": True, "cached": True, "data": {
            "ticker":         existing.ticker,
            "name":           existing.name,
            "revenue_growth": existing.revenue_growth,
            "profit_margin":  existing.profit_margin,
            "score":          existing.score,
            "color":          existing.color,
            "fetched_at":     existing.fetched_at.strftime("%Y-%m-%d") if existing.fetched_at else None,
        }})

    data = fetcher.fetch_rule_of_40(ticker)
    if not data:
        raise HTTPException(status_code=400, detail=f"'{ticker}' 데이터를 가져올 수 없습니다. 티커를 확인해주세요.")

    count = db.query(models.Rule40Ticker).count()
    color = R40_COLORS[count % len(R40_COLORS)]

    if existing:
        existing.name           = data["name"]
        existing.revenue_growth = data["revenue_growth"]
        existing.profit_margin  = data["profit_margin"]
        existing.score          = data["score"]
        existing.fetched_at     = datetime.utcnow()
        item = existing
    else:
        item = models.Rule40Ticker(
            ticker=ticker,
            name=data["name"],
            revenue_growth=data["revenue_growth"],
            profit_margin=data["profit_margin"],
            score=data["score"],
            color=color,
            fetched_at=datetime.utcnow(),
            display_order=count,
        )
        db.add(item)
    db.commit()

    return JSONResponse({"ok": True, "cached": False, "data": {
        "ticker":         item.ticker,
        "name":           item.name,
        "revenue_growth": item.revenue_growth,
        "profit_margin":  item.profit_margin,
        "score":          item.score,
        "color":          item.color,
        "fetched_at":     item.fetched_at.strftime("%Y-%m-%d") if item.fetched_at else None,
    }})


@app.delete("/api/rule-of-40/{ticker}")
def api_r40_delete(ticker: str, db: Session = Depends(get_db)):
    item = db.query(models.Rule40Ticker).filter(
        models.Rule40Ticker.ticker == ticker.upper()
    ).first()
    if item:
        db.delete(item)
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/rule-of-40/refresh/{ticker}")
def api_r40_refresh(ticker: str, db: Session = Depends(get_db)):
    item = db.query(models.Rule40Ticker).filter(
        models.Rule40Ticker.ticker == ticker.upper()
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="종목을 찾을 수 없습니다.")

    data = fetcher.fetch_rule_of_40(ticker)
    if not data:
        raise HTTPException(status_code=400, detail="데이터를 가져올 수 없습니다.")

    item.name           = data["name"]
    item.revenue_growth = data["revenue_growth"]
    item.profit_margin  = data["profit_margin"]
    item.score          = data["score"]
    item.fetched_at     = datetime.utcnow()
    db.commit()

    return JSONResponse({"ok": True, "data": {
        "ticker":         item.ticker,
        "name":           item.name,
        "revenue_growth": item.revenue_growth,
        "profit_margin":  item.profit_margin,
        "score":          item.score,
        "color":          item.color,
        "fetched_at":     item.fetched_at.strftime("%Y-%m-%d") if item.fetched_at else None,
    }})


# ════════════════════════════════════════════════════════════
# 국내 Rule of 40
# ════════════════════════════════════════════════════════════

KR_R40_COLORS = [
    '#667eea', '#f59e0b', '#10b981', '#ef4444', '#e879f9',
    '#22d3ee', '#f97316', '#84cc16', '#ec4899', '#14b8a6',
]

KR_R40_SAMPLE_DEFS = [
    {"ticker": "035420.KS", "color": "#03C75A"},   # NAVER
    {"ticker": "035720.KS", "color": "#FFE500"},   # Kakao
]


def _kr_r40_item_dict(r: models.KrRule40Ticker) -> dict:
    return {
        "ticker":         r.ticker,
        "name":           r.name,
        "revenue_growth": r.revenue_growth,
        "profit_margin":  r.profit_margin,
        "score":          r.score,
        "color":          r.color,
        "fetched_at":     r.fetched_at.strftime("%Y-%m-%d") if r.fetched_at else None,
    }


def _ensure_kr_r40_samples(db: Session):
    """최초 1회만 샘플 시딩. 이후 사용자가 삭제하면 복원하지 않음."""
    seeded = db.query(models.AppSetting).filter_by(key="kr_r40_samples_seeded").first()
    if seeded:
        return
    for i, defn in enumerate(KR_R40_SAMPLE_DEFS):
        if not db.query(models.KrRule40Ticker).filter_by(ticker=defn["ticker"]).first():
            db.add(models.KrRule40Ticker(
                ticker=defn["ticker"], color=defn["color"],
                is_sample=True, display_order=-(len(KR_R40_SAMPLE_DEFS) - i),
            ))
    db.add(models.AppSetting(key="kr_r40_samples_seeded", value="1"))
    db.commit()


@app.get("/kr-rule-of-40", response_class=HTMLResponse)
def page_kr_rule_of_40(request: Request, db: Session = Depends(get_db)):
    _ensure_kr_r40_samples(db)

    kr_portfolio = (
        db.query(models.KrPortfolio)
        .order_by(models.KrPortfolio.display_order)
        .all()
    )
    all_items = (
        db.query(models.KrRule40Ticker)
        .order_by(models.KrRule40Ticker.display_order)
        .all()
    )

    cutoff = datetime.utcnow() - timedelta(hours=CACHE_HOURS)
    stale  = [r.ticker for r in all_items if r.fetched_at is None or r.fetched_at < cutoff]

    portfolio_tickers = [
        {"ticker": p.ticker, "name": (p.stock.name if p.stock else None) or p.ticker}
        for p in kr_portfolio
    ]
    sample_tickers = {d["ticker"] for d in KR_R40_SAMPLE_DEFS}
    r40_samples = [_kr_r40_item_dict(r) for r in all_items if r.ticker in sample_tickers and r.revenue_growth is not None]
    r40_data    = [_kr_r40_item_dict(r) for r in all_items if r.ticker not in sample_tickers and r.revenue_growth is not None]

    return templates.TemplateResponse("kr_rule_of_40.html", {
        "request":           request,
        "active":            "kr_rule_of_40",
        "portfolio_tickers": json.dumps(portfolio_tickers),
        "r40_samples":       json.dumps(r40_samples),
        "r40_data":          json.dumps(r40_data),
        "stale_r40":         json.dumps(stale),
    })


@app.post("/api/kr-rule-of-40/add")
async def api_kr_r40_add(
    request: Request,
    ticker: str = Form(...),
    db: Session = Depends(get_db),
):
    raw = ticker.strip()
    # suffix 결정: 이미 .KS/.KQ가 붙었으면 그대로, 아니면 fetch가 결정
    existing_by_raw = (
        db.query(models.KrRule40Ticker)
        .filter(models.KrRule40Ticker.ticker == raw)
        .first()
        or db.query(models.KrRule40Ticker)
        .filter(models.KrRule40Ticker.ticker == raw + ".KS")
        .first()
        or db.query(models.KrRule40Ticker)
        .filter(models.KrRule40Ticker.ticker == raw + ".KQ")
        .first()
    )
    if existing_by_raw and existing_by_raw.revenue_growth is not None:
        return JSONResponse({"ok": True, "cached": True, "data": _kr_r40_item_dict(existing_by_raw)})

    data = fetcher.fetch_kr_rule_of_40(raw)
    if not data:
        raise HTTPException(status_code=400, detail=f"'{raw}' 데이터를 가져올 수 없습니다. 종목코드를 확인해주세요.")

    real_ticker = data["ticker"]
    count = db.query(models.KrRule40Ticker).count()
    color = KR_R40_COLORS[count % len(KR_R40_COLORS)]

    existing = db.query(models.KrRule40Ticker).filter(models.KrRule40Ticker.ticker == real_ticker).first()
    if existing:
        existing.name           = data["name"]
        existing.revenue_growth = data["revenue_growth"]
        existing.profit_margin  = data["profit_margin"]
        existing.score          = data["score"]
        existing.fetched_at     = datetime.utcnow()
        item = existing
    else:
        item = models.KrRule40Ticker(
            ticker=real_ticker,
            name=data["name"],
            revenue_growth=data["revenue_growth"],
            profit_margin=data["profit_margin"],
            score=data["score"],
            color=color,
            fetched_at=datetime.utcnow(),
            display_order=count,
        )
        db.add(item)
    db.commit()

    return JSONResponse({"ok": True, "cached": False, "data": _kr_r40_item_dict(item)})


@app.delete("/api/kr-rule-of-40/{ticker:path}")
def api_kr_r40_delete(ticker: str, db: Session = Depends(get_db)):
    item = db.query(models.KrRule40Ticker).filter(
        models.KrRule40Ticker.ticker == ticker
    ).first()
    if item:
        db.delete(item)
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/kr-rule-of-40/refresh/{ticker:path}")
def api_kr_r40_refresh(ticker: str, db: Session = Depends(get_db)):
    item = db.query(models.KrRule40Ticker).filter(
        models.KrRule40Ticker.ticker == ticker
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="종목을 찾을 수 없습니다.")

    data = fetcher.fetch_kr_rule_of_40(ticker)
    if not data:
        raise HTTPException(status_code=400, detail="데이터를 가져올 수 없습니다.")

    item.name           = data["name"]
    item.revenue_growth = data["revenue_growth"]
    item.profit_margin  = data["profit_margin"]
    item.score          = data["score"]
    item.fetched_at     = datetime.utcnow()
    db.commit()

    return JSONResponse({"ok": True, "data": _kr_r40_item_dict(item)})


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
        return RedirectResponse(f"/portfolio?error={ticker}+종목을+찾을+수+없습니다", status_code=303)

    return RedirectResponse("/portfolio", status_code=303)


@app.post("/portfolio/delete/{item_id}")
def portfolio_delete(item_id: int, db: Session = Depends(get_db)):
    db.query(models.Portfolio).filter(models.Portfolio.id == item_id).delete()
    db.commit()
    return RedirectResponse("/portfolio", status_code=303)


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
    today = date.today()
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
    today = date.today()
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
    loan        = s.hana_loan or 0
    realestate  = 0.0

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
            elif acc.category == 'loan':       loan        += val
            elif acc.category == 'realestate': realestate  += val

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


# ──────────────────────────────────────────────
#  부동산
# ──────────────────────────────────────────────

@app.get("/realestate", response_class=HTMLResponse)
def page_realestate(request: Request):
    return templates.TemplateResponse("realestate.html", {"request": request, "active": "realestate"})


def _realestate_to_dict(r: models.RealEstate) -> dict:
    return {
        "id":             r.id,
        "name":           r.name,
        "contract_type":  r.contract_type or "sale",
        "property_type":  r.property_type or "아파트",
        "purchase_price": r.purchase_price or 0,
        "current_value":  r.current_value or 0,
        "loan_amount":    r.loan_amount or 0,
        "purchase_date":  r.purchase_date or "",
        "rent_type":      r.rent_type or "전세",
        "deposit":        r.deposit or 0,
        "deposit_loan":   r.deposit_loan or 0,
        "monthly_rent":   r.monthly_rent or 0,
        "contract_start": r.contract_start or "",
        "contract_end":   r.contract_end or "",
        "address":        r.address or "",
        "area_m2":        r.area_m2 or 0,
        "memo":           r.memo or "",
        "display_order":  r.display_order or 0,
    }


def _apply_realestate_body(row: models.RealEstate, body: dict):
    row.name          = (body.get("name") or "").strip()
    row.contract_type = body.get("contract_type") or "sale"
    row.address       = (body.get("address") or "").strip()
    row.area_m2       = float(body.get("area_m2") or 0)
    row.memo          = (body.get("memo") or "").strip()
    if row.contract_type == "sale":
        row.property_type  = (body.get("property_type") or "아파트").strip()
        row.purchase_price = float(body.get("purchase_price") or 0)
        row.current_value  = float(body.get("current_value") or 0)
        row.loan_amount    = float(body.get("loan_amount") or 0)
        row.purchase_date  = (body.get("purchase_date") or "").strip()
        row.rent_type = ""; row.deposit = 0; row.deposit_loan = 0; row.monthly_rent = 0
        row.contract_start = ""; row.contract_end = ""
    else:
        row.rent_type      = body.get("rent_type") or "전세"
        row.deposit        = float(body.get("deposit") or 0)
        row.deposit_loan   = float(body.get("deposit_loan") or 0)
        row.monthly_rent   = float(body.get("monthly_rent") or 0)
        row.contract_start = (body.get("contract_start") or "").strip()
        row.contract_end   = (body.get("contract_end") or "").strip()
        row.property_type  = ""; row.purchase_price = 0
        row.current_value  = 0; row.loan_amount = 0; row.purchase_date = ""


@app.get("/api/realestate")
def api_realestate_list(db: Session = Depends(get_db)):
    rows = db.query(models.RealEstate).order_by(
        models.RealEstate.display_order, models.RealEstate.id
    ).all()
    return [_realestate_to_dict(r) for r in rows]


@app.post("/api/realestate")
async def api_realestate_create(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    if not (body.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="매물명을 입력해주세요.")
    row = models.RealEstate(display_order=db.query(models.RealEstate).count())
    _apply_realestate_body(row, body)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _realestate_to_dict(row)


@app.put("/api/realestate/{rid}")
async def api_realestate_update(rid: int, request: Request, db: Session = Depends(get_db)):
    row = db.query(models.RealEstate).filter(models.RealEstate.id == rid).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.json()
    if not (body.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="매물명을 입력해주세요.")
    _apply_realestate_body(row, body)
    db.commit()
    db.refresh(row)
    return _realestate_to_dict(row)


@app.delete("/api/realestate/{rid}")
def api_realestate_delete(rid: int, db: Session = Depends(get_db)):
    db.query(models.RealEstate).filter(models.RealEstate.id == rid).delete()
    db.commit()
    return {"ok": True}


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
