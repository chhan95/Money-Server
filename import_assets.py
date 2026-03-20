"""CSV → asset_snapshots DB 임포트 (일회성 스크립트)"""
import csv, re, os, sys

# 프로젝트 루트 기준 실행
sys.path.insert(0, os.path.dirname(__file__))
import database, models

database.create_tables()

CSV_PATH = os.path.join(os.path.dirname(__file__),
                        "총 자본 3449145befda4c93bbd8f6acc4d1858b_all.csv")

def parse_krw(s: str) -> float:
    s = (s or "").strip().strip('"')
    if not s or s in ("시작 전", "-", "₩", "Status"):
        return 0.0
    s = re.sub(r"[₩,\s]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0

db = database.SessionLocal()
try:
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        imported = updated = 0
        for row in reader:
            date_str = (row.get("날짜") or "").strip()
            if not date_str:
                continue

            snap = db.query(models.AssetSnapshot).filter(
                models.AssetSnapshot.snapshot_date == date_str
            ).first()
            is_new = snap is None
            if is_new:
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

            if is_new:
                imported += 1
            else:
                updated += 1

        db.commit()
        print(f"완료: 신규 {imported}건, 업데이트 {updated}건")

        # 검증: 마지막 행 확인
        last = db.query(models.AssetSnapshot).order_by(
            models.AssetSnapshot.snapshot_date.desc()
        ).first()
        if last:
            pension = (last.dc or 0) + (last.irp_miraeasset or 0) + (last.irp_samsung or 0) + (last.personal_pension or 0) + (last.pension_cma or 0)
            invest  = (last.isa or 0) + (last.miraeasset or 0) + (last.samsung_trading or 0) + (last.toss_securities or 0)
            savings = last.housing_subscription or 0
            liquid  = (last.young_hana or 0) + (last.naverpay_hana or 0) + (last.shinhan or 0) + (last.toss_savings or 0)
            loan    = last.hana_loan or 0
            total   = pension + invest + savings + liquid - loan
            print(f"\n[검증] 최신 날짜: {last.snapshot_date}")
            print(f"  연금합: {pension:,.0f}")
            print(f"  투자합: {invest:,.0f}")
            print(f"  저축합: {savings:,.0f}")
            print(f"  자유입출금합: {liquid:,.0f}")
            print(f"  대출: -{loan:,.0f}")
            print(f"  총 자본: {total:,.0f}")
            print(f"  (CSV 기준 총 자본: 393,738,983)")
finally:
    db.close()
