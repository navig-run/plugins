"""Dataclasses mirroring the DB schema (for type-safe passing between layers)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class Contact:
    uid: str
    full_name: Optional[str] = None
    gender: str = "unknown"
    language: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    photo_path: Optional[str] = None
    source_profile: Optional[str] = None
    is_deleted: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # DB id — set after insert
    id: Optional[int] = None


@dataclass
class SocialProfile:
    contact_id: int
    platform: str
    handle: Optional[str] = None
    profile_url: Optional[str] = None
    is_banned: int = 0
    # ok | gone | invalid | not_a_user; None means the handle was never checked.
    resolve_state: Optional[str] = None
    resolve_checked_at: Optional[str] = None
    id: Optional[int] = None


@dataclass
class ContactStatus:
    contact_id: int
    ever_talked: int = 0
    last_talked_at: Optional[str] = None
    last_talked_platform: Optional[str] = None
    notes: Optional[str] = None
    id: Optional[int] = None


@dataclass
class ImportSummary:
    source: str
    total: int = 0
    inserted: int = 0
    duplicates: int = 0
    errors: int = 0
    photos_matched: int = 0
    photos_unmatched: int = 0
    skipped_no_uid: int = 0
