"""Purpose: Provides the config application module.

Used by: Imported during FastAPI startup and backend runtime.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongodb_uri: str
    mongodb_database: str = "flexicore_incident_rag_prototype"

    # Comma-separated for deployment platforms such as Cloudflare Tunnel/Pages.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"
    ollama_timeout_seconds: int = 120
    agentic_retrieval_enabled: bool = True
    agentic_retrieval_max_queries: int = 3
    embedding_provider: str = "local"
    embedding_local_files_only: bool = True
    huggingface_api_token: str | None = None
    huggingface_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    huggingface_embedding_timeout_seconds: int = 30
    hipo_profile_short_circuit_enabled: bool = False

    gemini_agent_enabled: bool = True
    gemini_api_key: str | None = None
    gemini_agent_model: str = "gemini-3.7-flash"
    gemini_agent_max_retries: int = 2
    gemini_agent_timeout_seconds: int = 45
    gemini_agent_cooldown_seconds: int = 60
    gemini_agent_redact_pii: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """Return normalized browser origins accepted by the API."""

        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
