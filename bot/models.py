from __future__ import annotations

import datetime as dt
import logging
from contextlib import contextmanager

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, create_engine
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


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(42), index=True)
    tx_hash: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    tx_type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    token_symbol: Mapped[str] = mapped_column(String(24), default="ERROR404")
    token_contract: Mapped[str] = mapped_column(String(42), default="")
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    broadcasted: Mapped[bool] = mapped_column(Boolean, default=False)


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
        raise RuntimeError("init_db() must be called before get_session()")
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
