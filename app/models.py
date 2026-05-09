from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def now_utc() -> datetime:
    return datetime.utcnow()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=20, nullable=False)
    model_check_interval_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    balance_check_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    probe_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    openai_test_mode: Mapped[str] = mapped_column(String(32), default="chat_completions", nullable=False)
    extractor_template_id: Mapped[int | None] = mapped_column(ForeignKey("extractor_templates.id"), nullable=True)
    extractor_vars_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc, nullable=False)

    models: Mapped[list["ChannelModel"]] = relationship(cascade="all, delete-orphan", back_populates="channel")
    health_checks: Mapped[list["HealthCheck"]] = relationship(cascade="all, delete-orphan", back_populates="channel")
    balances: Mapped[list["BalanceSnapshot"]] = relationship(cascade="all, delete-orphan", back_populates="channel")
    alert_rules: Mapped[list["AlertRule"]] = relationship(cascade="all, delete-orphan", back_populates="channel")
    sync_links: Mapped[list["ChannelSyncLink"]] = relationship(cascade="all, delete-orphan", back_populates="channel")
    extractor_template: Mapped["ExtractorTemplate | None"] = relationship()


class ChannelModel(Base):
    __tablename__ = "channel_models"
    __table_args__ = (UniqueConstraint("channel_id", "model_id", name="uq_channel_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owned_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)

    channel: Mapped[Channel] = relationship(back_populates="models")


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)

    channel: Mapped[Channel] = relationship(back_populates="health_checks")


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invalid_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    remaining: Mapped[float | None] = mapped_column(Float, nullable=True)
    used: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)

    channel: Mapped[Channel] = relationship(back_populates="balances")


class ExtractorTemplate(Base):
    __tablename__ = "extractor_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_json: Mapped[str] = mapped_column(Text, nullable=False)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    error_window_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    error_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    model_change_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    balance_threshold_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    balance_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    channel: Mapped[Channel] = relationship(back_populates="alert_rules")


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)


class SyncTarget(Base):
    __tablename__ = "sync_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    name_prefix: Mapped[str] = mapped_column(String(80), default="union_", nullable=False)
    auth_config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    default_config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc, nullable=False)

    sync_links: Mapped[list["ChannelSyncLink"]] = relationship(cascade="all, delete-orphan", back_populates="target")


class ChannelSyncLink(Base):
    __tablename__ = "channel_sync_links"
    __table_args__ = (UniqueConstraint("channel_id", "target_id", name="uq_channel_sync_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    target_id: Mapped[int] = mapped_column(ForeignKey("sync_targets.id"), nullable=False)
    remote_type: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    remote_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sub2api_group_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    sub2api_priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    sub2api_concurrency: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_sync_status: Mapped[str] = mapped_column(String(32), default="never", nullable=False)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc, nullable=False)

    channel: Mapped[Channel] = relationship(back_populates="sync_links")
    target: Mapped[SyncTarget] = relationship(back_populates="sync_links")


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"), nullable=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("sync_targets.id", ondelete="SET NULL"), nullable=True)
    link_id: Mapped[int | None] = mapped_column(ForeignKey("channel_sync_links.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="warning", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    notification_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    notification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
