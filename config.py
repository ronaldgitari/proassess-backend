from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    APP_NAME: str = "ProAssess"
    APP_ENV: str = "development"
    SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://proassess:proassess@localhost:5432/proassess"
    DATABASE_URL_SYNC: str = "postgresql://proassess:proassess@localhost:5432/proassess"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_CHAT_MODEL: str = "gpt-4o"
    OPENAI_GRADER_MODEL: str = "gpt-4o-mini"   # cheap, deterministic context grading (reflection loop)
    OPENAI_FEEDBACK_MODEL: str = "gpt-4o-mini"  # scenario/case-study draft feedback (an LM reviews it, so mini is enough)

    # RAG-quality scorer (RAGAS-style faithfulness + context precision). Uses a Qwen
    # model via any OpenAI-compatible endpoint (Alibaba DashScope, OpenRouter, Together,
    # local vLLM/Ollama). Put the key in .env as RAG_SCORER_API_KEY (see base_url below).
    RAG_SCORER_MODEL: str = "qwen-plus"
    RAG_SCORER_BASE_URL: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    RAG_SCORER_API_KEY: str = ""
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-large"
    OPENAI_EMBEDDING_DIMENSIONS: int = 3072
    OPENAI_MAX_TOKENS: int = 8192
    OPENAI_TEMPERATURE: float = 0.2

    # Chroma
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8001
    CHROMA_COLLECTION: str = "proassess_docs"

    # RAG
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 64
    TOP_K_RETRIEVAL: int = 20
    TOP_K_FINAL: int = 10
    RERANKER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    MAX_REGRADE: int = 2   # hard cap on grade→re-query reflection loops (bounded latency/cost)
    # Cap on assessments generating at once (per worker). A burst of concurrent
    # "create" actions otherwise contends on embeddings/retrieval, thinning the
    # context the grader sees → spurious insufficient_context failures.
    MAX_CONCURRENT_GENERATIONS: int = 3

    # Self-correcting evaluation (Maker → Checker → Router loop)
    ENABLE_SELF_CORRECTING_EVAL: bool = True
    EVAL_MAKER_MODEL: str = "gpt-4o-mini"
    EVAL_CHECKER_MODEL: str = "gpt-4o"
    MAX_EVAL_ATTEMPTS: int = 3

    # Web research for rich scenario feedback (case-study assessments).
    # Provider plug-in for credible external sources cited in AI feedback.
    # Supported: "" (disabled → grounded-only feedback) | "tavily".
    WEB_SEARCH_PROVIDER: str = ""
    WEB_SEARCH_API_KEY: str = ""
    WEB_SEARCH_MAX_RESULTS: int = 4

    # Storage
    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET: str = "proassess-documents"
    S3_REGION: str = "us-east-1"

    # CORS
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
