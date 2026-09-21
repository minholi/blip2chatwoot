from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlipMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    from_: str = Field(alias="from")
    to: str | None = None
    type: str
    content: Any
    metadata: dict[str, Any] = Field(default_factory=dict)


class BlipNotification(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    to: str | None = None
    from_: str | None = Field(default=None, alias="from")
    event: str
    reason: dict[str, Any] | None = None
