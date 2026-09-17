# 💰 Money — 포트폴리오 주주 지분 소득 대시보드

보유 주식 수 기준으로 **지분비례 월 소득**을 계산하고 시각화하는 개인용 대시보드입니다.
Yahoo Finance 데이터를 자동 조회하며, 실시간 USD/KRW 환율을 적용합니다.

---

## 주요 기능

| 페이지 | 내용 |
|--------|------|
| **홈** | 포트폴리오 전체 요약, 섹터 비중, 연도별 월 소득 바 차트, 종목별 내역 테이블 |
| **포트폴리오** | 종목 추가·수정·삭제 관리 |
| **리서치 터미널** | 종목별 ROIC·재투자율·마진·멀티플 히스토리·WACC 등 심층 분석 (별도 캐시) |
| **Business Quality** | 사업 품질·밸류에이션 점수화, 포트폴리오 중앙값 대비 비교, 종목 상세 |
| **히스토리** | 월별/연별 포트폴리오 가치·소득 추이 |
| **자산 현황 / 부의 단계 / 마일스톤** | 전체 자산 스냅샷, 목표 단계, 이미지가 포함된 마일스톤 타임라인 |
| **국내 주식** | 홈·포트폴리오·히스토리 (KRX 종목) |

- 애널리스트 예상치(현재 FY / 내년 FY) 포함
- 라이트 / 다크 모드 토글 (브라우저 저장)
- 재무데이터 24시간 서버 캐시 (SQLite)

---

## 시작하기

### 1. 요구사항

- Python 3.11 이상

### 2. 설치

```bash
# 가상환경 생성 및 활성화
python -m venv venv
venv\Scripts\activate       # Windows
# source venv/bin/activate  # Mac/Linux

# 패키지 설치
pip install -r requirements.txt
```

### 3. 실행

```bash
python run.py
```

브라우저에서 [http://localhost:8000](http://localhost:8000) 접속

---

## 프로젝트 구조

```
Money/
├── main.py          # FastAPI 라우트 및 비즈니스 로직
├── fetcher.py       # Yahoo Finance 데이터 조회
├── analytics.py     # ROIC·재투자율·멀티플·WACC 계산 (리서치 터미널)
├── quality.py       # Business Quality 뷰모델 구성
├── scoring.py       # 사업 품질 / 밸류에이션 점수화
├── models.py        # SQLAlchemy ORM 모델
├── database.py      # DB 초기화
├── run.py           # 서버 실행 진입점
├── requirements.txt
├── data/company_notes.json   # 종목별 해자·리스크 메모
├── resources/       # 기업 로고, 업로드 이미지
├── static/
│   └── style.css    # 전체 디자인 시스템 (라이트/다크 모드)
└── templates/
    ├── base.html    # 공통 레이아웃 및 네비게이션
    ├── index.html   # 홈 (포트폴리오 요약)
    ├── research.html / quality.html / quality_detail.html
    ├── history.html / milestone.html / goals.html / assets.html
    └── portfolio.html
```

---

## 데이터 출처

- 재무제표·주가·환율: **Yahoo Finance** (yfinance)
- 갱신 주기: 24시간 캐시 후 자동 갱신
