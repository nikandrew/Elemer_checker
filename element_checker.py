from __future__ import annotations

import math
import queue
import struct
import threading
import time
import tkinter as tk
import zipfile
import json
import os
from dataclasses import dataclass
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - shown in UI at runtime
    serial = None
    list_ports = None

try:
    import psycopg
except ImportError:  # pragma: no cover - fallback to psycopg2 below
    psycopg = None

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:  # pragma: no cover - DB saving is disabled at runtime
    psycopg2 = None
    execute_values = None

DB_DRIVER = "psycopg3" if psycopg is not None else "psycopg2" if psycopg2 is not None else ""

FUNCTIONS = {
    "01 - Read Coils": 0x01,
    "02 - Read Discrete Inputs": 0x02,
    "03 - Read Holding Registers": 0x03,
    "04 - Read Input Registers": 0x04,
    "05 - Write Single Coil": 0x05,
    "06 - Write Single Register": 0x06,
}

EXCEPTION_CODES = {
    0x01: "Illegal Function",
    0x02: "Illegal Data Address",
    0x03: "Illegal Data Value",
    0x04: "Slave Device Failure",
    0x06: "Slave Device Busy",
    0x20: "Invalid data length",
    0x21: "Read-only access",
}

READ_FUNCTIONS = {0x01, 0x02, 0x03, 0x04}
WRITE_SINGLE_FUNCTIONS = {0x05, 0x06}
TELEMETRY_BASE_ADDRESS = 0x0500
TELEMETRY_WITH_ERRORS_BASE_ADDRESS = 0x0520
TELEMETRY_CHANNELS = 16
TELEMETRY_REGISTERS_PER_CHANNEL = 2
TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL = 4
SENSOR_TYPE_BASE_ADDRESS = 0x0860
DEVICE_COUNT = 3
SETTINGS_FILE = "element_checker_settings.xlsx"
CHANNEL_SETTINGS_FILE = "channel_settings.json"
DB_TABLE_NAME = "sensor_measurements"
DEVICE_BAUD_REGISTER_ADDRESS = 0x0409
DEVICE_BAUD_CODE_TO_RATE = {
    4: 2400,
    5: 4800,
    6: 9600,
    7: 19200,
    8: 38400,
    9: 57600,
    10: 115200,
}
DEVICE_BAUD_RATE_TO_CODE = {rate: code for code, rate in DEVICE_BAUD_CODE_TO_RATE.items()}
SENSOR_KIND_TEMPERATURE = 1
SENSOR_KIND_HEAT_FLUX = 2
AUTO_SENSOR_POLL_PERIOD_S = 0.5
STEFAN_BOLTZMANN = 5.670374419e-8
TEMP_LOW_COLOR = "#9fd7ff"
TEMP_OK_COLOR = "#9fe6a0"
TEMP_HIGH_COLOR = "#ff9b9b"
TEMP_IDLE_COLOR = "#f0f0f0"
TEMP_DISABLED_COLOR = "#d9d9d9"


@dataclass
class PortSettings:
    port: str
    baudrate: int
    stopbits: float
    slave_addr: int
    scan_rate_ms: int


@dataclass
class ModbusResult:
    valid: bool
    message: str
    data: bytes = b""


@dataclass
class SensorSettings:
    num: str
    name: str
    used: bool
    tmin: float | None = None
    tmax: float | None = None
    twar: float | None = None
    tcrit: float | None = None
    temerg: float | None = None
    sensor_type: str = "Термодатчик"
    calibration_a: float = 1.0
    calibration_b: float = 0.0
    emissivity: float = 1.0


@dataclass
class TelemetryMeasurement:
    timestamp: datetime
    device_index: int
    slave_addr: int
    channel: int
    sensor_num: str
    sensor_name: str
    sensor_used: bool
    limit_tmin: float | None
    limit_tmax: float | None
    limit_twar: float | None
    limit_tcrit: float | None
    limit_temerg: float | None
    color_level: int
    sensor_kind_code: int
    calibration_a: float | None
    calibration_b: float | None
    emissivity: float | None
    raw_temperature: float | None
    temperature: float | None
    error_code: int | None
    error_text: str
    timer_code: int | None
    sensor_type_code: int | None
    sensor_type_text: str
    valid: bool
    validation_message: str
    raw_response: str


MEASUREMENT_ERROR_TEXT = {
    0: "OK",
    1: "Sensor circuit break",
    2: "Division by zero or result is out of range",
    3: "Nonexistent channel number",
    4: "Parameter memory read error",
    5: "Parameter memory write error",
    6: "No result",
    8: "Overflow high",
    10: "Unknown thermocouple cold junction temperature",
    11: "ADC data read error",
    12: "Input voltage measurement error",
    13: "Overflow low",
    14: "Unknown sensor type",
    15: "Parameter memory data error",
    16: "Invalid parameter number",
    17: "Invalid parameter value",
    19: "ADC internal error",
    21: "Unknown secondary processing type",
    22: "Secondary processing parameter error",
}

SENSOR_TYPE_TEXT = {
    0: "Cu85",
    1: "Cu65",
    2: "Cu81",
    3: "Cu61",
    4: "PtH5",
    5: "PtH1",
    6: "Ptb1",
    7: "ni1",
    8: "Gr21",
    9: "Gr23",
    10: "tc.H",
    11: "tc.L",
    12: "tc.S",
    13: "tc.r",
    14: "tc.b",
    15: "tc.A1",
    16: "tc.A2",
    17: "tc.A3",
    18: "tc.J",
    19: "tc.t",
    20: "tc.n",
    21: "tc.E",
    22: "i05",
    23: "i020",
    24: "i420",
    25: "U100",
    26: "U75",
    27: "r320",
}


def modbus_crc(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, byteorder="little")


def check_crc(frame: bytes) -> bool:
    return len(frame) >= 4 and modbus_crc(frame[:-2]) == frame[-2:]


def build_request(slave_addr: int, function_code: int, start_address: int, quantity: int) -> bytes:
    if not 0 <= slave_addr <= 247:
        raise ValueError("Slave address must be 0..247")
    if not 0 <= start_address <= 0xFFFF:
        raise ValueError("Start address must be 0..65535")
    if not 0 <= quantity <= 0xFFFF:
        raise ValueError("Quantity/value must be 0..65535")

    payload = bytes(
        [
            slave_addr,
            function_code,
            (start_address >> 8) & 0xFF,
            start_address & 0xFF,
            (quantity >> 8) & 0xFF,
            quantity & 0xFF,
        ]
    )
    return payload + modbus_crc(payload)


def build_read_response_frame(slave_addr: int, function_code: int, data: bytes) -> bytes:
    payload = bytes([slave_addr, function_code, len(data)]) + data
    return payload + modbus_crc(payload)


def expected_response_size(function_code: int, quantity: int) -> int:
    if function_code in {0x01, 0x02}:
        return 5 + math.ceil(quantity / 8)
    if function_code in {0x03, 0x04}:
        return 5 + quantity * 2
    if function_code in WRITE_SINGLE_FUNCTIONS:
        return 8
    return 0


def validate_read_response(response: bytes, slave_addr: int, function_code: int, expected_data_len: int) -> ModbusResult:
    if not response:
        return ModbusResult(False, "timeout/no data")
    if len(response) < 5:
        return ModbusResult(False, f"too short: {len(response)} byte(s)")
    if not check_crc(response):
        return ModbusResult(False, "CRC mismatch")
    if response[0] != slave_addr:
        return ModbusResult(False, f"wrong slave addr: {response[0]}")

    if response[1] == function_code + 0x80:
        code = response[2]
        description = EXCEPTION_CODES.get(code, "Unknown exception")
        return ModbusResult(False, f"Modbus exception {code:02X}: {description}")

    if response[1] != function_code:
        return ModbusResult(False, f"wrong function: {response[1]:02X}")
    if response[2] != expected_data_len:
        return ModbusResult(False, f"wrong byte count: {response[2]}, expected {expected_data_len}")

    expected_frame_len = 3 + expected_data_len + 2
    if len(response) != expected_frame_len:
        return ModbusResult(False, f"wrong frame length: {len(response)}, expected {expected_frame_len}")

    return ModbusResult(True, "valid", response[3 : 3 + expected_data_len])


def validate_write_single_response(response: bytes, request: bytes) -> ModbusResult:
    if not response:
        return ModbusResult(False, "timeout/no data")
    if len(response) < 5:
        return ModbusResult(False, f"too short: {len(response)} byte(s)")
    if not check_crc(response):
        return ModbusResult(False, "CRC mismatch")
    if response[0] != request[0]:
        return ModbusResult(False, f"wrong slave addr: {response[0]}")
    if response[1] == request[1] + 0x80:
        code = response[2]
        description = EXCEPTION_CODES.get(code, "Unknown exception")
        return ModbusResult(False, f"Modbus exception {code:02X}: {description}")
    if response != request:
        return ModbusResult(False, f"write echo mismatch: {format_hex(response)}")
    return ModbusResult(True, "valid")


def parse_int(value: str, base_name: str) -> int:
    value = value.strip()
    if not value:
        raise ValueError("Empty numeric value")
    if base_name == "Hex":
        return int(value, 16)
    return int(value, 10)


def format_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def decode_tm5104_temperature(data: bytes) -> float:
    if len(data) != 4:
        raise ValueError(f"Temperature payload must contain 4 bytes, got {len(data)}")
    value = struct.unpack(">f", data)[0]
    if not math.isfinite(value):
        raise ValueError("Temperature is not finite")
    return value


def decode_ushort(data: bytes) -> int:
    if len(data) != 2:
        raise ValueError(f"ushort payload must contain 2 bytes, got {len(data)}")
    return int.from_bytes(data, byteorder="big", signed=False)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


