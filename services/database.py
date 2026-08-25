"""PostgreSQL storage for dairy dashboard data."""

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    inspect,
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


class User(Base):
    """Dashboard login account with page and sync-action permissions."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    perm_home = Column(Boolean, nullable=False, default=True)
    perm_office = Column(Boolean, nullable=False, default=False)
    perm_parlours = Column(Boolean, nullable=False, default=False)
    perm_stock = Column(Boolean, nullable=False, default=False)
    perm_events = Column(Boolean, nullable=False, default=False)
    perm_genetics = Column(Boolean, nullable=False, default=False)
    perm_milk_quality = Column(Boolean, nullable=False, default=False)
    perm_sync_outlook = Column(Boolean, nullable=False, default=False)
    perm_sync_onedrive = Column(Boolean, nullable=False, default=False)
    perm_sync_dataflow = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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


class MilkingEfficiencyDayCache(Base):
    """Precomputed day-level parlour metrics, refreshed after import/cron."""

    __tablename__ = "milking_efficiency_day_cache"
    __table_args__ = (
        UniqueConstraint(
            "farm_code",
            "milking_date",
            "shift_id",
            name="uq_eff_cache_farm_date_shift",
        ),
    )

    id = Column(Integer, primary_key=True)
    farm_code = Column(String(16), nullable=False, index=True)
    milking_date = Column(Date, nullable=False, index=True)
    shift_id = Column(String(32), nullable=False, index=True)
    metrics_json = Column(Text, nullable=False)
    computed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AppSetting(Base):
    """Simple key-value settings, used for herd import fingerprints."""

    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(128), unique=True, nullable=False, index=True)
    value = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class CowEvent(Base):
    """Cow events from DairyComp DCEXPORT *EVENTS.CSV files."""

    __tablename__ = "cow_events"

    id = Column(Integer, primary_key=True)
    cow_id = Column(String(64), index=True)
    etag = Column(String(64), index=True)
    bdat = Column(Date)
    fdat = Column(Date)
    lact = Column(Integer)
    gndr = Column(String(8))
    edat = Column(Date)
    event = Column(String(64), index=True)
    dim = Column(Float)
    event_date = Column(Date, index=True)
    remark = Column(String(255))
    r = Column(String(64))
    t = Column(String(64))
    b = Column(String(64))
    protocols = Column(String(255))
    technician = Column(String(128))
    dest = Column(String(128))
    farm = Column(String(8), nullable=False, index=True)
    month_label = Column(String(16))
    fiscal_year = Column(Integer, index=True)
    sort_key = Column(Integer)
    parity = Column(String(32))
    cbrd = Column(Integer)
    import_timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class HerdBirth(Base):
    """Birth records from DairyComp DCEXPORT *BORN.CSV files."""

    __tablename__ = "herd_births"

    id = Column(Integer, primary_key=True)
    cow_id = Column(String(64), index=True)
    etag = Column(String(64), index=True)
    bdat = Column(Date, index=True)
    cbrd = Column(Integer)
    gndr = Column(String(8))
    category = Column(String(16), index=True)
    event = Column(String(64))
    farm = Column(String(8), nullable=False, index=True)
    fiscal_year = Column(Integer, index=True)
    import_timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class HerdInventory(Base):
    """Current herd snapshot from DairyComp DCEXPORT *INV.CSV files."""

    __tablename__ = "herd_inventory"

    id = Column(Integer, primary_key=True)
    cow_id = Column(String(64), index=True)
    etag = Column(String(64), index=True)
    bdat = Column(Date)
    edat = Column(Date)
    cbrd = Column(Float)
    sbrd = Column(String(16))
    fdat = Column(Date)
    dim = Column(Float)
    lact = Column(Float)
    hdat = Column(Date)
    dslh = Column(Float)
    rc = Column(Float)
    rpro = Column(String(32))
    pen = Column(String(32))
    tbrd = Column(Integer)
    remark = Column(String(255))
    ewgt = Column(Float)
    httag = Column(String(32))
    rum = Column(Float)
    dcc = Column(Float)
    due = Column(Date)
    lsir = Column(String(64))
    sirc = Column(String(64))
    lsbrd = Column(String(32), index=True)
    farm = Column(String(8), nullable=False, index=True)
    category = Column(String(32), index=True)
    gender = Column(String(8), index=True)
    aged = Column(Integer)
    months_old = Column(Integer, index=True)
    expected_due = Column(Date, index=True)
    fiscal_year_due = Column(Integer)
    sort_key = Column(Integer, index=True)
    expected_month = Column(String(16))
    value = Column(Float)
    import_timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


STOCK_GROUP_COWS = "cows"
STOCK_GROUP_YOUNGSTOCK = "youngstock"
STOCK_GROUP_BEEF = "beef"
STOCK_GROUP_OPTIONS: tuple[str, ...] = (
    STOCK_GROUP_COWS,
    STOCK_GROUP_YOUNGSTOCK,
    STOCK_GROUP_BEEF,
)
STOCK_GROUP_CATEGORY: dict[str, str] = {
    STOCK_GROUP_COWS: "Dairy",
    STOCK_GROUP_YOUNGSTOCK: "Youngstock",
    STOCK_GROUP_BEEF: "Beef",
}


class StockOpeningBaseline(Base):
    """Opening stock count for a farm/group from a specific month onward."""

    __tablename__ = "stock_opening_baselines"
    __table_args__ = (
        UniqueConstraint("farm", "stock_group", name="uq_stock_opening_baseline_farm_group"),
    )

    id = Column(Integer, primary_key=True)
    farm = Column(String(8), nullable=False, index=True)
    stock_group = Column(String(16), nullable=False, index=True)
    month_start = Column(Date, nullable=False, index=True)
    opening_count = Column(Integer, nullable=False)


class StockAccrualSnapshot(Base):
    """Pre-computed monthly stock accrual rows per farm (rebuilt on herd import)."""

    __tablename__ = "stock_accrual_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "anchor_import_timestamp",
            "farm",
            "stock_group",
            "month_start",
            name="uq_stock_accrual_snapshot",
        ),
    )

    id = Column(Integer, primary_key=True)
    anchor_import_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    farm = Column(String(8), nullable=False, index=True)
    stock_group = Column(String(16), nullable=False, index=True)
    month_start = Column(Date, nullable=False, index=True)
    opening_count = Column(Integer, nullable=False, default=0)
    sales = Column(JSON, nullable=False)
    sales_total = Column(Integer, nullable=False, default=0)
    deaths = Column(Integer, nullable=False, default=0)
    births = Column(Integer, nullable=False, default=0)
    calvings = Column(Integer, nullable=False, default=0)
    purchases = Column(Integer, nullable=False, default=0)
    closing_count = Column(Integer, nullable=False, default=0)
    warning = Column(Boolean, nullable=False, default=False)


class StockPurchaseAnimal(Base):
    """Purchased animals derived from cow events (EDAT != BDAT), rebuilt on herd import."""

    __tablename__ = "stock_purchase_animals"
    __table_args__ = (
        UniqueConstraint("farm", "etag", name="uq_stock_purchase_animal_farm_etag"),
    )

    id = Column(Integer, primary_key=True)
    farm = Column(String(8), nullable=False, index=True)
    etag = Column(String(64), nullable=False, index=True)
    edat = Column(Date, nullable=False, index=True)
    bdat = Column(Date, nullable=False)
    lact = Column(Integer)
    cbrd = Column(Integer)
    gndr = Column(String(8))
    stock_group = Column(String(16), nullable=False, index=True)
    import_timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class GenomicResult(Base):
    """AHDB genomic evaluation traits from Genetics/animals_ahdb, keyed by last 12 eartag digits."""

    __tablename__ = "genomic_results"

    id = Column(Integer, primary_key=True)
    hbn = Column(String(32), unique=True, nullable=False, index=True)
    eartag = Column(String(64))
    sire_name = Column(String(128))
    sire_reg = Column(String(64))
    milk_kg = Column(Float)
    fat_kg = Column(Float)
    protein_kg = Column(Float)
    fat_pct = Column(Float)
    protein_pct = Column(Float)
    pli = Column(Float)
    cci = Column(Float)
    fertility_index = Column(Float)
    scc = Column(Float)
    life_span = Column(Float)
    mastitis = Column(Float)
    milking_speed = Column(Float)
    type_merit = Column(Float)
    mammary = Column(Float)
    legs_and_feet = Column(Float)
    stature = Column(Float)
    chest_width = Column(Float)
    body_depth = Column(Float)
    mature_weight = Column(Float)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class NmlMilkResult(Base):
    """Per-collection milk quality from NML report PDFs (emailed daily)."""

    __tablename__ = "nml_milk_results"
    __table_args__ = (
        UniqueConstraint(
            "producer_ref",
            "sample_date",
            "sample_id",
            name="uq_nml_producer_sample",
        ),
    )

    id = Column(Integer, primary_key=True)
    farm = Column(String(8), index=True)
    producer_ref = Column(String(32), nullable=False, index=True)
    milk_buyer = Column(String(64))
    report_month = Column(String(16))
    report_date = Column(Date)
    sample_date = Column(Date, nullable=False, index=True)
    sample_id = Column(String(32), nullable=False)
    load_number = Column(Integer)
    litres_load = Column(Float)
    litres_weighbridge = Column(Float)
    temp_c = Column(Float)
    butterfat_pct = Column(Float)
    protein_pct = Column(Float)
    scc = Column(Integer)
    bactoscan = Column(Integer)
    fpd = Column(Integer)
    antibiotic_pass = Column(Boolean)
    urea_pct = Column(Float)
    sample_missing = Column(Boolean, nullable=False, default=False)
    nml_matched = Column(Boolean, nullable=False, default=False)
    source = Column(String(32))
    source_message_id = Column(String(256))
    source_file = Column(String(256))
    imported_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class BreedingSireClassification(Base):
    """Manual beef/dairy classification for breeding sires without .b/.s suffix."""

    __tablename__ = "breeding_sire_classifications"

    id = Column(Integer, primary_key=True)
    sire_code = Column(String(255), unique=True, nullable=False, index=True)
    semen_type = Column(String(16), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sire_code": self.sire_code,
            "semen_type": self.semen_type,
        }


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


def _ensure_user_permission_columns(engine) -> None:
    """create_all will not add new columns to an existing users table."""
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("users")}
    needed = {
        "perm_events": "ALTER TABLE users ADD COLUMN perm_events BOOLEAN NOT NULL DEFAULT FALSE",
        "perm_stock": "ALTER TABLE users ADD COLUMN perm_stock BOOLEAN NOT NULL DEFAULT FALSE",
        "perm_genetics": "ALTER TABLE users ADD COLUMN perm_genetics BOOLEAN NOT NULL DEFAULT FALSE",
        "perm_milk_quality": "ALTER TABLE users ADD COLUMN perm_milk_quality BOOLEAN NOT NULL DEFAULT FALSE",
    }
    with engine.begin() as conn:
        for column, ddl in needed.items():
            if column not in existing:
                conn.execute(text(ddl))


def _ensure_herd_inventory_columns(engine) -> None:
    """create_all will not add new columns to an existing herd_inventory table."""
    inspector = inspect(engine)
    if "herd_inventory" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("herd_inventory")}
    needed = {
        "hdat": "ALTER TABLE herd_inventory ADD COLUMN hdat DATE",
        "dslh": "ALTER TABLE herd_inventory ADD COLUMN dslh FLOAT",
        "rc": "ALTER TABLE herd_inventory ADD COLUMN rc FLOAT",
        "rpro": "ALTER TABLE herd_inventory ADD COLUMN rpro VARCHAR(32)",
        "tbrd": "ALTER TABLE herd_inventory ADD COLUMN tbrd INTEGER",
        "ewgt": "ALTER TABLE herd_inventory ADD COLUMN ewgt FLOAT",
        "httag": "ALTER TABLE herd_inventory ADD COLUMN httag VARCHAR(32)",
        "rum": "ALTER TABLE herd_inventory ADD COLUMN rum FLOAT",
        "dcc": "ALTER TABLE herd_inventory ADD COLUMN dcc FLOAT",
        "due": "ALTER TABLE herd_inventory ADD COLUMN due DATE",
        "lsir": "ALTER TABLE herd_inventory ADD COLUMN lsir VARCHAR(64)",
        "sirc": "ALTER TABLE herd_inventory ADD COLUMN sirc VARCHAR(64)",
        "lsbrd": "ALTER TABLE herd_inventory ADD COLUMN lsbrd VARCHAR(32)",
        "aged": "ALTER TABLE herd_inventory ADD COLUMN aged INTEGER",
        "months_old": "ALTER TABLE herd_inventory ADD COLUMN months_old INTEGER",
        "expected_due": "ALTER TABLE herd_inventory ADD COLUMN expected_due DATE",
        "fiscal_year_due": "ALTER TABLE herd_inventory ADD COLUMN fiscal_year_due INTEGER",
        "sort_key": "ALTER TABLE herd_inventory ADD COLUMN sort_key INTEGER",
        "expected_month": "ALTER TABLE herd_inventory ADD COLUMN expected_month VARCHAR(16)",
        "value": "ALTER TABLE herd_inventory ADD COLUMN value FLOAT",
    }
    with engine.begin() as conn:
        for column, ddl in needed.items():
            if column not in existing:
                conn.execute(text(ddl))


def _ensure_nml_milk_result_columns(engine) -> None:
    """create_all will not add new columns to an existing nml_milk_results table."""
    inspector = inspect(engine)
    if "nml_milk_results" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("nml_milk_results")}
    needed = {
        "load_number": "ALTER TABLE nml_milk_results ADD COLUMN load_number INTEGER",
        "litres_load": "ALTER TABLE nml_milk_results ADD COLUMN litres_load FLOAT",
        "litres_weighbridge": "ALTER TABLE nml_milk_results ADD COLUMN litres_weighbridge FLOAT",
        "sample_missing": (
            "ALTER TABLE nml_milk_results ADD COLUMN sample_missing BOOLEAN "
            "NOT NULL DEFAULT FALSE"
        ),
        "source": "ALTER TABLE nml_milk_results ADD COLUMN source VARCHAR(32)",
        "temp_c": "ALTER TABLE nml_milk_results ADD COLUMN temp_c FLOAT",
        "nml_matched": (
            "ALTER TABLE nml_milk_results ADD COLUMN nml_matched BOOLEAN "
            "NOT NULL DEFAULT FALSE"
        ),
    }
    with engine.begin() as conn:
        for column, ddl in needed.items():
            if column not in existing:
                conn.execute(text(ddl))


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_user_permission_columns(engine)
    _ensure_herd_inventory_columns(engine)
    _ensure_nml_milk_result_columns(engine)
    return engine


def ensure_auth_ready():
    """Create tables and seed the bootstrap admin when the user table is empty."""
    init_db()
    from services.auth import seed_admin_user

    return seed_admin_user()


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


def save_milk_flow_records(
    batch_meta: dict,
    records: list[dict],
    *,
    skip_duplicates: bool = True,
) -> tuple[int, int]:
    """
    Persist Milk Flow rows.
    Always de-dupes within the batch. When skip_duplicates is True (default),
    also skips rows that already exist in the DB by unique milking key.
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
        seen_keys: set[tuple] = set()
        pending_orm: list[MilkFlowRecord] = []
        pending_maps: list[dict] = []

        def _flush_pending() -> None:
            nonlocal pending_orm, pending_maps, inserted
            if pending_maps:
                session.bulk_insert_mappings(MilkFlowRecord, pending_maps)
                inserted += len(pending_maps)
                pending_maps = []
            if pending_orm:
                session.bulk_save_objects(pending_orm)
                inserted += len(pending_orm)
                pending_orm = []

        for row in records:
            key = (
                row["farm_code"],
                row["cow_number"],
                row["milking_date"],
                (row.get("shift") or "").strip(),
                row.get("cow_milking_start_time"),
                row.get("milking_point"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)

            if skip_duplicates:
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
                pending_orm.append(
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
            else:
                pending_maps.append(
                    {
                        "batch_id": batch.id,
                        "farm_code": row["farm_code"],
                        "cow_number": row["cow_number"],
                        "avg_milk_flow_l_per_min": row.get("avg_milk_flow_l_per_min"),
                        "milking_date": row["milking_date"],
                        "shift": row.get("shift"),
                        "dim": row.get("dim"),
                        "shift_yield_l": row.get("shift_yield_l"),
                        "peak_milk_flow_l_per_min": row.get("peak_milk_flow_l_per_min"),
                        "peak_milk_flow_time": row.get("peak_milk_flow_time"),
                        "flow_rate_15s_ml_per_min": row.get("flow_rate_15s_ml_per_min"),
                        "flow_rate_30s_ml_per_min": row.get("flow_rate_30s_ml_per_min"),
                        "flow_rate_60s_ml_per_min": row.get("flow_rate_60s_ml_per_min"),
                        "flow_rate_120s_ml_per_min": row.get("flow_rate_120s_ml_per_min"),
                        "percentage_yield_at_2_min": row.get("percentage_yield_at_2_min"),
                        "milk_yield_at_2_min_l": row.get("milk_yield_at_2_min_l"),
                        "group_number": row.get("group_number"),
                        "flow_rate_at_removal_ml_per_min": row.get(
                            "flow_rate_at_removal_ml_per_min"
                        ),
                        "unit_on_time": row.get("unit_on_time"),
                        "cow_milking_start_time": row.get("cow_milking_start_time"),
                        "final_detaching": row.get("final_detaching"),
                        "milking_point": row.get("milking_point"),
                    }
                )
                if len(pending_maps) >= 1000:
                    _flush_pending()

        _flush_pending()
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


def save_rotary_entry_id_records(
    batch_meta: dict,
    records: list[dict],
    *,
    skip_duplicates: bool = True,
) -> tuple[int, int]:
    """
    Persist Rotary Entry ID rows.
    Always de-dupes within the batch. When skip_duplicates is True (default),
    also skips rows that already exist by cow + date + identification time.
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
        seen_keys: set[tuple] = set()
        pending_orm: list[RotaryEntryIdRecord] = []
        pending_maps: list[dict] = []

        def _flush_pending() -> None:
            nonlocal pending_orm, pending_maps, inserted
            if pending_maps:
                session.bulk_insert_mappings(RotaryEntryIdRecord, pending_maps)
                inserted += len(pending_maps)
                pending_maps = []
            if pending_orm:
                session.bulk_save_objects(pending_orm)
                inserted += len(pending_orm)
                pending_orm = []

        for row in records:
            key = (
                row["farm_code"],
                row["cow_number"],
                row["milking_date"],
                row["identification_time"],
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)

            if skip_duplicates:
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
                pending_orm.append(
                    RotaryEntryIdRecord(
                        batch_id=batch.id,
                        farm_code=row["farm_code"],
                        cow_number=row["cow_number"],
                        milking_date=row["milking_date"],
                        shift=row.get("shift"),
                        identification_time=row["identification_time"],
                    )
                )
            else:
                pending_maps.append(
                    {
                        "batch_id": batch.id,
                        "farm_code": row["farm_code"],
                        "cow_number": row["cow_number"],
                        "milking_date": row["milking_date"],
                        "shift": row.get("shift"),
                        "identification_time": row["identification_time"],
                    }
                )
                if len(pending_maps) >= 1000:
                    _flush_pending()

        _flush_pending()
        batch.row_count = inserted
        return batch.id, inserted


def get_imported_parlour_message_ids(farm_code: str | None = None) -> set[str]:
    with get_session() as session:
        query = session.query(ParlourImportBatch.message_id).filter(
            ParlourImportBatch.message_id.isnot(None)
        )
        if farm_code:
            query = query.filter(ParlourImportBatch.farm_code == farm_code)
        rows = query.all()
        return {row[0] for row in rows if row[0]}


def delete_parlour_records_in_date_range(
    farm_code: str | None,
    start_date,
    end_date,
) -> dict[str, int]:
    """Delete milk-flow and rotary-entry rows between inclusive dates.

    When farm_code is None, deletes for every farm in that window.
    """
    with get_session() as session:
        milk_query = session.query(MilkFlowRecord).filter(
            MilkFlowRecord.milking_date >= start_date,
            MilkFlowRecord.milking_date <= end_date,
        )
        entry_query = session.query(RotaryEntryIdRecord).filter(
            RotaryEntryIdRecord.milking_date >= start_date,
            RotaryEntryIdRecord.milking_date <= end_date,
        )
        if farm_code:
            milk_query = milk_query.filter(MilkFlowRecord.farm_code == farm_code)
            entry_query = entry_query.filter(RotaryEntryIdRecord.farm_code == farm_code)

        milk_deleted = milk_query.delete(synchronize_session=False)
        entry_deleted = entry_query.delete(synchronize_session=False)
        return {
            "milk_flow_deleted": int(milk_deleted or 0),
            "rotary_entry_deleted": int(entry_deleted or 0),
        }
