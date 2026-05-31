from __future__ import annotations

import random
import struct
import threading
import time
from datetime import datetime, timedelta, timezone

import tkinter as tk

from element_checker import (
    AUTO_SENSOR_POLL_PERIOD_S,
    DEVICE_COUNT,
    TELEMETRY_CHANNELS,
    ElementCheckerApp,
    PortSettings,
    build_read_response_frame,
)


MODELER_MIN_VALUE = 5.0
MODELER_MAX_VALUE = 35.0
MODELER_MAX_STEP = 0.5
MODELER_SENSOR_TYPE_CODE = 0


class ModelerSerialPort:
    def __init__(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False


class ElementCheckerModelerApp(ElementCheckerApp):
    def _build_ui(self) -> None:
        super()._build_ui()

        for child in self.grid_slaves():
            row = int(child.grid_info().get("row", 0))
            child.grid_configure(row=row + 1)

        banner = tk.Label(
            self,
            text="ЭМУЛЯТОР! ДАННЫЕ СЛУЧАЙНЫЕ, ДАТЧИКИ И COM-ПОРТ НЕ ОПРАШИВАЮТСЯ",
            bg="#b00020",
            fg="white",
            font=("", 18, "bold"),
            padx=16,
            pady=14,
        )
        banner.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")

    def __init__(self) -> None:
        super().__init__()
        self.modeler_values: list[list[float | None]] = [
            [None for _channel in range(TELEMETRY_CHANNELS)]
            for _device in range(DEVICE_COUNT)
        ]
        self.title("Element TM5104 Modbus Checker - ЭМУЛЯТОР")
        self.serial_port = ModelerSerialPort()
        self.connect_button.configure(text="Эмулятор включен")
        self.status_var.set("Эмулятор: моделируются только активные датчики, сырая температура 5..35 C")
        self.auto_poll_status_var.set("Эмулятор готов")
        self._append_log("ЭМУЛЯТОР: COM-порт не используется, моделируются только активные датчики из настроек.")
        self.after(500, self._start_auto_poll)

    def _ensure_connected(self) -> bool:
        if self.serial_port is None or not self.serial_port.is_open:
            self.serial_port = ModelerSerialPort()
        return True

    def _connect(self) -> None:
        self.serial_port = ModelerSerialPort()
        self.connect_button.configure(text="Эмулятор включен")
        self.status_var.set("Эмулятор подключен")
        self._append_log("Эмулятор подключен без COM-порта")

    def _disconnect(self) -> None:
        if self.serial_port is not None:
            self.serial_port.close()
        self.serial_port = None
        if hasattr(self, "connect_button"):
            self.connect_button.configure(text="Включить эмулятор")

    def _toggle_port(self) -> None:
        if self.serial_port is not None and self.serial_port.is_open:
            self._disconnect()
            self.status_var.set("Эмулятор выключен")
        else:
            self._connect()

    def _settings(self, device_index: int | None = None) -> PortSettings:
        device = 0 if device_index is None else device_index
        return PortSettings(
            port="MODELER",
            baudrate=115200,
            stopbits=2,
            slave_addr=device + 2,
            scan_rate_ms=1000,
        )

    def _open_settings(self) -> None:
        self.status_var.set("Эмулятор: настройки COM-порта не используются")
        self._append_log("Эмулятор: окно настроек COM-порта отключено")

    def _send_manual_request(self) -> None:
        self.status_var.set("Эмулятор: ручной Modbus-запрос не отправляется")
        self._append_log("Эмулятор: ручной Modbus-запрос пропущен")

    def _start_auto_poll(self) -> None:
        if self.serial_port is None or not self.serial_port.is_open:
            self.serial_port = ModelerSerialPort()

        self.auto_poll_var.set(True)
        started_at = datetime.now(timezone.utc)
        self.measurement_started_at = started_at
        self.measurement_export_from = started_at
        self.next_hourly_export_at = started_at + timedelta(hours=1)
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
        self.auto_poll_status_var.set("Эмулятор пишет данные")
        self._append_log("Эмулятор: автоматическая запись случайных данных запущена")
        self._schedule_auto_poll(0)

    def _request_temperature(self, device_index: int, channel: int) -> None:
        if not 1 <= channel <= TELEMETRY_CHANNELS:
            return
        if not self.sensor_settings[device_index][channel - 1].used:
            return
        self._set_temperature_label(device_index, channel, "reading")
        self._handle_emulated_measurement(device_index, channel, self._settings(device_index).slave_addr)

    def _request_all_temperatures(self, auto: bool = False) -> bool:
        self._ensure_connected()
        if self.temperature_poll_running:
            if not auto:
                self.status_var.set("Эмулятор: опрос уже выполняется")
            return False

        self.temperature_poll_running = True

        def worker() -> None:
            try:
                for device_index in range(DEVICE_COUNT):
                    settings = self._settings(device_index)
                    channels = [
                        channel
                        for channel in range(1, TELEMETRY_CHANNELS + 1)
                        if self.sensor_settings[device_index][channel - 1].used
                    ]
                    self._poll_channels_block_for_worker(
                        settings,
                        device_index,
                        channels,
                    )
            finally:
                self.ui_queue.put(("poll_done", "auto" if auto else "manual"))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _request_due_temperatures(self) -> bool:
        self._ensure_connected()
        if self.temperature_poll_running:
            return False

        now = time.monotonic()
        due_channels: list[tuple[int, int]] = []
        for device_index in range(DEVICE_COUNT):
            for channel in range(1, TELEMETRY_CHANNELS + 1):
                if not self.sensor_settings[device_index][channel - 1].used:
                    self.auto_poll_next_due.pop((device_index, channel), None)
                    continue
                key = (device_index, channel)
                if key not in self.auto_poll_next_due:
                    self.auto_poll_next_due[key] = now
                if now >= self.auto_poll_next_due[key]:
                    due_channels.append(key)
                    self.auto_poll_next_due[key] = now + AUTO_SENSOR_POLL_PERIOD_S

        if not due_channels:
            return False

        self.temperature_poll_running = True

        def worker() -> None:
            try:
                channels_by_device: dict[int, list[int]] = {}
                for device_index, channel in due_channels:
                    channels_by_device.setdefault(device_index, []).append(channel)
                for device_index, channels in channels_by_device.items():
                    self._poll_channels_block_for_worker(self._settings(device_index), device_index, channels)
            finally:
                self.ui_queue.put(("poll_done", "auto"))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _poll_channels_block_for_worker(self, settings: PortSettings, device_index: int, channels: list[int]) -> None:
        for channel in channels:
            self.ui_queue.put(("temp", f"{device_index}|{channel}|reading"))
            response = self._emulated_response(settings.slave_addr, device_index, channel)
            self.ui_queue.put(
                (
                    "telemetry",
                    f"{device_index}|{channel}|{settings.slave_addr}|{MODELER_SENSOR_TYPE_CODE}|{response.hex()}",
                )
            )

    def _read_sensor_types_block_for_worker(self, settings: PortSettings, device_index: int) -> list[int | None]:
        values: list[int | None] = [MODELER_SENSOR_TYPE_CODE for _ in range(TELEMETRY_CHANNELS)]
        self.sensor_type_cache[device_index] = values
        return values

    def _read_sensor_type_for_worker(
        self,
        settings: PortSettings,
        device_index: int,
        channel: int,
        log_transaction: bool = True,
    ) -> int:
        self.sensor_type_cache[device_index][channel - 1] = MODELER_SENSOR_TYPE_CODE
        return MODELER_SENSOR_TYPE_CODE

    def _handle_emulated_measurement(self, device_index: int, channel: int, slave_addr: int) -> None:
        response = self._emulated_response(slave_addr, device_index, channel)
        self._handle_temperature_response(device_index, channel, response, slave_addr, MODELER_SENSOR_TYPE_CODE)

    def _emulated_response(self, slave_addr: int, device_index: int, channel: int) -> bytes:
        previous = self.modeler_values[device_index][channel - 1]
        if previous is None:
            raw_temperature = random.uniform(MODELER_MIN_VALUE, MODELER_MAX_VALUE)
        else:
            step = random.uniform(-MODELER_MAX_STEP, MODELER_MAX_STEP)
            raw_temperature = min(MODELER_MAX_VALUE, max(MODELER_MIN_VALUE, previous + step))
        self.modeler_values[device_index][channel - 1] = raw_temperature
        payload = (
            struct.pack(">f", raw_temperature)
            + (0).to_bytes(2, byteorder="big", signed=False)
            + random.randint(0, 65535).to_bytes(2, byteorder="big", signed=False)
        )
        return build_read_response_frame(slave_addr, 0x03, payload)

if __name__ == "__main__":
    app = ElementCheckerModelerApp()
    app.mainloop()
