from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, UniqueConstraint, Date
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class Stock(Base):
    __tablename__ = "stocks"

    ticker              = Column(String(20), primary_key=True)
    name                = Column(String(200))
    shares_outstanding  = Column(Float, default=0)   # millions
    current_price       = Column(Float, default=0)   # USD
    fiscal_note         = Column(String(100))
    forecasts_json      = Column(Text, default="[]")   # JSON list of forecast dicts
    dividend_yield      = Column(Float, default=0)     # 배당율 (e.g. 0.025 = 2.5%)
    dividend_rate       = Column(Float, default=0)     # 연간 주당 배당금 USD
    market_cap          = Column(Float, default=0)     # 시가총액 USD
    trailing_pe         = Column(Float)                # TTM P/E
    pb_ratio            = Column(Float)                # TTM P/B
    trailing_roe        = Column(Float)                # TTM ROE
    trailing_eps        = Column(Float)                # TTM EPS
    fetched_at          = Column(DateTime)

    fiscal_years = relationship(
        "FiscalYear", back_populates="stock",
        cascade="all, delete-orphan",
        order_by="FiscalYear.year_key",
    )
    portfolio = relationship("Portfolio", back_populates="stock", uselist=False)


class FiscalYear(Base):
    __tablename__ = "fiscal_years"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    ticker      = Column(String(20), ForeignKey("stocks.ticker", ondelete="CASCADE"), nullable=False)
    year_key    = Column(String(10), nullable=False)   # e.g. "fy2024"
    label       = Column(String(10), nullable=False)   # e.g. "FY2024"
    end_date    = Column(String(10))                   # e.g. "2024-01" (YYYY-MM)
    revenue     = Column(Float, default=0)    # millions USD
    operating   = Column(Float, default=0)   # millions USD
    net         = Column(Float, default=0)   # millions USD
    shares      = Column(Float, default=0)   # millions shares
    eps         = Column(Float)              # USD per share
    roe         = Column(Float)              # ratio (e.g. 0.25 = 25%)
    roi         = Column(Float)              # ratio (net/assets)
    bvps        = Column(Float)              # book value per share (USD)

    stock = relationship("Stock", back_populates="fiscal_years")

    __table_args__ = (
        UniqueConstraint("ticker", "year_key", name="uq_ticker_year"),
    )


class Portfolio(Base):
    __tablename__ = "portfolio"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    ticker        = Column(String(20), ForeignKey("stocks.ticker"), unique=True, nullable=False)
    shares_owned  = Column(Float, nullable=False, default=0)
    avg_price     = Column(Float, default=0)    # USD
    memo          = Column(String(500), default="")
    display_order = Column(Integer, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    stock = relationship("Stock", back_populates="portfolio")


class DailySnapshot(Base):
    """포트폴리오 일별 스냅샷 — 히스토리 차트용"""
    __tablename__ = "daily_snapshots"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date       = Column(Date, unique=True, nullable=False)  # YYYY-MM-DD
    total_value_krw     = Column(Float, default=0)   # 포트폴리오 평가액 (KRW)
    monthly_revenue_krw = Column(Float, default=0)   # 지분비례 월 매출 (KRW)
    monthly_op_krw      = Column(Float, default=0)   # 지분비례 월 영업이익 (KRW)
    monthly_net_krw     = Column(Float, default=0)   # 지분비례 월 순이익 (KRW)
    unrealized_gain_krw = Column(Float, default=0)   # 평가이익 (KRW)
    unrealized_gain_usd = Column(Float, default=0)   # 평가이익 (USD)
    fx_rate             = Column(Float, default=0)   # 적용 환율
