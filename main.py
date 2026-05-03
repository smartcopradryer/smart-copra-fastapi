# main.py

import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import router
from config import (
    APP_DESCRIPTION,
    APP_TITLE,
    APP_VERSION,
    get_cors_origins,
    initialize_firebase,
    now_iso,
)
from services import ws_manager
from mqtt_service import mqtt_publisher


app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    description=APP_DESCRIPTION,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(router)


@app.on_event("startup")
async def startup():
    try:
        initialize_firebase()

    except Exception as error:
        print("[FIREBASE] Initialization failed:", error)

    try:
        mqtt_publisher.start()

    except Exception as error:
        print("[MQTT] Startup failed:", error)


@app.on_event("shutdown")
async def shutdown():
    try:
        mqtt_publisher.stop()

    except Exception as error:
        print("[MQTT] Shutdown failed:", error)

    print("[FIREBASE] Shutdown complete")


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