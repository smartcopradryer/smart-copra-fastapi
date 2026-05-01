import os
import json
import base64
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Dict, List, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlparse, parse_qs

import firebase_admin
from firebase_admin import credentials, db, auth
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    Header,
    Depends,
)
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
    version="2.5.0",
    description="FastAPI + Firebase Realtime Database + WebSocket + Google Auth + Dryer Pairing API.",
)


# =========================================================
# CORS
# =========================================================
def get_cors_origins():
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


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in ["1", "true", "yes", "y", "on"]


def sanitize_firebase_key(value: str) -> str:
    if not value:
        return value

    sanitized = value.strip()

    for char in [".", "#", "$", "/", "[", "]"]:
        sanitized = sanitized.replace(char, "_")

    return sanitized


def normalize_id(value: str) -> str:
    return value.strip().upper()


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


def get_pairing_secret() -> str:
    secret = os.getenv("PAIRING_SECRET", "").strip()

    if not secret:
        secret = "dev-smart-copra-dryer-pairing-secret"

    return secret


def hash_pair_code(machine_id: str, pair_code: str) -> str:
    raw = f"{normalize_id(machine_id)}:{pair_code.strip()}:{get_pairing_secret()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_pair_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    part1 = "".join(secrets.choice(alphabet) for _ in range(4))
    part2 = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"{part1}-{part2}"


