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
            # stocks 테이블
        ]:
            try:
                conn.execute(text(f"ALTER TABLE fiscal_years ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass
        for col_def in [
            "forecasts_json TEXT",
        ]:
            try:
                conn.execute(text(f"ALTER TABLE stocks ADD COLUMN {col_def}"))
                conn.commit()
            except Exception:
                pass  # 이미 존재하면 무시
