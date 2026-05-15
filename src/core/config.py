"""
Central configuration management using Pydantic Settings.
Supports .env, environment variables, and runtime overrides.
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, validator, field_validator


class Settings(BaseSettings):
    """Application settings with validation and secrets management."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    # App
    APP_NAME: str = "GhanaGoetheBooking"
    ENVIRONMENT: str = Field("development", pattern="^(development|staging|production)$")
    DEBUG: bool = False
    SECRET_KEY: SecretStr = Field(..., min_length=32)
    API_V1_PREFIX: str = "/api/v1"
    WORKERS_COUNT: int = Field(4, ge=1, le=16)
    CORS_ORIGINS: List[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    ALLOWED_HOSTS: List[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    
    # Database
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: SecretStr = Field(...)
    POSTGRES_DB: str = "ghana_goethe_booking"
    DATABASE_URL: Optional[str] = None
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 40
    DB_POOL_TIMEOUT: int = 30
    
    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_url(cls, v: Optional[str], info) -> str:
        if v:
            return v
        values = info.data
        return f"postgresql://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD'].get_secret_value()}@{values['POSTGRES_HOST']}:{values['POSTGRES_PORT']}/{values['POSTGRES_DB']}"
    
    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[SecretStr] = None
    REDIS_DB: int = 0
    REDIS_URL: Optional[str] = None
    REDIS_MAX_CONNECTIONS: int = 50
    
    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def assemble_redis_url(cls, v: Optional[str], info) -> str:
        if v:
            return v
        values = info.data
        password = f":{values['REDIS_PASSWORD'].get_secret_value()}@" if values.get('REDIS_PASSWORD') else ""
        return f"redis://{password}{values['REDIS_HOST']}:{values['REDIS_PORT']}/{values['REDIS_DB']}"
    
    # Admin Auth
    ADMIN_EMAIL: str
    ADMIN_PASSWORD_HASH: str  # bcrypt hash
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_SECRET_KEY: Optional[SecretStr] = None
    
    @field_validator("JWT_SECRET_KEY", mode="before")
    @classmethod
    def set_jwt_secret(cls, v: Optional[SecretStr], info) -> SecretStr:
        if v:
            return v
        return info.data["SECRET_KEY"]
    
    # Playwright Automation
    PLAYWRIGHT_HEADLESS: bool = True
    PLAYWRIGHT_TIMEOUT: int = 30000  # milliseconds
    BROWSER_TYPE: str = "chromium"  # chromium, firefox, webkit
    BROWSER_MAX_CONCURRENT: int = 5
    BROWSER_CONTEXTS_PER_SESSION: int = 3
    BROWSER_ARGS: List[str] = Field(default_factory=lambda: [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-accelerated-2d-canvas",
        "--disable-gpu"
    ])
    NAVIGATION_TIMEOUT: int = 60000
    SCREENSHOT_ON_FAILURE: bool = True
    
    # Proxy
    PROXY_ENABLED: bool = False
    PROXY_LIST_URL: Optional[str] = None
    PROXY_ROTATION_STRATEGY: str = "round_robin"  # round_robin, random, least_used
    PROXY_HEALTH_CHECK_INTERVAL: int = 300
    PROXY_AUTH_USER: Optional[str] = None
    PROXY_AUTH_PASS: Optional[SecretStr] = None
    PROXY_TIMEOUT: int = 30
    PROXY_MAX_FAILURES: int = 3
    
    # Retry Engine
    RETRY_MAX_ATTEMPTS: int = 5
    RETRY_BACKOFF_FACTOR: float = 2.0
    RETRY_MAX_DELAY: int = 60
    RETRY_ON_STATUS_CODES: List[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])
    RETRY_EXCEPTIONS: List[str] = Field(default_factory=lambda: [
        "TimeoutError", "ConnectionError", "NetworkError", "PlaywrightError"
    ])
    
    # Telegram
    TELEGRAM_BOT_TOKEN: Optional[SecretStr] = None
    TELEGRAM_CHAT_ID: Optional[str] = None
    NOTIFICATIONS_ENABLED: bool = True
    TELEGRAM_PARSE_MODE: str = "HTML"
    
    # Task Queue
    TASK_QUEUE_MAX_WORKERS: int = 10
    TASK_QUEUE_POLL_INTERVAL: float = 1.0
    TASK_RETRY_DELAY_SECONDS: int = 5
    TASK_MAX_RETRIES: int = 3
    
    # Monitoring
    LOG_LEVEL: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    LOG_FORMAT: str = "json"  # json or text
    SENTRY_DSN: Optional[str] = None
    METRICS_ENABLED: bool = True
    
    # Booking specific
    GOETHE_BASE_URL: str = "https://www.goethe.de/ins/gh/en/kur/ange.html"
    BOOKING_MAX_RETRIES: int = 10
    BOOKING_RETRY_DELAY: int = 30
    BOOKING_SLOT_CHECK_INTERVAL: int = 60
    CACHE_BOOKING_SLOTS_TTL: int = 300  # seconds
    
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"
    
    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"


@lru_cache()
def get_settings() -> Settings:
    """Cache settings for performance."""
    return Settings()


settings = get_settings()