class TimescaleMeasurementWriter:
    def __init__(self, log_callback) -> None:
        self.log_callback = log_callback
        self.enabled = DB_DRIVER != ""
        self.queue: queue.Queue[TelemetryMeasurement | None] = queue.Queue()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.connection_kwargs = {
            "dbname": os.getenv("ELEMER_DB_NAME", "elemer_tvi"),
            "user": os.getenv("ELEMER_DB_USER", "postgres"),
            "password": os.getenv("ELEMER_DB_PASSWORD", ""),
            "host": os.getenv("ELEMER_DB_HOST", "localhost"),
            "port": int(os.getenv("ELEMER_DB_PORT", "5432")),
        }
        self.schema_ready = False
        self.disabled_reason = ""
        self.database_ready = False
        self.inserted_rows = 0
        self.last_insert_log = 0.0
        self.storage_mode = "PostgreSQL"
        if not self.enabled:
            self.disabled_reason = "PostgreSQL driver is not installed; DB saving is disabled"

    def start(self) -> None:
        if not self.enabled:
            self.log_callback(self.disabled_reason)
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self.log_callback(
            "TimescaleDB writer starting: "
            f"{self.connection_kwargs['host']}:{self.connection_kwargs['port']}/{self.connection_kwargs['dbname']} "
            f"user={self.connection_kwargs['user']} driver={DB_DRIVER}"
        )
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def enqueue(self, measurement: TelemetryMeasurement) -> None:
        if self.enabled:
            self.queue.put(measurement)
        elif self.disabled_reason:
            self.log_callback(self.disabled_reason)

    def wait_until_idle(self, timeout_s: float = 10.0) -> bool:
        if not self.enabled:
            return False
        deadline = time.monotonic() + timeout_s
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.queue.unfinished_tasks == 0

    def close(self) -> None:
        if not self.enabled:
            return
        self.stop_event.set()
        self.queue.put(None)
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)

    def _ensure_database(self) -> None:
        if self.database_ready:
            return
        dbname = self.connection_kwargs["dbname"]
        maintenance_kwargs = dict(self.connection_kwargs)
        maintenance_kwargs["dbname"] = os.getenv("ELEMER_DB_MAINTENANCE_DB", "postgres")
        connection = None
        try:
            connection = psycopg.connect(**maintenance_kwargs) if psycopg is not None else psycopg2.connect(**maintenance_kwargs)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
                exists = cursor.fetchone() is not None
                if not exists:
                    safe_dbname = str(dbname).replace('"', '""')
                    cursor.execute(f'CREATE DATABASE "{safe_dbname}"')
                    self.log_callback(f"PostgreSQL database created: {dbname}")
            self.database_ready = True
        except Exception as exc:
            self.log_callback(f"PostgreSQL database auto-create skipped: {exc}")
        finally:
            if connection is not None:
                connection.close()

    def _connect(self):
        self._ensure_database()
        if psycopg is not None:
            return psycopg.connect(**self.connection_kwargs)
        return psycopg2.connect(**self.connection_kwargs)

    def _connection_closed(self, connection) -> bool:
        return connection is None or bool(getattr(connection, "closed", True))

    def _safe_rollback(self, connection) -> None:
        if self._connection_closed(connection):
            return
        try:
            connection.rollback()
        except Exception:
            pass

    def _safe_close(self, connection) -> None:
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass

    def _ensure_schema(self, connection) -> None:
        if self.schema_ready:
            return
        timescaledb_installed = False
        timescaledb_available = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
                timescaledb_installed = cursor.fetchone() is not None
                timescaledb_available = timescaledb_installed
            connection.commit()
        except Exception as exc:
            self._safe_rollback(connection)
            if self._connection_closed(connection):
                raise
            self.log_callback(f"TimescaleDB availability check skipped: {exc}")

        if timescaledb_available:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SHOW shared_preload_libraries")
                    preload_libraries = str(cursor.fetchone()[0] or "").lower()
                    timescaledb_available = "timescaledb" in preload_libraries
                connection.commit()
            except Exception as exc:
                self._safe_rollback(connection)
                if self._connection_closed(connection):
                    raise
                timescaledb_available = False
                self.log_callback(f"TimescaleDB preload check skipped: {exc}")

        if timescaledb_available:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                connection.commit()
            except Exception as exc:
                self._safe_rollback(connection)
                timescaledb_available = False
                self.log_callback(f"TimescaleDB extension enable skipped: {exc}")
        elif timescaledb_installed:
            self.log_callback("TimescaleDB is not preloaded; saving to a regular PostgreSQL table")
        else:
            self.log_callback("TimescaleDB extension is not installed on this PostgreSQL server; saving to a regular table")

        if timescaledb_available:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT to_regproc('create_hypertable') IS NOT NULL")
                    timescaledb_available = bool(cursor.fetchone()[0])
                connection.commit()
            except Exception as exc:
                self._safe_rollback(connection)
                if self._connection_closed(connection):
                    raise
                timescaledb_available = False
                self.log_callback(f"TimescaleDB function check skipped: {exc}")

        self.storage_mode = "TimescaleDB" if timescaledb_available else "PostgreSQL"

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {DB_TABLE_NAME} (
                    time TIMESTAMPTZ NOT NULL,
                    device_index INTEGER NOT NULL,
                    device_label TEXT NOT NULL,
                    slave_addr INTEGER NOT NULL,
                    channel INTEGER NOT NULL,
                    global_channel INTEGER NOT NULL,
                    sensor_num TEXT,
                    sensor_name TEXT,
                    sensor_used BOOLEAN NOT NULL DEFAULT TRUE,
                    limit_tmin DOUBLE PRECISION,
                    limit_tmax DOUBLE PRECISION,
                    limit_twar DOUBLE PRECISION,
                    limit_tcrit DOUBLE PRECISION,
                    limit_temerg DOUBLE PRECISION,
                    color_level INTEGER NOT NULL DEFAULT -1,
                    sensor_kind_code INTEGER NOT NULL DEFAULT 1,
                    calibration_a DOUBLE PRECISION,
                    calibration_b DOUBLE PRECISION,
                    emissivity DOUBLE PRECISION,
                    raw_temperature DOUBLE PRECISION,
                    temperature DOUBLE PRECISION,
                    measurement_error_code INTEGER,
                    measurement_error_text TEXT,
                    timer_code INTEGER,
                    sensor_type_code INTEGER,
                    sensor_type_text TEXT,
                    valid BOOLEAN NOT NULL,
                    validation_message TEXT,
                    raw_response TEXT
                )
                """
            )
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{DB_TABLE_NAME}_channel_time "
                f"ON {DB_TABLE_NAME} (global_channel, time DESC)"
            )
            cursor.execute(
                f"ALTER TABLE {DB_TABLE_NAME} "
                "ADD COLUMN IF NOT EXISTS sensor_used BOOLEAN NOT NULL DEFAULT TRUE"
            )
            for column_name in ("limit_tmin", "limit_tmax", "limit_twar", "limit_tcrit", "limit_temerg"):
                cursor.execute(
                    f"ALTER TABLE {DB_TABLE_NAME} "
                    f"ADD COLUMN IF NOT EXISTS {column_name} DOUBLE PRECISION"
                )
            cursor.execute(
                f"ALTER TABLE {DB_TABLE_NAME} "
                "ADD COLUMN IF NOT EXISTS color_level INTEGER NOT NULL DEFAULT -1"
            )
            cursor.execute(
                f"ALTER TABLE {DB_TABLE_NAME} "
                "ADD COLUMN IF NOT EXISTS sensor_kind_code INTEGER NOT NULL DEFAULT 1"
            )
            for column_name in ("calibration_a", "calibration_b", "emissivity"):
                cursor.execute(
                    f"ALTER TABLE {DB_TABLE_NAME} "
                    f"ADD COLUMN IF NOT EXISTS {column_name} DOUBLE PRECISION"
                )
            cursor.execute(
                f"ALTER TABLE {DB_TABLE_NAME} "
                "ADD COLUMN IF NOT EXISTS raw_temperature DOUBLE PRECISION"
            )
        connection.commit()

        if timescaledb_available:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT create_hypertable('{DB_TABLE_NAME}', 'time', if_not_exists => TRUE)"
                    )
                connection.commit()
            except Exception as exc:
                self._safe_rollback(connection)
                if self._connection_closed(connection):
                    raise
                self.storage_mode = "PostgreSQL"
                self.log_callback(f"Timescale hypertable setup skipped; saving to a regular table: {exc}")

        self.schema_ready = True
        self.log_callback(
            f"{self.storage_mode} saving enabled: "
            f"{self.connection_kwargs['host']}:{self.connection_kwargs['port']}/{self.connection_kwargs['dbname']}"
        )

    def _insert_batch(self, connection, batch: list[TelemetryMeasurement]) -> None:
        if not batch:
            return
        rows = [
            (
                item.timestamp,
                item.device_index + 1,
                f"Elemer {item.device_index + 1}",
                item.slave_addr,
                item.channel,
                item.device_index * TELEMETRY_CHANNELS + item.channel,
                item.sensor_num,
                item.sensor_name,
                item.sensor_used,
                item.limit_tmin,
                item.limit_tmax,
                item.limit_twar,
                item.limit_tcrit,
                item.limit_temerg,
                item.color_level,
                item.sensor_kind_code,
                item.calibration_a,
                item.calibration_b,
                item.emissivity,
                item.raw_temperature,
                item.temperature,
                item.error_code,
                item.error_text,
                item.timer_code,
                item.sensor_type_code,
                item.sensor_type_text,
                item.valid,
                item.validation_message,
                item.raw_response,
            )
            for item in batch
        ]
        with connection.cursor() as cursor:
            if execute_values is not None and DB_DRIVER == "psycopg2":
                execute_values(
                    cursor,
                    f"""
                    INSERT INTO {DB_TABLE_NAME} (
                        time, device_index, device_label, slave_addr, channel, global_channel,
                        sensor_num, sensor_name, sensor_used, limit_tmin, limit_tmax,
                        limit_twar, limit_tcrit, limit_temerg, color_level, sensor_kind_code,
                        calibration_a, calibration_b, emissivity, raw_temperature, temperature, measurement_error_code,
                        measurement_error_text, timer_code, sensor_type_code, sensor_type_text,
                        valid, validation_message, raw_response
                    ) VALUES %s
                    """,
                    rows,
                )
            else:
                cursor.executemany(
                    f"""
                    INSERT INTO {DB_TABLE_NAME} (
                        time, device_index, device_label, slave_addr, channel, global_channel,
                        sensor_num, sensor_name, sensor_used, limit_tmin, limit_tmax,
                        limit_twar, limit_tcrit, limit_temerg, color_level, sensor_kind_code,
                        calibration_a, calibration_b, emissivity, raw_temperature, temperature, measurement_error_code,
                        measurement_error_text, timer_code, sensor_type_code, sensor_type_text,
                        valid, validation_message, raw_response
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    rows,
                )
        connection.commit()
        self.inserted_rows += len(batch)
        now = time.monotonic()
        if now - self.last_insert_log >= 1.0:
            self.log_callback(f"{self.storage_mode} saved total {self.inserted_rows} row(s)")
            self.last_insert_log = now

    def _run(self) -> None:
        connection = None
        last_log = 0.0
        while True:
            item = self.queue.get()
            try:
                should_stop = item is None and self.stop_event.is_set()
                if item is None:
                    if should_stop:
                        break
                    continue

                try:
                    if self._connection_closed(connection):
                        connection = self._connect()
                        self.schema_ready = False
                    self._ensure_schema(connection)
                    if self._connection_closed(connection):
                        raise RuntimeError("database connection closed before insert")
                    self._insert_batch(connection, [item])
                except Exception as exc:
                    self._safe_rollback(connection)
                    self._safe_close(connection)
                    connection = None
                    self.schema_ready = False
                    now = time.monotonic()
                    if now - last_log > 10:
                        self.log_callback(f"{self.storage_mode} save error: {exc}")
                        last_log = now
            finally:
                self.queue.task_done()

        self._safe_close(connection)


def _default_sensor_settings() -> list[list[SensorSettings]]:
    return [
        [
            SensorSettings(num=f"{device + 1}_{channel}", name=str(channel), used=True)
            for channel in range(1, TELEMETRY_CHANNELS + 1)
        ]
        for device in range(DEVICE_COUNT)
    ]


def _cell_column(cell_ref: str) -> str:
    return "".join(char for char in cell_ref if char.isalpha())


def _xlsx_rows(path: Path) -> list[dict[str, str]]:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("a:si", ns):
                shared_strings.append("".join(node.text or "" for node in item.findall(".//a:t", ns)))

        sheet_root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows: list[dict[str, str]] = []
    for row in sheet_root.findall(".//a:row", ns):
        values: dict[str, str] = {}
        for cell in row.findall("a:c", ns):
            cell_ref = cell.get("r", "")
            value_node = cell.find("a:v", ns)
            inline_node = cell.find("a:is/a:t", ns)
            value = ""
            if value_node is not None and value_node.text is not None:
                value = value_node.text
                if cell.get("t") == "s":
                    value = shared_strings[int(value)]
            elif inline_node is not None and inline_node.text is not None:
                value = inline_node.text
            values[_cell_column(cell_ref)] = value.strip()
        rows.append(values)
    return rows


def _used_value(value: str) -> bool:
    value = value.strip()
    if value == "1":
        return True
    try:
        return float(value) == 1.0
    except ValueError:
        return False


