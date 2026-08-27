from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ListingModel(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_listing_source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    district: Mapped[Optional[str]] = mapped_column(String(255))
    locality: Mapped[Optional[str]] = mapped_column(String(255))
    address: Mapped[Optional[str]] = mapped_column(Text)
    direction: Mapped[Optional[str]] = mapped_column(String(255))
    price_original: Mapped[Optional[float]] = mapped_column(Float)
    currency_original: Mapped[Optional[str]] = mapped_column(String(16))
    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    area_sotok: Mapped[Optional[float]] = mapped_column(Float)
    distance_mkad_km: Mapped[Optional[float]] = mapped_column(Float)
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    facade_m: Mapped[Optional[float]] = mapped_column(Float)
    depth_m: Mapped[Optional[float]] = mapped_column(Float)
    purpose: Mapped[Optional[str]] = mapped_column(Text)
    electricity_raw: Mapped[Optional[str]] = mapped_column(Text)
    electricity_kw: Mapped[Optional[float]] = mapped_column(Float)
    gas_raw: Mapped[Optional[str]] = mapped_column(Text)
    water_raw: Mapped[Optional[str]] = mapped_column(Text)
    sewerage_raw: Mapped[Optional[str]] = mapped_column(Text)
    internet_raw: Mapped[Optional[str]] = mapped_column(Text)
    road_raw: Mapped[Optional[str]] = mapped_column(Text)
    nature_raw: Mapped[Optional[str]] = mapped_column(Text)
    ownership_raw: Mapped[Optional[str]] = mapped_column(Text)
    seller_type: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasons: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    raw_payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_notified_hash: Mapped[Optional[str]] = mapped_column(String(64))
    sheet_row: Mapped[Optional[int]] = mapped_column(Integer)
    possible_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    snapshots: Mapped[List["ListingSnapshotModel"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class ListingSnapshotModel(Base):
    __tablename__ = "listing_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    listing: Mapped[ListingModel] = relationship(back_populates="snapshots")


class ListingEventModel(Base):
    __tablename__ = "listing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ListingProfileModel(Base):
    __tablename__ = "listing_profiles"
    __table_args__ = (
        UniqueConstraint("listing_id", "profile_id", name="uq_listing_profile"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasons: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ListingActivityModel(Base):
    __tablename__ = "listing_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    consecutive_unavailable: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_status: Mapped[Optional[str]] = mapped_column(String(32))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_available_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ListingDecisionModel(Base):
    __tablename__ = "listing_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class WidgetLeadModel(Base):
    __tablename__ = "widget_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    consent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    source_page: Mapped[Optional[str]] = mapped_column(Text)
    utm: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    otp_hash: Mapped[Optional[str]] = mapped_column(String(64))
    otp_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    otp_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    otp_request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    otp_window_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    last_code_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    session_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64), index=True
    )
    session_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class WidgetInterestModel(Base):
    __tablename__ = "widget_interests"
    __table_args__ = (
        UniqueConstraint("lead_id", "listing_id", name="uq_widget_lead_listing"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("widget_leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_title: Mapped[str] = mapped_column(Text, nullable=False)
    listing_url: Mapped[str] = mapped_column(Text, nullable=False)
    search_params: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_page: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ScanRunModel(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    source_stats: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    errors: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SourceHealthModel(Base):
    __tablename__ = "source_health"
    __table_args__ = (
        UniqueConstraint("source", "profile_id", name="uq_source_health_profile"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="never")
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    last_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    baseline_item_count: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_degraded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    diagnostics: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class LocationProfileModel(Base):
    __tablename__ = "location_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    district: Mapped[Optional[str]] = mapped_column(String(255))
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    listing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    house_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    premium_house_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    median_house_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    median_distance_mkad_km: Mapped[Optional[float]] = mapped_column(Float)
    electricity_share: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    gas_share: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    internet_share: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    paved_road_share: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    verdict: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)
    signals: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    risks: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    member_external_ids: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    next_refresh_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class LocationObservationModel(Base):
    __tablename__ = "location_observations"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_id",
            "signal_type",
            name="uq_location_observation_source_signal",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_key: Mapped[str] = mapped_column(
        String(320), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text)
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ServiceStateModel(Base):
    __tablename__ = "service_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
