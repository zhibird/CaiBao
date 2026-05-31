from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LLMModelConfigUpsertRequest(BaseModel):
    team_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=2048)


class LLMModelConfigItem(BaseModel):
    config_id: str
    team_id: str
    user_id: str
    model_name: str
    base_url: str
    has_api_key: bool
    masked_api_key: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LLMDefaultModelItem(BaseModel):
    model_name: str
    provider: str
    base_url: str
    has_api_key: bool
    masked_api_key: str


class LLMModelConfigListResponse(BaseModel):
    team_id: str
    user_id: str
    default_model: LLMDefaultModelItem
    items: list[LLMModelConfigItem]

