"""ORM models."""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Preference(Base):
    __tablename__ = "preferences"
    key = Column(String(64), primary_key=True)
    value = Column(String(512))
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class FavouriteTG(Base):
    __tablename__ = "favourite_tgs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tg = Column(Integer, unique=True, nullable=False, index=True)
    label = Column(String(64), nullable=False)
    sort_order = Column(Integer, default=0)
    added_at = Column(DateTime, default=utcnow)


class ListenEvent(Base):
    __tablename__ = "listen_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tg = Column(Integer, nullable=False, index=True)
    src_id = Column(Integer, index=True)  # caller's DMR ID, used for radioid.net lookup
    callsign = Column(String(16))
    name = Column(String(64))
    duration_seconds = Column(Integer)
    heard_at = Column(DateTime, default=utcnow, index=True)


class RadioIDUser(Base):
    __tablename__ = "radioid_users"
    dmr_id = Column(Integer, primary_key=True)
    callsign = Column(String(16), index=True)
    name = Column(String(96))
    city = Column(String(96))
    state = Column(String(96))
    country = Column(String(96))
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
