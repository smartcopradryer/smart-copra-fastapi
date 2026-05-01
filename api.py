# api.py

from typing import Optional, Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from config import firebase_ready, get_ref, now_iso
from services import (
    get_current_app_user,
    MachineService,
    CommandService,
    TelemetryService,
    ws_manager,
)


router = APIRouter()


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


class MachineRegisterPayload(BaseModel):
    machine_id: str = Field(..., alias="machineId")
    serial_number: Optional[str] = Field(default=None, alias="serialNumber")
    model_name: Optional[str] = Field(default=None, alias="modelName")
    firmware_version: Optional[str] = Field(default=None, alias="firmwareVersion")
    pair_code: Optional[str] = Field(default=None, alias="pairCode")
    allow_replace: bool = Field(default=False, alias="allowReplace")

    class Config:
        populate_by_name = True


class PairingPayload(BaseModel):
    user_id: Optional[str] = Field(default=None, alias="userId")

    machine_id: Optional[str] = Field(default=None, alias="machineId")
    pair_code: Optional[str] = Field(default=None, alias="pairCode")
    qr_payload: Optional[str] = Field(default=None, alias="qrPayload")

    force_unpair_old: bool = Field(default=False, alias="forceUnpairOld")
    require_pairing_mode: Optional[bool] = Field(default=None, alias="requirePairingMode")

    class Config:
        populate_by_name = True


class UnpairPayload(BaseModel):
    machine_id: Optional[str] = Field(default=None, alias="machineId")

    class Config:
        populate_by_name = True


class PairingModePayload(BaseModel):
    machine_id: str = Field(..., alias="machineId")
    minutes: int = 2

    class Config:
        populate_by_name = True


class StartSessionCommandPayload(BaseModel):
    duration_minutes: int = Field(..., alias="durationMinutes")

    class Config:
        populate_by_name = True


class CommandResultPayload(BaseModel):
    result: str = "ACCEPTED"
    machine_status: Optional[str] = Field(default=None, alias="machineStatus")
    message: Optional[str] = None

    class Config:
        populate_by_name = True


# =========================================================
# BASIC ROUTES
# =========================================================
@router.get("/")
async def root():
    return {
        "success": True,
        "message": "Smart Copra Dryer API is running",
        "database": "firebase_realtime_database",
        "websocket": "/ws",
        "docs": "/docs",
        "health": "/health",
        "auth": {
            "sync_google_user": "/api/auth/google/sync",
            "me": "/api/auth/me",
        },
        "pairing": {
            "register_machine": "/api/machines/register",
            "pair": "/api/pairing/pair",
            "unpair": "/api/pairing/unpair",
            "current_pairing": "/api/users/me/paired-machine",
        },
        "commands": {
            "start_session": "/api/machines/{machine_id}/commands/start-session",
            "pause": "/api/machines/{machine_id}/commands/pause",
            "resume": "/api/machines/{machine_id}/commands/resume",
            "stop": "/api/machines/{machine_id}/commands/stop",
            "pending": "/api/machines/{machine_id}/commands/pending",
            "result": "/api/machines/{machine_id}/commands/{command_id}/result",
        },
        "sessions": {
            "history": "/api/machines/{machine_id}/sessions/history",
        },
    }


@router.get("/health")
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
            "websocket_clients": len(ws_manager.active_connections),
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


@router.get("/api/ws-info")
async def websocket_info(request: Request):
    scheme = "wss" if request.url.scheme == "https" else "ws"
    host = request.headers.get("host", "localhost:3000")

    return {
        "success": True,
        "websocket_path": "/ws",
        "websocket_url": f"{scheme}://{host}/ws",
        "clients": len(ws_manager.active_connections),
    }


