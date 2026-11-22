"""Shared base for request bodies."""

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
  model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
