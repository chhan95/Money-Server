# 💰 Money — 포트폴리오 주주 지분 소득 대시보드

보유 주식 수 기준으로 **지분비례 월 소득**을 계산하고 시각화하는 개인용 대시보드입니다.
Yahoo Finance 데이터를 자동 조회하며, 실시간 USD/KRW 환율을 적용합니다.

---

## 주요 기능

| 페이지 | 내용 |
|--------|------|
| **홈** | 포트폴리오 전체 요약, 파이 차트, 연도별 월 소득 바 차트, 종목별 내역 테이블 |
| **계산기** | 개별 종목 지분비례 소득 계산, FY 탭 전환, ROE·ROA·EPS 지표, 연도별 비교 차트 |
| **포트폴리오** | 종목 추가·수정·삭제 관리 |

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
├── models.py        # SQLAlchemy ORM 모델
├── database.py      # DB 초기화
├── run.py           # 서버 실행 진입점
├── requirements.txt
├── static/
│   └── style.css    # 전체 디자인 시스템 (라이트/다크 모드)
└── templates/
    ├── base.html    # 공통 레이아웃 및 네비게이션
    ├── index.html   # 홈 (포트폴리오 요약)
    ├── calculator.html
    └── portfolio.html
```

---

## 데이터 출처

- 재무제표·주가·환율: **Yahoo Finance** (yfinance)
- 갱신 주기: 24시간 캐시 후 자동 갱신