def parse_qr_payload(qr_payload: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Supported QR formats:

    1. URL:
       https://yourapp.com/pair?machineId=SCD-000123&pairCode=AB7K-92XM

    2. Text:
       SMART_COPRA_DRYER|machineId=SCD-000123|pairCode=AB7K-92XM

    3. JSON:
       {"machineId":"SCD-000123","pairCode":"AB7K-92XM"}
    """
    if not qr_payload:
        return None, None

    qr_payload = qr_payload.strip()

    if qr_payload.startswith("{"):
        try:
            parsed = json.loads(qr_payload)

            machine_id = (
                parsed.get("machineId")
                or parsed.get("machine_id")
                or parsed.get("deviceId")
                or parsed.get("device_id")
            )

            pair_code = (
                parsed.get("pairCode")
                or parsed.get("pair_code")
                or parsed.get("claimToken")
                or parsed.get("claim_token")
                or parsed.get("code")
            )

            return machine_id, pair_code

        except Exception:
            pass

    if qr_payload.startswith("http://") or qr_payload.startswith("https://"):
        parsed_url = urlparse(qr_payload)
        query = parse_qs(parsed_url.query)

        def first_value(*keys):
            for key in keys:
                values = query.get(key)
                if values:
                    return values[0]
            return None

        machine_id = first_value("machineId", "machine_id", "deviceId", "device_id")
        pair_code = first_value("pairCode", "pair_code", "claimToken", "claim_token", "code")

        return machine_id, pair_code

    parts = qr_payload.split("|")
    values = {}

    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            values[key.strip()] = value.strip()

    machine_id = (
        values.get("machineId")
        or values.get("machine_id")
        or values.get("deviceId")
        or values.get("device_id")
    )

    pair_code = (
        values.get("pairCode")
        or values.get("pair_code")
        or values.get("claimToken")
        or values.get("claim_token")
        or values.get("code")
    )

    return machine_id, pair_code


def build_qr_payload(machine_id: str, pair_code: str) -> str:
    app_pair_url = os.getenv("APP_PAIR_URL", "").strip()

    if app_pair_url:
        separator = "&" if "?" in app_pair_url else "?"
        return f"{app_pair_url}{separator}machineId={machine_id}&pairCode={pair_code}"

    return f"SMART_COPRA_DRYER|machineId={machine_id}|pairCode={pair_code}"


# =========================================================
# AUTH HELPERS
# =========================================================
def get_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "message": "Missing Authorization header",
                "hint": "Use Authorization: Bearer <firebase_id_token>",
            },
        )

    parts = authorization.split(" ")

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "message": "Invalid Authorization header",
                "hint": "Use Authorization: Bearer <firebase_id_token>",
            },
        )

    return parts[1].strip()

async def get_current_firebase_user(
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    token = get_bearer_token(authorization)

    try:
        decoded_token = auth.verify_id_token(
            token,
            clock_skew_seconds=10,
        )

        return decoded_token

    except Exception as error:
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "message": "Invalid or expired Firebase ID token",
                "error": str(error),
            },
        )

def generate_custom_user_id() -> str:
    year = datetime.now(get_timezone()).year
    counter_ref = get_ref("meta/user_counter")

    def increment_counter(current_value):
        if current_value is None:
            return 1

        return int(current_value) + 1

    next_value = counter_ref.transaction(increment_counter)

    return f"USR-{year}-{int(next_value):06d}"


def get_or_create_app_user(decoded_token: Dict[str, Any]) -> Dict[str, Any]:
    firebase_uid = decoded_token.get("uid")

    if not firebase_uid:
        raise HTTPException(
            status_code=401,
            detail={
                "success": False,
                "message": "Firebase token has no uid",
            },
        )

    uid_key = sanitize_firebase_key(firebase_uid)
    existing_user_id = get_ref(f"firebase_uid_to_user_id/{uid_key}").get()
    timestamp = now_iso()

    email = decoded_token.get("email")
    display_name = decoded_token.get("name")
    photo_url = decoded_token.get("picture")
    email_verified = decoded_token.get("email_verified", False)

    firebase_claims = decoded_token.get("firebase", {})
    sign_in_provider = firebase_claims.get("sign_in_provider", "google.com")

    if existing_user_id:
        user_key = sanitize_firebase_key(existing_user_id)
        user_ref = get_ref(f"users/{user_key}")
        existing_user = user_ref.get() or {}

        updated_user = {
            **existing_user,
            "user_id": existing_user_id,
            "user_key": user_key,
            "firebase_uid": firebase_uid,
            "email": email,
            "email_verified": email_verified,
            "display_name": display_name,
            "photo_url": photo_url,
            "provider": sign_in_provider,
            "updated_at": timestamp,
            "last_login_at": timestamp,
        }

        if not updated_user.get("created_at"):
            updated_user["created_at"] = timestamp

        user_ref.set(updated_user)

        return updated_user

    custom_user_id = generate_custom_user_id()
    user_key = sanitize_firebase_key(custom_user_id)

    new_user = {
        "user_id": custom_user_id,
        "user_key": user_key,
        "firebase_uid": firebase_uid,
        "email": email,
        "email_verified": email_verified,
        "display_name": display_name,
        "photo_url": photo_url,
        "provider": sign_in_provider,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_login_at": timestamp,
    }

    updates = {
        f"users/{user_key}": new_user,
        f"firebase_uid_to_user_id/{uid_key}": custom_user_id,
    }

    get_ref("/").update(updates)

    return new_user


async def get_current_app_user(
    decoded_token: Dict[str, Any] = Depends(get_current_firebase_user),
) -> Dict[str, Any]:
    return get_or_create_app_user(decoded_token)


# =========================================================
# WEBSOCKET MANAGER
# =========================================================
class WebSocketManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

        await websocket.send_json({
            "type": "connected",
            "message": "Connected to Smart Copra Dryer WebSocket",
            "server_time": now_iso(),
            "clients": len(self.active_connections),
        })

        print(f"[WS] Client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

        print(f"[WS] Client disconnected. Total: {len(self.active_connections)}")

    async def send_personal_message(self, websocket: WebSocket, message: Dict[str, Any]):
        try:
            await websocket.send_json(message)

        except Exception as error:
            print("[WS] Personal send failed:", error)
            self.disconnect(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        if not self.active_connections:
            return

        disconnected_clients = []

        for connection in self.active_connections:
            try:
                await connection.send_json(message)

            except Exception as error:
                print("[WS] Broadcast failed:", error)
                disconnected_clients.append(connection)

        for connection in disconnected_clients:
            self.disconnect(connection)


ws_manager = WebSocketManager()


# =========================================================
# FIREBASE INIT
# =========================================================
def initialize_firebase():
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
    # userId is intentionally optional.
    # Secure routes use Firebase token and ignore body userId.
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


# =========================================================
# ROUTES - BASIC
# =========================================================
@app.get("/")
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


@app.get("/api/ws-info")
async def websocket_info(request: Request):
    scheme = "wss" if request.url.scheme == "https" else "ws"
    host = request.headers.get("host", "localhost:3000")

    return {
        "success": True,
        "websocket_path": "/ws",
        "websocket_url": f"{scheme}://{host}/ws",
        "clients": len(ws_manager.active_connections),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)

    try:
        while True:
            data = await websocket.receive_text()

            try:
                parsed = json.loads(data)

            except Exception:
                parsed = {
                    "type": "message",
                    "message": data,
                }

            message_type = parsed.get("type")

            if message_type == "ping":
                await ws_manager.send_personal_message(
                    websocket,
                    {
                        "type": "pong",
                        "server_time": now_iso(),
                    },
                )

            else:
                await ws_manager.send_personal_message(
                    websocket,
                    {
                        "type": "echo",
                        "received": parsed,
                        "server_time": now_iso(),
                    },
                )

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

    except Exception as error:
        print("[WS] Error:", error)
        ws_manager.disconnect(websocket)


# =========================================================
# ROUTES - AUTH
# =========================================================
@app.post("/api/auth/google/sync")
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


@app.get("/api/auth/me")
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
# ROUTES - MACHINE REGISTRATION + PAIRING
# =========================================================
@app.post("/api/machines/register", status_code=201)
async def register_machine(payload: MachineRegisterPayload):
    """
    Manufacturing/admin endpoint.

    This returns the raw pairCode ONCE so you can print it as:
    - QR code
    - Manual Machine ID + Pair Code label

    In production, protect this endpoint with an admin API key or admin Firebase account.
    """
    try:
        machine_id = normalize_id(payload.machine_id)
        machine_key = sanitize_firebase_key(machine_id)

        existing_machine = get_ref(f"machines/{machine_key}").get()

        if existing_machine and not payload.allow_replace:
            raise HTTPException(
                status_code=409,
                detail={
                    "success": False,
                    "message": "Machine already registered",
                    "machineId": machine_id,
                    "hint": "Set allowReplace=true only if you intentionally want to update this machine.",
                },
            )

        pair_code = payload.pair_code.strip() if payload.pair_code else generate_pair_code()
        timestamp = now_iso()

        machine_data = {
            "machine_id": machine_id,
            "machine_key": machine_key,
            "serial_number": payload.serial_number,
            "model_name": payload.model_name,
            "firmware_version": payload.firmware_version,
            "pair_code_hash": hash_pair_code(machine_id, pair_code),
            "owner_user_id": existing_machine.get("owner_user_id") if existing_machine else None,
            "pairing_mode": False,
            "pairing_mode_until": None,
            "created_at": existing_machine.get("created_at") if existing_machine else timestamp,
            "updated_at": timestamp,
        }

        get_ref(f"machines/{machine_key}").set(machine_data)

        qr_payload = build_qr_payload(machine_id, pair_code)

        return {
            "success": True,
            "message": "Machine registered successfully",
            "data": {
                "machineId": machine_id,
                "machineKey": machine_key,
                "pairCode": pair_code,
                "qrPayload": qr_payload,
                "warning": "Save/print the pairCode now. The backend stores only its hash.",
            },
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


@app.post("/api/machines/pairing-mode")
async def enable_pairing_mode(payload: PairingModePayload):
    """
    Optional endpoint for physical PAIR button.

    Dryer calls this when user holds PAIR button.
    If PAIRING_REQUIRE_MODE=true, user cannot pair unless this mode is active.
    """
    try:
        machine_id = normalize_id(payload.machine_id)
        machine_key = sanitize_firebase_key(machine_id)

        machine = get_ref(f"machines/{machine_key}").get()

        if not machine:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "message": "Machine is not registered",
                    "machineId": machine_id,
                },
            )

        minutes = max(1, min(payload.minutes, 10))
        until = datetime.now(get_timezone()) + timedelta(minutes=minutes)
        timestamp = now_iso()

        updates = {
            f"machines/{machine_key}/pairing_mode": True,
            f"machines/{machine_key}/pairing_mode_until": until.isoformat(),
            f"machines/{machine_key}/updated_at": timestamp,
        }

        get_ref("/").update(updates)

        await ws_manager.broadcast({
            "type": "machine_pairing_mode_enabled",
            "message": "Machine entered pairing mode",
            "server_time": now_iso(),
            "data": {
                "machineId": machine_id,
                "pairingModeUntil": until.isoformat(),
            },
        })

        return {
            "success": True,
            "message": "Pairing mode enabled",
            "data": {
                "machineId": machine_id,
                "pairingModeUntil": until.isoformat(),
            },
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


@app.get("/api/machines/{machine_id}/pairing-status")
async def get_machine_pairing_status(machine_id: str):
    try:
        normalized_machine_id = normalize_id(machine_id)
        machine_key = sanitize_firebase_key(normalized_machine_id)

        machine = get_ref(f"machines/{machine_key}").get()

        if not machine:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "message": "Machine is not registered",
                    "machineId": normalized_machine_id,
                },
            )

        pairing_until = parse_iso_datetime(machine.get("pairing_mode_until"))
        pairing_active = False

        if machine.get("pairing_mode") and pairing_until:
            pairing_active = datetime.now(get_timezone()) <= pairing_until

        return {
            "success": True,
            "data": {
                "machineId": normalized_machine_id,
                "registered": True,
                "owned": bool(machine.get("owner_user_id")),
                "ownerUserId": machine.get("owner_user_id"),
                "pairingMode": pairing_active,
                "pairingModeUntil": machine.get("pairing_mode_until"),
                "lastSeenAt": machine.get("last_seen_at"),
                "updatedAt": machine.get("updated_at"),
            },
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


@app.post("/api/pairing/pair")
async def pair_machine(
    payload: PairingPayload,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    """
    Secure pairing route.

    The user is taken from the Firebase ID token.
    The app should NOT manually decide userId.

    Manual body:
    {
      "machineId": "SCD-000123",
      "pairCode": "AB7K-92XM"
    }

    QR body:
    {
      "qrPayload": "SMART_COPRA_DRYER|machineId=SCD-000123|pairCode=AB7K-92XM"
    }

    If user already has another dryer:
    {
      "machineId": "SCD-000124",
      "pairCode": "CD8M-45PQ",
      "forceUnpairOld": true
    }
    """
    try:
        user_id = app_user["user_id"]
        user_key = app_user["user_key"]

        machine_id = payload.machine_id
        pair_code = payload.pair_code

        if payload.qr_payload:
            qr_machine_id, qr_pair_code = parse_qr_payload(payload.qr_payload)
            machine_id = machine_id or qr_machine_id
            pair_code = pair_code or qr_pair_code

        if not machine_id or not pair_code:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "message": "machineId and pairCode are required. You may provide them directly or through qrPayload.",
                },
            )

        machine_id = normalize_id(machine_id)
        machine_key = sanitize_firebase_key(machine_id)
        pair_code = pair_code.strip()

        machine_ref = get_ref(f"machines/{machine_key}")
        machine = machine_ref.get()

        if not machine:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "message": "Machine is not registered",
                    "machineId": machine_id,
                },
            )

        owner_user_id = machine.get("owner_user_id")

        if owner_user_id and owner_user_id != user_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "success": False,
                    "message": "Machine is already paired to another user",
                    "machineId": machine_id,
                },
            )

        expected_hash = machine.get("pair_code_hash")
        submitted_hash = hash_pair_code(machine_id, pair_code)

        if not expected_hash or submitted_hash != expected_hash:
            raise HTTPException(
                status_code=401,
                detail={
                    "success": False,
                    "message": "Invalid pair code",
                },
            )

        require_pairing_mode = (
            payload.require_pairing_mode
            if payload.require_pairing_mode is not None
            else env_bool("PAIRING_REQUIRE_MODE", False)
        )

        if require_pairing_mode:
            pairing_until = parse_iso_datetime(machine.get("pairing_mode_until"))
            pairing_active = False

            if machine.get("pairing_mode") and pairing_until:
                pairing_active = datetime.now(get_timezone()) <= pairing_until

            if not pairing_active:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "success": False,
                        "message": "Machine is not in pairing mode",
                        "hint": "Press and hold the dryer PAIR button, then try again.",
                    },
                )

        current_user_pairing = get_ref(f"user_pairings/{user_key}").get()
        current_machine_id = None
        current_machine_key = None

        if current_user_pairing and current_user_pairing.get("active"):
            current_machine_id = current_user_pairing.get("machine_id")
            current_machine_key = current_user_pairing.get("machine_key")

        if current_machine_id and current_machine_id != machine_id:
            if not payload.force_unpair_old:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "success": False,
                        "message": "User is already paired to another machine",
                        "currentMachineId": current_machine_id,
                        "nextAction": "Send forceUnpairOld=true to unpair old machine and pair the new one.",
                    },
                )

        timestamp = now_iso()
        updates = {}

        if current_machine_key and current_machine_id != machine_id:
            updates[f"machines/{current_machine_key}/owner_user_id"] = None
            updates[f"machines/{current_machine_key}/unpaired_at"] = timestamp
            updates[f"machines/{current_machine_key}/updated_at"] = timestamp
            updates[f"machine_pairings/{current_machine_key}"] = None
            updates[f"devices/{current_machine_key}/owner_user_id"] = None
            updates[f"devices/{current_machine_key}/updated_at"] = timestamp

        pairing_data = {
            "user_id": user_id,
            "user_key": user_key,
            "firebase_uid": app_user.get("firebase_uid"),
            "email": app_user.get("email"),
            "machine_id": machine_id,
            "machine_key": machine_key,
            "role": "owner",
            "active": True,
            "paired_at": timestamp,
            "updated_at": timestamp,
        }

        updates[f"machines/{machine_key}/owner_user_id"] = user_id
        updates[f"machines/{machine_key}/owner_user_key"] = user_key
        updates[f"machines/{machine_key}/paired_at"] = timestamp
        updates[f"machines/{machine_key}/pairing_mode"] = False
        updates[f"machines/{machine_key}/pairing_mode_until"] = None
        updates[f"machines/{machine_key}/updated_at"] = timestamp

        updates[f"user_pairings/{user_key}"] = pairing_data
        updates[f"machine_pairings/{machine_key}"] = pairing_data

        updates[f"devices/{machine_key}/owner_user_id"] = user_id
        updates[f"devices/{machine_key}/owner_user_key"] = user_key
        updates[f"devices/{machine_key}/machine_id"] = machine_id
        updates[f"devices/{machine_key}/updated_at"] = timestamp

        get_ref("/").update(updates)

        await ws_manager.broadcast({
            "type": "machine_paired",
            "message": "Machine paired successfully",
            "server_time": now_iso(),
            "data": pairing_data,
        })

        return {
            "success": True,
            "message": "Machine paired successfully",
            "data": pairing_data,
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


@app.post("/api/pairing/unpair")
async def unpair_machine(
    payload: UnpairPayload,
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        user_id = app_user["user_id"]
        user_key = app_user["user_key"]

        user_pairing = get_ref(f"user_pairings/{user_key}").get()

        if not user_pairing or not user_pairing.get("active"):
            return {
                "success": True,
                "message": "User has no active paired machine",
                "data": None,
            }

        paired_machine_id = user_pairing.get("machine_id")
        machine_id = normalize_id(payload.machine_id) if payload.machine_id else paired_machine_id
        machine_key = sanitize_firebase_key(machine_id)

        if paired_machine_id != machine_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "success": False,
                    "message": "This user is not paired to the requested machine",
                    "pairedMachineId": paired_machine_id,
                    "requestedMachineId": machine_id,
                },
            )

        machine = get_ref(f"machines/{machine_key}").get()

        if machine and machine.get("owner_user_id") and machine.get("owner_user_id") != user_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "success": False,
                    "message": "This user is not the owner of the machine",
                },
            )

        timestamp = now_iso()

        updates = {
            f"user_pairings/{user_key}": None,
            f"machine_pairings/{machine_key}": None,
            f"machines/{machine_key}/owner_user_id": None,
            f"machines/{machine_key}/owner_user_key": None,
            f"machines/{machine_key}/unpaired_at": timestamp,
            f"machines/{machine_key}/updated_at": timestamp,
            f"devices/{machine_key}/owner_user_id": None,
            f"devices/{machine_key}/owner_user_key": None,
            f"devices/{machine_key}/updated_at": timestamp,
        }

        get_ref("/").update(updates)

        ws_payload = {
            "type": "machine_unpaired",
            "message": "Machine unpaired successfully",
            "server_time": now_iso(),
            "data": {
                "userId": user_id,
                "userKey": user_key,
                "machineId": machine_id,
                "unpairedAt": timestamp,
            },
        }

        await ws_manager.broadcast(ws_payload)

        return {
            "success": True,
            "message": "Machine unpaired successfully",
            "data": ws_payload["data"],
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


@app.get("/api/users/me/paired-machine")
async def get_my_paired_machine(
    app_user: Dict[str, Any] = Depends(get_current_app_user),
):
    try:
        user_key = app_user["user_key"]
        pairing = get_ref(f"user_pairings/{user_key}").get()

        if not pairing or not pairing.get("active"):
            return {
                "success": True,
                "message": "No active paired machine",
                "data": None,
            }

        machine_key = pairing.get("machine_key")
        machine = get_ref(f"machines/{machine_key}").get() if machine_key else None
        device = get_ref(f"devices/{machine_key}").get() if machine_key else None

        return {
            "success": True,
            "data": {
                "pairing": pairing,
                "machine": machine,
                "device": device,
            },
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


# Backward-compatible route if you still want to check by custom user ID.
# This is read-only and not recommended for app security decisions.
@app.get("/api/users/{user_id}/paired-machine")
async def get_user_paired_machine_by_id(user_id: str):
    try:
        user_key = sanitize_firebase_key(user_id.strip())
        pairing = get_ref(f"user_pairings/{user_key}").get()

        if not pairing or not pairing.get("active"):
            return {
                "success": True,
                "message": "No active paired machine",
                "data": None,
            }

        machine_key = pairing.get("machine_key")
        machine = get_ref(f"machines/{machine_key}").get() if machine_key else None
        device = get_ref(f"devices/{machine_key}").get() if machine_key else None

        return {
            "success": True,
            "data": {
                "pairing": pairing,
                "machine": machine,
                "device": device,
            },
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
# ROUTES - TELEMETRY
# =========================================================
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
        original_device_id = normalize_id(payload.device_id)
        device_key = sanitize_firebase_key(original_device_id)
        timestamp = now_iso()

        device_ref = get_ref(f"devices/{device_key}")
        telemetry_logs_ref = get_ref("telemetry_logs")

        existing_device = device_ref.get()
        machine = get_ref(f"machines/{device_key}").get()

        device_data = {
            "device_id": original_device_id,
            "machine_id": original_device_id,
            "latest_temp": payload.temp,
            "latest_status": payload.status,
            "overheat": payload.overheat,
            "last_seen_at": timestamp,
            "updated_at": timestamp,
        }

        if machine and machine.get("owner_user_id"):
            device_data["owner_user_id"] = machine.get("owner_user_id")
            device_data["owner_user_key"] = machine.get("owner_user_key")

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

        if machine:
            updates[f"machines/{device_key}/last_seen_at"] = timestamp
            updates[f"machines/{device_key}/updated_at"] = timestamp
            updates[f"machines/{device_key}/latest_status"] = payload.status
            updates[f"machines/{device_key}/latest_temp"] = payload.temp

        get_ref("/").update(updates)

        ws_payload = {
            "type": "telemetry_created",
            "message": "New telemetry saved",
            "server_time": now_iso(),
            "data": {
                "device": device_data,
                "log": log_data,
            },
        }

        await ws_manager.broadcast(ws_payload)

        return {
            "success": True,
            "message": "Telemetry saved successfully",
            "websocket_broadcasted": True,
            "websocket_clients": len(ws_manager.active_connections),
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
        normalized_device_id = normalize_id(device_id)
        device_key = sanitize_firebase_key(normalized_device_id)
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
        normalized_device_id = normalize_id(device_id)
        device_key = sanitize_firebase_key(normalized_device_id)
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