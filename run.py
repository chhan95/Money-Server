"""
서버 실행: python run.py
브라우저에서 http://localhost:8000 접속
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
