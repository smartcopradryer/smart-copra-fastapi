import os
import json
import base64
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Dict, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    version="2.2.0",
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
def get_timezone():
    timezone_name = os.getenv("APP_TIMEZONE", "Asia/Manila")

    try:
        return ZoneInfo(timezone_name)

    except ZoneInfoNotFoundError:
        print(f"[TIMEZONE] {timezone_name} not found. Falling back to UTC+08:00.")
        return timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(get_timezone()).isoformat()


def sanitize_firebase_key(value: str) -> str:
    """
    Firebase Realtime Database keys cannot contain:
    . # $ / [ ]

    This converts invalid characters to underscore.
    Example:
    dryer/001 -> dryer_001
    """
    if not value:
        return value

    sanitized = value.strip()

    for char in [".", "#", "$", "/", "[", "]"]:
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
                "hint": "Check FIREBASE_DATABASE_URL and FIREBASE_SERVICE_ACCOUNT_JSON_B64.",
            },
        )

    return db.reference(path)


# =========================================================
# FIREBASE INIT
# =========================================================
def initialize_firebase():
    """
    Uses Firebase service account from environment variable.

    Preferred:
    FIREBASE_SERVICE_ACCOUNT_JSON_B64=base64_encoded_service_account_json

    Optional fallback:
    FIREBASE_SERVICE_ACCOUNT_JSON=one_line_json
    """
    if firebase_ready():
        return

    database_url = os.getenv("FIREBASE_DATABASE_URL")
    service_account_json_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_B64")
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not database_url:
        raise RuntimeError("FIREBASE_DATABASE_URL is missing in .env")

    try:
        if service_account_json_b64:
            print("[FIREBASE] Using service account from FIREBASE_SERVICE_ACCOUNT_JSON_B64")

            decoded_json = base64.b64decode(service_account_json_b64).decode("utf-8")
            service_account_info = json.loads(decoded_json)

        elif service_account_json:
            print("[FIREBASE] Using service account from FIREBASE_SERVICE_ACCOUNT_JSON")

            service_account_info = json.loads(service_account_json)

        else:
            raise RuntimeError(
                "Missing Firebase credentials. "
                "Set FIREBASE_SERVICE_ACCOUNT_JSON_B64 in .env."
            )

    except Exception as error:
        raise RuntimeError(
            f"Failed to parse Firebase service account from environment variable. "
            f"Error: {error}"
        )

    required_keys = [
        "type",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "token_uri",
    ]

    missing_keys = [
        key for key in required_keys
        if key not in service_account_info or not service_account_info.get(key)
    ]

    if missing_keys:
        raise RuntimeError(
            f"Firebase service account is missing required keys: {missing_keys}"
        )

    private_key = service_account_info.get("private_key", "")

    # Supports either escaped \\n or real newlines.
    if "\\n" in private_key:
        service_account_info["private_key"] = private_key.replace("\\n", "\n")

    try:
        cred = credentials.Certificate(service_account_info)

        firebase_admin.initialize_app(
            cred,
            {
                "databaseURL": database_url,
            },
        )

        print("[FIREBASE] Realtime Database connected")

    except Exception as error:
        raise RuntimeError(f"Firebase initialization failed: {error}")


@app.on_event("startup")
async def startup():
    try:
        initialize_firebase()

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

        root_keys = get_ref("/").get(shallow=True)

        return {
            "success": True,
            "message": "OK",
            "database": "connected",
            "database_type": "firebase_realtime_database",
            "server_time": now_iso(),
            "firebase_ready": True,
            "root_keys": root_keys if root_keys else {},
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

        device_ref = get_ref(f"devices/{device_key}")
        telemetry_logs_ref = get_ref("telemetry_logs")

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

    except HTTPException:
        raise

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