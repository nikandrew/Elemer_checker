from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

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

READ_FUNCTIONS = {0x01, 0x02, 0x03, 0x04}
WRITE_SINGLE_FUNCTIONS = {0x05, 0x06}


@dataclass
class PortSettings:
    port: str
    baudrate: int
    stopbits: float
    slave_addr: int
    scan_rate_ms: int


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


def parse_int(value: str, base_name: str) -> int:
    value = value.strip()
    if not value:
        raise ValueError("Empty numeric value")
    if base_name == "Hex":
        return int(value, 16)
    return int(value, 10)


def format_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


class ElementCheckerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Element TM5104 Modbus Checker")
        self.geometry("760x560")
        self.minsize(720, 500)

        self.serial_port = None
        self.worker_lock = threading.Lock()
        self.ui_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.auto_poll_after_id = None

        self.port_var = tk.StringVar(value="COM22")
        self.baud_var = tk.StringVar(value="115200")
        self.stopbits_var = tk.StringVar(value="2")
        self.slave_var = tk.StringVar(value="2")
        self.scan_rate_var = tk.StringVar(value="1000")
        self.function_var = tk.StringVar(value="03 - Read Holding Registers")
        self.start_address_var = tk.StringVar(value="1")
        self.address_base_var = tk.StringVar(value="Dec")
        self.quantity_var = tk.StringVar(value="2")
        self.expected_var = tk.StringVar(value="")
        self.auto_poll_var = tk.BooleanVar(value=False)

        self._build_ui()
        self._refresh_expected()
        self.after(100, self._drain_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        port_frame = ttk.LabelFrame(self, text="Port settings")
        port_frame.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        for column in range(8):
            port_frame.columnconfigure(column, weight=1)

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

        ttk.Label(port_frame, text="Stop bits").grid(row=0, column=4, padx=8, pady=8, sticky="w")
        ttk.Combobox(port_frame, textvariable=self.stopbits_var, values=("1", "1.5", "2"), width=8).grid(
            row=0, column=5, padx=8, pady=8, sticky="ew"
        )

        self.connect_button = ttk.Button(port_frame, text="Connect port", command=self._toggle_port)
        self.connect_button.grid(row=0, column=6, columnspan=2, padx=8, pady=8, sticky="ew")

        modbus_frame = ttk.LabelFrame(self, text="Modbus request")
        modbus_frame.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
        for column in range(8):
            modbus_frame.columnconfigure(column, weight=1)

        ttk.Label(modbus_frame, text="Slave addr").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Spinbox(modbus_frame, from_=0, to=247, textvariable=self.slave_var, width=8).grid(
            row=0, column=1, padx=8, pady=8, sticky="ew"
        )

        ttk.Label(modbus_frame, text="Scan Rate (ms)").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        ttk.Spinbox(modbus_frame, from_=50, to=60000, increment=50, textvariable=self.scan_rate_var, width=10).grid(
            row=0, column=3, padx=8, pady=8, sticky="ew"
        )

        ttk.Label(modbus_frame, text="Function").grid(row=0, column=4, padx=8, pady=8, sticky="w")
        function_combo = ttk.Combobox(modbus_frame, textvariable=self.function_var, values=tuple(FUNCTIONS), state="readonly")
        function_combo.grid(row=0, column=5, columnspan=3, padx=8, pady=8, sticky="ew")
        function_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_expected())

        ttk.Label(modbus_frame, text="Start Address").grid(row=1, column=0, padx=8, pady=8, sticky="w")
        address_spin = ttk.Spinbox(modbus_frame, from_=0, to=65535, textvariable=self.start_address_var, width=10)
        address_spin.grid(row=1, column=1, padx=8, pady=8, sticky="ew")

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
        self.quantity_var.trace_add("write", lambda *_args: self._refresh_expected())

        ttk.Label(modbus_frame, text="Expected response bytes").grid(row=1, column=5, padx=8, pady=8, sticky="w")
        ttk.Entry(modbus_frame, textvariable=self.expected_var, state="readonly", width=8).grid(
            row=1, column=6, padx=8, pady=8, sticky="ew"
        )

        ttk.Checkbutton(modbus_frame, text="Auto", variable=self.auto_poll_var, command=self._toggle_auto_poll).grid(
            row=1, column=7, padx=8, pady=8, sticky="w"
        )

        action_frame = ttk.Frame(self)
        action_frame.grid(row=3, column=0, padx=12, pady=(6, 12), sticky="ew")
        action_frame.columnconfigure(0, weight=1)

        self.request_button = ttk.Button(action_frame, text="Send request", command=self._send_request)
        self.request_button.grid(row=0, column=0, sticky="ew")

        response_frame = ttk.LabelFrame(self, text="Response")
        response_frame.grid(row=2, column=0, padx=12, pady=6, sticky="nsew")
        response_frame.columnconfigure(0, weight=1)
        response_frame.rowconfigure(0, weight=1)

        self.response_text = tk.Text(response_frame, wrap="word", height=14)
        self.response_text.grid(row=0, column=0, sticky="nsew")
        response_scroll = ttk.Scrollbar(response_frame, orient="vertical", command=self.response_text.yview)
        response_scroll.grid(row=0, column=1, sticky="ns")
        self.response_text.configure(yscrollcommand=response_scroll.set)

        self.status_var = tk.StringVar(value="Port disconnected")
        ttk.Label(self, textvariable=self.status_var, anchor="w").grid(row=4, column=0, padx=12, pady=(0, 10), sticky="ew")

    def _available_ports(self) -> tuple[str, ...]:
        if list_ports is None:
            return ("COM22",)
        ports = [port.device for port in list_ports.comports()]
        if "COM22" not in ports:
            ports.insert(0, "COM22")
        return tuple(ports)

    def _settings(self) -> PortSettings:
        return PortSettings(
            port=self.port_var.get().strip() or "COM22",
            baudrate=int(self.baud_var.get()),
            stopbits=float(self.stopbits_var.get()),
            slave_addr=int(self.slave_var.get()),
            scan_rate_ms=max(50, int(self.scan_rate_var.get())),
        )

    def _toggle_port(self) -> None:
        if self.serial_port and self.serial_port.is_open:
            self._disconnect()
            return
        self._connect()

    def _connect(self) -> None:
        if serial is None:
            messagebox.showerror("pyserial is not installed", "Install dependency first:\npython -m pip install -r requirements.txt")
            return

        try:
            settings = self._settings()
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
        self.auto_poll_var.set(False)
        self._cancel_auto_poll()
        if self.serial_port:
            try:
                self.serial_port.close()
            except Exception:
                pass
        self.connect_button.configure(text="Connect port")
        self.status_var.set("Port disconnected")
        self._append_log("Port disconnected")

    def _send_request(self) -> None:
        if not self.serial_port or not self.serial_port.is_open:
            messagebox.showwarning("Port is not connected", "Connect the COM port first.")
            return

        try:
            function_code = FUNCTIONS[self.function_var.get()]
            settings = self._settings()
            start_address = parse_int(self.start_address_var.get(), self.address_base_var.get())
            quantity = int(self.quantity_var.get())
            request = build_request(settings.slave_addr, function_code, start_address, quantity)
            expected_size = expected_response_size(function_code, quantity)
        except Exception as exc:
            messagebox.showerror("Request settings error", str(exc))
            return

        thread = threading.Thread(target=self._request_worker, args=(request, expected_size), daemon=True)
        thread.start()

    def _request_worker(self, request: bytes, expected_size: int) -> None:
        if not self.worker_lock.acquire(blocking=False):
            self.ui_queue.put(("log", "Request is already running"))
            return

        try:
            assert self.serial_port is not None
            self.serial_port.reset_input_buffer()
            self.serial_port.write(request)
            self.serial_port.flush()
            response = self.serial_port.read(expected_size or 256)

            self.ui_queue.put(("log", f"TX: {format_hex(request)}"))
            if response:
                self.ui_queue.put(("log", f"RX: {format_hex(response)}"))
                self.ui_queue.put(("status", f"Received {len(response)} byte(s)"))
            else:
                self.ui_queue.put(("log", "RX: <timeout/no data>"))
                self.ui_queue.put(("status", "No response"))
        except Exception as exc:
            self.ui_queue.put(("log", f"Error: {exc}"))
            self.ui_queue.put(("status", "Request error"))
        finally:
            self.worker_lock.release()

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

    def _toggle_auto_poll(self) -> None:
        if self.auto_poll_var.get():
            self._schedule_auto_poll(0)
        else:
            self._cancel_auto_poll()

    def _schedule_auto_poll(self, delay_ms: int | None = None) -> None:
        self._cancel_auto_poll()
        try:
            delay = max(50, int(self.scan_rate_var.get())) if delay_ms is None else delay_ms
        except ValueError:
            delay = 1000
        self.auto_poll_after_id = self.after(delay, self._auto_poll_once)

    def _auto_poll_once(self) -> None:
        self.auto_poll_after_id = None
        if self.auto_poll_var.get():
            self._send_request()
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
            else:
                self._append_log(value)
        self.after(100, self._drain_ui_queue)

    def _on_close(self) -> None:
        self._disconnect()
        self.destroy()


if __name__ == "__main__":
    app = ElementCheckerApp()
    app.mainloop()
