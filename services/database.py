"""PostgreSQL storage for dairy dashboard data."""

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
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


class ParlourImportBatch(Base):
    """One imported parlour CSV attachment (e.g. Milk Flow Report)."""

    __tablename__ = "parlour_import_batches"

    id = Column(Integer, primary_key=True)
    farm_code = Column(String(16), nullable=False, index=True, default="ALH")
    report_type = Column(String(64), nullable=False, default="milk_flow")
    filename = Column(String(512), nullable=False)
    email_subject = Column(String(512))
    email_from = Column(String(255))
    email_received_at = Column(DateTime(timezone=True))
    message_id = Column(String(512), index=True)
    row_count = Column(Integer, default=0)
    imported_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    records = relationship("MilkFlowRecord", back_populates="batch", cascade="all, delete-orphan")


class MilkFlowRecord(Base):
    """One cow milking row from a Milk Flow Report CSV."""

    __tablename__ = "milk_flow_records"
    __table_args__ = (
        UniqueConstraint(
            "farm_code",
            "cow_number",
            "milking_date",
            "shift",
            "cow_milking_start_time",
            "milking_point",
            name="uq_milk_flow_cow_milking",
        ),
    )

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("parlour_import_batches.id"), nullable=False, index=True)
    farm_code = Column(String(16), nullable=False, index=True, default="ALH")

    cow_number = Column(String(64), nullable=False, index=True)
    avg_milk_flow_l_per_min = Column(Float)
    milking_date = Column(Date, nullable=False, index=True)
    shift = Column(String(64), index=True)
    dim = Column(Integer)
    shift_yield_l = Column(Float)
    peak_milk_flow_l_per_min = Column(Float)
    peak_milk_flow_time = Column(String(32))  # mm:ss
    flow_rate_15s_ml_per_min = Column(Float)
    flow_rate_30s_ml_per_min = Column(Float)
    flow_rate_60s_ml_per_min = Column(Float)
    flow_rate_120s_ml_per_min = Column(Float)
    percentage_yield_at_2_min = Column(Float)
    milk_yield_at_2_min_l = Column(Float)
    group_number = Column(String(64), index=True)
    flow_rate_at_removal_ml_per_min = Column(Float)
    unit_on_time = Column(String(32))  # renamed from Individual Milking Time By Shift
    cow_milking_start_time = Column(String(32))  # HH:MM:SS
    final_detaching = Column(String(128))
    milking_point = Column(String(64), index=True)

    batch = relationship("ParlourImportBatch", back_populates="records")


class RotaryEntryIdRecord(Base):
    """One cow identification row from a Rotary Entry ID Report CSV."""

    __tablename__ = "rotary_entry_id_records"
    __table_args__ = (
        UniqueConstraint(
            "farm_code",
            "cow_number",
            "milking_date",
            "identification_time",
            name="uq_rotary_entry_cow_id_time",
        ),
    )

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("parlour_import_batches.id"), nullable=False, index=True)
    farm_code = Column(String(16), nullable=False, index=True, default="ALH")

    cow_number = Column(String(64), nullable=False, index=True)
    milking_date = Column(Date, nullable=False, index=True)
    shift = Column(String(64), index=True)  # optional; some CSVs omit Shift
    identification_time = Column(String(32), nullable=False)  # HH:MM:SS when ID'd on parlour

    batch = relationship("ParlourImportBatch")


