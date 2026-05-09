from __future__ import annotations

import math
import queue
import struct
import threading
import time
import tkinter as tk
import zipfile
import csv
import json
from dataclasses import dataclass
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from xml.etree import ElementTree

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - shown in UI at runtime
    serial = None
    list_ports = None

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
TELEMETRY_CHANNELS = 16
TELEMETRY_REGISTERS_PER_CHANNEL = 2
DEVICE_COUNT = 3
SETTINGS_FILE = "element_checker_settings.xlsx"
CHANNEL_SETTINGS_FILE = "channel_settings.json"
MEASUREMENTS_DIR = "measurements"
MEASUREMENT_FLUSH_INTERVAL_SECONDS = 10 * 60
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
    meter_channel: int = 1
    tmin: float | None = None
    tmax: float | None = None
    sensor_type: str = "Термодатчик"
    poll_period_s: float = 1.0
    panel_zone: str = ""
    show_temperature_graph: bool = True
    show_heat_flux_graph: bool = False
    sweep_linked: bool = False
    limits_text: str = ""
    graph_color: str = "#0b67d1"
    graph_line_type: str = "Сплошная"
    graph_line_width: int = 2
    graph_visibility_percent: int = 100
    graph_show_legend: bool = True
    graph_priority: int = 1
    calculation_flag: str = ""
    calibration_a: float = 1.0
    calibration_b: float = 0.0


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


