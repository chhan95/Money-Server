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
    fin_currency        = Column(String(10))               # 재무제표 통화 (e.g. "USD", "TWD")
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


class Milestone(Base):
    """인생 마일스톤"""
    __tablename__ = "milestones"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    title         = Column(String(200), nullable=False)
    status        = Column(String(20), default='in_progress')  # completed / in_progress
    category      = Column(String(50), default='')  # 카테고리
    note          = Column(String(500), default='')
    milestone_date = Column(String(20))   # 완료일 or 목표일 (YYYY-MM-DD)
    display_order = Column(Integer, default=0)


class RealEstate(Base):
    """보유 부동산"""
    __tablename__ = "real_estate"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    name           = Column(String(200), nullable=False)        # 매물명
    contract_type  = Column(String(10), default='sale')         # sale(매매) / rent(임대차)

    # 매매 전용
    property_type  = Column(String(50), default='아파트')       # 아파트/빌라/오피스텔/토지/상가/기타
    purchase_price = Column(Float, default=0)                   # 매입가 (만원)
    current_value  = Column(Float, default=0)                   # 현재 시세 (만원)
    loan_amount    = Column(Float, default=0)                   # 대출금 (만원)
    purchase_date  = Column(String(10), default='')             # YYYY-MM-DD

    # 임대차 전용
    rent_type      = Column(String(10), default='전세')         # 전세 / 월세
    deposit        = Column(Float, default=0)                   # 보증금 (만원)
    deposit_loan   = Column(Float, default=0)                   # 보증금 대출 (만원)
    monthly_rent   = Column(Float, default=0)                   # 월세 (만원, 전세=0)
    contract_start = Column(String(10), default='')             # 계약 시작일
    contract_end   = Column(String(10), default='')             # 계약 종료일

    # 공통
    address        = Column(String(500), default='')
    area_m2        = Column(Float, default=0)                   # 전용면적 m²
    memo           = Column(String(500), default='')
    display_order  = Column(Integer, default=0)
    created_at     = Column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    """앱 설정 키-값 저장소"""
    __tablename__ = "app_settings"
    key   = Column(String(100), primary_key=True)
    value = Column(Text, default='')


class CustomAccount(Base):
    """사용자 정의 계좌 (built-in 외 추가 통장)"""
    __tablename__ = "custom_accounts"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    name          = Column(String(100), nullable=False)
    category      = Column(String(20),  nullable=False)  # pension/invest/savings/liquid/loan
    display_order = Column(Integer, default=0)


class AssetSnapshot(Base):
    """자산관리 월별 스냅샷"""
    __tablename__ = "asset_snapshots"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date   = Column(String(10), unique=True, nullable=False, index=True)  # YYYY/MM/DD

    # 연금
    dc              = Column(Float, default=0)   # 퇴직연금 DC
    irp_miraeasset  = Column(Float, default=0)   # IRP(미래에셋)
    irp_samsung     = Column(Float, default=0)   # IRP(삼성)
    personal_pension = Column(Float, default=0)  # 개인연금(미래에셋)
    pension_cma     = Column(Float, default=0)   # 연금 CMA(삼성)

    # 투자
    isa             = Column(Float, default=0)   # ISA
    miraeasset      = Column(Float, default=0)   # 종합(미래에셋증권)
    samsung_trading = Column(Float, default=0)   # 종합매매(삼성증권)
    toss_securities = Column(Float, default=0)   # 토스증권

    # 저축
    hana_salary_savings  = Column(Float, default=0)  # 급여 하나 월복리 적금
    hana_home_savings    = Column(Float, default=0)  # 내집마련더블업적금(하나)
    housing_subscription = Column(Float, default=0)  # 주택청약종합저축
    fixed_deposit        = Column(Float, default=0)  # 정기예금합

    # 자유입출금
    young_hana    = Column(Float, default=0)  # Young하나통장
    naverpay_hana = Column(Float, default=0)  # 네이버페이 머니 하나 통장
    shinhan       = Column(Float, default=0)  # 신한 주거래 우대통장
    toss_savings  = Column(Float, default=0)  # 토스 자유입출금

    # 대출 (양수로 저장)
    hana_loan = Column(Float, default=0)  # 하나은행 대출

    # 커스텀 계좌 값 {"account_id": value, ...}
    extra_json = Column(Text, default='{}')

    note = Column(String(500), default="")