def _optional_float(value: str) -> float | None:
    value = value.strip().replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def color_level_for_measurement(temperature: float | None, valid: bool, sensor: SensorSettings) -> int:
    if not valid or temperature is None:
        return -1
    if sensor.temerg is not None and temperature > sensor.temerg:
        return 5
    if sensor.tcrit is not None and temperature > sensor.tcrit:
        return 4
    if sensor.twar is not None and temperature > sensor.twar:
        return 3
    if sensor.tmax is not None and temperature > sensor.tmax:
        return 2
    if sensor.tmin is not None and temperature < sensor.tmin:
        return 0
    return 1


def sensor_kind_code(sensor_type: str) -> int:
    return SENSOR_KIND_HEAT_FLUX if "теплового" in sensor_type.lower() else SENSOR_KIND_TEMPERATURE


def apply_sensor_conversion(raw_value: float, sensor: SensorSettings) -> float:
    if sensor_kind_code(sensor.sensor_type) == SENSOR_KIND_HEAT_FLUX:
        kelvin = raw_value + 273.15
        if kelvin < 0:
            raise ValueError(f"Temperature below absolute zero: {raw_value}")
        return sensor.emissivity * STEFAN_BOLTZMANN * kelvin**4
    return sensor.calibration_a * raw_value + sensor.calibration_b


def measurement_coefficients(sensor: SensorSettings) -> tuple[float | None, float | None, float | None]:
    if sensor_kind_code(sensor.sensor_type) == SENSOR_KIND_HEAT_FLUX:
        return None, None, sensor.emissivity
    return sensor.calibration_a, sensor.calibration_b, None


def load_sensor_settings(path: Path) -> tuple[list[list[SensorSettings]], str | None]:
    sensors = _default_sensor_settings()
    if not path.exists():
        return sensors, f"{path.name} not found, all sensors enabled"

    try:
        rows = _xlsx_rows(path)
        if not rows:
            return sensors, f"{path.name} is empty, all sensors enabled"

        header_row = rows[0]
        headers = {value.strip().lower(): column for column, value in header_row.items() if value.strip()}

        group_column = headers.get("group")
        sn_column = headers.get("sn")
        num_column = headers.get("num")
        name_column = headers.get("name")
        used_column = headers.get("used")
        tmin_column = headers.get("tmin")
        tmax_column = headers.get("tmax")

        if name_column is None or used_column is None:
            return sensors, f"{path.name}: Name/Used columns not found, all sensors enabled"

        for row in rows[1:]:
            group_text = row.get(group_column, "") if group_column else ""
            sn_text = row.get(sn_column, "") if sn_column else ""

            if (not group_text or not sn_text) and num_column is not None:
                num_text = row.get(num_column, "")
                if "_" in num_text:
                    group_text, sn_text = num_text.split("_", 1)

            if not group_text or not sn_text:
                continue

            try:
                device_index = int(float(group_text)) - 1
                channel = int(float(sn_text))
            except ValueError:
                continue

            if not 0 <= device_index < DEVICE_COUNT or not 1 <= channel <= TELEMETRY_CHANNELS:
                continue

            name = row.get(name_column, "").strip() or str(channel)
            used = _used_value(row.get(used_column, ""))
            num = row.get(num_column, "").strip() if num_column is not None else f"{device_index + 1}_{channel}"
            tmin = _optional_float(row.get(tmin_column, "")) if tmin_column is not None else None
            tmax = _optional_float(row.get(tmax_column, "")) if tmax_column is not None else None
            sensors[device_index][channel - 1] = SensorSettings(
                num=num,
                name=name,
                used=used,
                tmin=tmin,
                tmax=tmax,
            )
    except Exception as exc:
        return sensors, f"{path.name}: failed to read settings: {exc}"

    return sensors, None


def load_channel_settings(path: Path, sensors: list[list[SensorSettings]]) -> str | None:
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        devices = payload.get("devices", [])
        for device_index, device_settings in enumerate(devices):
            if not 0 <= device_index < DEVICE_COUNT:
                continue
            channels = device_settings.get("channels", [])
            for channel_index, channel_settings in enumerate(channels):
                if not 0 <= channel_index < TELEMETRY_CHANNELS or not isinstance(channel_settings, dict):
                    continue
                sensor = sensors[device_index][channel_index]
                old_limits = channel_settings.get("limits_text")
                if isinstance(old_limits, str) and (".." in old_limits or "-" in old_limits):
                    separator = ".." if ".." in old_limits else "-"
                    left, right = old_limits.split(separator, 1)
                    if "tmin" not in channel_settings:
                        sensor.tmin = _optional_float(left)
                    if "tmax" not in channel_settings:
                        sensor.tmax = _optional_float(right)
                for key, value in channel_settings.items():
                    if hasattr(sensor, key):
                        setattr(sensor, key, value)
    except Exception as exc:
        return f"{path.name}: failed to read channel settings: {exc}"

    return None


