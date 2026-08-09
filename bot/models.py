"""SQLAlchemy models + session factory."""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import contextmanager

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, index=True)
    encrypted_private_key: Mapped[str] = mapped_column(String(512))
    join_date: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    referral_code: Mapped[str] = mapped_column(String(16), unique=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    monitor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    broadcast_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    slippage: Mapped[float] = mapped_column(Float, default=1.0)
    gas_strategy: Mapped[str] = mapped_column(String(10), default="medium")
    default_eth_amount: Mapped[float] = mapped_column(Float, default=0.01)


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    referee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    reward_claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    reward_amount: Mapped[float] = mapped_column(Float, default=0.0)
    date: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(42), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    tx_type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    token_symbol: Mapped[str] = mapped_column(String(24), default="ERROR")
    token_contract: Mapped[str] = mapped_column(String(42), default="")
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    broadcasted: Mapped[bool] = mapped_column(Boolean, default=False)


class LimitOrder(Base):
    __tablename__ = "limit_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    order_type: Mapped[str] = mapped_column(String(4))
    token_address: Mapped[str] = mapped_column(String(42))
    amount: Mapped[float] = mapped_column(Float)
    price_target: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    target_price: Mapped[float] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(5))
    triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class DCAJob(Base):
    __tablename__ = "dca_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    total_eth: Mapped[float] = mapped_column(Float)
    num_buys: Mapped[int] = mapped_column(Integer)
    buys_done: Mapped[int] = mapped_column(Integer, default=0)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    eth_per_buy: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


_engine = None
SessionLocal: sessionmaker | None = None


def init_db(database_url: str):
    global _engine, SessionLocal
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if database_url.startswith("postgres"):
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
    else:
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(database_url, **kwargs)
    Base.metadata.create_all(_engine)
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    log.info("Database ready (%s)", database_url.split("@")[-1])
    return _engine


@contextmanager
def get_session():
    if SessionLocal is None:
        raise RuntimeError("init_db() must be called first")
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