def _normalize_database_url(url: str) -> str:
    """Normalize DB URLs for SQLAlchemy. Add SSL for remote Postgres only."""
    if url.startswith("sqlite:"):
        return url

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

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        # Local offline default
        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{data_dir / 'local.db'}"

    connect_args = {}
    if database_url.startswith("sqlite:"):
        connect_args["check_same_thread"] = False

    _engine = create_engine(
        _normalize_database_url(database_url),
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args=connect_args,
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


def save_milk_flow_records(batch_meta: dict, records: list[dict]) -> tuple[int, int]:
    """
    Persist Milk Flow rows. Skips duplicates by unique milking key.
    Returns (batch_id, inserted_count).
    """
    with get_session() as session:
        batch = ParlourImportBatch(
            farm_code=batch_meta.get("farm_code", "ALH"),
            report_type=batch_meta.get("report_type", "milk_flow"),
            filename=batch_meta.get("filename", "milk_flow.csv"),
            email_subject=batch_meta.get("email_subject"),
            email_from=batch_meta.get("email_from"),
            email_received_at=batch_meta.get("email_received_at"),
            message_id=batch_meta.get("message_id"),
            row_count=len(records),
        )
        session.add(batch)
        session.flush()

        inserted = 0
        for row in records:
            exists = (
                session.query(MilkFlowRecord.id)
                .filter_by(
                    farm_code=row["farm_code"],
                    cow_number=row["cow_number"],
                    milking_date=row["milking_date"],
                    shift=row.get("shift"),
                    cow_milking_start_time=row.get("cow_milking_start_time"),
                    milking_point=row.get("milking_point"),
                )
                .first()
            )
            if exists:
                continue

            session.add(
                MilkFlowRecord(
                    batch_id=batch.id,
                    farm_code=row["farm_code"],
                    cow_number=row["cow_number"],
                    avg_milk_flow_l_per_min=row.get("avg_milk_flow_l_per_min"),
                    milking_date=row["milking_date"],
                    shift=row.get("shift"),
                    dim=row.get("dim"),
                    shift_yield_l=row.get("shift_yield_l"),
                    peak_milk_flow_l_per_min=row.get("peak_milk_flow_l_per_min"),
                    peak_milk_flow_time=row.get("peak_milk_flow_time"),
                    flow_rate_15s_ml_per_min=row.get("flow_rate_15s_ml_per_min"),
                    flow_rate_30s_ml_per_min=row.get("flow_rate_30s_ml_per_min"),
                    flow_rate_60s_ml_per_min=row.get("flow_rate_60s_ml_per_min"),
                    flow_rate_120s_ml_per_min=row.get("flow_rate_120s_ml_per_min"),
                    percentage_yield_at_2_min=row.get("percentage_yield_at_2_min"),
                    milk_yield_at_2_min_l=row.get("milk_yield_at_2_min_l"),
                    group_number=row.get("group_number"),
                    flow_rate_at_removal_ml_per_min=row.get("flow_rate_at_removal_ml_per_min"),
                    unit_on_time=row.get("unit_on_time"),
                    cow_milking_start_time=row.get("cow_milking_start_time"),
                    final_detaching=row.get("final_detaching"),
                    milking_point=row.get("milking_point"),
                )
            )
            inserted += 1

        batch.row_count = inserted
        return batch.id, inserted


def get_milk_flow_summary(farm_code: str | None = "ALH") -> dict:
    with get_session() as session:
        query = session.query(MilkFlowRecord)
        batch_query = session.query(ParlourImportBatch).filter_by(report_type="milk_flow")
        if farm_code:
            query = query.filter_by(farm_code=farm_code)
            batch_query = batch_query.filter_by(farm_code=farm_code)

        total_records = query.count()
        total_batches = batch_query.count()
        latest_import = batch_query.with_entities(func.max(ParlourImportBatch.imported_at)).scalar()
        latest_date = query.with_entities(func.max(MilkFlowRecord.milking_date)).scalar()
        cow_count = query.with_entities(func.count(func.distinct(MilkFlowRecord.cow_number))).scalar() or 0

        recent_batches = (
            batch_query.order_by(ParlourImportBatch.imported_at.desc()).limit(10).all()
        )
        session.expunge_all()
        return {
            "total_records": total_records,
            "total_batches": total_batches,
            "latest_import": latest_import,
            "latest_milking_date": latest_date,
            "cow_count": cow_count,
            "recent_batches": recent_batches,
        }


def get_recent_milk_flow_records(farm_code: str = "ALH", limit: int = 100) -> list[MilkFlowRecord]:
    with get_session() as session:
        records = (
            session.query(MilkFlowRecord)
            .filter_by(farm_code=farm_code)
            .order_by(
                MilkFlowRecord.milking_date.desc(),
                MilkFlowRecord.shift.asc(),
                MilkFlowRecord.cow_milking_start_time.desc(),
                MilkFlowRecord.id.desc(),
            )
            .limit(limit)
            .all()
        )
        session.expunge_all()
        return records


def save_rotary_entry_id_records(batch_meta: dict, records: list[dict]) -> tuple[int, int]:
    """
    Persist Rotary Entry ID rows. Skips duplicates by cow + date + identification time.
    Returns (batch_id, inserted_count).
    """
    with get_session() as session:
        batch = ParlourImportBatch(
            farm_code=batch_meta.get("farm_code", "ALH"),
            report_type=batch_meta.get("report_type", "rotary_entry_id"),
            filename=batch_meta.get("filename", "rotary_entry_id.csv"),
            email_subject=batch_meta.get("email_subject"),
            email_from=batch_meta.get("email_from"),
            email_received_at=batch_meta.get("email_received_at"),
            message_id=batch_meta.get("message_id"),
            row_count=len(records),
        )
        session.add(batch)
        session.flush()

        inserted = 0
        for row in records:
            exists = (
                session.query(RotaryEntryIdRecord.id)
                .filter_by(
                    farm_code=row["farm_code"],
                    cow_number=row["cow_number"],
                    milking_date=row["milking_date"],
                    identification_time=row["identification_time"],
                )
                .first()
            )
            if exists:
                continue

            session.add(
                RotaryEntryIdRecord(
                    batch_id=batch.id,
                    farm_code=row["farm_code"],
                    cow_number=row["cow_number"],
                    milking_date=row["milking_date"],
                    shift=row.get("shift"),
                    identification_time=row["identification_time"],
                )
            )
            inserted += 1

        batch.row_count = inserted
        return batch.id, inserted
