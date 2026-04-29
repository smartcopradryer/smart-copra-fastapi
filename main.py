# main.py

import os
from typing import Optional, Any

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


load_dotenv()

app = FastAPI(
    title="Smart Copra Dryer API",
    version="1.0.0",
)


# ===============================
# CORS
# ===============================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development. Restrict this in production.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===============================
# DATABASE
# ===============================
db_pool: Optional[asyncpg.Pool] = None


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if database_url:
        return database_url

    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    database = os.getenv("DB_NAME", "smart_copra_dryer")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")

    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


async def get_pool() -> asyncpg.Pool:
    if db_pool is None:
        raise HTTPException(
            status_code=500,
            detail="Database pool is not initialized",
        )

    return db_pool


def serialize_record(record: Any):
    """
    Converts asyncpg Record objects into normal dicts.
    Also handles lists of records.
    """
    if record is None:
        return None

    if isinstance(record, list):
        return [dict(row) for row in record]

    return dict(record)


@app.on_event("startup")
async def startup():
    global db_pool

    database_url = get_database_url()

    db_pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=10,
    )

    print("[DB] PostgreSQL connected")


@app.on_event("shutdown")
async def shutdown():
    global db_pool

    if db_pool:
        await db_pool.close()
        print("[DB] PostgreSQL disconnected")


# ===============================
# MODELS
# ===============================
class SessionPayload(BaseModel):
    active: Optional[bool] = None
    duration_ms: Optional[int] = Field(default=None, alias="durationMs")
    remaining_ms: Optional[int] = Field(default=None, alias="remainingMs")

    class Config:
        populate_by_name = True


class TelemetryPayload(BaseModel):
    device_id: str = Field(..., alias="deviceId")
    event: str = "HEARTBEAT"
    temp: Optional[float] = None
    status: str = "IDLE"
    overheat: bool = False
    session: Optional[SessionPayload] = None

    class Config:
        populate_by_name = True


# ===============================
# ROUTES
# ===============================
@app.get("/")
async def root():
    return {
        "success": True,
        "message": "Smart Copra Dryer API is running",
    }


@app.get("/health")
async def health():
    try:
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT NOW() AS now;")

        return {
            "success": True,
            "message": "OK",
            "serverTime": row["now"],
        }

    except Exception as error:
        print("[GET /health] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Database connection failed",
                "error": str(error),
            },
        )


@app.post("/api/telemetry", status_code=201)
async def save_telemetry(payload: TelemetryPayload):
    if not payload.device_id:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "message": "deviceId is required",
            },
        )

    pool = await get_pool()

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                device_row = await conn.fetchrow(
                    """
                    INSERT INTO dbo.devices (
                        device_id,
                        latest_temp,
                        latest_status,
                        overheat,
                        last_seen_at,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, NOW(), NOW(), NOW())
                    ON CONFLICT (device_id)
                    DO UPDATE SET
                        latest_temp = EXCLUDED.latest_temp,
                        latest_status = EXCLUDED.latest_status,
                        overheat = EXCLUDED.overheat,
                        last_seen_at = NOW(),
                        updated_at = NOW()
                    RETURNING *;
                    """,
                    payload.device_id,
                    payload.temp,
                    payload.status,
                    payload.overheat,
                )

                session_active = None
                session_duration_ms = None
                session_remaining_ms = None

                if payload.session:
                    session_active = payload.session.active
                    session_duration_ms = payload.session.duration_ms
                    session_remaining_ms = payload.session.remaining_ms

                log_row = await conn.fetchrow(
                    """
                    INSERT INTO dbo.telemetry_logs (
                        device_id,
                        event,
                        temp,
                        status,
                        overheat,
                        session_active,
                        session_duration_ms,
                        session_remaining_ms,
                        created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    RETURNING *;
                    """,
                    payload.device_id,
                    payload.event,
                    payload.temp,
                    payload.status,
                    payload.overheat,
                    session_active,
                    session_duration_ms,
                    session_remaining_ms,
                )

        return {
            "success": True,
            "message": "Telemetry saved successfully",
            "data": {
                "device": serialize_record(device_row),
                "log": serialize_record(log_row),
            },
        }

    except Exception as error:
        print("[POST /api/telemetry] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@app.get("/api/devices/{device_id}/latest")
async def get_latest_device(device_id: str):
    try:
        pool = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM dbo.devices
                WHERE device_id = $1
                LIMIT 1;
                """,
                device_id,
            )

        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "message": "Device not found",
                },
            )

        return {
            "success": True,
            "data": serialize_record(row),
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[GET latest] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@app.get("/api/devices/{device_id}/history")
async def get_device_history(
    device_id: str,
    limit: int = Query(default=50, ge=1, le=1000),
):
    try:
        pool = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM dbo.telemetry_logs
                WHERE device_id = $1
                ORDER BY created_at DESC
                LIMIT $2;
                """,
                device_id,
                limit,
            )

        return {
            "success": True,
            "data": serialize_record(rows),
        }

    except Exception as error:
        print("[GET history] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@app.get("/api/devices")
async def get_devices():
    try:
        pool = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM dbo.devices
                ORDER BY updated_at DESC;
                """
            )

        return {
            "success": True,
            "data": serialize_record(rows),
        }

    except Exception as error:
        print("[GET dbo.devices] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@app.get("/api/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=2000),
):
    try:
        pool = await get_pool()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM dbo.telemetry_logs
                ORDER BY created_at DESC
                LIMIT $1;
                """,
                limit,
            )

        return {
            "success": True,
            "data": serialize_record(rows),
        }

    except Exception as error:
        print("[GET logs] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


# ===============================
# 404 HANDLER
# ===============================
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return {
        "success": False,
        "message": "Route not found",
    }
