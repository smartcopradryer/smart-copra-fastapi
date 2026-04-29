---
title: Smart Copra FastAPI
emoji: 🔥
colorFrom: red
colorTo: yellow
sdk: docker
app_port: 7860
---

# Smart Copra Dryer API

FastAPI + PostgreSQL API for Smart Copra Dryer telemetry.

## Endpoints

- `GET /`
- `GET /health`
- `POST /api/telemetry`
- `GET /api/devices`
- `GET /api/devices/{device_id}/latest`
- `GET /api/devices/{device_id}/history`
- `GET /api/logs`

## Local run

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -c "import base64; print(base64.b64encode(open('firebase-service-account.json','rb').read()).decode())"
uvicorn main:app --host 0.0.0.0 --port 3000 --reload