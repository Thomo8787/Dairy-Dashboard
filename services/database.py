"""PostgreSQL storage for dairy dashboard data."""

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    text,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

Base = declarative_base()

_engine = None
_SessionLocal = None


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id = Column(Integer, primary_key=True)
    filename = Column(String(512), nullable=False)
    email_subject = Column(String(512))
    email_received_at = Column(DateTime(timezone=True))
    row_count = Column(Integer, default=0)
    imported_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    records = relationship("DairyRecord", back_populates="batch", cascade="all, delete-orphan")


class DairyRecord(Base):
    __tablename__ = "dairy_records"

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("import_batches.id"), nullable=False, index=True)
    record_date = Column(DateTime(timezone=True), index=True)
    category = Column(String(255), index=True)
    metric_name = Column(String(255), index=True)
    metric_value = Column(Float)
    unit = Column(String(64))
    notes = Column(Text)
    batch = relationship("ImportBatch", back_populates="records")


class GraphToken(Base):
    """Stored Microsoft delegated tokens for the signed-in user (e.g. mark@alhfarm.co.uk)."""

    __tablename__ = "graph_tokens"

    id = Column(Integer, primary_key=True)
    account_key = Column(String(64), unique=True, nullable=False, default="default")
    user_email = Column(String(255))
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text)
    expires_at = Column(DateTime(timezone=True))
    scopes = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


def _normalize_database_url(url: str) -> str:
    """Render uses postgres://; SQLAlchemy 2.x expects postgresql://. Ensure SSL on Render."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    is_local = host in {"localhost", "127.0.0.1"} or host.endswith(".local")
    query = parse_qs(parsed.query)
    if not is_local and "sslmode" not in query:
        query["sslmode"] = ["require"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def get_engine():
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    _engine = create_engine(
        _normalize_database_url(database_url),
        pool_pre_ping=True,
        pool_recycle=300,
    )
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _engine


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def get_session() -> Session:
    get_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def save_dataframe(batch_meta: dict, records: list[dict]) -> int:
    """Persist parsed Excel rows and return the new batch id."""
    with get_session() as session:
        batch = ImportBatch(
            filename=batch_meta.get("filename", "unknown.xlsx"),
            email_subject=batch_meta.get("email_subject"),
            email_received_at=batch_meta.get("email_received_at"),
            row_count=len(records),
        )
        session.add(batch)
        session.flush()
        batch_id = batch.id

        for row in records:
            session.add(
                DairyRecord(
                    batch_id=batch_id,
                    record_date=row.get("record_date"),
                    category=row.get("category"),
                    metric_name=row.get("metric_name"),
                    metric_value=row.get("metric_value"),
                    unit=row.get("unit"),
                    notes=row.get("notes"),
                )
            )

        return batch_id


def get_dashboard_summary() -> dict:
    with get_session() as session:
        total_records = session.query(func.count(DairyRecord.id)).scalar() or 0
        total_batches = session.query(func.count(ImportBatch.id)).scalar() or 0
        latest_import = session.query(func.max(ImportBatch.imported_at)).scalar()

        recent_batches = (
            session.query(ImportBatch)
            .order_by(ImportBatch.imported_at.desc())
            .limit(10)
            .all()
        )

        category_totals = (
            session.query(DairyRecord.category, func.sum(DairyRecord.metric_value))
            .group_by(DairyRecord.category)
            .order_by(func.sum(DairyRecord.metric_value).desc())
            .limit(10)
            .all()
        )

        session.expunge_all()

        return {
            "total_records": total_records,
            "total_batches": total_batches,
            "latest_import": latest_import,
            "recent_batches": recent_batches,
            "category_totals": category_totals,
        }


def get_recent_records(limit: int = 100) -> list[DairyRecord]:
    with get_session() as session:
        records = (
            session.query(DairyRecord)
            .order_by(DairyRecord.record_date.desc().nullslast(), DairyRecord.id.desc())
            .limit(limit)
            .all()
        )
        session.expunge_all()
        return records


def health_check() -> bool:
    with get_session() as session:
        session.execute(text("SELECT 1"))
        return True
