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


# ════════════════════════════════════════════════════════════
# 자산관리
# ════════════════════════════════════════════════════════════

def _asset_to_dict(s: models.AssetSnapshot, custom_accounts: list = None) -> dict:
    pension = (s.dc or 0) + (s.irp_miraeasset or 0) + (s.irp_samsung or 0) + (s.personal_pension or 0) + (s.pension_cma or 0)
    invest  = (s.isa or 0) + (s.miraeasset or 0) + (s.samsung_trading or 0) + (s.toss_securities or 0)
    # 저축합 = 주택청약종합저축만 (CSV 기준 — 급여적금·내집마련·정기예금합은 집계 미포함)
    savings = s.housing_subscription or 0
    liquid  = (s.young_hana or 0) + (s.naverpay_hana or 0) + (s.shinhan or 0) + (s.toss_savings or 0)
    loan    = s.hana_loan or 0

    # 커스텀 계좌 집계
    try:
        extra = json.loads(s.extra_json or '{}')
    except Exception:
        extra = {}

    if custom_accounts:
        for acc in custom_accounts:
            val = float(extra.get(str(acc.id), 0) or 0)
            if acc.category == 'pension':  pension  += val
            elif acc.category == 'invest': invest   += val
            elif acc.category == 'savings': savings += val
            elif acc.category == 'liquid': liquid   += val
            elif acc.category == 'loan':   loan     += val

    total = pension + invest + savings + liquid - loan
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
        "totalCapital": total,
    }


@app.get("/assets", response_class=HTMLResponse)
def page_assets(request: Request):
    return templates.TemplateResponse("assets.html", {"request": request, "active": "assets"})


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
    if not name or category not in ("pension", "invest", "savings", "liquid", "loan"):
        raise HTTPException(status_code=400, detail="이름과 분류를 확인해주세요.")
    max_order = db.query(models.CustomAccount).count()
    acc = models.CustomAccount(name=name, category=category, display_order=max_order)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return {"id": acc.id, "name": acc.name, "category": acc.category, "displayOrder": acc.display_order}


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
