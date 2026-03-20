"""
번들(.exe) 여부에 따라 리소스/데이터 경로를 반환하는 헬퍼.
- BUNDLE_DIR : templates/, static/ 등 읽기 전용 리소스 위치
- DATA_DIR   : money.db 등 사용자 데이터 위치 (exe와 같은 폴더)
"""
import sys
import os


def _bundle_dir() -> str:
    if getattr(sys, "frozen", False):   # PyInstaller 번들
        return sys._MEIPASS             # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)  # exe 옆 폴더
    return os.path.dirname(os.path.abspath(__file__))


BUNDLE_DIR = _bundle_dir()
DATA_DIR   = _data_dir()
