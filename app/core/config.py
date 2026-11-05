"""
Clutch (ক্ল্যাচ) - Global Configuration & Environment Settings
Pydantic-Settings schema managing environment variables, security secrets,
and courier/messaging credentials with safe sandbox defaults.
"""

from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and/or .env file.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime Environment
    ENVIRONMENT: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Application lifecycle environment",
    )
    API_PORT: int = Field(
        default=8000,
        description="Uvicorn HTTP server port",
    )

    # Meta Graph Webhooks (Messenger / Instagram Direct)
    META_VERIFY_TOKEN: str = Field(
        default="clutch_verify_token",
        description="Verification token for Meta Webhook setup challenge",
    )
    META_APP_SECRET: str = Field(
        default="clutch_dev_secret",
        description="Meta App Secret for HMAC-SHA256 signature verification",
    )
    META_PAGE_ACCESS_TOKEN: str = Field(
        default="",
        description="Page Access Token for outbound Messenger Graph API messages",
    )

    # WhatsApp Cloud API
    WHATSAPP_API_TOKEN: str = Field(
        default="test_whatsapp_token",
        description="Permanent or system user access token for WhatsApp Cloud API",
    )
    WHATSAPP_PHONE_NUMBER_ID: str = Field(
        default="1234567890",
        description="WhatsApp Business Account Phone Number ID",
    )

    # Steadfast Courier Credentials
    STEADFAST_API_KEY: str = Field(
        default="test",
        description="Steadfast Courier API Key (use 'test' for mock sandbox dispatch)",
    )
    STEADFAST_SECRET_KEY: str = Field(
        default="test",
        description="Steadfast Courier Secret Key (use 'test' for mock sandbox dispatch)",
    )
    STEADFAST_BASE_URL: str = Field(
        default="https://portal.steadfast.com.bd/api/v1",
        description="Steadfast API endpoint URL",
    )

    # Redis Cache & Session Debounce Buffer
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection string for multi-message buffering & locks",
    )

    # Invoicing & Branding
    INVOICE_DIR: str = Field(
        default="invoices",
        description="Filesystem directory where generated PDF invoices are saved",
    )
    BRAND_NAME: str = Field(
        default="Clutch Fashion",
        description="Merchant brand name displayed on customer invoices and summaries",
    )


# Export singleton instance
settings = Settings()

