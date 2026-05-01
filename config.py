# config.py

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
from firebase_admin import credentials, db
from dotenv import load_dotenv
from fastapi import HTTPException


load_dotenv()


# =========================================================
# ENV / SETTINGS
# =========================================================
APP_TITLE = "Smart Copra Dryer API"
APP_VERSION = "2.7.0"
APP_DESCRIPTION = (
    "FastAPI + Firebase Realtime Database + WebSocket + Google Auth "
    "+ Dryer Pairing + Machine Command API."
)

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Manila")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

PAIRING_SECRET = os.getenv(
    "PAIRING_SECRET",
    "dev-smart-copra-dryer-pairing-secret",
)
APP_PAIR_URL = os.getenv("APP_PAIR_URL", "")
PAIRING_REQUIRE_MODE = os.getenv("PAIRING_REQUIRE_MODE", "false")

FIREBASE_TOKEN_CLOCK_SKEW_SECONDS = int(
    os.getenv("FIREBASE_TOKEN_CLOCK_SKEW_SECONDS", "10")
)


def env_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default

    return value.strip().lower() in ["1", "true", "yes", "y", "on"]


def get_cors_origins() -> List[str]:
    if CORS_ORIGINS.strip() == "*":
        return ["*"]

    return [
        origin.strip()
        for origin in CORS_ORIGINS.split(",")
        if origin.strip()
    ]


def get_timezone():
    try:
        return ZoneInfo(APP_TIMEZONE)

    except ZoneInfoNotFoundError:
        print(f"[TIMEZONE] {APP_TIMEZONE} not found. Falling back to UTC+08:00.")
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


# =========================================================
# FIREBASE
# =========================================================
def firebase_ready() -> bool:
    return len(firebase_admin._apps) > 0


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
        key
        for key in required_keys
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
# PAIRING HELPERS
# =========================================================
def hash_pair_code(machine_id: str, pair_code: str) -> str:
    raw = f"{normalize_id(machine_id)}:{pair_code.strip()}:{PAIRING_SECRET}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_pair_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    part1 = "".join(secrets.choice(alphabet) for _ in range(4))
    part2 = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"{part1}-{part2}"


def parse_qr_payload(qr_payload: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Supported:
    1. SMART_COPRA_DRYER|machineId=SCD-000123|pairCode=AB7K-92XM
    2. https://yourapp.com/pair?machineId=SCD-000123&pairCode=AB7K-92XM
    3. {"machineId":"SCD-000123","pairCode":"AB7K-92XM"}
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
    app_pair_url = APP_PAIR_URL.strip()

    if app_pair_url:
        separator = "&" if "?" in app_pair_url else "?"
        return f"{app_pair_url}{separator}machineId={machine_id}&pairCode={pair_code}"

    return f"SMART_COPRA_DRYER|machineId={machine_id}|pairCode={pair_code}"