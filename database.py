from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = "sqlite:///./money.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_tables():
    Base.metadata.create_all(bind=engine)
    # 컬럼 추가 마이그레이션 (SQLite는 ALTER TABLE ADD COLUMN만 지원)
    with engine.connect() as conn:
        from sqlalchemy import text
        for col_def in [
            "end_date VARCHAR(10)",
            "eps FLOAT",
            "roe FLOAT",
            "roi FLOAT",
            "bvps FLOAT",
        ]:
            try:
                conn.execute(text(f"ALTER TABLE fiscal_years ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass
        for col_def in [
            "forecasts_json TEXT",
            "dividend_yield FLOAT",
            "dividend_rate FLOAT",
            "market_cap FLOAT",
            "trailing_pe FLOAT",
            "pb_ratio FLOAT",
            "trailing_roe FLOAT",
            "trailing_eps FLOAT",
        ]:
            try:
                conn.execute(text(f"ALTER TABLE stocks ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass  # 이미 존재하면 무시
        # divi_ver 컬럼 추가 (배당율 단위 마이그레이션 버전 관리)
        try:
            conn.execute(text("ALTER TABLE stocks ADD COLUMN divi_ver INT DEFAULT 0"))
            conn.commit()
        except Exception:
            pass
        # 신규 TTM 필드가 없는 캐시 → 한 번만 무효화
        try:
            conn.execute(text(
                "UPDATE stocks SET fetched_at = NULL "
                "WHERE (trailing_roe IS NULL OR market_cap IS NULL OR market_cap = 0) "
                "AND fetched_at IS NOT NULL"
            ))
            conn.commit()
        except Exception:
            pass
        # 배당율 단위 오류 수정 — rate/price로 잘못 계산된 캐시 재조회 (일회성)
        try:
            conn.execute(text(
                "UPDATE stocks SET fetched_at = NULL, divi_ver = 1 "
                "WHERE divi_ver = 0 OR divi_ver IS NULL"
            ))
            conn.commit()
        except Exception:
            pass
        # portfolio avg_price 컬럼
        try:
            conn.execute(text("ALTER TABLE portfolio ADD COLUMN avg_price FLOAT DEFAULT 0"))
            conn.commit()
        except Exception:
            pass
        # daily_snapshots unrealized_gain_usd 컬럼
        try:
            conn.execute(text("ALTER TABLE daily_snapshots ADD COLUMN unrealized_gain_usd FLOAT DEFAULT 0"))
            conn.commit()
        except Exception:
            pass
        # 재무제표 통화 컬럼 (비USD 종목 환산 지원)
        try:
            conn.execute(text("ALTER TABLE stocks ADD COLUMN fin_currency VARCHAR(10)"))
            conn.commit()
        except Exception:
            pass
        # fin_currency 미설정 종목 → 재조회 (TWD 등 비USD 환산 적용)
        try:
            conn.execute(text(
                "UPDATE stocks SET fetched_at = NULL "
                "WHERE fin_currency IS NULL AND fetched_at IS NOT NULL"
            ))
            conn.commit()
        except Exception:
            pass
        # app_settings 테이블 — create_all로 자동 생성됨 (별도 마이그레이션 불필요)
        # asset_snapshots 테이블 생성은 create_all로 자동 처리됨
        # extra_json 컬럼 추가 (커스텀 계좌 값)
        try:
            conn.execute(text("ALTER TABLE asset_snapshots ADD COLUMN extra_json TEXT DEFAULT '{}'"))
            conn.commit()
        except Exception:
            pass
        # milestones category 컬럼 추가
        try:
            conn.execute(text("ALTER TABLE milestones ADD COLUMN category VARCHAR(50) DEFAULT ''"))
            conn.commit()
        except Exception:
            pass
