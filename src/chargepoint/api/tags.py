# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""RFID tags and tag groups."""

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .. import store
from ..charger import clear_charger_caches
from .models import Model

router = APIRouter()


class TagCreate(Model):
  id_tag: str = Field(min_length=1, max_length=20)  # OCPP 1.6 IdToken limit
  name: str = Field("", max_length=100)
  authorized: bool = True
  parent_id_tag: str = Field("", max_length=20)  # group; "" = none


class TagUpdate(Model):
  name: str | None = Field(None, max_length=100)
  authorized: bool | None = None
  parent_id_tag: str | None = Field(None, max_length=20)


class GroupCreate(Model):
  name: str = Field(min_length=1, max_length=20)  # sent as parentIdTag


@router.get("/api/tags")
async def list_tags() -> list[store.TagRecord]:
  return store.sorted_tags()


@router.post("/api/tags")
async def register_tag(body: TagCreate) -> store.TagRecord:
  try:
    return store.add_tag(body.id_tag, body.name, body.authorized, body.parent_id_tag)
  except KeyError:
    raise HTTPException(400, "Tag is already registered")
  except ValueError as e:
    raise HTTPException(400, str(e))


@router.post("/api/tags/{id_tag}")
async def update_tag(id_tag: str, body: TagUpdate) -> store.TagRecord:
  try:
    tag, stale = store.update_tag(
        id_tag, body.name, body.authorized, body.parent_id_tag
    )
  except KeyError:
    raise HTTPException(404, "Unknown tag")
  except ValueError as e:
    raise HTTPException(400, str(e))
  if stale:
    clear_charger_caches()
  return tag


@router.delete("/api/tags/{id_tag}")
async def delete_tag(id_tag: str) -> dict[str, bool]:
  try:
    store.remove_tag(id_tag)
  except KeyError:
    raise HTTPException(404, "Unknown tag")
  clear_charger_caches()
  return {"ok": True}


@router.get("/api/groups")
async def list_groups() -> list[store.GroupRecord]:
  return store.sorted_groups()


@router.post("/api/groups")
async def add_group(body: GroupCreate) -> store.GroupRecord:
  try:
    return store.add_group(body.name)
  except KeyError:
    raise HTTPException(400, "A group with that name already exists")


@router.delete("/api/groups/{name}")
async def delete_group(name: str) -> dict[str, int]:
  """Deletes a group; its tags are left without a group."""
  try:
    members = store.remove_group(name)
  except KeyError:
    raise HTTPException(404, "Unknown group")
  if members:
    clear_charger_caches()  # they may have cached the old parentIdTag
  return {"tags_ungrouped": len(members)}