def save_channel_settings(path: Path, sensors: list[list[SensorSettings]]) -> None:
    payload = {
        "version": 2,
        "devices": [
            {
                "device": device_index + 1,
                "channels": [asdict(sensor) for sensor in device_settings],
            }
            for device_index, device_settings in enumerate(sensors)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def safe_excel_text(value) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        for encoding in ("utf-8", "cp1251", "latin-1"):
            try:
                text = value.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = value.hex(" ")
    else:
        try:
            text = str(value)
        except UnicodeDecodeError:
            text = repr(value)
    return "".join(
        char
        for char in text
        if char in "\t\n\r" or ord(char) >= 0x20
    )


def excel_cell_xml(row: int, column: int, value) -> str:
    cell_ref = f"{excel_column_name(column)}{row}"
    if value is None:
        return f'<c r="{cell_ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{cell_ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f'<c r="{cell_ref}"><v>{value}</v></c>'
    if isinstance(value, datetime):
        text = value.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    else:
        text = safe_excel_text(value)
    return f'<c r="{cell_ref}" t="inlineStr"><is><t>{xml_escape(text)}</t></is></c>'


def write_xlsx(path: Path, columns: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_rows = []
    all_rows = [tuple(columns), *rows]
    for row_index, values in enumerate(all_rows, start=1):
        cells = "".join(excel_cell_xml(row_index, column_index, value) for column_index, value in enumerate(values, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        '</worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="measurements" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


class ElementCheckerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Element TM5104 Modbus Checker")
        self.geometry("980x760")
        self.minsize(900, 680)

        self.serial_port = None
        self.worker_lock = threading.Lock()
        self.ui_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.auto_poll_after_id = None
        self.temperature_poll_running = False
        self.temperature_vars: list[list[tk.StringVar]] = []
        self.temperature_buttons: list[list[tk.Button]] = []
        self.temperature_history: list[list[list[float]]] = [[[] for _channel in range(TELEMETRY_CHANNELS)] for _device in range(DEVICE_COUNT)]
        self.sensor_type_cache: list[list[int | None]] = [[None for _channel in range(TELEMETRY_CHANNELS)] for _device in range(DEVICE_COUNT)]
        self.settings_window: tk.Toplevel | None = None
        self.engineering_window: tk.Toplevel | None = None
        self.logs_window: tk.Toplevel | None = None
        self.logs_text: tk.Text | None = None
        self.log_lines: list[str] = []
        self.auto_poll_next_due: dict[tuple[int, int], float] = {}
        self.export_start_entry: ttk.Entry | None = None
        self.export_end_entry: ttk.Entry | None = None
        self.engineering_channel_buttons: list[list[tk.Button]] = []
        self.engineering_detail_frame: ttk.LabelFrame | None = None
        self.engineering_vars: dict[str, tk.Variable] = {}
        self.engineering_selected: tuple[int, int] | None = None
        self.measurement_started_at: datetime | None = None
        self.pending_stop_export = False
        self.sensor_settings, self.sensor_settings_warning = load_sensor_settings(Path(__file__).with_name(SETTINGS_FILE))
        self.channel_settings_path = Path(__file__).with_name(CHANNEL_SETTINGS_FILE)
        channel_settings_warning = load_channel_settings(self.channel_settings_path, self.sensor_settings)
        if channel_settings_warning:
            self.sensor_settings_warning = (
                f"{self.sensor_settings_warning}\n{channel_settings_warning}"
                if self.sensor_settings_warning
                else channel_settings_warning
            )
        load_env_file(Path(__file__).with_name(".env"))
        self.db_writer = TimescaleMeasurementWriter(lambda message: self.ui_queue.put(("log", message)))
        self.db_writer.start()

        self.port_var = tk.StringVar(value="COM22")
        self.baud_var = tk.StringVar(value="115200")
        self.stopbits_var = tk.StringVar(value="2")
        self.slave_vars = [tk.StringVar(value=str(index + 2)) for index in range(DEVICE_COUNT)]
        self.selected_device_var = tk.StringVar(value="1")
        self.device_speed_device_var = tk.StringVar(value="1")
        self.device_speed_var = tk.StringVar(value="115200")
        self.device_speed_status_var = tk.StringVar(value="Speed is not checked")
        self.scan_rate_var = tk.StringVar(value="1000")
        self.function_var = tk.StringVar(value="03 - Read Holding Registers")
        self.start_address_var = tk.StringVar(value="0500")
        self.address_base_var = tk.StringVar(value="Hex")
        self.quantity_var = tk.StringVar(value="2")
        self.expected_var = tk.StringVar(value="")
        self.auto_poll_var = tk.BooleanVar(value=False)
        self.auto_poll_status_var = tk.StringVar(value="Auto poll stopped")
        self.export_mode_var = tk.StringVar(value="За последний час")
        now_local = datetime.now()
        self.export_start_var = tk.StringVar(value=(now_local - timedelta(days=1)).strftime("%d.%m.%y %H:%M"))
        self.export_end_var = tk.StringVar(value=now_local.strftime("%d.%m.%y %H:%M"))
        self.export_status_var = tk.StringVar(value="Выгрузка готова")
        self.quantity_var.trace_add("write", lambda *_args: self._refresh_expected())

        self._build_ui()
        if self.sensor_settings_warning:
            self._append_log(self.sensor_settings_warning)
        self._refresh_expected()
        self.after(20, self._drain_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)

        top_frame = ttk.Frame(self)
        top_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        top_frame.columnconfigure(5, weight=1)

        self.connect_button = ttk.Button(top_frame, text="Connect port", command=self._toggle_port)
        self.connect_button.grid(row=0, column=0, padx=(0, 8), sticky="w")
        ttk.Button(top_frame, text="Settings", command=self._open_settings).grid(row=0, column=1, padx=(0, 12), sticky="w")
        ttk.Button(top_frame, text="Инженерное меню", command=self._open_engineering_window).grid(
            row=0, column=2, padx=(0, 12), sticky="w"
        )
        ttk.Button(top_frame, text="Logs", command=self._open_logs_window).grid(row=0, column=3, padx=(0, 12), sticky="w")

        self.status_var = tk.StringVar(value="Port disconnected")
        ttk.Label(top_frame, textvariable=self.status_var, anchor="w").grid(row=0, column=5, sticky="ew")

        telemetry_frame = ttk.LabelFrame(self, text="TM5104 telemetry")
        telemetry_frame.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
        for column in range(DEVICE_COUNT):
            telemetry_frame.columnconfigure(column, weight=1)

        for device_index in range(DEVICE_COUNT):
            device_frame = ttk.LabelFrame(telemetry_frame, text=f"Device {device_index + 1}")
            device_frame.grid(row=0, column=device_index, padx=6, pady=6, sticky="nsew")
            for column in range(4):
                device_frame.columnconfigure(column, weight=1)

            ttk.Label(device_frame, text="Slave address").grid(row=0, column=0, columnspan=2, padx=4, pady=(4, 2), sticky="e")
            ttk.Label(
                device_frame,
                textvariable=self.slave_vars[device_index],
                anchor="w",
                font=("", 10, "bold"),
            ).grid(row=0, column=2, columnspan=2, padx=4, pady=(4, 2), sticky="w")

            device_values: list[tk.StringVar] = []
            device_buttons: list[tk.Button] = []
            for index in range(TELEMETRY_CHANNELS):
                channel = index + 1
                row = index // 4 + 1
                column = index % 4
                sensor = self.sensor_settings[device_index][channel - 1]
                value_var = tk.StringVar(value=self._sensor_label(device_index, channel, "--"))
                device_values.append(value_var)

                button = tk.Button(
                    device_frame,
                    textvariable=value_var,
                    width=8,
                    height=3,
                    relief="raised",
                    bg=TEMP_IDLE_COLOR,
                    activebackground=TEMP_IDLE_COLOR,
                    command=lambda dev=device_index, selected=channel: self._request_temperature(dev, selected),
                )
                button.grid(row=row, column=column, padx=3, pady=3, sticky="nsew")
                if not sensor.used:
                    button.configure(state="disabled", bg=TEMP_DISABLED_COLOR, activebackground=TEMP_DISABLED_COLOR)
                device_buttons.append(button)

            self.temperature_vars.append(device_values)
            self.temperature_buttons.append(device_buttons)

        action_frame = ttk.Frame(self)
        action_frame.grid(row=2, column=0, padx=12, pady=6, sticky="ew")
        action_frame.columnconfigure(0, weight=1)
        action_frame.columnconfigure(1, weight=1)

        self.request_button = ttk.Button(action_frame, text="Send manual request", command=self._send_manual_request)
        self.request_button.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ttk.Button(action_frame, text="Request all temperatures", command=self._request_all_temperatures).grid(
            row=0, column=1, padx=(6, 0), sticky="ew"
        )

        auto_frame = ttk.LabelFrame(self, text="Auto temperature polling")
        auto_frame.grid(row=3, column=0, padx=12, pady=6, sticky="ew")
        auto_frame.columnconfigure(2, weight=1)

        self.auto_start_button = ttk.Button(auto_frame, text="Start measurement", command=self._start_auto_poll)
        self.auto_start_button.grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        self.auto_stop_button = ttk.Button(auto_frame, text="Stop measurement", command=self._stop_auto_poll)
        self.auto_stop_button.grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        self.auto_stop_button.state(["disabled"])
        ttk.Label(auto_frame, textvariable=self.auto_poll_status_var, anchor="w").grid(
            row=0, column=2, padx=8, pady=8, sticky="ew"
        )

        export_frame = ttk.LabelFrame(self, text="Выгрузка в Excel")
        export_frame.grid(row=4, column=0, padx=12, pady=6, sticky="ew")
        export_frame.columnconfigure(1, weight=1)
        export_frame.columnconfigure(3, weight=1)

        ttk.Label(export_frame, text="Период").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        export_mode = ttk.Combobox(
            export_frame,
            textvariable=self.export_mode_var,
            values=("За последний час", "За 6 часов", "За сегодня", "За период"),
            state="readonly",
            width=18,
        )
        export_mode.grid(row=0, column=1, padx=8, pady=6, sticky="ew")
        export_mode.bind("<<ComboboxSelected>>", lambda _event: self._refresh_export_period_state())

        ttk.Label(export_frame, text="С").grid(row=0, column=2, padx=8, pady=6, sticky="w")
        self.export_start_entry = ttk.Entry(export_frame, textvariable=self.export_start_var, width=16)
        self.export_start_entry.grid(row=0, column=3, padx=8, pady=6, sticky="ew")
        ttk.Label(export_frame, text="По").grid(row=0, column=4, padx=8, pady=6, sticky="w")
        self.export_end_entry = ttk.Entry(export_frame, textvariable=self.export_end_var, width=16)
        self.export_end_entry.grid(row=0, column=5, padx=8, pady=6, sticky="ew")
        ttk.Button(export_frame, text="Выгрузить данные", command=self._export_selected_period).grid(
            row=0, column=6, padx=8, pady=6, sticky="ew"
        )
        ttk.Label(export_frame, textvariable=self.export_status_var, anchor="w").grid(
            row=1, column=0, columnspan=7, padx=8, pady=(0, 6), sticky="ew"
        )
        self._refresh_export_period_state()

    def _sensor_trend(self, device_index: int, channel: int) -> str:
        history = self.temperature_history[device_index][channel - 1]
        if len(history) < 10:
            return ""
        recent = history[-10:]
        first_avg = sum(recent[:5]) / 5
        last_avg = sum(recent[5:]) / 5
        delta = last_avg - first_avg
        if abs(delta) <= 2.0:
            return "✕"
        return "↑" if delta > 0 else "↓"

    def _sensor_label(self, device_index: int, channel: int, value: str) -> str:
        name = self.sensor_settings[device_index][channel - 1].name
        trend = self._sensor_trend(device_index, channel)
        return f"{name}\n{value} {trend}".rstrip()

    def _set_temperature_label(self, device_index: int, channel: int, value: str) -> None:
        self.temperature_vars[device_index][channel - 1].set(self._sensor_label(device_index, channel, value))

    def _set_temperature_color(self, device_index: int, channel: int, temperature: float | None) -> None:
        button = self.temperature_buttons[device_index][channel - 1]
        sensor = self.sensor_settings[device_index][channel - 1]
        color = TEMP_IDLE_COLOR
        if temperature is not None:
            if sensor.tmin is not None and temperature < sensor.tmin:
                color = TEMP_LOW_COLOR
            elif sensor.tmax is not None and temperature > sensor.tmax:
                color = TEMP_HIGH_COLOR
            else:
                color = TEMP_OK_COLOR
        button.configure(bg=color, activebackground=color)

    def _apply_sensor_ui_state(self, device_index: int, channel: int) -> None:
        sensor = self.sensor_settings[device_index][channel - 1]
        button = self.temperature_buttons[device_index][channel - 1]
        self._set_temperature_label(device_index, channel, "--")
        if sensor.used:
            button.configure(state="normal", bg=TEMP_IDLE_COLOR, activebackground=TEMP_IDLE_COLOR)
        else:
            button.configure(state="disabled", bg=TEMP_DISABLED_COLOR, activebackground=TEMP_DISABLED_COLOR)
        if self.engineering_channel_buttons:
            eng_button = self.engineering_channel_buttons[device_index][channel - 1]
            color = self._engineering_button_color(device_index, channel)
            relief = "solid" if self.engineering_selected == (device_index, channel) else "raised"
            border_width = 6 if self.engineering_selected == (device_index, channel) else 2
            eng_button.configure(
                text=self._engineering_button_text(device_index, channel),
                bg=color,
                activebackground=color,
                relief=relief,
                bd=border_width,
                highlightthickness=2 if self.engineering_selected == (device_index, channel) else 0,
                highlightbackground="black",
            )

    def _open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_set()
            return

        window = tk.Toplevel(self)
        window.title("Settings")
        window.transient(self)
        window.resizable(False, False)
        window.columnconfigure(1, weight=1)

        window.protocol("WM_DELETE_WINDOW", self._close_settings)
        self.settings_window = window

        port_frame = ttk.LabelFrame(window, text="Port settings")
        port_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")

        ttk.Label(port_frame, text="COM Port").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.port_combo = ttk.Combobox(port_frame, textvariable=self.port_var, values=self._available_ports(), width=12)
        self.port_combo.grid(row=0, column=1, padx=8, pady=8, sticky="ew")

        ttk.Label(port_frame, text="Baudrate").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        ttk.Combobox(
            port_frame,
            textvariable=self.baud_var,
            values=("9600", "19200", "38400", "57600", "115200", "230400"),
            width=10,
        ).grid(row=0, column=3, padx=8, pady=8, sticky="ew")

        ttk.Label(port_frame, text="Stop bits").grid(row=1, column=0, padx=8, pady=8, sticky="w")
        ttk.Combobox(port_frame, textvariable=self.stopbits_var, values=("1", "1.5", "2"), width=8).grid(
            row=1, column=1, padx=8, pady=8, sticky="ew"
        )

        ttk.Label(port_frame, text="Scan Rate (ms)").grid(row=1, column=2, padx=8, pady=8, sticky="w")
        ttk.Spinbox(port_frame, from_=50, to=60000, increment=50, textvariable=self.scan_rate_var, width=10).grid(
            row=1, column=3, padx=8, pady=8, sticky="ew"
        )

        device_frame = ttk.LabelFrame(window, text="Device slave addresses")
        device_frame.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
        for device_index, slave_var in enumerate(self.slave_vars):
            ttk.Label(device_frame, text=f"Device {device_index + 1}").grid(
                row=0, column=device_index * 2, padx=8, pady=8, sticky="w"
            )
            ttk.Spinbox(device_frame, from_=0, to=247, textvariable=slave_var, width=8).grid(
                row=0, column=device_index * 2 + 1, padx=8, pady=8, sticky="ew"
            )

        modbus_frame = ttk.LabelFrame(window, text="Manual Modbus request")
        modbus_frame.grid(row=2, column=0, padx=12, pady=6, sticky="ew")

        ttk.Label(modbus_frame, text="Device").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Combobox(
            modbus_frame,
            textvariable=self.selected_device_var,
            values=tuple(str(index + 1) for index in range(DEVICE_COUNT)),
            state="readonly",
            width=8,
        ).grid(row=0, column=1, padx=8, pady=8, sticky="ew")

        ttk.Label(modbus_frame, text="Function").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        function_combo = ttk.Combobox(modbus_frame, textvariable=self.function_var, values=tuple(FUNCTIONS), state="readonly")
        function_combo.grid(row=0, column=3, columnspan=3, padx=8, pady=8, sticky="ew")
        function_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_expected())

        ttk.Label(modbus_frame, text="Start Address").grid(row=1, column=0, padx=8, pady=8, sticky="w")
        ttk.Spinbox(modbus_frame, from_=0, to=65535, textvariable=self.start_address_var, width=10).grid(
            row=1, column=1, padx=8, pady=8, sticky="ew"
        )

        address_base = ttk.Combobox(
            modbus_frame,
            textvariable=self.address_base_var,
            values=("Dec", "Hex"),
            state="readonly",
            width=8,
        )
        address_base.grid(row=1, column=2, padx=8, pady=8, sticky="ew")
        address_base.bind("<<ComboboxSelected>>", self._convert_start_address)

        ttk.Label(modbus_frame, text="Quantity").grid(row=1, column=3, padx=8, pady=8, sticky="w")
        quantity_spin = ttk.Spinbox(modbus_frame, from_=1, to=65535, textvariable=self.quantity_var, width=10)
        quantity_spin.grid(row=1, column=4, padx=8, pady=8, sticky="ew")
        quantity_spin.configure(command=self._refresh_expected)

        ttk.Label(modbus_frame, text="Expected bytes").grid(row=1, column=5, padx=8, pady=8, sticky="w")
        ttk.Entry(modbus_frame, textvariable=self.expected_var, state="readonly", width=8).grid(
            row=1, column=6, padx=8, pady=8, sticky="ew"
        )

        speed_frame = ttk.LabelFrame(window, text="Device baud rate")
        speed_frame.grid(row=3, column=0, padx=12, pady=6, sticky="ew")

        ttk.Label(speed_frame, text="Device").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Combobox(
            speed_frame,
            textvariable=self.device_speed_device_var,
            values=tuple(str(index + 1) for index in range(DEVICE_COUNT)),
            state="readonly",
            width=8,
        ).grid(row=0, column=1, padx=8, pady=8, sticky="ew")

        ttk.Label(speed_frame, text="New baudrate").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        ttk.Combobox(
            speed_frame,
            textvariable=self.device_speed_var,
            values=tuple(str(rate) for rate in DEVICE_BAUD_RATE_TO_CODE),
            state="readonly",
            width=10,
        ).grid(row=0, column=3, padx=8, pady=8, sticky="ew")

        ttk.Button(speed_frame, text="Check current speed", command=self._check_device_speed).grid(
            row=1, column=0, columnspan=2, padx=8, pady=8, sticky="ew"
        )
        ttk.Button(speed_frame, text="Set device speed", command=self._set_device_speed).grid(
            row=1, column=2, columnspan=2, padx=8, pady=8, sticky="ew"
        )
        ttk.Label(speed_frame, textvariable=self.device_speed_status_var, anchor="w").grid(
            row=2, column=0, columnspan=4, padx=8, pady=(0, 8), sticky="ew"
        )

        ttk.Button(window, text="Close", command=self._close_settings).grid(row=4, column=0, padx=12, pady=(6, 12), sticky="e")

    def _close_settings(self) -> None:
        if self.settings_window is not None:
            self.settings_window.destroy()
            self.settings_window = None

    def _selected_speed_device_index(self) -> int:
        device_index = int(self.device_speed_device_var.get()) - 1
        if not 0 <= device_index < DEVICE_COUNT:
            raise ValueError(f"Device must be 1..{DEVICE_COUNT}")
        return device_index

    def _check_device_speed(self) -> None:
        if self.auto_poll_var.get():
            messagebox.showwarning("Auto polling is running", "Stop measurement before checking device speed.")
            return
        try:
            device_index = self._selected_speed_device_index()
            settings = self._settings(device_index)
            request = build_request(settings.slave_addr, 0x03, DEVICE_BAUD_REGISTER_ADDRESS, 1)
        except Exception as exc:
            messagebox.showerror("Speed check error", str(exc))
            return

        self.device_speed_status_var.set(f"Checking Elemer {device_index + 1} speed...")

        def handle_response(response: bytes, slave_addr=settings.slave_addr, device=device_index) -> None:
            result = validate_read_response(response, slave_addr, 0x03, 2)
            if not result.valid:
                self.device_speed_status_var.set(f"Elemer {device + 1}: speed check failed - {result.message}")
                return
            code = decode_ushort(result.data)
            rate = DEVICE_BAUD_CODE_TO_RATE.get(code)
            if rate is None:
                self.device_speed_status_var.set(f"Elemer {device + 1}: unknown speed code {code}")
                return
            self.device_speed_var.set(str(rate))
            self.device_speed_status_var.set(f"Elemer {device + 1}: current speed {rate} bit/s, code {code}")

        self._send_request(request, expected_response_size(0x03, 1), handle_response)

    def _set_device_speed(self) -> None:
        if self.auto_poll_var.get():
            messagebox.showwarning("Auto polling is running", "Stop measurement before setting device speed.")
            return
        try:
            device_index = self._selected_speed_device_index()
            settings = self._settings(device_index)
            rate = int(self.device_speed_var.get())
            code = DEVICE_BAUD_RATE_TO_CODE[rate]
            request = build_request(settings.slave_addr, 0x06, DEVICE_BAUD_REGISTER_ADDRESS, code)
        except Exception as exc:
            messagebox.showerror("Speed setup error", str(exc))
            return

        if not messagebox.askyesno(
            "Set device speed",
            f"Set Elemer {device_index + 1} baudrate to {rate} bit/s?\n"
            "After the command the application COM baudrate will also be switched to this value.",
            parent=self.settings_window,
        ):
            return

        self.device_speed_status_var.set(f"Setting Elemer {device_index + 1} speed to {rate} bit/s...")

        def handle_response(response: bytes, device=device_index, new_rate=rate, write_request=request) -> None:
            result = validate_write_single_response(response, write_request)
            if not result.valid:
                self.device_speed_status_var.set(f"Elemer {device + 1}: speed setup failed - {result.message}")
                return
            self.baud_var.set(str(new_rate))
            if self.serial_port is not None and self.serial_port.is_open:
                self.serial_port.baudrate = new_rate
            self.device_speed_status_var.set(
                f"Elemer {device + 1}: speed set to {new_rate} bit/s; COM port switched to {new_rate}"
            )
            self._append_log(f"Elemer {device + 1}: baudrate set to {new_rate} bit/s")

        self._send_request(request, 8, handle_response)

    def _engineering_button_text(self, device_index: int, channel: int) -> str:
        sensor = self.sensor_settings[device_index][channel - 1]
        return f"{channel}\n{sensor.name}"

    def _engineering_button_color(self, device_index: int, channel: int) -> str:
        sensor = self.sensor_settings[device_index][channel - 1]
        return TEMP_OK_COLOR if sensor.used else TEMP_DISABLED_COLOR

    def _refresh_engineering_selection(self) -> None:
        if not self.engineering_channel_buttons:
            return
        for device_index, buttons in enumerate(self.engineering_channel_buttons):
            for channel_index, button in enumerate(buttons):
                channel = channel_index + 1
                color = self._engineering_button_color(device_index, channel)
                relief = "solid" if self.engineering_selected == (device_index, channel) else "raised"
                border_width = 6 if self.engineering_selected == (device_index, channel) else 2
                button.configure(
                    text=self._engineering_button_text(device_index, channel),
                    bg=color,
                    activebackground=color,
                    relief=relief,
                    bd=border_width,
                    highlightthickness=2 if self.engineering_selected == (device_index, channel) else 0,
                    highlightbackground="black",
                )

    def _open_engineering_window(self) -> None:
        if self.engineering_window is not None and self.engineering_window.winfo_exists():
            self.engineering_window.deiconify()
            self.engineering_window.lift()
            self.engineering_window.attributes("-topmost", True)
            self.engineering_window.after_idle(lambda: self.engineering_window.attributes("-topmost", False))
            self.engineering_window.focus_set()
            return

        window = tk.Toplevel(self)
        window.title("Инженерное меню")
        window.geometry("1180x760")
        window.protocol("WM_DELETE_WINDOW", self._close_engineering_window)
        window.columnconfigure(0, weight=1)
        window.columnconfigure(1, weight=2)
        window.rowconfigure(0, weight=1)
        self.engineering_window = window
        self.engineering_channel_buttons = []
        self.engineering_selected = None

        channels_frame = ttk.Frame(window)
        channels_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        channels_frame.columnconfigure(0, weight=1)
        for device_index in range(DEVICE_COUNT):
            group_frame = ttk.LabelFrame(channels_frame, text=f"Элемер №{device_index + 1}")
            group_frame.grid(row=device_index, column=0, pady=6, sticky="ew")
            for column in range(4):
                group_frame.columnconfigure(column, weight=1)

            group_buttons: list[tk.Button] = []
            for channel in range(1, TELEMETRY_CHANNELS + 1):
                sensor = self.sensor_settings[device_index][channel - 1]
                color = TEMP_OK_COLOR if sensor.used else TEMP_DISABLED_COLOR
                button = tk.Button(
                    group_frame,
                    text=self._engineering_button_text(device_index, channel),
                    width=12,
                    height=2,
                    bg=color,
                    activebackground=color,
                    bd=2,
                    relief="raised",
                    command=lambda dev=device_index, ch=channel: self._show_engineering_channel(dev, ch),
                )
                button.grid(row=(channel - 1) // 4, column=(channel - 1) % 4, padx=4, pady=4, sticky="ew")
                group_buttons.append(button)
            self.engineering_channel_buttons.append(group_buttons)

        self._refresh_engineering_selection()

        self.engineering_detail_frame = ttk.LabelFrame(window, text="Настройки канала")
        self.engineering_detail_frame.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="nsew")
        self.engineering_detail_frame.columnconfigure(1, weight=1)
        ttk.Label(self.engineering_detail_frame, text="Выберите канал слева").grid(
            row=0, column=0, padx=12, pady=12, sticky="w"
        )

    def _close_engineering_window(self) -> None:
        if self.engineering_window is not None:
            self.engineering_window.destroy()
            self.engineering_window = None
        self.engineering_detail_frame = None
        self.engineering_channel_buttons = []
        self.engineering_vars = {}
        self.engineering_selected = None

    def _show_engineering_channel(self, device_index: int, channel: int) -> None:
        if self.engineering_detail_frame is None:
            return

        for child in self.engineering_detail_frame.winfo_children():
            child.destroy()

        sensor = self.sensor_settings[device_index][channel - 1]
        self.engineering_selected = (device_index, channel)
        self._refresh_engineering_selection()
        self.engineering_detail_frame.configure(text=f"Элемер №{device_index + 1}, канал {channel}")
        self.engineering_vars = {
            "name": tk.StringVar(value=sensor.name),
            "sensor_type": tk.StringVar(value=sensor.sensor_type),
            "used": tk.StringVar(value="Активен" if sensor.used else "Выключен"),
            "tmin": tk.StringVar(value="" if sensor.tmin is None else str(sensor.tmin).replace(".", ",")),
            "tmax": tk.StringVar(value="" if sensor.tmax is None else str(sensor.tmax).replace(".", ",")),
            "twar": tk.StringVar(value="" if sensor.twar is None else str(sensor.twar).replace(".", ",")),
            "tcrit": tk.StringVar(value="" if sensor.tcrit is None else str(sensor.tcrit).replace(".", ",")),
            "temerg": tk.StringVar(value="" if sensor.temerg is None else str(sensor.temerg).replace(".", ",")),
            "calibration_a": tk.StringVar(value=str(sensor.calibration_a).replace(".", ",")),
            "calibration_b": tk.StringVar(value=str(sensor.calibration_b).replace(".", ",")),
            "emissivity": tk.StringVar(value=str(sensor.emissivity).replace(".", ",")),
        }

        fields = [
            ("Наименование канала", "name", "entry"),
            ("Тип датчика / назначение", "sensor_type", ("Термодатчик", "Датчик теплового потока")),
            ("Признак активности", "used", ("Активен", "Выключен")),
            ("Допустимые пределы", "limits", "limits"),
            ("Преобразование измерения", "calibration", "calibration"),
        ]

        for row, (label, key, editor) in enumerate(fields):
            ttk.Label(self.engineering_detail_frame, text=label).grid(row=row, column=0, padx=8, pady=5, sticky="w")
            if isinstance(editor, tuple):
                ttk.Combobox(
                    self.engineering_detail_frame,
                    textvariable=self.engineering_vars[key],
                    values=editor,
                    state="readonly",
                ).grid(row=row, column=1, padx=8, pady=5, sticky="ew")
            elif editor == "calibration":
                calibration_frame = ttk.Frame(self.engineering_detail_frame)
                calibration_frame.grid(row=row, column=1, padx=8, pady=5, sticky="ew")
                calibration_frame.columnconfigure(1, weight=1)
                calibration_frame.columnconfigure(3, weight=1)
                ttk.Label(calibration_frame, text="a=").grid(row=0, column=0, sticky="w")
                calibration_a_entry = ttk.Entry(calibration_frame, textvariable=self.engineering_vars["calibration_a"], width=12)
                calibration_a_entry.grid(
                    row=0, column=1, padx=(4, 12), sticky="ew"
                )
                ttk.Label(calibration_frame, text="b=").grid(row=0, column=2, sticky="w")
                calibration_b_entry = ttk.Entry(calibration_frame, textvariable=self.engineering_vars["calibration_b"], width=12)
                calibration_b_entry.grid(
                    row=0, column=3, padx=(4, 0), sticky="ew"
                )
                ttk.Label(calibration_frame, text="ε=").grid(row=1, column=0, pady=(6, 0), sticky="w")
                emissivity_entry = ttk.Entry(calibration_frame, textvariable=self.engineering_vars["emissivity"], width=12)
                emissivity_entry.grid(row=1, column=1, padx=(4, 12), pady=(6, 0), sticky="ew")
                ttk.Label(calibration_frame, text="Степень черноты").grid(
                    row=1, column=2, columnspan=2, pady=(6, 0), sticky="w"
                )

                def refresh_conversion_inputs(*_args) -> None:
                    heat_flux = sensor_kind_code(self.engineering_vars["sensor_type"].get()) == SENSOR_KIND_HEAT_FLUX
                    calibration_state = "disabled" if heat_flux else "normal"
                    emissivity_state = "normal" if heat_flux else "disabled"
                    calibration_a_entry.configure(state=calibration_state)
                    calibration_b_entry.configure(state=calibration_state)
                    emissivity_entry.configure(state=emissivity_state)

                self.engineering_vars["sensor_type"].trace_add("write", refresh_conversion_inputs)
                refresh_conversion_inputs()
            elif editor == "limits":
                limits_frame = ttk.LabelFrame(self.engineering_detail_frame, text="Допустимые пределы")
                limits_frame.grid(row=row, column=1, padx=8, pady=5, sticky="ew")
                limits_frame.columnconfigure(1, weight=1)
                unit_var = tk.StringVar()

                def refresh_limit_unit(*_args) -> None:
                    sensor_type_value = self.engineering_vars["sensor_type"].get()
                    unit_var.set("Вт/м²" if "теплового" in sensor_type_value.lower() else "°C")

                self.engineering_vars["sensor_type"].trace_add("write", refresh_limit_unit)
                refresh_limit_unit()
                limit_fields = [
                    ("Рабочая уставка Tmin", "tmin"),
                    ("Рабочая уставка Tmax", "tmax"),
                    ("Предупредительный уровень Twar", "twar"),
                    ("Критический уровень Tcrit", "tcrit"),
                    ("Аварийный уровень Temerg", "temerg"),
                ]
                for limit_row, (limit_label, limit_key) in enumerate(limit_fields):
                    ttk.Label(limits_frame, text=limit_label).grid(row=limit_row, column=0, padx=6, pady=3, sticky="w")
                    ttk.Entry(limits_frame, textvariable=self.engineering_vars[limit_key], width=14).grid(
                        row=limit_row, column=1, padx=6, pady=3, sticky="ew"
                    )
                    ttk.Label(limits_frame, textvariable=unit_var, width=8).grid(
                        row=limit_row, column=2, padx=6, pady=3, sticky="w"
                    )
            else:
                ttk.Entry(self.engineering_detail_frame, textvariable=self.engineering_vars[key]).grid(
                    row=row, column=1, padx=8, pady=5, sticky="ew"
                )

        button_frame = ttk.Frame(self.engineering_detail_frame)
        button_frame.grid(row=len(fields), column=0, columnspan=2, padx=8, pady=(12, 4), sticky="ew")
        button_frame.columnconfigure(0, weight=1)
        button_frame.columnconfigure(1, weight=1)
        ttk.Button(button_frame, text="Сохранить канал", command=self._save_engineering_channel).grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )

    def _save_engineering_channel(self) -> None:
        if self.engineering_selected is None:
            return
        device_index, channel = self.engineering_selected
        sensor = self.sensor_settings[device_index][channel - 1]

        try:
            def parse_limit(var_name: str) -> float | None:
                text = self.engineering_vars[var_name].get().strip().replace(",", ".")
                if not text:
                    return None
                return float(text)

            calibration_a = float(self.engineering_vars["calibration_a"].get().replace(",", "."))
            calibration_b = float(self.engineering_vars["calibration_b"].get().replace(",", "."))
            emissivity = float(self.engineering_vars["emissivity"].get().replace(",", "."))
            if not 0 < emissivity <= 1:
                raise ValueError("Emissivity must be greater than 0 and not greater than 1")
            tmin = parse_limit("tmin")
            tmax = parse_limit("tmax")
            twar = parse_limit("twar")
            tcrit = parse_limit("tcrit")
            temerg = parse_limit("temerg")
        except Exception as exc:
            messagebox.showerror("Channel settings error", str(exc))
            return

        if not messagebox.askyesno(
            "Save confirmation",
            "Save channel settings?",
            parent=self.engineering_window,
        ):
            return

        sensor.name = self.engineering_vars["name"].get().strip() or sensor.num
        sensor.sensor_type = self.engineering_vars["sensor_type"].get()
        used_value = self.engineering_vars["used"].get()
        sensor.used = used_value == "Активен" or used_value.startswith("Рђ")
        sensor.tmin = tmin
        sensor.tmax = tmax
        sensor.twar = twar
        sensor.tcrit = tcrit
        sensor.temerg = temerg
        sensor.calibration_a = calibration_a
        sensor.calibration_b = calibration_b
        sensor.emissivity = emissivity
        try:
            save_channel_settings(self.channel_settings_path, self.sensor_settings)
        except Exception as exc:
            messagebox.showerror("JSON save error", str(exc))
            return

        self._apply_sensor_ui_state(device_index, channel)
        key = (device_index, channel)
        if self.auto_poll_var.get():
            if sensor.used:
                self.auto_poll_next_due[key] = time.monotonic()
            else:
                self.auto_poll_next_due.pop(key, None)
        self.status_var.set(f"Channel saved: Elemer {device_index + 1}, channel {channel}")
        self._append_log(f"Engineering settings saved for Elemer {device_index + 1}, channel {channel}")

    def _open_logs_window(self) -> None:
        if self.logs_window is not None and self.logs_window.winfo_exists():
            self.logs_window.lift()
            self.logs_window.focus_set()
            return

        window = tk.Toplevel(self)
        window.title("Logs")
        window.geometry("900x420")
        window.protocol("WM_DELETE_WINDOW", self._close_logs_window)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        self.logs_window = window

        frame = ttk.Frame(window)
        frame.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.logs_text = tk.Text(frame, wrap="word")
        self.logs_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.logs_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.logs_text.configure(yscrollcommand=scroll.set)
        if self.log_lines:
            self.logs_text.insert("end", "\n".join(self.log_lines) + "\n")
            self.logs_text.see("end")

    def _close_logs_window(self) -> None:
        self.logs_text = None
        if self.logs_window is not None:
            self.logs_window.destroy()
            self.logs_window = None

    def _available_ports(self) -> tuple[str, ...]:
        if list_ports is None:
            return ("COM22",)
        ports = [port.device for port in list_ports.comports()]
        if "COM22" not in ports:
            ports.insert(0, "COM22")
        return tuple(ports)

    def _selected_device_index(self) -> int:
        device_index = int(self.selected_device_var.get()) - 1
        if not 0 <= device_index < DEVICE_COUNT:
            raise ValueError(f"Device must be 1..{DEVICE_COUNT}")
        return device_index

    def _slave_addr(self, device_index: int) -> int:
        if not 0 <= device_index < DEVICE_COUNT:
            raise ValueError(f"Device must be 1..{DEVICE_COUNT}")
        slave_addr = int(self.slave_vars[device_index].get())
        if not 0 <= slave_addr <= 247:
            raise ValueError("Slave address must be 0..247")
        return slave_addr

    def _settings(self, device_index: int | None = None) -> PortSettings:
        if device_index is None:
            device_index = self._selected_device_index()
        return PortSettings(
            port=self.port_var.get().strip() or "COM22",
            baudrate=int(self.baud_var.get()),
            stopbits=float(self.stopbits_var.get()),
            slave_addr=self._slave_addr(device_index),
            scan_rate_ms=max(50, int(self.scan_rate_var.get())),
        )

    def _toggle_port(self) -> None:
        if self.serial_port and self.serial_port.is_open:
            self._disconnect()
            return
        self._connect()

    def _connect(self) -> None:
        if serial is None:
            messagebox.showerror("pyserial is not installed", "Install dependency first:\npython -m pip install pyserial")
            return

        try:
            settings = self._settings(0)
            self.serial_port = serial.Serial(
                port=settings.port,
                baudrate=settings.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=settings.stopbits,
                timeout=float(os.getenv("ELEMER_SERIAL_TIMEOUT_SECONDS", "0.2")),
                write_timeout=1,
            )
        except Exception as exc:
            messagebox.showerror("Connection error", str(exc))
            self.status_var.set("Port disconnected")
            return

        self.connect_button.configure(text="Disconnect port")
        self.status_var.set(f"Connected: {settings.port}, {settings.baudrate}, {settings.stopbits:g} stop bits")
        self._append_log("Port connected")

    def _disconnect(self) -> None:
        self._stop_auto_poll(export_session=False)
        if self.serial_port:
            try:
                self.serial_port.close()
            except Exception:
                pass
        self.connect_button.configure(text="Connect port")
        self.status_var.set("Port disconnected")
        self._append_log("Port disconnected")

    def _send_manual_request(self) -> None:
        try:
            function_code = FUNCTIONS[self.function_var.get()]
            device_index = self._selected_device_index()
            settings = self._settings(device_index)
            start_address = parse_int(self.start_address_var.get(), self.address_base_var.get())
            quantity = int(self.quantity_var.get())
            request = build_request(settings.slave_addr, function_code, start_address, quantity)
            expected_size = expected_response_size(function_code, quantity)
        except Exception as exc:
            messagebox.showerror("Request settings error", str(exc))
            return

        self._send_request(
            request,
            expected_size,
            lambda response, slave_addr=settings.slave_addr: self._handle_manual_response(response, slave_addr),
        )

    def _request_temperature(self, device_index: int, channel: int) -> None:
        if not 1 <= channel <= TELEMETRY_CHANNELS:
            messagebox.showerror("Telemetry error", f"Sensor channel must be 1..{TELEMETRY_CHANNELS}")
            return
        if not self.sensor_settings[device_index][channel - 1].used:
            return

        try:
            settings = self._settings(device_index)
            address = TELEMETRY_WITH_ERRORS_BASE_ADDRESS + (channel - 1) * TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL
            request = build_request(settings.slave_addr, 0x03, address, TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL)
        except Exception as exc:
            messagebox.showerror("Telemetry settings error", str(exc))
            return

        self._set_temperature_label(device_index, channel, "reading")

        def worker() -> None:
            try:
                sensor_type_code = self._read_sensor_type_for_worker(settings, device_index, channel)
                response = self._transact(request, 13)
                self.ui_queue.put(
                    (
                        "telemetry",
                        f"{device_index}|{channel}|{settings.slave_addr}|{sensor_type_code if sensor_type_code is not None else ''}|{response.hex()}",
                    )
                )
            except Exception as exc:
                self.ui_queue.put(("temp", f"{device_index}|{channel}|error"))
                self.ui_queue.put(("log", f"Device {device_index + 1}, sensor {channel}: Error: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _request_all_temperatures(self, auto: bool = False) -> bool:
        if not self._ensure_connected():
            return False
        if self.temperature_poll_running:
            if not auto:
                self.status_var.set("Temperature polling is already running")
            return False

        try:
            settings_by_device = [self._settings(device_index) for device_index in range(DEVICE_COUNT)]
        except Exception as exc:
            messagebox.showerror("Telemetry settings error", str(exc))
            return False

        self.temperature_poll_running = True

        def worker() -> None:
            try:
                for device_index, settings in enumerate(settings_by_device):
                    channels = [
                        channel
                        for channel in range(1, TELEMETRY_CHANNELS + 1)
                        if self.sensor_settings[device_index][channel - 1].used
                    ]
                    self._poll_channels_block_for_worker(settings, device_index, channels)
            finally:
                self.ui_queue.put(("poll_done", "auto" if auto else "manual"))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return True

    def _request_due_temperatures(self) -> bool:
        if not self._ensure_connected():
            return False
        if self.temperature_poll_running:
            return False

        now = time.monotonic()
        due_channels: list[tuple[int, int]] = []
        for device_index in range(DEVICE_COUNT):
            for channel in range(1, TELEMETRY_CHANNELS + 1):
                sensor = self.sensor_settings[device_index][channel - 1]
                key = (device_index, channel)
                if not sensor.used:
                    self.auto_poll_next_due.pop(key, None)
                    continue
                if key not in self.auto_poll_next_due:
                    self.auto_poll_next_due[key] = now
                if now >= self.auto_poll_next_due[key]:
                    due_channels.append(key)
                    self.auto_poll_next_due[key] = now + AUTO_SENSOR_POLL_PERIOD_S

        if not due_channels:
            return False

        try:
            settings_by_device = [self._settings(device_index) for device_index in range(DEVICE_COUNT)]
        except Exception as exc:
            messagebox.showerror("Telemetry settings error", str(exc))
            return False

        self.temperature_poll_running = True

        def worker() -> None:
            try:
                channels_by_device: dict[int, list[int]] = {}
                for device_index, channel in due_channels:
                    channels_by_device.setdefault(device_index, []).append(channel)
                for device_index, channels in channels_by_device.items():
                    self._poll_channels_block_for_worker(settings_by_device[device_index], device_index, channels)
            finally:
                self.ui_queue.put(("poll_done", "auto"))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _poll_channels_block_for_worker(self, settings: PortSettings, device_index: int, channels: list[int]) -> None:
        if not channels:
            return
        for channel in channels:
            self.ui_queue.put(("temp", f"{device_index}|{channel}|reading"))

        sensor_type_codes = self._read_sensor_types_block_for_worker(settings, device_index)
        try:
            request = build_request(settings.slave_addr, 0x03, TELEMETRY_WITH_ERRORS_BASE_ADDRESS, TELEMETRY_CHANNELS * TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL)
            response = self._transact(
                request,
                expected_response_size(0x03, TELEMETRY_CHANNELS * TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL),
                log_transaction=False,
            )
            result = validate_read_response(response, settings.slave_addr, 0x03, TELEMETRY_CHANNELS * TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL * 2)
            if not result.valid:
                self.ui_queue.put(("log", f"Device {device_index + 1}: telemetry block read failed - {result.message}"))
                for channel in channels:
                    self._poll_channel_individual_for_worker(settings, device_index, channel)
                return

            for channel in channels:
                offset = (channel - 1) * TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL * 2
                channel_data = result.data[offset : offset + TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL * 2]
                channel_response = build_read_response_frame(settings.slave_addr, 0x03, channel_data)
                sensor_type_code = sensor_type_codes[channel - 1]
                self.ui_queue.put(
                    (
                        "telemetry",
                        f"{device_index}|{channel}|{settings.slave_addr}|{sensor_type_code if sensor_type_code is not None else ''}|{channel_response.hex()}",
                    )
                )
        except Exception as exc:
            self.ui_queue.put(("log", f"Device {device_index + 1}: telemetry block error - {exc}"))
            for channel in channels:
                self._poll_channel_individual_for_worker(settings, device_index, channel)

    def _poll_channel_individual_for_worker(self, settings: PortSettings, device_index: int, channel: int) -> None:
        try:
            sensor_type_code = self._read_sensor_type_for_worker(
                settings, device_index, channel, log_transaction=False
            )
            address = TELEMETRY_WITH_ERRORS_BASE_ADDRESS + (channel - 1) * TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL
            request = build_request(settings.slave_addr, 0x03, address, TELEMETRY_WITH_ERRORS_REGISTERS_PER_CHANNEL)
            response = self._transact(request, 13, log_transaction=False)
            self.ui_queue.put(
                (
                    "telemetry",
                    f"{device_index}|{channel}|{settings.slave_addr}|{sensor_type_code if sensor_type_code is not None else ''}|{response.hex()}",
                )
            )
        except Exception as exc:
            self.ui_queue.put(("temp", f"{device_index}|{channel}|error"))
            self.ui_queue.put(("log", f"Device {device_index + 1}, sensor {channel}: Error: {exc}"))

    def _read_sensor_types_block_for_worker(self, settings: PortSettings, device_index: int) -> list[int | None]:
        if all(value is not None for value in self.sensor_type_cache[device_index]):
            return self.sensor_type_cache[device_index]

        request = build_request(settings.slave_addr, 0x03, SENSOR_TYPE_BASE_ADDRESS, TELEMETRY_CHANNELS)
        response = self._transact(request, expected_response_size(0x03, TELEMETRY_CHANNELS), log_transaction=False)
        result = validate_read_response(response, settings.slave_addr, 0x03, TELEMETRY_CHANNELS * 2)
        if not result.valid:
            self.ui_queue.put(("log", f"Device {device_index + 1}: sensor type block read failed - {result.message}"))
            return self.sensor_type_cache[device_index]

        for channel in range(1, TELEMETRY_CHANNELS + 1):
            offset = (channel - 1) * 2
            self.sensor_type_cache[device_index][channel - 1] = decode_ushort(result.data[offset : offset + 2])
        return self.sensor_type_cache[device_index]

    def _read_sensor_type_for_worker(
        self,
        settings: PortSettings,
        device_index: int,
        channel: int,
        log_transaction: bool = True,
    ) -> int | None:
        cached = self.sensor_type_cache[device_index][channel - 1]
        if cached is not None:
            return cached

        address = SENSOR_TYPE_BASE_ADDRESS + (channel - 1)
        request = build_request(settings.slave_addr, 0x03, address, 1)
        response = self._transact(request, 7, log_transaction=log_transaction)
        result = validate_read_response(response, settings.slave_addr, 0x03, 2)
        if not result.valid:
            self.ui_queue.put(("log", f"Device {device_index + 1}, sensor {channel}: sensor type read failed - {result.message}"))
            return None

        sensor_type_code = decode_ushort(result.data)
        self.sensor_type_cache[device_index][channel - 1] = sensor_type_code
        return sensor_type_code

    def _send_request(self, request: bytes, expected_size: int, callback) -> None:
        if not self._ensure_connected():
            return
        if not hasattr(self, "_pending_callbacks"):
            self._pending_callbacks = {}
        self._pending_callbacks[id(callback)] = callback

        def worker() -> None:
            try:
                response = self._transact(request, expected_size)
                self.ui_queue.put(("callback", f"{id(callback)}|{response.hex()}"))
            except Exception as exc:
                self._pending_callbacks.pop(id(callback), None)
                self.ui_queue.put(("log", f"Error: {exc}"))
                self.ui_queue.put(("status", "Request error"))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _transact(self, request: bytes, expected_size: int, log_transaction: bool = True) -> bytes:
        if not self.worker_lock.acquire(blocking=False):
            raise RuntimeError("Request is already running")
        try:
            assert self.serial_port is not None
            self.serial_port.reset_input_buffer()
            self.serial_port.write(request)
            self.serial_port.flush()
            response = self.serial_port.read(expected_size or 256)
            if log_transaction:
                self.ui_queue.put(("log", f"TX: {format_hex(request)}"))
                self.ui_queue.put(("log", f"RX: {format_hex(response) if response else '<timeout/no data>'}"))
            return response
        finally:
            self.worker_lock.release()

    def _handle_manual_response(self, response: bytes, slave_addr: int) -> None:
        try:
            function_code = FUNCTIONS[self.function_var.get()]
            quantity = int(self.quantity_var.get())
            expected_data_len = math.ceil(quantity / 8) if function_code in {0x01, 0x02} else quantity * 2
            result = validate_read_response(response, slave_addr, function_code, expected_data_len)
        except Exception as exc:
            self.status_var.set(f"Validation error: {exc}")
            return

        self.status_var.set("Manual response valid" if result.valid else f"Manual response invalid: {result.message}")
        self._append_log(f"Check: {result.message}")

    def _handle_temperature_response(
        self,
        device_index: int,
        channel: int,
        response: bytes,
        slave_addr: int,
        sensor_type_code: int | None,
    ) -> None:
        timestamp = datetime.now(timezone.utc)
        sensor = self.sensor_settings[device_index][channel - 1]
        raw_response = format_hex(response) if response else ""
        sensor_type_text = SENSOR_TYPE_TEXT.get(sensor_type_code, "Unknown" if sensor_type_code is not None else "")

        def save_db(
            temperature: float | None,
            raw_temperature: float | None,
            error_code: int | None,
            timer_code: int | None,
            valid: bool,
            validation_message: str,
        ) -> None:
            color_level = color_level_for_measurement(temperature, valid, sensor)
            kind_code = sensor_kind_code(sensor.sensor_type)
            calibration_a, calibration_b, emissivity = measurement_coefficients(sensor)
            self.db_writer.enqueue(
                TelemetryMeasurement(
                    timestamp=timestamp,
                    device_index=device_index,
                    slave_addr=slave_addr,
                    channel=channel,
                    sensor_num=sensor.num,
                    sensor_name=sensor.name,
                    sensor_used=sensor.used,
                    limit_tmin=sensor.tmin,
                    limit_tmax=sensor.tmax,
                    limit_twar=sensor.twar,
                    limit_tcrit=sensor.tcrit,
                    limit_temerg=sensor.temerg,
                    color_level=color_level,
                    sensor_kind_code=kind_code,
                    calibration_a=calibration_a,
                    calibration_b=calibration_b,
                    emissivity=emissivity,
                    raw_temperature=raw_temperature,
                    temperature=temperature,
                    error_code=error_code,
                    error_text=MEASUREMENT_ERROR_TEXT.get(error_code, "Unknown" if error_code is not None else ""),
                    timer_code=timer_code,
                    sensor_type_code=sensor_type_code,
                    sensor_type_text=sensor_type_text,
                    valid=valid,
                    validation_message=validation_message,
                    raw_response=raw_response,
                )
            )

        try:
            result = validate_read_response(response, slave_addr, 0x03, 8)
            if not result.valid:
                self._set_temperature_label(device_index, channel, "invalid")
                self._set_temperature_color(device_index, channel, None)
                self.status_var.set(f"Device {device_index + 1}, sensor {channel}: {result.message}")
                self._append_log(f"Device {device_index + 1}, sensor {channel}: invalid response - {result.message}")
                save_db(None, None, None, None, False, result.message)
                return

            raw_temperature = decode_tm5104_temperature(result.data[:4])
            error_code = decode_ushort(result.data[4:6])
            timer_code = decode_ushort(result.data[6:8])
            measurement_valid = error_code == 0
            temperature = apply_sensor_conversion(raw_temperature, sensor) if measurement_valid else None
            history = self.temperature_history[device_index][channel - 1]
            if temperature is not None:
                history.append(temperature)
            del history[:-10]

            if measurement_valid:
                unit = "W/m²" if sensor_kind_code(sensor.sensor_type) == SENSOR_KIND_HEAT_FLUX else "C"
                self._set_temperature_label(device_index, channel, f"{temperature:.1f} {unit}")
                self._set_temperature_color(device_index, channel, temperature)
                self.status_var.set(f"Device {device_index + 1}, sensor {channel}: {temperature:.2f} {unit}")
                self._append_log(
                    f"Device {device_index + 1}, sensor {channel}: valid, value = {temperature:.3f} {unit}, "
                    f"raw = {raw_temperature:.3f} C, "
                    f"sensor type = {sensor_type_code if sensor_type_code is not None else 'unknown'} {sensor_type_text}".rstrip()
                )
            else:
                error_text = MEASUREMENT_ERROR_TEXT.get(error_code, "Unknown")
                self._set_temperature_label(device_index, channel, f"err {error_code}")
                self._set_temperature_color(device_index, channel, None)
                self.status_var.set(f"Device {device_index + 1}, sensor {channel}: measurement error {error_code}")
                self._append_log(
                    f"Device {device_index + 1}, sensor {channel}: measurement error {error_code} ({error_text}), "
                    f"temperature payload = {raw_temperature:.3f} C"
                )

            save_db(
                temperature,
                raw_temperature,
                error_code,
                timer_code,
                measurement_valid,
                "valid" if measurement_valid else f"measurement error {error_code}",
            )
        except Exception as exc:
            self._set_temperature_label(device_index, channel, "error")
            self._set_temperature_color(device_index, channel, None)
            self.status_var.set(f"Device {device_index + 1}, sensor {channel}: decode error")
            self._append_log(f"Device {device_index + 1}, sensor {channel}: decode error - {exc}")
            save_db(None, None, None, None, False, f"decode error: {exc}")

    def _ensure_connected(self) -> bool:
        if not self.serial_port or not self.serial_port.is_open:
            messagebox.showwarning("Port is not connected", "Connect the COM port first.")
            return False
        return True

    def _refresh_expected(self) -> None:
        try:
            function_code = FUNCTIONS[self.function_var.get()]
            quantity = int(self.quantity_var.get())
            size = expected_response_size(function_code, quantity)
            self.expected_var.set(str(size) if size else "")
        except Exception:
            self.expected_var.set("")

    def _convert_start_address(self, _event: object = None) -> None:
        try:
            current_base = "Hex" if self.address_base_var.get() == "Dec" else "Dec"
            value = parse_int(self.start_address_var.get(), current_base)
        except ValueError:
            return

        if self.address_base_var.get() == "Hex":
            self.start_address_var.set(f"{value:X}")
        else:
            self.start_address_var.set(str(value))

    def _refresh_export_period_state(self) -> None:
        state = "normal" if self.export_mode_var.get() == "За период" else "disabled"
        if self.export_start_entry is not None:
            self.export_start_entry.configure(state=state)
        if self.export_end_entry is not None:
            self.export_end_entry.configure(state=state)

    def _parse_export_datetime(self, value: str) -> datetime:
        value = value.strip()
        for fmt in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M"):
            try:
                return datetime.strptime(value, fmt).astimezone()
            except ValueError:
                pass
        raise ValueError("Use date format dd.mm.yy HH:MM")

    def _selected_export_range(self) -> tuple[datetime, datetime, str]:
        now = datetime.now().astimezone()
        mode = self.export_mode_var.get()
        if mode == "За последний час":
            return now - timedelta(hours=1), now, "last_hour"
        if mode == "За 6 часов":
            return now - timedelta(hours=6), now, "last_6_hours"
        if mode == "За сегодня":
            return now.replace(hour=0, minute=0, second=0, microsecond=0), now, "today"
        start = self._parse_export_datetime(self.export_start_var.get())
        end = self._parse_export_datetime(self.export_end_var.get())
        if end <= start:
            raise ValueError("End time must be later than start time")
        return start, end, "custom"

    def _export_selected_period(self) -> None:
        try:
            start, end, label = self._selected_export_range()
        except Exception as exc:
            messagebox.showerror("Export period error", str(exc))
            return
        self._export_measurements_async(start, end, label)

    def _export_measurements_async(self, start: datetime, end: datetime, label: str) -> None:
        if not self.db_writer.enabled:
            messagebox.showerror("Export error", self.db_writer.disabled_reason or "Database driver is not installed")
            return
        self.export_status_var.set("Exporting data to Excel...")

        def worker() -> None:
            try:
                self.db_writer.wait_until_idle()
                path, row_count = self._export_measurements_to_excel(start, end, label)
                self.ui_queue.put(("export_done", f"ok|{path}|{row_count}"))
            except Exception as exc:
                self.ui_queue.put(("export_done", f"error|{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _export_measurements_to_excel(self, start: datetime, end: datetime, label: str) -> tuple[Path, int]:
        if psycopg is not None:
            connection = psycopg.connect(**self.db_writer.connection_kwargs)
        elif psycopg2 is not None:
            connection = psycopg2.connect(**self.db_writer.connection_kwargs)
        else:
            raise RuntimeError("PostgreSQL driver is not installed")

        export_columns = [
            "time",
            "device_index",
            "device_label",
            "slave_addr",
            "channel",
            "global_channel",
            "sensor_num",
            "sensor_name",
            "sensor_used",
            "limit_tmin",
            "limit_tmax",
            "limit_twar",
            "limit_tcrit",
            "limit_temerg",
            "color_level",
            "sensor_kind_code",
            "calibration_a",
            "calibration_b",
            "emissivity",
            "raw_temperature",
            "temperature",
            "measurement_error_code",
            "measurement_error_text",
            "timer_code",
            "sensor_type_code",
            "sensor_type_text",
            "valid",
            "validation_message",
            "raw_response",
        ]
        text_columns = {
            "device_label",
            "sensor_num",
            "sensor_name",
            "measurement_error_text",
            "sensor_type_text",
            "validation_message",
            "raw_response",
        }
        select_parts = []
        for column in export_columns:
            quoted = f'"{column}"'
            if column in text_columns:
                select_parts.append(f"encode({quoted}::bytea, 'hex') AS {quoted}")
            else:
                select_parts.append(quoted)
        query = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM {DB_TABLE_NAME} "
            'WHERE "time" >= %s AND "time" <= %s '
            'ORDER BY "time" ASC, "global_channel" ASC'
        )

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (start.astimezone(timezone.utc), end.astimezone(timezone.utc)),
                )
                fetched_rows = cursor.fetchall()
                columns = [getattr(description, "name", description[0]) for description in cursor.description]
        finally:
            connection.close()

        text_indexes = [index for index, column in enumerate(columns) if column in text_columns]
        rows = []
        for row in fetched_rows:
            values = list(row)
            for index in text_indexes:
                if values[index] is not None:
                    try:
                        values[index] = safe_excel_text(bytes.fromhex(str(values[index])))
                    except Exception:
                        values[index] = safe_excel_text(values[index])
            rows.append(tuple(values))

        export_dir = Path(__file__).with_name("measurements")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = export_dir / f"elemer_measurements_{label}_{timestamp}.xlsx"
        write_xlsx(path, columns, rows)
        return path, len(rows)

    def _start_auto_poll(self) -> None:
        if not self._ensure_connected():
            return

        self.auto_poll_var.set(True)
        self.measurement_started_at = datetime.now(timezone.utc)
        self.pending_stop_export = False
        now = time.monotonic()
        self.auto_poll_next_due = {
            (device_index, channel): now
            for device_index in range(DEVICE_COUNT)
            for channel in range(1, TELEMETRY_CHANNELS + 1)
            if self.sensor_settings[device_index][channel - 1].used
        }
        self.auto_start_button.state(["disabled"])
        self.auto_stop_button.state(["!disabled"])
        self.auto_poll_status_var.set("Auto poll running")
        self._schedule_auto_poll(0)

    def _stop_auto_poll(self, export_session: bool = True) -> None:
        was_running = self.auto_poll_var.get()
        session_start = self.measurement_started_at
        self.auto_poll_var.set(False)
        self._cancel_auto_poll()
        self.auto_poll_next_due = {}
        if hasattr(self, "auto_start_button"):
            self.auto_start_button.state(["!disabled"])
        if hasattr(self, "auto_stop_button"):
            self.auto_stop_button.state(["disabled"])
        self.auto_poll_status_var.set("Auto poll stopped")
        if export_session and was_running and session_start is not None:
            if self.temperature_poll_running:
                self.pending_stop_export = True
                self.export_status_var.set("Waiting for the current poll to finish before export...")
            else:
                self._export_measurements_async(session_start, datetime.now(timezone.utc), "session")
                self.measurement_started_at = None

    def _schedule_auto_poll(self, delay_ms: int | None = None) -> None:
        self._cancel_auto_poll()
        delay = 10 if delay_ms is None else delay_ms
        self.auto_poll_after_id = self.after(delay, self._auto_poll_once)

    def _auto_poll_once(self) -> None:
        self.auto_poll_after_id = None
        if self.auto_poll_var.get():
            if not self.serial_port or not self.serial_port.is_open:
                self._stop_auto_poll()
                return
            self.auto_poll_status_var.set("Polling...")
            started = self._request_due_temperatures()
            if not started:
                self._schedule_auto_poll()

    def _cancel_auto_poll(self) -> None:
        if self.auto_poll_after_id is not None:
            self.after_cancel(self.auto_poll_after_id)
            self.auto_poll_after_id = None

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_lines.append(line)
        del self.log_lines[:-2000]
        if self.logs_text is not None and self.logs_text.winfo_exists():
            self.logs_text.insert("end", line + "\n")
            self.logs_text.see("end")

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                kind, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "status":
                self.status_var.set(value)
            elif kind == "temp":
                device_text, channel_text, label = value.split("|", 2)
                channel = int(channel_text)
                self._set_temperature_label(int(device_text), channel, label)
            elif kind == "telemetry":
                device_text, channel_text, slave_text, sensor_type_text, response_hex = value.split("|", 4)
                sensor_type_code = int(sensor_type_text) if sensor_type_text else None
                self._handle_temperature_response(
                    int(device_text),
                    int(channel_text),
                    bytes.fromhex(response_hex),
                    int(slave_text),
                    sensor_type_code,
                )
            elif kind == "poll_done":
                self.temperature_poll_running = False
                if value == "auto" and self.auto_poll_var.get():
                    self.auto_poll_status_var.set("Auto poll running")
                    self._schedule_auto_poll()
                elif value == "auto":
                    self.auto_poll_status_var.set("Auto poll stopped")
                    if self.pending_stop_export:
                        self.pending_stop_export = False
                        end = datetime.now(timezone.utc)
                        self._export_measurements_async(self.measurement_started_at or end, end, "session")
                        self.measurement_started_at = None
            elif kind == "callback":
                callback_id_text, response_hex = value.split("|", 1)
                callback = self._pending_callbacks.pop(int(callback_id_text), None)
                if callback is not None:
                    callback(bytes.fromhex(response_hex))
            elif kind == "export_done":
                parts = value.split("|", 2)
                if parts[0] == "ok":
                    path = parts[1]
                    row_count = parts[2]
                    self.export_status_var.set(f"Saved {row_count} row(s): {path}")
                    messagebox.showinfo("Excel export", f"Data saved to file:\n{path}")
                else:
                    error = parts[1] if len(parts) > 1 else "unknown error"
                    self.export_status_var.set(f"Export failed: {error}")
                    messagebox.showerror("Excel export error", error)
            else:
                self._append_log(value)
        self.after(20, self._drain_ui_queue)

    def _on_close(self) -> None:
        self._disconnect()
        self.db_writer.close()
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        if self.engineering_window is not None and self.engineering_window.winfo_exists():
            self.engineering_window.destroy()
        if self.logs_window is not None and self.logs_window.winfo_exists():
            self.logs_window.destroy()
        self.destroy()


if __name__ == "__main__":
    app = ElementCheckerApp()
    app.mainloop()
