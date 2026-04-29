CREATE SCHEMA IF NOT EXISTS dbo;

CREATE TABLE IF NOT EXISTS dbo.devices (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR(100) UNIQUE NOT NULL,
    latest_temp NUMERIC NULL,
    latest_status VARCHAR(50) NOT NULL DEFAULT 'IDLE',
    overheat BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dbo.telemetry_logs (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR(100) NOT NULL,
    event VARCHAR(100) NOT NULL DEFAULT 'HEARTBEAT',
    temp NUMERIC NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'IDLE',
    overheat BOOLEAN NOT NULL DEFAULT FALSE,
    session_active BOOLEAN NULL,
    session_duration_ms INTEGER NULL,
    session_remaining_ms INTEGER NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_logs_device_id
ON dbo.telemetry_logs(device_id);

CREATE INDEX IF NOT EXISTS idx_telemetry_logs_created_at
ON dbo.telemetry_logs(created_at DESC);