# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""Shared base for request bodies."""

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
  model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
