# mqtt_service.py

import json
import os
import threading
import time
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt


MQTT_ENABLED = os.getenv("MQTT_ENABLED", "true").strip().lower() in [
    "1",
    "true",
    "yes",
    "y",
    "on",
]

MQTT_HOST = os.getenv("MQTT_HOST", "broker.emqx.io")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "").strip()
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "smart-copra-fastapi")
MQTT_KEEPALIVE = int(os.getenv("MQTT_KEEPALIVE", "60"))

MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "smartcopra")


class MqttCommandPublisher:
    def __init__(self):
        self.enabled = MQTT_ENABLED
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.started = False
        self.lock = threading.Lock()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            print("[MQTT] Connected to broker")
        else:
            self.connected = False
            print("[MQTT] Connect failed. rc =", rc)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        print("[MQTT] Disconnected. rc =", rc)

    def start(self):
        if not self.enabled:
            print("[MQTT] Disabled by MQTT_ENABLED=false")
            return

        with self.lock:
            if self.started:
                return

            self.client = mqtt.Client(client_id=MQTT_CLIENT_ID, clean_session=True)

            if MQTT_USERNAME:
                self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect

            try:
                print(f"[MQTT] Connecting to {MQTT_HOST}:{MQTT_PORT} ...")
                self.client.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
                self.client.loop_start()
                self.started = True

            except Exception as error:
                self.connected = False
                self.started = False
                print("[MQTT] Start failed:", error)

    def stop(self):
        with self.lock:
            if not self.client:
                return

            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception as error:
                print("[MQTT] Stop failed:", error)

            self.client = None
            self.connected = False
            self.started = False

    def publish_json(self, topic: str, payload: Dict[str, Any], qos: int = 0, retain: bool = False) -> bool:
        if not self.enabled:
            return False

        if not self.started:
            self.start()

        if not self.client:
            print("[MQTT] No client available")
            return False

        body = json.dumps(payload, separators=(",", ":"))

        try:
            result = self.client.publish(topic, body, qos=qos, retain=retain)

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                print("[MQTT] Published:", topic, body)
                return True

            print("[MQTT] Publish returned rc:", result.rc)
            return False

        except Exception as error:
            print("[MQTT] Publish failed:", error)
            return False


mqtt_publisher = MqttCommandPublisher()


def normalize_topic_machine_id(machine_id: str) -> str:
    return str(machine_id or "").strip().lower()


def command_topic(machine_id: str) -> str:
    return f"{MQTT_TOPIC_PREFIX}/{normalize_topic_machine_id(machine_id)}/commands"


def telemetry_topic(machine_id: str) -> str:
    return f"{MQTT_TOPIC_PREFIX}/{normalize_topic_machine_id(machine_id)}/telemetry"


def result_topic(machine_id: str) -> str:
    return f"{MQTT_TOPIC_PREFIX}/{normalize_topic_machine_id(machine_id)}/result"


def publish_machine_command(command_data: Dict[str, Any]) -> bool:
    machine_id = (
        command_data.get("machine_id")
        or command_data.get("machineId")
        or command_data.get("device_id")
        or command_data.get("deviceId")
    )

    if not machine_id:
        print("[MQTT] Cannot publish command. Missing machine_id.")
        return False

    payload = {
        "type": "machine_command",
        "commandId": command_data.get("command_id") or command_data.get("id"),
        "id": command_data.get("command_id") or command_data.get("id"),
        "machineId": machine_id,
        "machineKey": command_data.get("machine_key"),
        "command": command_data.get("command"),
        "payload": command_data.get("payload") or {},
        "createdAt": command_data.get("created_at"),
        "source": "fastapi",
        "sentAt": time.time(),
    }

    return mqtt_publisher.publish_json(
        command_topic(machine_id),
        payload,
        qos=0,
        retain=False,
    )