def _default_sensor_settings() -> list[list[SensorSettings]]:
    return [
        [
            SensorSettings(num=f"{device + 1}_{channel}", name=str(channel), used=True, meter_channel=channel)
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
            limits_text = ""
            if tmin is not None or tmax is not None:
                limits_text = f"{'' if tmin is None else tmin}..{'' if tmax is None else tmax}"
            sensors[device_index][channel - 1] = SensorSettings(
                num=num,
                name=name,
                used=used,
                meter_channel=channel,
                tmin=tmin,
                tmax=tmax,
                limits_text=limits_text,
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
                for key, value in channel_settings.items():
                    if hasattr(sensor, key):
                        setattr(sensor, key, value)
    except Exception as exc:
        return f"{path.name}: failed to read channel settings: {exc}"

    return None


def save_channel_settings(path: Path, sensors: list[list[SensorSettings]]) -> None:
    payload = {
        "version": 1,
        "devices": [
            {
                "device": device_index + 1,
                "channels": [asdict(sensor) for sensor in device_settings],
            }
            for device_index, device_settings in enumerate(sensors)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        self.plot_history: list[list[list[tuple[datetime, float]]]] = [
            [[] for _channel in range(TELEMETRY_CHANNELS)] for _device in range(DEVICE_COUNT)
        ]
        self.settings_window: tk.Toplevel | None = None
        self.engineering_window: tk.Toplevel | None = None
        self.logs_window: tk.Toplevel | None = None
        self.logs_text: tk.Text | None = None
        self.graphs_window: tk.Toplevel | None = None
        self.log_lines: list[str] = []
        self.small_graph_vars: list[tk.StringVar] = []
        self.small_graph_auto_axis_vars: list[tk.BooleanVar] = []
        self.big_graph_vars: list[tk.BooleanVar] = []
        self.big_graph_selected: dict[str, tk.BooleanVar] = {}
        self.graph_option_map: dict[str, tuple[int, int]] = {}
        self.small_graph_canvases: list[tk.Canvas] = []
        self.big_graph_canvas: tk.Canvas | None = None
        self.graph_y_min_var = tk.StringVar(value="")
        self.graph_y_max_var = tk.StringVar(value="")
        self.graph_y_step_var = tk.StringVar(value="")
        self.graph_points_var = tk.StringVar(value="100")
        self.graph_bg_var = tk.StringVar(value="white")
        self.graph_x_min_var = tk.StringVar(value="")
        self.graph_x_max_var = tk.StringVar(value="")
        self.graph_x_step_var = tk.StringVar(value="")
        self.graph_time_range_min_var = tk.StringVar(value="30")
        self.graph_time_step_min_var = tk.StringVar(value="5")
        self.big_graph_auto_axis_var = tk.BooleanVar(value=True)
        self.auto_poll_next_due: dict[tuple[int, int], float] = {}
        self.engineering_channel_buttons: list[list[tk.Button]] = []
        self.engineering_detail_frame: ttk.LabelFrame | None = None
        self.engineering_vars: dict[str, tk.Variable] = {}
        self.engineering_selected: tuple[int, int] | None = None
        self.sensor_settings, self.sensor_settings_warning = load_sensor_settings(Path(__file__).with_name(SETTINGS_FILE))
        self.channel_settings_path = Path(__file__).with_name(CHANNEL_SETTINGS_FILE)
        channel_settings_warning = load_channel_settings(self.channel_settings_path, self.sensor_settings)
        if channel_settings_warning:
            self.sensor_settings_warning = (
                f"{self.sensor_settings_warning}\n{channel_settings_warning}"
                if self.sensor_settings_warning
                else channel_settings_warning
            )
        self.measurements_dir = Path(__file__).with_name(MEASUREMENTS_DIR)
        self.measurements_dir.mkdir(exist_ok=True)
        self.measurement_rows: list[dict[str, object]] = []
        self.current_measurement_row: dict[str, object] | None = None
        self.measurement_segment_start: datetime | None = None
        self.measurement_recording = False

        self.port_var = tk.StringVar(value="COM22")
        self.baud_var = tk.StringVar(value="115200")
        self.stopbits_var = tk.StringVar(value="2")
        self.slave_vars = [tk.StringVar(value=str(index + 2)) for index in range(DEVICE_COUNT)]
        self.selected_device_var = tk.StringVar(value="1")
        self.scan_rate_var = tk.StringVar(value="1000")
        self.function_var = tk.StringVar(value="03 - Read Holding Registers")
        self.start_address_var = tk.StringVar(value="0500")
        self.address_base_var = tk.StringVar(value="Hex")
        self.quantity_var = tk.StringVar(value="2")
        self.expected_var = tk.StringVar(value="")
        self.auto_poll_var = tk.BooleanVar(value=False)
        self.auto_poll_status_var = tk.StringVar(value="Auto poll stopped")
        self.quantity_var.trace_add("write", lambda *_args: self._refresh_expected())

        self._build_ui()
        if self.sensor_settings_warning:
            self._append_log(self.sensor_settings_warning)
        self._refresh_expected()
        self.after(100, self._drain_ui_queue)
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
        ttk.Button(top_frame, text="Графики температур", command=self._open_graphs_window).grid(
            row=0, column=4, padx=(0, 12), sticky="w"
        )

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

    def _measurement_time_label(self, value: datetime) -> str:
        return value.strftime("%H%M_%d%m%y")

    def _start_measurement_segment(self, start_time: datetime | None = None) -> None:
        self.measurement_segment_start = start_time or datetime.now()
        self.measurement_rows = []
        self.current_measurement_row = None
        self.measurement_recording = True

    def _write_measurement_segment(self, end_time: datetime | None = None) -> Path | None:
        if not self.measurement_rows or self.measurement_segment_start is None:
            return None

        end_time = end_time or datetime.now()
        filename = f"Elemer_{self._measurement_time_label(self.measurement_segment_start)}_{self._measurement_time_label(end_time)}.csv"
        path = self.measurements_dir / filename
        suffix = 1
        while path.exists():
            path = self.measurements_dir / f"{filename[:-4]}_{suffix}.csv"
            suffix += 1

        fieldnames = ["timestamp"] + [
            self.sensor_settings[device_index][channel].num
            for device_index in range(DEVICE_COUNT)
            for channel in range(TELEMETRY_CHANNELS)
        ]
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            writer.writerows(self.measurement_rows)

        self._append_log(f"Measurements saved: {path}")
        self.measurement_rows = []
        self.measurement_segment_start = None
        return path

    def _record_measurement(self, device_index: int, channel: int, temperature: float) -> None:
        if not self.measurement_recording:
            return

        now = datetime.now()
        if self.measurement_segment_start is None:
            self._start_measurement_segment(now)

        sensor = self.sensor_settings[device_index][channel - 1]
        if self.current_measurement_row is None:
            self.current_measurement_row = {"timestamp": now.isoformat(timespec="seconds")}
        self.current_measurement_row[sensor.num] = f"{temperature:.3f}"

    def _finish_measurement_row(self) -> None:
        if not self.current_measurement_row:
            return

        self.measurement_rows.append(self.current_measurement_row)
        self.current_measurement_row = None
        now = datetime.now()

        if self.measurement_segment_start and (now - self.measurement_segment_start).total_seconds() >= MEASUREMENT_FLUSH_INTERVAL_SECONDS:
            self._write_measurement_segment(now)
            if self.measurement_recording:
                self._start_measurement_segment(now)

    def _open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_set()
            return

        window = tk.Toplevel(self)
        window.title("Settings")
        window.transient(self)
        window.resizable(False, False)
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

        ttk.Button(window, text="Close", command=self._close_settings).grid(row=3, column=0, padx=12, pady=(6, 12), sticky="e")

    def _close_settings(self) -> None:
        if self.settings_window is not None:
            self.settings_window.destroy()
            self.settings_window = None

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
            self.engineering_window.lift()
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
        ttk.Button(
            channels_frame,
            text="Назначить цвета автоматически",
            command=self._auto_assign_graph_styles,
        ).grid(row=0, column=0, pady=(0, 8), sticky="ew")
        for device_index in range(DEVICE_COUNT):
            group_frame = ttk.LabelFrame(channels_frame, text=f"Элемер №{device_index + 1}")
            group_frame.grid(row=device_index + 1, column=0, pady=6, sticky="ew")
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

    def _auto_assign_graph_styles(self) -> None:
        if not messagebox.askyesno(
            "Подтверждение",
            "Назначить цвета, толщины и типы линий автоматически для всех активных каналов?",
            parent=self.engineering_window,
        ):
            return

        palette = [
            "#1f77b4",
            "#ff7f0e",
            "#2ca02c",
            "#d62728",
            "#9467bd",
            "#8c564b",
            "#e377c2",
            "#7f7f7f",
            "#bcbd22",
            "#17becf",
            "#00429d",
            "#73a2c6",
            "#f4777f",
            "#93003a",
            "#009392",
            "#39b185",
            "#9ccb86",
            "#e9e29c",
            "#eeb479",
            "#e88471",
            "#cf597e",
            "#6c4c9c",
            "#003f5c",
            "#ffa600",
        ]
        line_types = ("Сплошная", "Пунктирная", "Точечная")
        widths = (2, 3, 4)

        active_index = 0
        for device_index in range(DEVICE_COUNT):
            for channel_index in range(TELEMETRY_CHANNELS):
                sensor = self.sensor_settings[device_index][channel_index]
                if not sensor.used:
                    continue
                sensor.graph_color = palette[active_index % len(palette)]
                sensor.graph_line_type = line_types[(active_index // len(palette)) % len(line_types)]
                sensor.graph_line_width = widths[(active_index // (len(palette) * len(line_types))) % len(widths)]
                sensor.graph_visibility_percent = 100
                sensor.graph_show_legend = True
                sensor.graph_priority = min(active_index + 1, 48)
                active_index += 1

        try:
            save_channel_settings(self.channel_settings_path, self.sensor_settings)
        except Exception as exc:
            messagebox.showerror("Ошибка сохранения JSON", str(exc), parent=self.engineering_window)
            return

        if self.engineering_selected is not None:
            device_index, channel = self.engineering_selected
            self._show_engineering_channel(device_index, channel)
        else:
            self._refresh_engineering_selection()
        if self.graphs_window is not None and self.graphs_window.winfo_exists():
            self._draw_graphs()
        self._append_log(f"Automatic graph styles assigned for {active_index} active channel(s)")
        self.status_var.set("Graph styles assigned automatically")

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
            "meter_channel": tk.StringVar(value=str(sensor.meter_channel)),
            "name": tk.StringVar(value=sensor.name),
            "sensor_type": tk.StringVar(value=sensor.sensor_type),
            "used": tk.StringVar(value="Активен" if sensor.used else "Выключен"),
            "poll_period_s": tk.StringVar(value=str(sensor.poll_period_s).replace(".", ",")),
            "panel_zone": tk.StringVar(value=sensor.panel_zone),
            "show_temperature_graph": tk.StringVar(value="Отображать" if sensor.show_temperature_graph else "Нет"),
            "show_heat_flux_graph": tk.StringVar(value="Отображать" if sensor.show_heat_flux_graph else "Нет"),
            "sweep_linked": tk.StringVar(value="Привязан" if sensor.sweep_linked else "Нет"),
            "limits_text": tk.StringVar(value=sensor.limits_text),
            "graph_color": tk.StringVar(value=sensor.graph_color),
            "graph_line_type": tk.StringVar(value=sensor.graph_line_type),
            "graph_line_width": tk.StringVar(value=str(sensor.graph_line_width)),
            "graph_visibility_percent": tk.StringVar(value=str(sensor.graph_visibility_percent)),
            "graph_show_legend": tk.StringVar(value="Отображать" if sensor.graph_show_legend else "Нет"),
            "graph_priority": tk.StringVar(value=str(sensor.graph_priority)),
            "calculation_flag": tk.StringVar(value=sensor.calculation_flag),
            "calibration_a": tk.StringVar(value=str(sensor.calibration_a).replace(".", ",")),
            "calibration_b": tk.StringVar(value=str(sensor.calibration_b).replace(".", ",")),
        }

        fields = [
            ("Номер канала измерителя", "meter_channel", "entry"),
            ("Наименование канала", "name", "entry"),
            ("Тип датчика / назначение", "sensor_type", ("Термодатчик", "Датчик теплового потока")),
            ("Признак активности", "used", ("Активен", "Выключен")),
            ("Период опроса, с", "poll_period_s", "entry"),
            ("Панель / группа / зона", "panel_zone", "entry"),
            ("График температуры", "show_temperature_graph", ("Отображать", "Нет")),
            ("График теплового потока", "show_heat_flux_graph", ("Отображать", "Нет")),
            ("Поле развертки", "sweep_linked", ("Привязан", "Нет")),
            ("Допустимые пределы", "limits_text", "entry"),
            ("Настройки линии графика", "graph_line_settings", "line_settings"),
            ("Линейная калибровка", "calibration", "calibration"),
            ("Участие в расчетах", "calculation_flag", "entry"),
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
            elif editor == "line_settings":
                line_frame = ttk.LabelFrame(self.engineering_detail_frame, text="Линия")
                line_frame.grid(row=row, column=1, padx=8, pady=5, sticky="ew")
                line_frame.columnconfigure(1, weight=1)
                line_frame.columnconfigure(3, weight=1)

                ttk.Label(line_frame, text="Цвет").grid(row=0, column=0, padx=6, pady=4, sticky="w")
                ttk.Entry(line_frame, textvariable=self.engineering_vars["graph_color"], width=14).grid(
                    row=0, column=1, padx=6, pady=4, sticky="ew"
                )
                color_swatch = tk.Label(line_frame, width=4, relief="solid", bd=1)
                color_swatch.grid(row=0, column=2, padx=6, pady=4, sticky="ns")

                def update_swatch(*_args, variable=self.engineering_vars["graph_color"], swatch=color_swatch) -> None:
                    color = variable.get().strip() or "#0b67d1"
                    try:
                        swatch.configure(bg=color)
                    except tk.TclError:
                        swatch.configure(bg=TEMP_DISABLED_COLOR)

                self.engineering_vars["graph_color"].trace_add("write", update_swatch)
                update_swatch()

                ttk.Label(line_frame, text="Тип").grid(row=1, column=0, padx=6, pady=4, sticky="w")
                ttk.Combobox(
                    line_frame,
                    textvariable=self.engineering_vars["graph_line_type"],
                    values=("Сплошная", "Пунктирная", "Точечная"),
                    state="readonly",
                ).grid(row=1, column=1, columnspan=3, padx=6, pady=4, sticky="ew")

                ttk.Label(line_frame, text="Толщина").grid(row=2, column=0, padx=6, pady=4, sticky="w")
                ttk.Entry(line_frame, textvariable=self.engineering_vars["graph_line_width"], width=10).grid(
                    row=2, column=1, padx=6, pady=4, sticky="ew"
                )
                ttk.Label(line_frame, text="Видимость, %").grid(row=2, column=2, padx=6, pady=4, sticky="w")
                ttk.Entry(line_frame, textvariable=self.engineering_vars["graph_visibility_percent"], width=10).grid(
                    row=2, column=3, padx=6, pady=4, sticky="ew"
                )

                ttk.Label(line_frame, text="Легенда").grid(row=3, column=0, padx=6, pady=4, sticky="w")
                ttk.Combobox(
                    line_frame,
                    textvariable=self.engineering_vars["graph_show_legend"],
                    values=("Отображать", "Нет"),
                    state="readonly",
                ).grid(row=3, column=1, padx=6, pady=4, sticky="ew")
                ttk.Label(line_frame, text="Приоритет").grid(row=3, column=2, padx=6, pady=4, sticky="w")
                ttk.Entry(line_frame, textvariable=self.engineering_vars["graph_priority"], width=10).grid(
                    row=3, column=3, padx=6, pady=4, sticky="ew"
                )
            elif editor == "calibration":
                calibration_frame = ttk.Frame(self.engineering_detail_frame)
                calibration_frame.grid(row=row, column=1, padx=8, pady=5, sticky="ew")
                calibration_frame.columnconfigure(1, weight=1)
                calibration_frame.columnconfigure(3, weight=1)
                ttk.Label(calibration_frame, text="a=").grid(row=0, column=0, sticky="w")
                ttk.Entry(calibration_frame, textvariable=self.engineering_vars["calibration_a"], width=12).grid(
                    row=0, column=1, padx=(4, 12), sticky="ew"
                )
                ttk.Label(calibration_frame, text="b=").grid(row=0, column=2, sticky="w")
                ttk.Entry(calibration_frame, textvariable=self.engineering_vars["calibration_b"], width=12).grid(
                    row=0, column=3, padx=(4, 0), sticky="ew"
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
            poll_period_s = float(self.engineering_vars["poll_period_s"].get().replace(",", "."))
            if not 0.5 <= poll_period_s <= 2.0:
                raise ValueError("Период опроса должен быть от 0,5 до 2 секунд")
            line_width = int(self.engineering_vars["graph_line_width"].get())
            if line_width <= 0:
                raise ValueError("Толщина линии должна быть положительным целым числом")
            visibility_percent = int(self.engineering_vars["graph_visibility_percent"].get())
            if not 0 <= visibility_percent <= 100:
                raise ValueError("Видимость линии должна быть от 0 до 100 процентов")
            graph_priority = int(self.engineering_vars["graph_priority"].get())
            if not 1 <= graph_priority <= 48:
                raise ValueError("Приоритет отображения должен быть от 1 до 48")
            meter_channel = int(self.engineering_vars["meter_channel"].get())
            if meter_channel <= 0:
                raise ValueError("Номер канала измерителя должен быть положительным целым числом")
            calibration_a = float(self.engineering_vars["calibration_a"].get().replace(",", "."))
            calibration_b = float(self.engineering_vars["calibration_b"].get().replace(",", "."))
        except Exception as exc:
            messagebox.showerror("Ошибка настроек канала", str(exc))
            return

        if not messagebox.askyesno(
            "Подтверждение сохранения",
            "Вы точно хотите сохранить параметры канала?",
            parent=self.engineering_window,
        ):
            return

        sensor.meter_channel = meter_channel
        sensor.name = self.engineering_vars["name"].get().strip() or sensor.num
        sensor.sensor_type = self.engineering_vars["sensor_type"].get()
        sensor.used = self.engineering_vars["used"].get() == "Активен"
        sensor.poll_period_s = poll_period_s
        sensor.panel_zone = self.engineering_vars["panel_zone"].get().strip()
        sensor.show_temperature_graph = self.engineering_vars["show_temperature_graph"].get() == "Отображать"
        sensor.show_heat_flux_graph = self.engineering_vars["show_heat_flux_graph"].get() == "Отображать"
        sensor.sweep_linked = self.engineering_vars["sweep_linked"].get() == "Привязан"
        sensor.limits_text = self.engineering_vars["limits_text"].get().strip()
        sensor.graph_color = self.engineering_vars["graph_color"].get().strip() or "#0b67d1"
        sensor.graph_line_type = self.engineering_vars["graph_line_type"].get()
        sensor.graph_line_width = line_width
        sensor.graph_visibility_percent = visibility_percent
        sensor.graph_show_legend = self.engineering_vars["graph_show_legend"].get() == "Отображать"
        sensor.graph_priority = graph_priority
        sensor.calculation_flag = self.engineering_vars["calculation_flag"].get().strip()
        sensor.calibration_a = calibration_a
        sensor.calibration_b = calibration_b
        try:
            save_channel_settings(self.channel_settings_path, self.sensor_settings)
        except Exception as exc:
            messagebox.showerror("Ошибка сохранения JSON", str(exc))
            return

        self._apply_sensor_ui_state(device_index, channel)
        key = (device_index, channel)
        if self.auto_poll_var.get():
            if sensor.used:
                self.auto_poll_next_due[key] = time.monotonic()
            else:
                self.auto_poll_next_due.pop(key, None)
        if self.graphs_window is not None and self.graphs_window.winfo_exists():
            self._draw_graphs()
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

    def _sensor_option_label(self, device_index: int, channel_index: int) -> str:
        sensor = self.sensor_settings[device_index][channel_index]
        return f"{sensor.num} - {sensor.name}"

    def _graph_options(self) -> list[str]:
        options: list[str] = []
        self.graph_option_map = {}
        for device_index in range(DEVICE_COUNT):
            for channel_index in range(TELEMETRY_CHANNELS):
                sensor = self.sensor_settings[device_index][channel_index]
                if not sensor.used or sensor.sensor_type != "Термодатчик" or not sensor.show_temperature_graph:
                    continue
                label = self._sensor_option_label(device_index, channel_index)
                options.append(label)
                self.graph_option_map[label] = (device_index, channel_index)
        return sorted(
            options,
            key=lambda item: self.sensor_settings[self.graph_option_map[item][0]][self.graph_option_map[item][1]].graph_priority,
        )

    def _open_graphs_window(self) -> None:
        if self.graphs_window is not None and self.graphs_window.winfo_exists():
            self.graphs_window.lift()
            self.graphs_window.focus_set()
            return

        window = tk.Toplevel(self)
        window.title("Графики температур")
        window.geometry("1180x820")
        window.protocol("WM_DELETE_WINDOW", self._close_graphs_window)
        window.columnconfigure(0, weight=3)
        window.columnconfigure(1, weight=1)
        window.rowconfigure(0, weight=1)
        self.graphs_window = window
        self.small_graph_vars = []
        self.small_graph_auto_axis_vars = []
        self.small_graph_canvases = []
        self.big_graph_selected = {}

        options = self._graph_options()

        graphs_frame = ttk.Frame(window)
        graphs_frame.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        graphs_frame.columnconfigure(0, weight=1)
        graphs_frame.rowconfigure(0, weight=2)
        graphs_frame.rowconfigure(1, weight=1)
        graphs_frame.rowconfigure(2, weight=1)

        big_frame = ttk.LabelFrame(graphs_frame, text="Основной общий график")
        big_frame.grid(row=0, column=0, padx=0, pady=(0, 6), sticky="nsew")
        big_frame.columnconfigure(0, weight=1)
        big_frame.rowconfigure(0, weight=1)
        self.big_graph_canvas = tk.Canvas(big_frame, bg=self.graph_bg_var.get(), highlightthickness=1, highlightbackground="#b0b0b0")
        self.big_graph_canvas.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self.big_graph_canvas.bind("<Button-3>", self._open_graph_axis_settings)
        ttk.Checkbutton(
            big_frame,
            text="Автонастройка осей",
            variable=self.big_graph_auto_axis_var,
            command=self._draw_graphs,
        ).grid(row=1, column=0, padx=6, pady=(0, 6), sticky="w")

        for index in range(2):
            graph_frame = ttk.LabelFrame(graphs_frame, text=f"Дополнительный график {index + 1}")
            graph_frame.grid(row=index + 1, column=0, padx=0, pady=(6, 6) if index == 0 else (0, 0), sticky="nsew")
            graph_frame.columnconfigure(0, weight=1)
            graph_frame.rowconfigure(1, weight=1)

            selected = tk.StringVar(value=options[index] if index < len(options) else "")
            auto_axis = tk.BooleanVar(value=True)
            self.small_graph_vars.append(selected)
            self.small_graph_auto_axis_vars.append(auto_axis)
            combo = ttk.Combobox(graph_frame, textvariable=selected, values=options, state="readonly")
            combo.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
            combo.bind("<<ComboboxSelected>>", lambda _event: self._draw_graphs())
            ttk.Checkbutton(
                graph_frame,
                text="Автонастройка осей",
                variable=auto_axis,
                command=self._draw_graphs,
            ).grid(row=0, column=1, padx=4, pady=4, sticky="e")

            canvas = tk.Canvas(graph_frame, height=130, bg=self.graph_bg_var.get(), highlightthickness=1, highlightbackground="#b0b0b0")
            canvas.grid(row=1, column=0, columnspan=2, padx=4, pady=(0, 4), sticky="nsew")
            canvas.bind("<Button-3>", self._open_graph_axis_settings)
            self.small_graph_canvases.append(canvas)

        side_frame = ttk.Frame(window)
        side_frame.grid(row=0, column=1, padx=(0, 8), pady=8, sticky="nsew")
        side_frame.columnconfigure(0, weight=1)
        side_frame.rowconfigure(1, weight=1)

        settings_frame = ttk.LabelFrame(side_frame, text="Настройки графиков")
        settings_frame.grid(row=0, column=0, sticky="ew")
        settings_frame.columnconfigure(1, weight=1)
        ttk.Label(settings_frame, text="Фон").grid(row=0, column=0, padx=6, pady=4, sticky="w")
        ttk.Entry(settings_frame, textvariable=self.graph_bg_var, width=10).grid(row=0, column=1, padx=6, pady=4, sticky="ew")
        ttk.Label(settings_frame, text="Точек истории").grid(row=1, column=0, padx=6, pady=4, sticky="w")
        ttk.Entry(settings_frame, textvariable=self.graph_points_var, width=10).grid(row=1, column=1, padx=6, pady=4, sticky="ew")
        ttk.Button(settings_frame, text="Оси и диапазоны", command=self._open_graph_axis_settings_window).grid(
            row=2, column=0, columnspan=2, padx=6, pady=6, sticky="ew"
        )
        ttk.Button(settings_frame, text="Refresh", command=self._draw_graphs).grid(row=3, column=0, columnspan=2, padx=6, pady=(0, 6), sticky="ew")

        checks_frame = ttk.LabelFrame(side_frame, text="Каналы общего графика")
        checks_frame.grid(row=1, column=0, pady=(8, 0), sticky="nsew")
        checks_frame.columnconfigure(0, weight=1)
        checks_frame.rowconfigure(0, weight=1)

        checks_canvas = tk.Canvas(checks_frame, highlightthickness=0)
        checks_canvas.grid(row=0, column=0, sticky="nsew")
        checks_scroll = ttk.Scrollbar(checks_frame, orient="vertical", command=checks_canvas.yview)
        checks_scroll.grid(row=0, column=1, sticky="ns")
        checks_canvas.configure(yscrollcommand=checks_scroll.set)
        inner = ttk.Frame(checks_canvas)
        checks_canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: checks_canvas.configure(scrollregion=checks_canvas.bbox("all")))

        for index, option in enumerate(options):
            selected = tk.BooleanVar(value=index < 3)
            self.big_graph_selected[option] = selected
            ttk.Checkbutton(inner, text=option, variable=selected, command=self._draw_graphs).grid(
                row=index, column=0, padx=4, pady=1, sticky="w"
            )

        self._draw_graphs()
        self._schedule_graph_refresh()

    def _close_graphs_window(self) -> None:
        if self.graphs_window is not None:
            self.graphs_window.destroy()
            self.graphs_window = None
        self.big_graph_canvas = None
        self.small_graph_canvases = []
        self.small_graph_auto_axis_vars = []

    def _graph_points_limit(self) -> int:
        try:
            return max(2, min(5000, int(self.graph_points_var.get())))
        except ValueError:
            return 100

    def _axis_float(self, variable: tk.StringVar) -> float | None:
        value = variable.get().strip().replace(",", ".")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _graph_temperature_bounds(
        self, series_points: list[list[tuple[datetime, float]]], auto_axis: bool = False
    ) -> tuple[float, float]:
        values = [temperature for series in series_points for _timestamp, temperature in series]
        manual_min = None if auto_axis else self._axis_float(self.graph_y_min_var)
        manual_max = None if auto_axis else self._axis_float(self.graph_y_max_var)
        y_min = manual_min if manual_min is not None else (min(values) if values else 0.0)
        y_max = manual_max if manual_max is not None else (max(values) if values else 1.0)
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0
        elif auto_axis:
            padding = max(0.5, (y_max - y_min) * 0.08)
            y_min -= padding
            y_max += padding
        return y_min, y_max

    def _graph_time_bounds(
        self, series_points: list[list[tuple[datetime, float]]], auto_axis: bool = False
    ) -> tuple[datetime, datetime]:
        timestamps = [timestamp for series in series_points for timestamp, _temperature in series]
        now = datetime.utcnow()
        if auto_axis and timestamps:
            start = min(timestamps)
            end = max(timestamps)
            if start == end:
                start -= timedelta(seconds=30)
                end += timedelta(seconds=30)
            else:
                padding = max(1.0, (end - start).total_seconds() * 0.05)
                start -= timedelta(seconds=padding)
                end += timedelta(seconds=padding)
            return start, end
        try:
            minutes = max(1.0, float(self.graph_time_range_min_var.get().replace(",", ".")))
        except ValueError:
            minutes = 30.0
        end = max(timestamps) if timestamps else now
        start = end.timestamp() - minutes * 60
        return datetime.utcfromtimestamp(start), end

    def _graph_y_bounds(self, series_values: list[list[float]]) -> tuple[float, float]:
        values = [value for series in series_values for value in series]
        try:
            y_min = float(self.graph_y_min_var.get().replace(",", ".")) if self.graph_y_min_var.get().strip() else min(values)
        except (ValueError, TypeError):
            y_min = min(values) if values else 0.0
        try:
            y_max = float(self.graph_y_max_var.get().replace(",", ".")) if self.graph_y_max_var.get().strip() else max(values)
        except (ValueError, TypeError):
            y_max = max(values) if values else 1.0
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0
        return y_min, y_max

    def _series_style(self, label: str, fallback_color: str) -> tuple[str, int, tuple[int, ...] | None, str | None, bool]:
        sensor_ref = self.graph_option_map.get(label)
        if sensor_ref is None:
            return fallback_color, 2, None, None, True
        device_index, channel_index = sensor_ref
        sensor = self.sensor_settings[device_index][channel_index]
        color = sensor.graph_color or fallback_color
        width = max(1, sensor.graph_line_width)
        dash = None
        if sensor.graph_line_type == "Пунктирная":
            dash = (6, 3)
        elif sensor.graph_line_type == "Точечная":
            dash = (2, 3)
        stipple = None
        if sensor.graph_visibility_percent <= 0:
            stipple = "gray12"
        elif sensor.graph_visibility_percent < 35:
            stipple = "gray25"
        elif sensor.graph_visibility_percent < 70:
            stipple = "gray50"
        elif sensor.graph_visibility_percent < 100:
            stipple = "gray75"
        return color, width, dash, stipple, sensor.graph_show_legend

    def _draw_series_canvas(self, canvas: tk.Canvas, series: list[tuple[str, list[float]]]) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 240)
        height = max(canvas.winfo_height(), 120)
        margin_left = 42
        margin_right = 12
        margin_top = 16
        margin_bottom = 28
        plot_width = max(1, width - margin_left - margin_right)
        plot_height = max(1, height - margin_top - margin_bottom)

        canvas.create_rectangle(margin_left, margin_top, width - margin_right, height - margin_bottom, outline="#c8c8c8")
        non_empty = [(label, values) for label, values in series if values]
        if not non_empty:
            canvas.create_text(width / 2, height / 2, text="No data", fill="#666666")
            return

        y_min, y_max = self._graph_y_bounds([values for _label, values in non_empty])
        canvas.create_text(6, margin_top, text=f"{y_max:.1f}", anchor="nw", fill="#666666")
        canvas.create_text(6, height - margin_bottom - 12, text=f"{y_min:.1f}", anchor="nw", fill="#666666")
        colors = ("#0b67d1", "#c23b22", "#2f8f2f", "#8a2be2", "#d18f00", "#008b8b", "#444444", "#e377c2")

        non_empty = sorted(
            non_empty,
            key=lambda item: self.sensor_settings[self.graph_option_map[item[0]][0]][self.graph_option_map[item[0]][1]].graph_priority
            if item[0] in self.graph_option_map
            else 48,
        )

        legend_row = 0
        for series_index, (label, values) in enumerate(non_empty):
            points: list[float] = []
            count = len(values)
            for index, value in enumerate(values):
                x = margin_left + (plot_width * index / max(1, count - 1))
                y = margin_top + plot_height - ((value - y_min) / (y_max - y_min) * plot_height)
                points.extend([x, y])
            color = colors[series_index % len(colors)]
            color, line_width, dash, stipple, show_legend = self._series_style(label, color)
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=line_width, dash=dash, stipple=stipple)
            else:
                canvas.create_oval(points[0] - 2, points[1] - 2, points[0] + 2, points[1] + 2, fill=color, outline=color)
            if show_legend:
                canvas.create_text(margin_left + 6, margin_top + 12 + legend_row * 14, text=label, anchor="w", fill=color)
                legend_row += 1

    def _draw_temperature_canvas(
        self,
        canvas: tk.Canvas,
        series: list[tuple[str, list[tuple[datetime, float]]]],
        auto_axis: bool = False,
    ) -> None:
        canvas.delete("all")
        try:
            canvas.configure(bg=self.graph_bg_var.get().strip() or "white")
        except tk.TclError:
            canvas.configure(bg="white")

        width = max(canvas.winfo_width(), 260)
        height = max(canvas.winfo_height(), 140)
        margin_left = 78
        margin_right = 18
        margin_top = 18
        margin_bottom = 42
        plot_width = max(1, width - margin_left - margin_right)
        plot_height = max(1, height - margin_top - margin_bottom)
        canvas.create_rectangle(margin_left, margin_top, width - margin_right, height - margin_bottom, outline="#c8c8c8")

        non_empty = [(label, points) for label, points in series if points]
        if not non_empty:
            canvas.create_text(width / 2, height / 2, text="No data", fill="#666666")
            return

        y_min, y_max = self._graph_temperature_bounds([points for _label, points in non_empty], auto_axis)
        t_min, t_max = self._graph_time_bounds([points for _label, points in non_empty], auto_axis)
        time_span = max(1.0, (t_max - t_min).total_seconds())

        canvas.create_text(margin_left, height - 18, text="UTC", anchor="w", fill="#555555")
        canvas.create_text(8, margin_top, text="Температура, °C", anchor="nw", fill="#555555")
        canvas.create_text(margin_left, height - margin_bottom + 4, text=t_min.strftime("%H:%M:%S"), anchor="n", fill="#666666")
        canvas.create_text(width - margin_right, height - margin_bottom + 4, text=t_max.strftime("%H:%M:%S"), anchor="n", fill="#666666")
        canvas.create_text(8, margin_top + 18, text=f"{y_max:.1f}", anchor="nw", fill="#666666")
        canvas.create_text(8, height - margin_bottom - 12, text=f"{y_min:.1f}", anchor="nw", fill="#666666")

        try:
            time_step_min = max(0.1, float(self.graph_time_step_min_var.get().replace(",", ".")))
        except ValueError:
            time_step_min = 5.0
        step_seconds = time_step_min * 60
        step_count = int(time_span // step_seconds)
        for step in range(1, step_count + 1):
            x = margin_left + (step * step_seconds / time_span * plot_width)
            canvas.create_line(x, margin_top, x, height - margin_bottom, fill="#eeeeee")
            timestamp = t_min + timedelta(seconds=step * step_seconds)
            canvas.create_text(x, height - margin_bottom + 16, text=timestamp.strftime("%H:%M:%S"), anchor="n", fill="#777777")

        y_step = self._axis_float(self.graph_y_step_var)
        if y_step and y_step > 0:
            tick = math.ceil(y_min / y_step) * y_step
            while tick <= y_max:
                y = height - margin_bottom - ((tick - y_min) / (y_max - y_min) * plot_height)
                canvas.create_line(margin_left, y, width - margin_right, y, fill="#eeeeee")
                canvas.create_text(margin_left - 6, y, text=f"{tick:g}", anchor="e", fill="#777777")
                tick += y_step

        colors = ("#0b67d1", "#c23b22", "#2f8f2f", "#8a2be2", "#d18f00", "#008b8b", "#444444", "#e377c2")
        non_empty = sorted(
            non_empty,
            key=lambda item: self.sensor_settings[self.graph_option_map[item[0]][0]][self.graph_option_map[item[0]][1]].graph_priority
            if item[0] in self.graph_option_map
            else 48,
        )
        legend_row = 0
        for series_index, (label, points) in enumerate(non_empty):
            coords: list[float] = []
            for timestamp, temperature in points:
                x = margin_left + ((timestamp - t_min).total_seconds() / time_span * plot_width)
                y = height - margin_bottom - ((temperature - y_min) / (y_max - y_min) * plot_height)
                coords.extend([x, y])
            color = colors[series_index % len(colors)]
            color, line_width, dash, stipple, show_legend = self._series_style(label, color)
            if len(coords) >= 4:
                canvas.create_line(*coords, fill=color, width=line_width, dash=dash, stipple=stipple)
            elif coords:
                canvas.create_oval(coords[0] - 2, coords[1] - 2, coords[0] + 2, coords[1] + 2, fill=color, outline=color)
            if show_legend:
                canvas.create_text(margin_left + 6, margin_top + 12 + legend_row * 14, text=label, anchor="w", fill=color)
                legend_row += 1

    def _series_values(self, option: str) -> list[float]:
        sensor_ref = self.graph_option_map.get(option)
        if sensor_ref is None:
            return []
        device_index, channel_index = sensor_ref
        points = self.plot_history[device_index][channel_index][-self._graph_points_limit() :]
        return [temperature for _timestamp, temperature in points]

    def _series_points(self, option: str) -> list[tuple[datetime, float]]:
        sensor_ref = self.graph_option_map.get(option)
        if sensor_ref is None:
            return []
        device_index, channel_index = sensor_ref
        return self.plot_history[device_index][channel_index][-self._graph_points_limit() :]

    def _draw_graphs(self) -> None:
        if self.graphs_window is None or not self.graphs_window.winfo_exists():
            return

        for variable, canvas, auto_axis in zip(
            self.small_graph_vars, self.small_graph_canvases, self.small_graph_auto_axis_vars
        ):
            option = variable.get()
            self._draw_temperature_canvas(canvas, [(option, self._series_points(option))], auto_axis.get())

        if self.big_graph_canvas is not None:
            selected_series = [
                (option, self._series_points(option))
                for option, selected in self.big_graph_selected.items()
                if selected.get()
            ]
            self._draw_temperature_canvas(self.big_graph_canvas, selected_series, self.big_graph_auto_axis_var.get())

    def _schedule_graph_refresh(self) -> None:
        if self.graphs_window is None or not self.graphs_window.winfo_exists():
            return
        self._draw_graphs()
        self.graphs_window.after(1000, self._schedule_graph_refresh)

    def _open_graph_axis_settings(self, event: tk.Event) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Настроить оси и фон", command=self._open_graph_axis_settings_window)
        menu.tk_popup(event.x_root, event.y_root)

    def _open_graph_axis_settings_window(self) -> None:
        window = tk.Toplevel(self.graphs_window or self)
        window.title("Настройки осей графика")
        window.resizable(False, False)

        fields = [
            ("Y min, °C", self.graph_y_min_var),
            ("Y max, °C", self.graph_y_max_var),
            ("Дискретность Y, °C", self.graph_y_step_var),
            ("Диапазон времени X, мин", self.graph_time_range_min_var),
            ("Дискретность X, мин", self.graph_time_step_min_var),
            ("Фон графика", self.graph_bg_var),
            ("Точек истории", self.graph_points_var),
        ]
        for row, (label, variable) in enumerate(fields):
            ttk.Label(window, text=label).grid(row=row, column=0, padx=8, pady=5, sticky="w")
            ttk.Entry(window, textvariable=variable, width=18).grid(row=row, column=1, padx=8, pady=5, sticky="ew")
        ttk.Button(window, text="Применить", command=lambda: (self._draw_graphs(), window.destroy())).grid(
            row=len(fields), column=0, columnspan=2, padx=8, pady=10, sticky="ew"
        )


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
                timeout=max(0.2, settings.scan_rate_ms / 1000),
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
        self._stop_auto_poll()
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
            address = TELEMETRY_BASE_ADDRESS + (channel - 1) * TELEMETRY_REGISTERS_PER_CHANNEL
            request = build_request(settings.slave_addr, 0x03, address, TELEMETRY_REGISTERS_PER_CHANNEL)
        except Exception as exc:
            messagebox.showerror("Telemetry settings error", str(exc))
            return

        self._set_temperature_label(device_index, channel, "reading")
        self._send_request(
            request,
            9,
            lambda response, dev=device_index, selected=channel, slave_addr=settings.slave_addr: self._handle_temperature_response(
                dev, selected, response, slave_addr, False
            ),
        )

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
                    for channel in range(1, TELEMETRY_CHANNELS + 1):
                        if not self.sensor_settings[device_index][channel - 1].used:
                            continue
                        try:
                            address = TELEMETRY_BASE_ADDRESS + (channel - 1) * TELEMETRY_REGISTERS_PER_CHANNEL
                            request = build_request(settings.slave_addr, 0x03, address, TELEMETRY_REGISTERS_PER_CHANNEL)
                            self.ui_queue.put(("temp", f"{device_index}|{channel}|reading"))
                            response = self._transact(request, 9)
                            save_to_csv = 1 if auto else 0
                            self.ui_queue.put(
                                ("telemetry", f"{device_index}|{channel}|{settings.slave_addr}|{save_to_csv}|{response.hex()}")
                            )
                            time.sleep(max(0.02, settings.scan_rate_ms / 1000 / 10))
                        except Exception as exc:
                            self.ui_queue.put(("temp", f"{device_index}|{channel}|error"))
                            self.ui_queue.put(("log", f"Device {device_index + 1}, sensor {channel}: Error: {exc}"))
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
                    self.auto_poll_next_due[key] = now + max(0.5, min(2.0, sensor.poll_period_s))

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
                for device_index, channel in due_channels:
                    settings = settings_by_device[device_index]
                    try:
                        address = TELEMETRY_BASE_ADDRESS + (channel - 1) * TELEMETRY_REGISTERS_PER_CHANNEL
                        request = build_request(settings.slave_addr, 0x03, address, TELEMETRY_REGISTERS_PER_CHANNEL)
                        self.ui_queue.put(("temp", f"{device_index}|{channel}|reading"))
                        response = self._transact(request, 9)
                        self.ui_queue.put(("telemetry", f"{device_index}|{channel}|{settings.slave_addr}|1|{response.hex()}"))
                        time.sleep(max(0.02, settings.scan_rate_ms / 1000 / 10))
                    except Exception as exc:
                        self.ui_queue.put(("temp", f"{device_index}|{channel}|error"))
                        self.ui_queue.put(("log", f"Device {device_index + 1}, sensor {channel}: Error: {exc}"))
            finally:
                self.ui_queue.put(("poll_done", "auto"))

        threading.Thread(target=worker, daemon=True).start()
        return True

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

    def _transact(self, request: bytes, expected_size: int) -> bytes:
        if not self.worker_lock.acquire(blocking=False):
            raise RuntimeError("Request is already running")
        try:
            assert self.serial_port is not None
            self.serial_port.reset_input_buffer()
            self.serial_port.write(request)
            self.serial_port.flush()
            response = self.serial_port.read(expected_size or 256)
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
        save_to_csv: bool,
    ) -> None:
        try:
            result = validate_read_response(response, slave_addr, 0x03, 4)
            if not result.valid:
                self._set_temperature_label(device_index, channel, "invalid")
                self._set_temperature_color(device_index, channel, None)
                self.status_var.set(f"Device {device_index + 1}, sensor {channel}: {result.message}")
                self._append_log(f"Device {device_index + 1}, sensor {channel}: invalid response - {result.message}")
                return

            temperature = decode_tm5104_temperature(result.data)
            history = self.temperature_history[device_index][channel - 1]
            history.append(temperature)
            del history[:-10]
            plot_history = self.plot_history[device_index][channel - 1]
            plot_history.append((datetime.now(), temperature))
            del plot_history[:-5000]
            self._set_temperature_label(device_index, channel, f"{temperature:.1f} C")
            self._set_temperature_color(device_index, channel, temperature)
            self.status_var.set(f"Device {device_index + 1}, sensor {channel}: {temperature:.2f} C")
            self._append_log(f"Device {device_index + 1}, sensor {channel}: valid, temperature = {temperature:.3f} C")
            if save_to_csv:
                self._record_measurement(device_index, channel, temperature)
        except Exception as exc:
            self._set_temperature_label(device_index, channel, "error")
            self._set_temperature_color(device_index, channel, None)
            self.status_var.set(f"Device {device_index + 1}, sensor {channel}: decode error")
            self._append_log(f"Device {device_index + 1}, sensor {channel}: decode error - {exc}")

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

    def _start_auto_poll(self) -> None:
        if not self._ensure_connected():
            return

        self.auto_poll_var.set(True)
        now = time.monotonic()
        self.auto_poll_next_due = {
            (device_index, channel): now
            for device_index in range(DEVICE_COUNT)
            for channel in range(1, TELEMETRY_CHANNELS + 1)
            if self.sensor_settings[device_index][channel - 1].used
        }
        self._start_measurement_segment()
        self.auto_start_button.state(["disabled"])
        self.auto_stop_button.state(["!disabled"])
        self.auto_poll_status_var.set("Auto poll running")
        self._schedule_auto_poll(0)

    def _stop_auto_poll(self) -> None:
        self.auto_poll_var.set(False)
        self._cancel_auto_poll()
        self.auto_poll_next_due = {}
        was_recording = self.measurement_recording
        self.measurement_recording = False
        if was_recording:
            self._finish_measurement_row()
            self._write_measurement_segment()
        if hasattr(self, "auto_start_button"):
            self.auto_start_button.state(["!disabled"])
        if hasattr(self, "auto_stop_button"):
            self.auto_stop_button.state(["disabled"])
        self.auto_poll_status_var.set("Auto poll stopped")

    def _schedule_auto_poll(self, delay_ms: int | None = None) -> None:
        self._cancel_auto_poll()
        delay = 100 if delay_ms is None else delay_ms
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
                device_text, channel_text, slave_text, save_text, response_hex = value.split("|", 4)
                self._handle_temperature_response(
                    int(device_text),
                    int(channel_text),
                    bytes.fromhex(response_hex),
                    int(slave_text),
                    save_text == "1",
                )
            elif kind == "poll_done":
                self.temperature_poll_running = False
                if value == "auto":
                    self._finish_measurement_row()
                if value == "auto" and self.auto_poll_var.get():
                    self.auto_poll_status_var.set("Auto poll running")
                    self._schedule_auto_poll()
                elif value == "auto":
                    self.auto_poll_status_var.set("Auto poll stopped")
            elif kind == "callback":
                callback_id_text, response_hex = value.split("|", 1)
                callback = self._pending_callbacks.pop(int(callback_id_text), None)
                if callback is not None:
                    callback(bytes.fromhex(response_hex))
            else:
                self._append_log(value)
        self.after(100, self._drain_ui_queue)

    def _on_close(self) -> None:
        self._disconnect()
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        if self.engineering_window is not None and self.engineering_window.winfo_exists():
            self.engineering_window.destroy()
        if self.logs_window is not None and self.logs_window.winfo_exists():
            self.logs_window.destroy()
        if self.graphs_window is not None and self.graphs_window.winfo_exists():
            self.graphs_window.destroy()
        self.destroy()


if __name__ == "__main__":
    app = ElementCheckerApp()
    app.mainloop()
