import os

from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,
    connect_args={"options": "-c statement_timeout=15000"},
)
SessionLocal = sessionmaker(engine)

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, kwargs={"options": "-c statement_timeout=15000"})
