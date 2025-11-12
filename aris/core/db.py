from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from urllib.parse import urlparse

from .config import settings


DATABASE_URL = settings.DATABASE_URL

connect_args = {}

# Exclusively for psycopg2: Enable TCP keepalive and reduce the probe interval.
if DATABASE_URL.startswith("postgresql"):
    connect_args.update(
        {
            "keepalives": 1,
            "keepalives_idle": 30,  # probe after idle for 30s
            "keepalives_interval": 10,  # probe every 10 seconds
            "keepalives_count": 5,  # 5 failures counts for disconnected
        }
    )

engine = create_engine(
    DATABASE_URL,
    future=True,
    echo=False,
    pool_pre_ping=True,  # reconnect if disconnected
    pool_recycle=180,  # Recycle timeout (in seconds) MUST be less than the idle timeout of any intermediate network, NAT, or PgBouncer.
    pool_size=10,
    max_overflow=20,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
