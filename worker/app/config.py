"""Process-wide settings, loaded from env vars by pydantic-settings.

Both the api and worker-gs containers import this. Environment vars
are documented in `.env.example` at the repo root.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    data_dir: Path = Field(default=Path("/data"), validation_alias="DATA_DIR")
    models_dir: Path = Field(default=Path("/models"), validation_alias="MODELS_DIR")

    db_filename: str = Field(default="studio.sqlite")

    log_level: str = Field(default="info", validation_alias="LOG_LEVEL")

    capture_max_frames: int = Field(default=2000, validation_alias="GS_CAPTURE_MAX_FRAMES")
    capture_jpeg_quality: int = Field(default=85, validation_alias="GS_CAPTURE_JPEG_QUALITY")
    pair_token_ttl_seconds: int = Field(default=600, validation_alias="GS_PAIR_TOKEN_TTL_SECONDS")

    train_iters: int = Field(default=15000, validation_alias="GS_TRAIN_ITERS")
    sfm_backend: str = Field(default="glomap", validation_alias="GS_SFM_BACKEND")

    # Splatfacto's FullImagesDataManager auto-falls-back to ``cpu``
    # for datasets > ~500 images to avoid OOM. On a 24GB+ GPU
    # (e.g. an RTX 4090) we have plenty of headroom for typical
    # phone captures, so default to forcing ``gpu`` and let the
    # user flip to ``cpu`` via env if their card is smaller. The
    # value passes through to the ns-train command as
    # ``--pipeline.datamanager.cache-images <value>``.
    train_cache_images: str = Field(
        default="gpu", validation_alias="GS_TRAIN_CACHE_IMAGES"
    )

    worker_class: str = Field(default="gs", validation_alias="WORKER_CLASS")
    api_base_url: str = Field(default="http://api:8000", validation_alias="API_BASE_URL")

    cors_origins: list[str] = Field(default=["*"], validation_alias="CORS_ORIGINS")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / self.db_filename}"

    @property
    def db_url_sync(self) -> str:
        return f"sqlite:///{self.data_dir / self.db_filename}"

    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    def scenes_dir(self) -> Path:
        return self.data_dir / "scenes"


@lru_cache
def get_settings() -> Settings:
    return Settings()
