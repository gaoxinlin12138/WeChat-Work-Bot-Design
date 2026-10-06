from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置；真实密钥仅从环境变量或本地 .env 读取。"""

    model_config = SettingsConfigDict(
        env_file=(".env", ".database.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "wecom-knowledge-bot"
    log_level: str = "INFO"
    knowledge_upload_dir: str = "data/uploads"
    knowledge_max_upload_mb: int = 50
    business_media_upload_dir: str = "data/product_media"
    business_media_max_upload_mb: int = 300
    business_script_max_upload_mb: int = 20
    business_trash_retention_days: int = Field(default=7, ge=1, le=3650)
    business_draft_retention_days: int = Field(default=7, ge=1, le=3650)
    admin_api_key: str | None = None

    wecom_bot_id: str | None = None
    wecom_bot_secret: str | None = None

    # 企业身份与通讯录。WECOM_SECRET 使用通讯录同步 Secret；敏感字段权限按部门校验。
    wecom_corp_id: str | None = None
    wecom_agent_id: str | None = None
    wecom_secret: str | None = None
    wecom_sales_department_id: int | None = Field(default=None, ge=1)
    wecom_directory_cache_seconds: int = Field(default=300, ge=30, le=3600)
    wecom_directory_failure_cache_seconds: int = Field(default=15, ge=1, le=300)
    wecom_directory_timeout_seconds: float = Field(default=8.0, ge=1.0, le=60.0)
    wecom_callback_token: str | None = None
    wecom_encoding_aes_key: str | None = None

    database_url: str | None = None
    redis_url: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int = Field(default=1024, ge=1, le=2000)
    # DashScope 单次最多 10 条，运行时会自动拆批；配置允许更大的逻辑批次。
    embedding_batch_size: int = Field(default=10, ge=1, le=100)
    embedding_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    embedding_max_retries: int = Field(default=2, ge=0, le=10)
    # 第二阶段候选重排。默认保留本地重排，配置为 dashscope 时启用百炼。
    rerank_provider: Literal["local", "dashscope", "disabled"] = "local"
    rerank_api_key: str | None = None
    rerank_model: str | None = None
    rerank_base_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    )
    rerank_enabled: bool = True
    rerank_timeout_seconds: float = Field(default=8.0, ge=1.0, le=60.0)
    rerank_candidate_limit: int = Field(default=20, ge=5, le=100)
    rag_top_k: int = Field(default=5, ge=1, le=50)
    rag_min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    rag_history_messages: int = Field(default=6, ge=0, le=50)
    rag_max_context_chars: int = Field(default=12000, ge=100, le=100000)
    task_planner_enabled: bool = True
    intent_confidence_threshold: float = Field(default=0.45, ge=0.0, le=1.0)
    product_query_v2_mode: Literal["off", "shadow", "on"] = "shadow"
    langgraph_checkpoint_enabled: bool = False
    langgraph_checkpoint_pool_size: int = Field(default=4, ge=1, le=32)


@lru_cache
def get_settings() -> Settings:
    return Settings()
