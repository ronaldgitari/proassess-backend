from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_ENV == "development",
    pool_pre_ping=True,
    # Sized for concurrent submits: each request holds one connection for its full
    # duration. Pipeline observability runs on its OWN pool (services/pipeline_tracker)
    # so it can't compete for these. Keep pool_size + max_overflow under Postgres
    # max_connections (default 100), accounting for the number of uvicorn workers.
    pool_size=20,
    max_overflow=40,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_tables():
    """Create all tables. Used during startup in development."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
