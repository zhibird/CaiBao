from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingModelConfigUpsertRequest(BaseModel):
    team_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    provider: str = Field(default="openai", min_length=1, max_length=32)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=2048)


class EmbeddingModelConfigItem(BaseModel):
    config_id: str
    team_id: str
    user_id: str
    model_name: str
    provider: str
    base_url: str | None
    has_api_key: bool
    masked_api_key: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EmbeddingDefaultModelItem(BaseModel):
    model_name: str
    provider: str
    base_url: str | None
    has_api_key: bool
    masked_api_key: str


class EmbeddingModelConfigListResponse(BaseModel):
    team_id: str
    user_id: str
    default_model: EmbeddingDefaultModelItem
    items: list[EmbeddingModelConfigItem]