# =========================================================
# AUTH ROUTES
# =========================================================
@router.post("/api/auth/google/sync")
async def sync_google_user(
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    return {
        "success": True,
        "message": "User synced successfully",
        "data": {
            "userId": app_user.get("user_id"),
            "userKey": app_user.get("user_key"),
            "firebaseUid": app_user.get("firebase_uid"),
            "email": app_user.get("email"),
            "emailVerified": app_user.get("email_verified"),
            "displayName": app_user.get("display_name"),
            "photoUrl": app_user.get("photo_url"),
            "provider": app_user.get("provider"),
            "createdAt": app_user.get("created_at"),
            "updatedAt": app_user.get("updated_at"),
            "lastLoginAt": app_user.get("last_login_at"),
        },
    }


@router.get("/api/auth/me")
async def get_me(
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    user_key = app_user.get("user_key")
    pairing = get_ref(f"user_pairings/{user_key}").get() if user_key else None

    return {
        "success": True,
        "data": {
            "user": {
                "userId": app_user.get("user_id"),
                "userKey": app_user.get("user_key"),
                "firebaseUid": app_user.get("firebase_uid"),
                "email": app_user.get("email"),
                "emailVerified": app_user.get("email_verified"),
                "displayName": app_user.get("display_name"),
                "photoUrl": app_user.get("photo_url"),
                "provider": app_user.get("provider"),
            },
            "pairing": pairing,
        },
    }


# =========================================================
# MACHINE / PAIRING ROUTES
# =========================================================
@router.post("/api/machines/register", status_code=201)
async def register_machine(payload: MachineRegisterPayload):
    try:
        data = MachineService.register_machine(payload)

        return {
            "success": True,
            "message": "Machine registered successfully",
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST /api/machines/register] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.post("/api/machines/pairing-mode")
async def enable_pairing_mode(payload: PairingModePayload):
    try:
        data = await MachineService.enable_pairing_mode(payload)

        return {
            "success": True,
            "message": "Pairing mode enabled",
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST /api/machines/pairing-mode] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.get("/api/machines/{machine_id}/pairing-status")
async def get_machine_pairing_status(machine_id: str):
    try:
        data = MachineService.get_pairing_status(machine_id)

        return {
            "success": True,
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[GET pairing-status] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.post("/api/pairing/pair")
async def pair_machine(
    payload: PairingPayload,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = await MachineService.pair_machine(payload, app_user)

        return {
            "success": True,
            "message": "Machine paired successfully",
            "data": data,
            "websocket_broadcasted": True,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST /api/pairing/pair] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.post("/api/pairing/unpair")
async def unpair_machine(
    payload: UnpairPayload,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = await MachineService.unpair_machine(payload, app_user)

        if data is None:
            return {
                "success": True,
                "message": "User has no active paired machine",
                "data": None,
            }

        return {
            "success": True,
            "message": "Machine unpaired successfully",
            "data": data,
            "websocket_broadcasted": True,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST /api/pairing/unpair] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.get("/api/users/me/paired-machine")
async def get_my_paired_machine(
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = MachineService.get_my_paired_machine(app_user)

        if data is None:
            return {
                "success": True,
                "message": "No active paired machine",
                "data": None,
            }

        return {
            "success": True,
            "data": data,
        }

    except Exception as error:
        print("[GET /api/users/me/paired-machine] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.get("/api/users/{user_id}/paired-machine")
async def get_user_paired_machine_by_id(user_id: str):
    try:
        data = MachineService.get_user_paired_machine_by_id(user_id)

        if data is None:
            return {
                "success": True,
                "message": "No active paired machine",
                "data": None,
            }

        return {
            "success": True,
            "data": data,
        }

    except Exception as error:
        print("[GET paired-machine by id] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


# =========================================================
# MACHINE COMMAND ROUTES
# =========================================================
@router.post("/api/machines/{machine_id}/commands/start-session")
async def start_machine_session(
    machine_id: str,
    payload: StartSessionCommandPayload,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = await CommandService.start_session(machine_id, payload, app_user)

        return {
            "success": True,
            "message": "Start session command queued",
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST start-session] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.post("/api/machines/{machine_id}/commands/pause")
async def pause_machine_session(
    machine_id: str,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = await CommandService.pause(machine_id, app_user)

        return {
            "success": True,
            "message": "Pause command queued",
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST pause] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.post("/api/machines/{machine_id}/commands/resume")
async def resume_machine_session(
    machine_id: str,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = await CommandService.resume(machine_id, app_user)

        return {
            "success": True,
            "message": "Resume command queued",
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST resume] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.post("/api/machines/{machine_id}/commands/stop")
async def stop_machine_session(
    machine_id: str,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = await CommandService.stop(machine_id, app_user)

        return {
            "success": True,
            "message": "Stop command queued",
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST stop] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.get("/api/machines/{machine_id}/commands/pending")
async def get_pending_machine_commands(
    machine_id: str,
    limit: int = Query(default=1, ge=1, le=10),
):
    try:
        data = CommandService.get_pending_commands(machine_id, limit)

        return {
            "success": True,
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[GET pending commands] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


@router.post("/api/machines/{machine_id}/commands/{command_id}/result")
async def post_machine_command_result(
    machine_id: str,
    command_id: str,
    payload: CommandResultPayload,
):
    try:
        data = await CommandService.mark_command_result(
            machine_id,
            command_id,
            payload,
        )

        return {
            "success": True,
            "message": "Command result saved",
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[POST command result] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


# =========================================================
# SESSION HISTORY ROUTES
# =========================================================
@router.get("/api/machines/{machine_id}/sessions/history")
async def get_machine_session_history(
    machine_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        data = CommandService.get_session_history(machine_id, app_user, limit)

        return {
            "success": True,
            "data": data,
        }

    except HTTPException:
        raise

    except Exception as error:
        print("[GET session history] Error:", error)

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Internal server error",
                "error": str(error),
            },
        )


# =========================================================
# TELEMETRY ROUTES
# =========================================================
@router.post("/api/telemetry", status_code=201)
async def save_telemetry(payload: TelemetryPayload):
    try:
        data = await TelemetryService.save_telemetry(payload)

        return {
            "success": True,
            "message": "Telemetry saved successfully",
            "websocket_broadcasted": True,
            "websocket_clients": len(ws_manager.active_connections),
            "data": data,
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


@router.get("/api/devices")
async def get_devices():
    try:
        return {
            "success": True,
            "data": TelemetryService.get_devices(),
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


@router.get("/api/devices/{device_id}/latest")
async def get_latest_device(device_id: str):
    try:
        return {
            "success": True,
            "data": TelemetryService.get_latest_device(device_id),
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


@router.get("/api/devices/{device_id}/history")
async def get_device_history(
    device_id: str,
    limit: int = Query(default=50, ge=1, le=1000),
):
    try:
        return {
            "success": True,
            "data": TelemetryService.get_device_history(device_id, limit),
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


@router.get("/api/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=2000),
):
    try:
        return {
            "success": True,
            "data": TelemetryService.get_logs(limit),
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