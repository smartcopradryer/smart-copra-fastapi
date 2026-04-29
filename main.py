import os
from datetime import datetime
from typing import Optional, Any, Dict, List
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# =========================================================
# LOAD ENV
# =========================================================
load_dotenv()


# =========================================================
# APP
# =========================================================
app = FastAPI(
    title="Smart Copra Dryer API",
    version="2.0.0",
    description="FastAPI + Firebase Realtime Database API for Smart Copra Dryer telemetry.",
)


# =========================================================
# CORS
# =========================================================
def get_cors_origins():
    """
    CORS_ORIGINS can be:
    CORS_ORIGINS=*
    or
    CORS_ORIGINS=http://localhost:4200,https://your-app.vercel.app
    """
    origins = os.getenv("CORS_ORIGINS", "*")

    if origins.strip() == "*":
        return ["*"]

    return [origin.strip() for origin in origins.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# HELPERS
# =========================================================
def get_timezone() -> ZoneInfo:
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Manila")
    return ZoneInfo(timezone_name)


def now_iso() -> str:
    return datetime.now(get_timezone()).isoformat()


def sanitize_firebase_key(value: str) -> str:
    """
    Firebase Realtime Database keys cannot contain:
    . # $ / [ ]

    This keeps device_id safe as a Firebase path key.
    """
    if not value:
        return value

    invalid_chars = [".", "#", "$", "/", "[", "]"]

    sanitized = value.strip()

    for char in invalid_chars:
        sanitized = sanitized.replace(char, "_")

    return sanitized


def sort_by_created_at_desc(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )


def limit_items(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    return items[:limit]


def firebase_ready() -> bool:
    return len(firebase_admin._apps) > 0


def get_ref(path: str):
    if not firebase_ready():
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Firebase is not initialized",
                "hint": "Check FIREBASE_DATABASE_URL and FIREBASE_SERVICE_ACCOUNT_PATH.",
            },
        )

    return db.reference(path)


# =========================================================
# FIREBASE INIT
# =========================================================
def initialize_firebase():
    if firebase_ready():
        return

    database_url = os.getenv("FIREBASE_DATABASE_URL")
    service_account_path = os.getenv(
        "FIREBASE_SERVICE_ACCOUNT_PATH",
        "firebase-service-account.json",
    )

    if not database_url:
        raise RuntimeError("FIREBASE_DATABASE_URL is missing in .env")

    if not os.path.exists(service_account_path):
        raise RuntimeError(
            f"Firebase service account file not found: {service_account_path}"
        )

    cred = credentials.Certificate(service_account_path)

    firebase_admin.initialize_app(
        cred,
        {
            "databaseURL": database_url,
        },
    )


@app.on_event("startup")
async def startup():
    try:
        initialize_firebase()
        print("[FIREBASE] Realtime Database connected")

    except Exception as error:
        print("[FIREBASE] Initialization failed:", error)


@app.on_event("shutdown")
async def shutdown():
    print("[FIREBASE] Shutdown complete")


# =========================================================
# MODELS
# =========================================================
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


