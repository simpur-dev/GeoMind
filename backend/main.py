"""FastAPI 入口（REST + SSE）。

    python -m backend.main            # 127.0.0.1:8000
    uvicorn backend.main:app --reload
"""
from __future__ import annotations

from backend.api.routes import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)
