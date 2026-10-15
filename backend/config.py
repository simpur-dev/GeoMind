"""GeoMind 全局配置。

默认值全部对齐本机实测口径（见 docs/SeekDB环境实测报告.md / 开发进度.md）：
  · SeekDB = Docker 容器，127.0.0.1:2881，root，空密码，必须 --ssl-mode=DISABLED
  · 向量维度 1024（BGE-m3），HNSW cosine
  · 网格：原点 (103.85, 30.45)，边长 0.01 度，50 列 × 40 行

环境变量前缀 GEOMIND_，可用 .env 覆盖（.env.example 有全部项）。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。所有默认值可直接跑通本机生产库 geomind。"""

    model_config = SettingsConfigDict(
        env_prefix="GEOMIND_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- L6 OceanBase SeekDB ----
    ob_host: str = "127.0.0.1"
    ob_port: int = 2881
    ob_user: str = "root"
    ob_password: str = ""
    ob_database: str = "geomind"
    ob_charset: str = "utf8mb4"
    ob_pool_size: int = Field(default=4, ge=1, le=32)
    ob_connect_timeout_s: int = 5
    ob_read_timeout_s: int = 10

    # ---- 网格规格（与 backend/db/migrations/001_init_schema.sql 注释一致） ----
    grid_origin_lon: float = 103.85
    grid_origin_lat: float = 30.45
    grid_cell_deg: float = 0.01
    grid_cols: int = 50
    grid_rows: int = 40

    # ---- 检索默认值 ----
    default_limit: int = Field(default=10, ge=1, le=200)
    max_limit: int = Field(default=200, ge=1)
    dry_run_timeout_s: float = 3.0

    # ---- Embedding ----
    embedding_provider: str = "synthetic"  # synthetic | bge
    embedding_dim: int = 1024

    # ---- LLM 网关 ----
    llm_provider: str = "fake"  # fake | doubao | deepseek
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    intent_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # ---- 高德网关（无 key 时所有调用降级为库内兜底，不产生外部请求） ----
    amap_key: str = ""
    amap_cache_ttl_s: int = 86400

    # ---- 场景打分权重（site_selection，可解释可复现） ----
    score_weight_semantic: float = 0.35
    score_weight_traffic: float = 0.30
    score_weight_rating: float = 0.25
    score_weight_competition: float = 0.10

    def grid_id_of(self, lon: float, lat: float) -> int:
        """坐标 → 网格 id。与 ETL seed_cd.py 同一公式（round 到 6 位，避免边界错位）。"""
        lon, lat = round(lon, 6), round(lat, 6)
        col = int((lon - self.grid_origin_lon) / self.grid_cell_deg)
        row = int((lat - self.grid_origin_lat) / self.grid_cell_deg)
        col = min(max(col, 0), self.grid_cols - 1)
        row = min(max(row, 0), self.grid_rows - 1)
        return row * self.grid_cols + col + 1

    def grid_ids_in_radius(self, lon: float, lat: float, radius_m: float) -> list[int]:
        """中心 + 半径（米）→ 覆盖的网格 id 列表（升序，去重）。

        半径换算：1 度 ≈ 111 320 m（本机实测口径，docs/压测报告-10万级.md）。
        """
        delta = radius_m / 111_320.0
        x0 = int((lon - delta - self.grid_origin_lon) / self.grid_cell_deg)
        x1 = int((lon + delta - self.grid_origin_lon) / self.grid_cell_deg)
        y0 = int((lat - delta - self.grid_origin_lat) / self.grid_cell_deg)
        y1 = int((lat + delta - self.grid_origin_lat) / self.grid_cell_deg)
        x0 = max(x0, 0)
        x1 = min(x1, self.grid_cols - 1)
        y0 = max(y0, 0)
        y1 = min(y1, self.grid_rows - 1)
        return sorted({r * self.grid_cols + c + 1
                       for r in range(y0, y1 + 1) for c in range(x0, x1 + 1)})


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试可用 get_settings.cache_clear() 重置。"""
    return Settings()