# =========================================================
# ROUTES
# =========================================================
@app.get("/")
async def root():
    return {
        "success": True,
        "message": "Smart Copra Dryer API is running",
        "database": "firebase_realtime_database",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
async def health():
    try:
        if not firebase_ready():
            raise RuntimeError("Firebase app is not initialized")

        # Simple read test
        connected_test = get_ref("/").get(shallow=True)

        return {
            "success": True,
            "message": "OK",
            "database": "connected",
            "database_type": "firebase_realtime_database",
            "server_time": now_iso(),
            "firebase_ready": True,
            "root_keys": connected_test if connected_test else {},
        }

    except Exception as error:
        print("[GET /health] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Firebase connection failed",
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

    try:
        original_device_id = payload.device_id.strip()
        device_key = sanitize_firebase_key(original_device_id)
        timestamp = now_iso()

        devices_ref = get_ref("devices")
        device_ref = get_ref(f"devices/{device_key}")
        telemetry_logs_ref = get_ref("telemetry_logs")
        device_logs_ref = get_ref(f"device_logs/{device_key}")

        existing_device = device_ref.get()

        device_data = {
            "device_id": original_device_id,
            "latest_temp": payload.temp,
            "latest_status": payload.status,
            "overheat": payload.overheat,
            "last_seen_at": timestamp,
            "updated_at": timestamp,
        }

        if existing_device and existing_device.get("created_at"):
            device_data["created_at"] = existing_device.get("created_at")
        else:
            device_data["created_at"] = timestamp

        session_active = None
        session_duration_ms = None
        session_remaining_ms = None

        if payload.session:
            session_active = payload.session.active
            session_duration_ms = payload.session.duration_ms
            session_remaining_ms = payload.session.remaining_ms

        log_ref = telemetry_logs_ref.push()
        log_id = log_ref.key

        log_data = {
            "id": log_id,
            "device_id": original_device_id,
            "device_key": device_key,
            "event": payload.event,
            "temp": payload.temp,
            "status": payload.status,
            "overheat": payload.overheat,
            "session_active": session_active,
            "session_duration_ms": session_duration_ms,
            "session_remaining_ms": session_remaining_ms,
            "created_at": timestamp,
        }

        # Multi-location update is closer to a transaction-style write in Firebase RTDB.
        updates = {
            f"devices/{device_key}": device_data,
            f"telemetry_logs/{log_id}": log_data,
            f"device_logs/{device_key}/{log_id}": True,
        }

        get_ref("/").update(updates)

        return {
            "success": True,
            "message": "Telemetry saved successfully",
            "data": {
                "device": device_data,
                "log": log_data,
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


@app.get("/api/devices")
async def get_devices():
    try:
        devices = get_ref("devices").get()

        if not devices:
            return {
                "success": True,
                "data": [],
            }

        rows = []

        for key, value in devices.items():
            if isinstance(value, dict):
                value["firebase_key"] = key
                rows.append(value)

        rows = sorted(
            rows,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

        return {
            "success": True,
            "data": rows,
        }

    except Exception as error:
        print("[GET /api/devices] Error:", error)

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
        device_key = sanitize_firebase_key(device_id)
        row = get_ref(f"devices/{device_key}").get()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "message": "Device not found",
                },
            )

        row["firebase_key"] = device_key

        return {
            "success": True,
            "data": row,
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
        device_key = sanitize_firebase_key(device_id)

        device_log_index = get_ref(f"device_logs/{device_key}").get()

        if not device_log_index:
            return {
                "success": True,
                "data": [],
            }

        log_ids = list(device_log_index.keys())

        rows = []

        for log_id in log_ids:
            log = get_ref(f"telemetry_logs/{log_id}").get()

            if log:
                rows.append(log)

        rows = sort_by_created_at_desc(rows)
        rows = limit_items(rows, limit)

        return {
            "success": True,
            "data": rows,
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


@app.get("/api/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=2000),
):
    try:
        logs = get_ref("telemetry_logs").get()

        if not logs:
            return {
                "success": True,
                "data": [],
            }

        rows = []

        for key, value in logs.items():
            if isinstance(value, dict):
                if "id" not in value:
                    value["id"] = key

                rows.append(value)

        rows = sort_by_created_at_desc(rows)
        rows = limit_items(rows, limit)

        return {
            "success": True,
            "data": rows,
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


# =========================================================
# OPTIONAL: CLEAR TEST DATA
# =========================================================
@app.delete("/api/debug/clear-data")
async def clear_data():
    """
    For development only.
    Remove this route in production if not needed.
    """
    try:
        get_ref("devices").delete()
        get_ref("telemetry_logs").delete()
        get_ref("device_logs").delete()

        return {
            "success": True,
            "message": "Firebase test data cleared",
        }

    except Exception as error:
        print("[DELETE clear-data] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


# =========================================================
# 404 HANDLER
# =========================================================
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={
            "success": False,
            "message": "Route not found",
            "path": str(request.url.path),
        },
    )