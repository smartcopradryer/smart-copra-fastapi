# services.py

from datetime import datetime, timedelta
from typing import Optional, Any, Dict, List

from firebase_admin import auth
from fastapi import HTTPException, Header, Depends, WebSocket

from config import (
    FIREBASE_TOKEN_CLOCK_SKEW_SECONDS,
    PAIRING_REQUIRE_MODE,
    build_qr_payload,
    env_bool,
    generate_pair_code,
    get_ref,
    get_timezone,
    hash_pair_code,
    limit_items,
    normalize_id,
    now_iso,
    parse_iso_datetime,
    parse_qr_payload,
    sanitize_firebase_key,
    sort_by_created_at_desc,
)


# =========================================================
# WEBSOCKET SERVICE
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
# AUTH SERVICE
# =========================================================
class AuthService:
    @staticmethod
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

    @staticmethod
    async def get_current_firebase_user(
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        token = AuthService.get_bearer_token(authorization)

        try:
            decoded_token = auth.verify_id_token(
                token,
                clock_skew_seconds=FIREBASE_TOKEN_CLOCK_SKEW_SECONDS,
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

    @staticmethod
    def generate_custom_user_id() -> str:
        year = datetime.now(get_timezone()).year
        counter_ref = get_ref("meta/user_counter")

        def increment_counter(current_value):
            if current_value is None:
                return 1

            return int(current_value) + 1

        next_value = counter_ref.transaction(increment_counter)

        return f"USR-{year}-{int(next_value):06d}"

    @staticmethod
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

        custom_user_id = AuthService.generate_custom_user_id()
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
    decoded_token: Dict[str, Any] = Depends(AuthService.get_current_firebase_user),
) -> Dict[str, Any]:
    return AuthService.get_or_create_app_user(decoded_token)


# =========================================================
# MACHINE / PAIRING SERVICE
# =========================================================
class MachineService:
    @staticmethod
    def register_machine(payload) -> Dict[str, Any]:
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
            "owner_user_key": existing_machine.get("owner_user_key") if existing_machine else None,
            "pairing_mode": False,
            "pairing_mode_until": None,
            "cycles": int(existing_machine.get("cycles") or 0) if existing_machine else 0,
            "cycle_warning": bool(existing_machine.get("cycle_warning")) if existing_machine else False,
            "created_at": existing_machine.get("created_at") if existing_machine else timestamp,
            "updated_at": timestamp,
        }

        get_ref(f"machines/{machine_key}").set(machine_data)

        return {
            "machineId": machine_id,
            "machineKey": machine_key,
            "pairCode": pair_code,
            "qrPayload": build_qr_payload(machine_id, pair_code),
            "warning": "Save/print the pairCode now. The backend stores only its hash.",
        }

    @staticmethod
    async def enable_pairing_mode(payload) -> Dict[str, Any]:
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

        data = {
            "machineId": machine_id,
            "pairingModeUntil": until.isoformat(),
        }

        await ws_manager.broadcast({
            "type": "machine_pairing_mode_enabled",
            "message": "Machine entered pairing mode",
            "server_time": now_iso(),
            "data": data,
        })

        return data

    @staticmethod
    def get_pairing_status(machine_id: str) -> Dict[str, Any]:
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
            "machineId": normalized_machine_id,
            "registered": True,
            "owned": bool(machine.get("owner_user_id")),
            "ownerUserId": machine.get("owner_user_id"),
            "pairingMode": pairing_active,
            "pairingModeUntil": machine.get("pairing_mode_until"),
            "lastSeenAt": machine.get("last_seen_at"),
            "updatedAt": machine.get("updated_at"),
        }

    @staticmethod
    async def pair_machine(payload, app_user: Dict[str, Any]) -> Dict[str, Any]:
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
            else env_bool(PAIRING_REQUIRE_MODE, False)
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
            updates[f"machines/{current_machine_key}/owner_user_key"] = None
            updates[f"machines/{current_machine_key}/unpaired_at"] = timestamp
            updates[f"machines/{current_machine_key}/updated_at"] = timestamp
            updates[f"machine_pairings/{current_machine_key}"] = None
            updates[f"devices/{current_machine_key}/owner_user_id"] = None
            updates[f"devices/{current_machine_key}/owner_user_key"] = None
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

        return pairing_data

    @staticmethod
    async def unpair_machine(payload, app_user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        user_id = app_user["user_id"]
        user_key = app_user["user_key"]

        user_pairing = get_ref(f"user_pairings/{user_key}").get()

        if not user_pairing or not user_pairing.get("active"):
            return None

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

        data = {
            "userId": user_id,
            "userKey": user_key,
            "machineId": machine_id,
            "unpairedAt": timestamp,
        }

        await ws_manager.broadcast({
            "type": "machine_unpaired",
            "message": "Machine unpaired successfully",
            "server_time": now_iso(),
            "data": data,
        })

        return data

    @staticmethod
    def get_my_paired_machine(app_user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        user_key = app_user["user_key"]
        pairing = get_ref(f"user_pairings/{user_key}").get()

        if not pairing or not pairing.get("active"):
            return None

        machine_key = pairing.get("machine_key")
        machine = get_ref(f"machines/{machine_key}").get() if machine_key else None
        device = get_ref(f"devices/{machine_key}").get() if machine_key else None

        return {
            "pairing": pairing,
            "machine": machine,
            "device": device,
        }

    @staticmethod
    def get_user_paired_machine_by_id(user_id: str) -> Optional[Dict[str, Any]]:
        user_key = sanitize_firebase_key(user_id.strip())
        pairing = get_ref(f"user_pairings/{user_key}").get()

        if not pairing or not pairing.get("active"):
            return None

        machine_key = pairing.get("machine_key")
        machine = get_ref(f"machines/{machine_key}").get() if machine_key else None
        device = get_ref(f"devices/{machine_key}").get() if machine_key else None

        return {
            "pairing": pairing,
            "machine": machine,
            "device": device,
        }


# =========================================================
# COMMAND SERVICE
# =========================================================
class CommandService:
    ACTIVE_STATUSES = [
        "STARTING",
        "DRYING",
        "RUNNING",
        "ACTIVE",
        "PAUSED",
        "PAUSING",
        "RESUMING",
    ]

    COMPLETED_STATUSES = [
        "COMPLETED",
        "TIME_COMPLETED",
    ]

    STOPPED_STATUSES = [
        "STOPPED",
        "STOPPING",
    ]

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _is_session_in_progress(machine: Dict[str, Any]) -> bool:
        latest_status = str(machine.get("latest_status") or "").upper()
        session_active = bool(machine.get("session_active"))
        session_paused = bool(machine.get("session_paused"))

        return session_active or session_paused or latest_status in CommandService.ACTIVE_STATUSES

    @staticmethod
    def _get_owned_machine(machine_id: str, app_user: Dict[str, Any]) -> Dict[str, Any]:
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

        owner_user_id = machine.get("owner_user_id")

        if owner_user_id != app_user.get("user_id"):
            raise HTTPException(
                status_code=403,
                detail={
                    "success": False,
                    "message": "You are not the owner of this machine",
                    "machineId": normalized_machine_id,
                },
            )

        return {
            "machine_id": normalized_machine_id,
            "machine_key": machine_key,
            "machine": machine,
        }

    @staticmethod
    def _create_command_record(
        machine_id: str,
        machine_key: str,
        app_user: Dict[str, Any],
        command: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        timestamp = now_iso()
        command_ref = get_ref("machine_commands").push()
        command_id = command_ref.key

        command_data = {
            "id": command_id,
            "command_id": command_id,
            "machine_id": machine_id,
            "machine_key": machine_key,
            "command": command,
            "payload": payload or {},
            "status": "QUEUED",
            "requested_by": app_user.get("user_id"),
            "requested_by_key": app_user.get("user_key"),
            "requested_by_email": app_user.get("email"),
            "created_at": timestamp,
            "updated_at": timestamp,
            "sent_at": None,
            "acknowledged_at": None,
            "completed_at": None,
            "result": None,
            "message": None,
            "error": None,
        }

        updates = {
            f"machine_commands/{command_id}": command_data,
            f"machine_command_queue/{machine_key}/{command_id}": True,
            f"machines/{machine_key}/latest_command_id": command_id,
            f"machines/{machine_key}/latest_command": command,
            f"machines/{machine_key}/latest_command_status": "QUEUED",
            f"machines/{machine_key}/updated_at": timestamp,
        }

        get_ref("/").update(updates)

        return command_data

    @staticmethod
    def _create_session_id() -> str:
        return get_ref("session_history_ids").push().key

    @staticmethod
    def _build_session_history_record(
        machine_id: str,
        machine_key: str,
        machine: Dict[str, Any],
        final_status: str,
        completion_reason: str,
        cycle_number: int,
        cycle_counted: bool,
    ) -> Dict[str, Any]:
        timestamp = now_iso()

        session_id = (
            machine.get("active_session_id")
            or machine.get("current_session_id")
            or CommandService._create_session_id()
        )

        duration_minutes = CommandService._safe_int(machine.get("session_duration_minutes"), 0)
        remaining_minutes = CommandService._safe_int(machine.get("session_remaining_minutes"), 0)
        elapsed_minutes = CommandService._safe_int(machine.get("session_elapsed_minutes"), 0)

        if elapsed_minutes <= 0 and duration_minutes > 0:
            elapsed_minutes = max(0, duration_minutes - remaining_minutes)

        if final_status.upper() in ["COMPLETED", "TIME_COMPLETED"]:
            elapsed_minutes = duration_minutes
            remaining_minutes = 0

        duration_ms = CommandService._safe_int(machine.get("session_duration_ms"), duration_minutes * 60 * 1000)
        remaining_ms = CommandService._safe_int(machine.get("session_remaining_ms"), remaining_minutes * 60 * 1000)
        elapsed_ms = CommandService._safe_int(machine.get("session_elapsed_ms"), elapsed_minutes * 60 * 1000)

        if final_status.upper() in ["COMPLETED", "TIME_COMPLETED"]:
            elapsed_ms = duration_ms
            remaining_ms = 0

        return {
            "session_id": session_id,
            "machine_id": machine_id,
            "machine_key": machine_key,
            "user_id": machine.get("owner_user_id"),
            "user_key": machine.get("owner_user_key"),
            "target_temperature": CommandService._safe_float(machine.get("target_temperature")),
            "duration_minutes": duration_minutes,
            "elapsed_minutes": elapsed_minutes,
            "remaining_minutes": remaining_minutes,
            "duration_ms": duration_ms,
            "elapsed_ms": elapsed_ms,
            "remaining_ms": remaining_ms,
            "started_at": machine.get("session_started_at"),
            "ended_at": timestamp,
            "final_status": final_status.upper(),
            "completion_reason": completion_reason,
            "cycle_counted": cycle_counted,
            "cycle_number": cycle_number,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    @staticmethod
    def _finalize_session(
        machine_id: str,
        machine_key: str,
        machine: Dict[str, Any],
        final_status: str,
        completion_reason: str,
    ) -> Dict[str, Any]:
        already_counted = bool(machine.get("session_cycle_counted"))

        current_cycles = CommandService._safe_int(machine.get("cycles"), 0)
        next_cycles = current_cycles if already_counted else current_cycles + 1
        cycle_counted = not already_counted

        history_record = CommandService._build_session_history_record(
            machine_id=machine_id,
            machine_key=machine_key,
            machine=machine,
            final_status=final_status,
            completion_reason=completion_reason,
            cycle_number=next_cycles,
            cycle_counted=cycle_counted,
        )

        session_id = history_record["session_id"]
        timestamp = now_iso()

        updates = {
            f"session_history/{machine_key}/{session_id}": history_record,
            f"session_history_by_user/{machine.get('owner_user_key')}/{session_id}": {
                "machine_key": machine_key,
                "machine_id": machine_id,
                "session_id": session_id,
                "created_at": history_record["created_at"],
            },

            f"machines/{machine_key}/latest_status": final_status.upper(),
            f"machines/{machine_key}/session_active": False,
            f"machines/{machine_key}/session_paused": False,
            f"machines/{machine_key}/session_remaining_minutes": 0,
            f"machines/{machine_key}/session_remaining_ms": 0,
            f"machines/{machine_key}/session_elapsed_minutes": history_record["elapsed_minutes"],
            f"machines/{machine_key}/session_elapsed_ms": history_record["elapsed_ms"],
            f"machines/{machine_key}/cycles": next_cycles,
            f"machines/{machine_key}/cycle_warning": next_cycles >= 8,
            f"machines/{machine_key}/session_cycle_counted": True,
            f"machines/{machine_key}/last_session_id": session_id,
            f"machines/{machine_key}/session_ended_at": history_record["ended_at"],
            f"machines/{machine_key}/updated_at": timestamp,

            f"devices/{machine_key}/latest_status": final_status.upper(),
            f"devices/{machine_key}/session_active": False,
            f"devices/{machine_key}/session_paused": False,
            f"devices/{machine_key}/session_remaining_minutes": 0,
            f"devices/{machine_key}/session_remaining_ms": 0,
            f"devices/{machine_key}/session_elapsed_minutes": history_record["elapsed_minutes"],
            f"devices/{machine_key}/session_elapsed_ms": history_record["elapsed_ms"],
            f"devices/{machine_key}/cycles": next_cycles,
            f"devices/{machine_key}/cycle_warning": next_cycles >= 8,
            f"devices/{machine_key}/last_session_id": session_id,
            f"devices/{machine_key}/updated_at": timestamp,
        }

        get_ref("/").update(updates)

        return {
            "session": history_record,
            "cycles": next_cycles,
            "cycleCounted": cycle_counted,
        }

    @staticmethod
    async def _broadcast_command(command_data: Dict[str, Any], message: str):
        await ws_manager.broadcast({
            "type": "machine_command_created",
            "message": message,
            "server_time": now_iso(),
            "data": command_data,
        })

    @staticmethod
    async def start_session(machine_id: str, payload, app_user: Dict[str, Any]) -> Dict[str, Any]:
        owned = CommandService._get_owned_machine(machine_id, app_user)

        normalized_machine_id = owned["machine_id"]
        machine_key = owned["machine_key"]
        machine = owned["machine"] or {}

        target_temperature = float(payload.target_temperature)
        duration_minutes = int(payload.duration_minutes)

        if target_temperature < 30 or target_temperature > 120:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "message": "Target temperature must be between 30 and 120 °C",
                },
            )

        if duration_minutes < 1 or duration_minutes > 1440:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "message": "Duration must be between 1 and 1440 minutes",
                },
            )

        if CommandService._is_session_in_progress(machine):
            raise HTTPException(
                status_code=409,
                detail={
                    "success": False,
                    "message": "A drying session is already in progress. Stop or complete it before starting a new session.",
                },
            )

        active_session_id = CommandService._create_session_id()

        command_payload = {
            "sessionId": active_session_id,
            "targetTemperature": target_temperature,
            "durationMinutes": duration_minutes,
            "durationMs": duration_minutes * 60 * 1000,
            "elapsedMinutes": 0,
            "elapsedMs": 0,
            "remainingMinutes": duration_minutes,
            "remainingMs": duration_minutes * 60 * 1000,
        }

        command_data = CommandService._create_command_record(
            machine_id=normalized_machine_id,
            machine_key=machine_key,
            app_user=app_user,
            command="START_SESSION",
            payload=command_payload,
        )

        timestamp = now_iso()
        existing_cycles = CommandService._safe_int(machine.get("cycles"), 0)

        updates = {
            f"machines/{machine_key}/latest_status": "STARTING",
            f"machines/{machine_key}/session_active": True,
            f"machines/{machine_key}/session_paused": False,
            f"machines/{machine_key}/active_session_id": active_session_id,
            f"machines/{machine_key}/current_session_id": active_session_id,
            f"machines/{machine_key}/target_temperature": target_temperature,
            f"machines/{machine_key}/session_duration_minutes": duration_minutes,
            f"machines/{machine_key}/session_elapsed_minutes": 0,
            f"machines/{machine_key}/session_remaining_minutes": duration_minutes,
            f"machines/{machine_key}/session_duration_ms": duration_minutes * 60 * 1000,
            f"machines/{machine_key}/session_elapsed_ms": 0,
            f"machines/{machine_key}/session_remaining_ms": duration_minutes * 60 * 1000,
            f"machines/{machine_key}/cycles": existing_cycles,
            f"machines/{machine_key}/cycle_warning": existing_cycles >= 8,
            f"machines/{machine_key}/session_cycle_counted": False,
            f"machines/{machine_key}/session_started_at": timestamp,
            f"machines/{machine_key}/session_ended_at": None,
            f"machines/{machine_key}/updated_at": timestamp,

            f"devices/{machine_key}/latest_status": "STARTING",
            f"devices/{machine_key}/session_active": True,
            f"devices/{machine_key}/session_paused": False,
            f"devices/{machine_key}/active_session_id": active_session_id,
            f"devices/{machine_key}/current_session_id": active_session_id,
            f"devices/{machine_key}/target_temperature": target_temperature,
            f"devices/{machine_key}/session_duration_minutes": duration_minutes,
            f"devices/{machine_key}/session_elapsed_minutes": 0,
            f"devices/{machine_key}/session_remaining_minutes": duration_minutes,
            f"devices/{machine_key}/session_duration_ms": duration_minutes * 60 * 1000,
            f"devices/{machine_key}/session_elapsed_ms": 0,
            f"devices/{machine_key}/session_remaining_ms": duration_minutes * 60 * 1000,
            f"devices/{machine_key}/cycles": existing_cycles,
            f"devices/{machine_key}/cycle_warning": existing_cycles >= 8,
            f"devices/{machine_key}/updated_at": timestamp,
        }

        get_ref("/").update(updates)

        await CommandService._broadcast_command(
            command_data,
            "Start session command queued",
        )

        return command_data

    @staticmethod
    async def pause(machine_id: str, app_user: Dict[str, Any]) -> Dict[str, Any]:
        owned = CommandService._get_owned_machine(machine_id, app_user)

        machine_id = owned["machine_id"]
        machine_key = owned["machine_key"]

        command_data = CommandService._create_command_record(
            machine_id=machine_id,
            machine_key=machine_key,
            app_user=app_user,
            command="PAUSE",
            payload={},
        )

        timestamp = now_iso()

        updates = {
            f"machines/{machine_key}/latest_status": "PAUSING",
            f"machines/{machine_key}/session_active": True,
            f"machines/{machine_key}/session_paused": True,
            f"machines/{machine_key}/updated_at": timestamp,

            f"devices/{machine_key}/latest_status": "PAUSING",
            f"devices/{machine_key}/session_active": True,
            f"devices/{machine_key}/session_paused": True,
            f"devices/{machine_key}/updated_at": timestamp,
        }

        get_ref("/").update(updates)

        await CommandService._broadcast_command(
            command_data,
            "Pause command queued",
        )

        return command_data

    @staticmethod
    async def resume(machine_id: str, app_user: Dict[str, Any]) -> Dict[str, Any]:
        owned = CommandService._get_owned_machine(machine_id, app_user)

        machine_id = owned["machine_id"]
        machine_key = owned["machine_key"]

        command_data = CommandService._create_command_record(
            machine_id=machine_id,
            machine_key=machine_key,
            app_user=app_user,
            command="RESUME",
            payload={},
        )

        timestamp = now_iso()

        updates = {
            f"machines/{machine_key}/latest_status": "RESUMING",
            f"machines/{machine_key}/session_active": True,
            f"machines/{machine_key}/session_paused": False,
            f"machines/{machine_key}/updated_at": timestamp,

            f"devices/{machine_key}/latest_status": "RESUMING",
            f"devices/{machine_key}/session_active": True,
            f"devices/{machine_key}/session_paused": False,
            f"devices/{machine_key}/updated_at": timestamp,
        }

        get_ref("/").update(updates)

        await CommandService._broadcast_command(
            command_data,
            "Resume command queued",
        )

        return command_data

    @staticmethod
    async def stop(machine_id: str, app_user: Dict[str, Any]) -> Dict[str, Any]:
        owned = CommandService._get_owned_machine(machine_id, app_user)

        machine_id = owned["machine_id"]
        machine_key = owned["machine_key"]
        machine = owned["machine"] or {}

        command_data = CommandService._create_command_record(
            machine_id=machine_id,
            machine_key=machine_key,
            app_user=app_user,
            command="STOP",
            payload={},
        )

        finalize_result = None

        if CommandService._is_session_in_progress(machine):
            finalize_result = CommandService._finalize_session(
                machine_id=machine_id,
                machine_key=machine_key,
                machine=machine,
                final_status="STOPPED",
                completion_reason="USER_STOPPED",
            )
        else:
            timestamp = now_iso()
            updates = {
                f"machines/{machine_key}/latest_status": "STOPPING",
                f"machines/{machine_key}/session_active": False,
                f"machines/{machine_key}/session_paused": False,
                f"machines/{machine_key}/updated_at": timestamp,

                f"devices/{machine_key}/latest_status": "STOPPING",
                f"devices/{machine_key}/session_active": False,
                f"devices/{machine_key}/session_paused": False,
                f"devices/{machine_key}/updated_at": timestamp,
            }

            get_ref("/").update(updates)

        await CommandService._broadcast_command(
            command_data,
            "Stop command queued",
        )

        command_data["finalize_result"] = finalize_result

        return command_data

    @staticmethod
    def get_pending_commands(machine_id: str, limit: int = 1) -> List[Dict[str, Any]]:
        normalized_machine_id = normalize_id(machine_id)
        machine_key = sanitize_firebase_key(normalized_machine_id)

        queue = get_ref(f"machine_command_queue/{machine_key}").get()

        if not queue:
            return []

        rows = []

        for command_id in list(queue.keys()):
            command = get_ref(f"machine_commands/{command_id}").get()

            if command and command.get("status") == "QUEUED":
                rows.append(command)

        rows = sort_by_created_at_desc(rows)
        return rows[:limit]

    @staticmethod
    def get_session_history(
        machine_id: str,
        app_user: Dict[str, Any],
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        owned = CommandService._get_owned_machine(machine_id, app_user)
        machine_key = owned["machine_key"]

        rows_raw = get_ref(f"session_history/{machine_key}").get()

        if not rows_raw:
            return []

        rows = []

        for key, value in rows_raw.items():
            if isinstance(value, dict):
                value["firebase_key"] = key
                rows.append(value)

        rows = sorted(
            rows,
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )

        return rows[:limit]

    @staticmethod
    async def mark_command_result(
        machine_id: str,
        command_id: str,
        payload,
    ) -> Dict[str, Any]:
        normalized_machine_id = normalize_id(machine_id)
        machine_key = sanitize_firebase_key(normalized_machine_id)

        command = get_ref(f"machine_commands/{command_id}").get()

        if not command:
            raise HTTPException(
                status_code=404,
                detail={
                    "success": False,
                    "message": "Command not found",
                    "commandId": command_id,
                },
            )

        if command.get("machine_key") != machine_key:
            raise HTTPException(
                status_code=403,
                detail={
                    "success": False,
                    "message": "Command does not belong to this machine",
                },
            )

        machine = get_ref(f"machines/{machine_key}").get() or {}

        timestamp = now_iso()
        result_status = str(payload.result or "ACCEPTED").upper()
        machine_status = str(payload.machine_status or command.get("command") or "").upper()

        updates = {
            f"machine_commands/{command_id}/status": result_status,
            f"machine_commands/{command_id}/result": payload.result,
            f"machine_commands/{command_id}/message": payload.message,
            f"machine_commands/{command_id}/machine_status": machine_status,
            f"machine_commands/{command_id}/updated_at": timestamp,
            f"machine_commands/{command_id}/acknowledged_at": timestamp,
            f"machine_command_queue/{machine_key}/{command_id}": None,

            f"machines/{machine_key}/latest_command_status": result_status,
            f"machines/{machine_key}/latest_status": machine_status,
            f"machines/{machine_key}/updated_at": timestamp,

            f"devices/{machine_key}/latest_status": machine_status,
            f"devices/{machine_key}/updated_at": timestamp,
        }

        if result_status in ["COMPLETED", "SUCCESS", "ACCEPTED"]:
            updates[f"machine_commands/{command_id}/completed_at"] = timestamp

        get_ref("/").update(updates)

        finalize_result = None

        if machine_status in CommandService.COMPLETED_STATUSES:
            finalize_result = CommandService._finalize_session(
                machine_id=normalized_machine_id,
                machine_key=machine_key,
                machine=machine,
                final_status="COMPLETED",
                completion_reason="TIME_COMPLETED",
            )

        elif machine_status in CommandService.STOPPED_STATUSES and CommandService._is_session_in_progress(machine):
            finalize_result = CommandService._finalize_session(
                machine_id=normalized_machine_id,
                machine_key=machine_key,
                machine=machine,
                final_status="STOPPED",
                completion_reason="MACHINE_STOPPED",
            )

        elif machine_status in ["DRYING", "RUNNING", "ACTIVE"]:
            get_ref("/").update({
                f"machines/{machine_key}/session_active": True,
                f"machines/{machine_key}/session_paused": False,
                f"devices/{machine_key}/session_active": True,
                f"devices/{machine_key}/session_paused": False,
            })

        elif machine_status in ["PAUSED", "PAUSING"]:
            get_ref("/").update({
                f"machines/{machine_key}/session_active": True,
                f"machines/{machine_key}/session_paused": True,
                f"devices/{machine_key}/session_active": True,
                f"devices/{machine_key}/session_paused": True,
            })

        data = {
            "machineId": normalized_machine_id,
            "commandId": command_id,
            "result": result_status,
            "message": payload.message,
            "machineStatus": machine_status,
            "finalizeResult": finalize_result,
            "updatedAt": timestamp,
        }

        await ws_manager.broadcast({
            "type": "machine_command_result",
            "message": "Machine command result received",
            "server_time": now_iso(),
            "data": data,
        })

        return data


# =========================================================
# TELEMETRY SERVICE
# =========================================================
class TelemetryService:
    @staticmethod
    async def save_telemetry(payload) -> Dict[str, Any]:
        if not payload.device_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "message": "deviceId is required",
                },
            )

        original_device_id = normalize_id(payload.device_id)
        device_key = sanitize_firebase_key(original_device_id)
        timestamp = now_iso()

        existing_device = get_ref(f"devices/{device_key}").get()
        machine = get_ref(f"machines/{device_key}").get()

        session_active = None
        session_duration_ms = None
        session_remaining_ms = None
        session_elapsed_ms = None

        session_duration_minutes = None
        session_remaining_minutes = None
        session_elapsed_minutes = None

        if payload.session:
            session_active = payload.session.active
            session_duration_ms = payload.session.duration_ms
            session_remaining_ms = payload.session.remaining_ms

        if session_duration_ms is not None:
            session_duration_minutes = round(session_duration_ms / 60000)

        if session_remaining_ms is not None:
            session_remaining_minutes = round(session_remaining_ms / 60000)

        if session_duration_ms is not None and session_remaining_ms is not None:
            session_elapsed_ms = max(0, session_duration_ms - session_remaining_ms)
            session_elapsed_minutes = round(session_elapsed_ms / 60000)

        device_data = {
            "device_id": original_device_id,
            "machine_id": original_device_id,
            "latest_temp": payload.temp,
            "latest_status": payload.status,
            "overheat": payload.overheat,
            "session_active": session_active,
            "session_duration_ms": session_duration_ms,
            "session_remaining_ms": session_remaining_ms,
            "session_elapsed_ms": session_elapsed_ms,
            "session_duration_minutes": session_duration_minutes,
            "session_remaining_minutes": session_remaining_minutes,
            "session_elapsed_minutes": session_elapsed_minutes,
            "last_seen_at": timestamp,
            "updated_at": timestamp,
        }

        if machine:
            if machine.get("owner_user_id"):
                device_data["owner_user_id"] = machine.get("owner_user_id")
                device_data["owner_user_key"] = machine.get("owner_user_key")

            if machine.get("active_session_id"):
                device_data["active_session_id"] = machine.get("active_session_id")
                device_data["current_session_id"] = machine.get("current_session_id")

            if machine.get("cycles") is not None:
                device_data["cycles"] = machine.get("cycles")
                device_data["cycle_warning"] = machine.get("cycle_warning", False)

            if machine.get("target_temperature") is not None:
                device_data["target_temperature"] = machine.get("target_temperature")

        if existing_device and existing_device.get("created_at"):
            device_data["created_at"] = existing_device.get("created_at")
        else:
            device_data["created_at"] = timestamp

        log_ref = get_ref("telemetry_logs").push()
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
            "session_elapsed_ms": session_elapsed_ms,
            "session_duration_minutes": session_duration_minutes,
            "session_remaining_minutes": session_remaining_minutes,
            "session_elapsed_minutes": session_elapsed_minutes,
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

            if session_active is not None:
                updates[f"machines/{device_key}/session_active"] = session_active

            if session_duration_ms is not None:
                updates[f"machines/{device_key}/session_duration_ms"] = session_duration_ms

            if session_remaining_ms is not None:
                updates[f"machines/{device_key}/session_remaining_ms"] = session_remaining_ms

            if session_elapsed_ms is not None:
                updates[f"machines/{device_key}/session_elapsed_ms"] = session_elapsed_ms

            if session_duration_minutes is not None:
                updates[f"machines/{device_key}/session_duration_minutes"] = session_duration_minutes

            if session_remaining_minutes is not None:
                updates[f"machines/{device_key}/session_remaining_minutes"] = session_remaining_minutes

            if session_elapsed_minutes is not None:
                updates[f"machines/{device_key}/session_elapsed_minutes"] = session_elapsed_minutes

        get_ref("/").update(updates)

        await ws_manager.broadcast({
            "type": "telemetry_created",
            "message": "New telemetry saved",
            "server_time": now_iso(),
            "data": {
                "device": device_data,
                "log": log_data,
            },
        })

        return {
            "device": device_data,
            "log": log_data,
        }

    @staticmethod
    def get_devices() -> List[Dict[str, Any]]:
        devices = get_ref("devices").get()

        if not devices:
            return []

        rows = []

        for key, value in devices.items():
            if isinstance(value, dict):
                value["firebase_key"] = key
                rows.append(value)

        return sorted(
            rows,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

    @staticmethod
    def get_latest_device(device_id: str) -> Dict[str, Any]:
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
        return row

    @staticmethod
    def get_device_history(device_id: str, limit: int) -> List[Dict[str, Any]]:
        normalized_device_id = normalize_id(device_id)
        device_key = sanitize_firebase_key(normalized_device_id)
        device_log_index = get_ref(f"device_logs/{device_key}").get()

        if not device_log_index:
            return []

        rows = []

        for log_id in list(device_log_index.keys()):
            log = get_ref(f"telemetry_logs/{log_id}").get()

            if log:
                rows.append(log)

        rows = sort_by_created_at_desc(rows)
        return limit_items(rows, limit)

    @staticmethod
    def get_logs(limit: int) -> List[Dict[str, Any]]:
        logs = get_ref("telemetry_logs").get()

        if not logs:
            return []

        rows = []

        for key, value in logs.items():
            if isinstance(value, dict):
                if "id" not in value:
                    value["id"] = key

                rows.append(value)

        rows = sort_by_created_at_desc(rows)
        return limit_items(rows, limit)