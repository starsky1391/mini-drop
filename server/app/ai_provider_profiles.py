"""Runtime AI provider profile storage.

Profiles are OpenAI-compatible provider configurations managed from the UI.
API keys are write-only from the client perspective and never returned in API
responses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.app.database import new_session
from server.app.models import AIProviderProfileModel

AIEnabledMode = Literal["none", "nlp-only", "rca-only", "full"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AIProviderProfileCreate(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    provider_label: str = Field(default="openai-compatible", min_length=1, max_length=128)
    base_url: str = Field(min_length=4, max_length=512)
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=4096)
    enabled: AIEnabledMode = "full"
    activate: bool = True

    @model_validator(mode="after")
    def normalize_url(self):
        self.base_url = normalize_base_url(self.base_url)
        return self


class AIProviderProfileUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    provider_label: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=4, max_length=512)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    enabled: AIEnabledMode | None = None

    @model_validator(mode="after")
    def normalize_url(self):
        if self.base_url is not None:
            self.base_url = normalize_base_url(self.base_url)
        return self


def normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if "://" not in base_url:
        base_url = f"http://{base_url}"
    return base_url


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def list_profiles() -> list[dict[str, Any]]:
    session = new_session()
    try:
        rows = (
            session.query(AIProviderProfileModel)
            .order_by(AIProviderProfileModel.is_active.desc(), AIProviderProfileModel.updated_at.desc())
            .all()
        )
        return [row.to_safe_dict() for row in rows]
    finally:
        session.close()


def get_profile(profile_id: str) -> dict[str, Any] | None:
    session = new_session()
    try:
        row = session.get(AIProviderProfileModel, profile_id)
        return row.to_safe_dict() if row else None
    finally:
        session.close()


def get_active_profile_settings() -> dict[str, Any] | None:
    """Return active profile including secret for server-side provider calls."""
    session = new_session()
    try:
        row = (
            session.query(AIProviderProfileModel)
            .filter(AIProviderProfileModel.is_active == 1)
            .order_by(AIProviderProfileModel.updated_at.desc())
            .first()
        )
        return row.to_settings_dict() if row else None
    except Exception:
        return None
    finally:
        session.close()


def create_profile(payload: AIProviderProfileCreate) -> dict[str, Any]:
    now = utcnow()
    session = new_session()
    try:
        if payload.activate:
            _clear_active_profiles(session)
        row = AIProviderProfileModel(
            id=f"ai_profile_{uuid4().hex[:12]}",
            name=payload.name,
            provider_label=payload.provider_label,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
            enabled=payload.enabled,
            is_active=1 if payload.activate else 0,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        return row.to_safe_dict()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_profile(profile_id: str, payload: AIProviderProfileUpdate) -> dict[str, Any] | None:
    session = new_session()
    try:
        row = session.get(AIProviderProfileModel, profile_id)
        if row is None:
            return None
        values = payload.model_dump(exclude_unset=True)
        for key, value in values.items():
            if value is not None:
                setattr(row, key, value)
        row.updated_at = utcnow()
        session.commit()
        return row.to_safe_dict()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def activate_profile(profile_id: str) -> dict[str, Any] | None:
    session = new_session()
    try:
        row = session.get(AIProviderProfileModel, profile_id)
        if row is None:
            return None
        _clear_active_profiles(session)
        row.is_active = 1
        row.updated_at = utcnow()
        session.commit()
        return row.to_safe_dict()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_profile(profile_id: str) -> bool:
    session = new_session()
    try:
        row = session.get(AIProviderProfileModel, profile_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _clear_active_profiles(session) -> None:
    for row in session.query(AIProviderProfileModel).filter(AIProviderProfileModel.is_active == 1).all():
        row.is_active = 0
        row.updated_at = utcnow()
