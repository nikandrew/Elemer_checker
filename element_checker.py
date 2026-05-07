from __future__ import annotations

import math
import queue
import struct
import threading
import time
import tkinter as tk
import zipfile
from dataclasses import dataclass
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
    name: str
    used: bool


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
        [SensorSettings(name=str(channel), used=True) for channel in range(1, TELEMETRY_CHANNELS + 1)]
        for _device in range(DEVICE_COUNT)
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
            sensors[device_index][channel - 1] = SensorSettings(name=name, used=used)
    except Exception as exc:
        return sensors, f"{path.name}: failed to read settings: {exc}"

    return sensors, None


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
        self.temperature_buttons: list[list[ttk.Button]] = []
        self.settings_window: tk.Toplevel | None = None
        self.sensor_settings, self.sensor_settings_warning = load_sensor_settings(Path(__file__).with_name(SETTINGS_FILE))

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
        self.auto_poll_interval_var = tk.StringVar(value="1000")
        self.auto_poll_unit_var = tk.StringVar(value="ms")
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
        self.rowconfigure(4, weight=1)

        top_frame = ttk.Frame(self)
        top_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        top_frame.columnconfigure(2, weight=1)

        self.connect_button = ttk.Button(top_frame, text="Connect port", command=self._toggle_port)
        self.connect_button.grid(row=0, column=0, padx=(0, 8), sticky="w")
        ttk.Button(top_frame, text="Settings", command=self._open_settings).grid(row=0, column=1, padx=(0, 12), sticky="w")

        self.status_var = tk.StringVar(value="Port disconnected")
        ttk.Label(top_frame, textvariable=self.status_var, anchor="w").grid(row=0, column=2, sticky="ew")

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
            device_buttons: list[ttk.Button] = []
            for index in range(TELEMETRY_CHANNELS):
                channel = index + 1
                row = index // 4 + 1
                column = index % 4
                sensor = self.sensor_settings[device_index][channel - 1]
                value_var = tk.StringVar(value=self._sensor_label(device_index, channel, "--"))
                device_values.append(value_var)

                button = ttk.Button(
                    device_frame,
                    textvariable=value_var,
                    width=8,
                    command=lambda dev=device_index, selected=channel: self._request_temperature(dev, selected),
                )
                button.grid(row=row, column=column, padx=3, pady=3, sticky="nsew")
                if not sensor.used:
                    button.state(["disabled"])
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
        auto_frame.columnconfigure(6, weight=1)

        ttk.Label(auto_frame, text="Interval").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Entry(
            auto_frame,
            textvariable=self.auto_poll_interval_var,
            width=10,
        ).grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        ttk.Combobox(
            auto_frame,
            textvariable=self.auto_poll_unit_var,
            values=("ms", "s"),
            state="readonly",
            width=6,
        ).grid(row=0, column=2, padx=8, pady=8, sticky="ew")

        self.auto_start_button = ttk.Button(auto_frame, text="Start measurement", command=self._start_auto_poll)
        self.auto_start_button.grid(row=0, column=3, padx=8, pady=8, sticky="ew")
        self.auto_stop_button = ttk.Button(auto_frame, text="Stop measurement", command=self._stop_auto_poll)
        self.auto_stop_button.grid(row=0, column=4, padx=8, pady=8, sticky="ew")
        self.auto_stop_button.state(["disabled"])
        ttk.Label(auto_frame, textvariable=self.auto_poll_status_var, anchor="w").grid(
            row=0, column=5, columnspan=2, padx=8, pady=8, sticky="ew"
        )

        content = ttk.PanedWindow(self, orient="vertical")
        content.grid(row=4, column=0, padx=12, pady=6, sticky="nsew")

        response_frame = ttk.LabelFrame(content, text="Response")
        response_frame.columnconfigure(0, weight=1)
        response_frame.rowconfigure(0, weight=1)
        content.add(response_frame, weight=1)

        self.response_text = tk.Text(response_frame, wrap="word", height=12)
        self.response_text.grid(row=0, column=0, sticky="nsew")
        response_scroll = ttk.Scrollbar(response_frame, orient="vertical", command=self.response_text.yview)
        response_scroll.grid(row=0, column=1, sticky="ns")
        self.response_text.configure(yscrollcommand=response_scroll.set)

    def _sensor_label(self, device_index: int, channel: int, value: str) -> str:
        name = self.sensor_settings[device_index][channel - 1].name
        return f"{name}\n{value}"

    def _set_temperature_label(self, device_index: int, channel: int, value: str) -> None:
        self.temperature_vars[device_index][channel - 1].set(self._sensor_label(device_index, channel, value))

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
                dev, selected, response, slave_addr
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
                            self.ui_queue.put(("telemetry", f"{device_index}|{channel}|{settings.slave_addr}|{response.hex()}"))
                            time.sleep(max(0.02, settings.scan_rate_ms / 1000 / 10))
                        except Exception as exc:
                            self.ui_queue.put(("temp", f"{device_index}|{channel}|error"))
                            self.ui_queue.put(("log", f"Device {device_index + 1}, sensor {channel}: Error: {exc}"))
            finally:
                self.ui_queue.put(("poll_done", "auto" if auto else "manual"))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
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

    def _handle_temperature_response(self, device_index: int, channel: int, response: bytes, slave_addr: int) -> None:
        try:
            result = validate_read_response(response, slave_addr, 0x03, 4)
            if not result.valid:
                self._set_temperature_label(device_index, channel, "invalid")
                self.status_var.set(f"Device {device_index + 1}, sensor {channel}: {result.message}")
                self._append_log(f"Device {device_index + 1}, sensor {channel}: invalid response - {result.message}")
                return

            temperature = decode_tm5104_temperature(result.data)
            self._set_temperature_label(device_index, channel, f"{temperature:.1f} C")
            self.status_var.set(f"Device {device_index + 1}, sensor {channel}: {temperature:.2f} C")
            self._append_log(f"Device {device_index + 1}, sensor {channel}: valid, temperature = {temperature:.3f} C")
        except Exception as exc:
            self._set_temperature_label(device_index, channel, "error")
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

    def _auto_poll_interval_ms(self) -> int:
        raw_value = self.auto_poll_interval_var.get().strip().replace(",", ".")
        if not raw_value:
            raise ValueError("Auto poll interval is empty")
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError("Auto poll interval must be a number") from exc
        if self.auto_poll_unit_var.get() == "s":
            value *= 1000
        delay = int(value)
        if not 100 <= delay <= 100000:
            raise ValueError("Auto poll interval must be from 100 ms to 100 s")
        return delay

    def _start_auto_poll(self) -> None:
        if not self._ensure_connected():
            return
        try:
            self._auto_poll_interval_ms()
        except Exception as exc:
            messagebox.showerror("Auto poll settings error", str(exc))
            return

        self.auto_poll_var.set(True)
        self.auto_start_button.state(["disabled"])
        self.auto_stop_button.state(["!disabled"])
        self.auto_poll_status_var.set("Auto poll running")
        self._schedule_auto_poll(0)

    def _stop_auto_poll(self) -> None:
        self.auto_poll_var.set(False)
        self._cancel_auto_poll()
        if hasattr(self, "auto_start_button"):
            self.auto_start_button.state(["!disabled"])
        if hasattr(self, "auto_stop_button"):
            self.auto_stop_button.state(["disabled"])
        self.auto_poll_status_var.set("Auto poll stopped")

    def _schedule_auto_poll(self, delay_ms: int | None = None) -> None:
        self._cancel_auto_poll()
        try:
            delay = self._auto_poll_interval_ms() if delay_ms is None else delay_ms
        except Exception as exc:
            self._stop_auto_poll()
            messagebox.showerror("Auto poll settings error", str(exc))
            return
        self.auto_poll_after_id = self.after(delay, self._auto_poll_once)

    def _auto_poll_once(self) -> None:
        self.auto_poll_after_id = None
        if self.auto_poll_var.get():
            if not self.serial_port or not self.serial_port.is_open:
                self._stop_auto_poll()
                return
            self.auto_poll_status_var.set("Polling...")
            started = self._request_all_temperatures(auto=True)
            if not started:
                self._schedule_auto_poll()

    def _cancel_auto_poll(self) -> None:
        if self.auto_poll_after_id is not None:
            self.after_cancel(self.auto_poll_after_id)
            self.auto_poll_after_id = None

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.response_text.insert("end", f"[{timestamp}] {message}\n")
        self.response_text.see("end")

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
                device_text, channel_text, slave_text, response_hex = value.split("|", 3)
                self._handle_temperature_response(
                    int(device_text),
                    int(channel_text),
                    bytes.fromhex(response_hex),
                    int(slave_text),
                )
            elif kind == "poll_done":
                self.temperature_poll_running = False
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
        self.destroy()


if __name__ == "__main__":
    app = ElementCheckerApp()
    app.mainloop()
