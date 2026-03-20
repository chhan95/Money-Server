"""
PyInstaller 빌드 진입점.
서버를 시작하고 브라우저를 자동으로 엽니다.
"""
import sys
import os
import threading
import time
import webbrowser

# ── SSL 인증서 경로 설정 (번들 exe에서 certifi 경로 수동 지정) ──
import certifi
os.environ.setdefault("SSL_CERT_FILE",       certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE",  certifi.where())
os.environ.setdefault("CURL_CA_BUNDLE",      certifi.where())

HOST = "127.0.0.1"
PORT = 8000


def _open_browser():
    time.sleep(2.5)
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()

    import uvicorn
    from main import app          # noqa: E402

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
