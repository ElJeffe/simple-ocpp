"""RFID tags and tag groups. Kept in memory (`tags`, `groups`) and written
through to the database, so authorizing a tag never touches the disk."""

import logging
from typing import TypedDict

from ocpp.v16.enums import AuthorizationStatus

from .database import db, now_iso


class TagRecord(TypedDict):
  id_tag: str  # spelling as first registered
  name: str
  authorized: bool
  added: str  # ISO timestamp
  # Group name, sent to chargers as OCPP parentIdTag; "" = not in a group.
  # Any tag in the same group may stop a session another member started.
  parent_id_tag: str


class GroupRecord(TypedDict):
  name: str  # sent as parentIdTag, so at most 20 characters
  added: str


# OCPP id tags are case-insensitive, so they are keyed in upper case.
tags: dict[str, TagRecord] = {}


def tag_key(id_tag: str | None) -> str:
  return (id_tag or "").strip().upper()


def load_tags() -> None:
  """Loads tags and groups."""
  with db() as conn:
    group_rows = conn.execute("SELECT * FROM groups").fetchall()
    rows = conn.execute("SELECT * FROM tags").fetchall()
  for row in group_rows:
    groups[row["key"]] = {"name": row["name"], "added": row["added"]}
  for row in rows:
    tags[row["key"]] = {
        "id_tag": row["id_tag"],
        "name": row["name"],
        "authorized": bool(row["authorized"]),
        "added": row["added"],
        "parent_id_tag": row["parent_id_tag"],
    }
  # Groups used to exist only as a value on tags; give those a groups row.
  for tag in tags.values():
    if tag["parent_id_tag"] and tag_key(tag["parent_id_tag"]) not in groups:
      add_group(tag["parent_id_tag"])


def save_tag(tag: TagRecord) -> None:
  with db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO tags (key, id_tag, name, authorized, added,"
        " parent_id_tag) VALUES (?, ?, ?, ?, ?, ?)",
        (tag_key(tag["id_tag"]), tag["id_tag"], tag["name"], tag["authorized"],
         tag["added"], tag["parent_id_tag"]),
    )


def tag_status(id_tag: str) -> AuthorizationStatus:
  """Only registered and authorized tags may charge."""
  tag = tags.get(tag_key(id_tag))
  if tag is None:
    return AuthorizationStatus.invalid
  return AuthorizationStatus.accepted if tag["authorized"] else AuthorizationStatus.blocked


def tag_parent(id_tag: str | None) -> str | None:
  """The group (OCPP parentIdTag) of a registered tag, or None."""
  tag = tags.get(tag_key(id_tag))
  return (tag["parent_id_tag"] or None) if tag else None


def group_name(name: str) -> str:
  """The exact name of an existing group ("" for none). Raises ValueError.

  Matching ignores case, like id tags, but the stored spelling is returned:
  chargers may compare parentIdTag values exactly.
  """
  if not name.strip():
    return ""
  group = groups.get(tag_key(name))
  if group is None:
    raise ValueError(f"Unknown group {name.strip()!r}")
  return group["name"]


def sorted_tags() -> list[TagRecord]:
  return sorted(tags.values(), key=lambda t: (t["name"].lower(), t["id_tag"]))


def add_tag(
    id_tag: str, name: str = "", authorized: bool = True, parent_id_tag: str = ""
) -> TagRecord:
  """Registers a tag.

  Raises KeyError if it is already registered, ValueError for an unknown group.
  """
  key = tag_key(id_tag)
  if key in tags:
    raise KeyError(id_tag)
  tag: TagRecord = {
      "id_tag": id_tag.strip(),
      "name": name.strip(),
      "authorized": authorized,
      "added": now_iso(),
      "parent_id_tag": group_name(parent_id_tag),
  }
  save_tag(tag)  # before caching, so a failed write leaves no phantom tag
  tags[key] = tag
  logging.info(f"Registered RFID {id_tag}: {tag}")
  return tag


def update_tag(
    id_tag: str, name: str | None = None, authorized: bool | None = None,
    parent_id_tag: str | None = None,
) -> tuple[TagRecord, bool]:
  """Changes a tag. Raises KeyError if unknown, ValueError for an unknown group.

  Returns (tag, stale) where stale means chargers may have cached outdated
  information about it (access revoked or group changed).
  """
  key = tag_key(id_tag)
  old = tags[key]
  tag = old.copy()
  if name is not None:
    tag["name"] = name.strip()
  if authorized is not None:
    tag["authorized"] = authorized
  if parent_id_tag is not None:
    tag["parent_id_tag"] = group_name(parent_id_tag)
  save_tag(tag)
  tags[key] = tag
  logging.info(f"Updated RFID {tag['id_tag']}: {tag}")
  revoked = old["authorized"] and not tag["authorized"]
  return tag, revoked or old["parent_id_tag"] != tag["parent_id_tag"]


def remove_tag(id_tag: str) -> TagRecord:
  """Forgets a tag. Raises KeyError if unknown."""
  key = tag_key(id_tag)
  tag = tags[key]
  with db() as conn:
    conn.execute("DELETE FROM tags WHERE key = ?", (key,))
  del tags[key]
  logging.info(f"Removed RFID {tag['id_tag']}")
  return tag


groups: dict[str, GroupRecord] = {}  # keyed by tag_key(name)


def sorted_groups() -> list[GroupRecord]:
  return sorted(groups.values(), key=lambda g: g["name"].lower())


def add_group(name: str) -> GroupRecord:
  """Creates a group. Raises KeyError if one with that name exists."""
  key = tag_key(name)
  if key in groups:
    raise KeyError(name)
  group: GroupRecord = {"name": name.strip(), "added": now_iso()}
  with db() as conn:
    conn.execute(
        "INSERT INTO groups (key, name, added) VALUES (?, ?, ?)",
        (key, group["name"], group["added"]),
    )
  groups[key] = group
  logging.info(f"Added tag group {group['name']}")
  return group


def remove_group(name: str) -> list[TagRecord]:
  """Deletes a group and takes its tags out of it. Returns those tags.

  Raises KeyError if there is no such group.
  """
  key = tag_key(name)
  group = groups[key]
  members = [k for k, t in tags.items() if t["parent_id_tag"] == group["name"]]
  with db() as conn:  # one transaction: either both happen or neither
    conn.execute(
        "UPDATE tags SET parent_id_tag = '' WHERE parent_id_tag = ?", (group["name"],)
    )
    conn.execute("DELETE FROM groups WHERE key = ?", (key,))
  del groups[key]
  for k in members:
    tags[k] = {**tags[k], "parent_id_tag": ""}
  logging.info(f"Removed tag group {group['name']} ({len(members)} tags)")
  return [tags[k] for k in members]
