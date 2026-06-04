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
    SENSOR_KIND_HEAT_FLUX,
    STEFAN_BOLTZMANN,
    TELEMETRY_CHANNELS,
    ElementCheckerApp,
    PortSettings,
    apply_sensor_conversion,
    build_read_response_frame,
    sensor_kind_code,
)


MODELER_GREEN_JITTER = 0.3
MODELER_MAX_STEP = 1.0
MODELER_EXCURSION_CHANCE = 0.02
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
        self.modeler_excursions: list[list[float | None]] = [
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

    def _green_range(self, device_index: int, channel: int) -> tuple[float, float]:
        sensor = self.sensor_settings[device_index][channel - 1]
        if sensor.tmin is not None and sensor.tmax is not None and sensor.tmin < sensor.tmax:
            return sensor.tmin, sensor.tmax
        if sensor.tmin is not None:
            return sensor.tmin, sensor.tmin + 2.0
        if sensor.tmax is not None:
            return sensor.tmax - 2.0, sensor.tmax
        return 20.0, 22.0

    def _high_excursion_target(self, device_index: int, channel: int) -> float:
        sensor = self.sensor_settings[device_index][channel - 1]
        green_min, green_max = self._green_range(device_index, channel)
        upper_limits = [
            value
            for value in (sensor.tmax, sensor.twar, sensor.tcrit, sensor.temerg)
            if value is not None
        ]
        highest_limit = max(upper_limits, default=green_max)
        return highest_limit + 1.0

    def _low_excursion_target(self, device_index: int, channel: int) -> float:
        sensor = self.sensor_settings[device_index][channel - 1]
        green_min, _green_max = self._green_range(device_index, channel)
        lower_limit = sensor.tmin if sensor.tmin is not None else green_min
        return lower_limit - 1.0

    def _measurement_from_raw(self, device_index: int, channel: int, raw_temperature: float | None) -> float | None:
        if raw_temperature is None:
            return None
        sensor = self.sensor_settings[device_index][channel - 1]
        try:
            return apply_sensor_conversion(raw_temperature, sensor)
        except Exception:
            return None

    def _raw_from_measurement(self, device_index: int, channel: int, measurement: float) -> float:
        sensor = self.sensor_settings[device_index][channel - 1]
        if sensor_kind_code(sensor.sensor_type) == SENSOR_KIND_HEAT_FLUX:
            emissivity = sensor.emissivity if sensor.emissivity > 0 else 1.0
            kelvin = (measurement / (emissivity * STEFAN_BOLTZMANN)) ** 0.25
            return kelvin - 273.15
        if sensor.calibration_a == 0:
            return measurement
        return (measurement - sensor.calibration_b) / sensor.calibration_a

    @staticmethod
    def _move_toward(current: float, target: float) -> float:
        if abs(target - current) <= MODELER_MAX_STEP:
            return target
        step = random.uniform(0.2, MODELER_MAX_STEP)
        if target < current:
            step = -step
        return current + step

    def _next_modeled_measurement(self, device_index: int, channel: int) -> float:
        green_min, green_max = self._green_range(device_index, channel)
        previous_raw = self.modeler_values[device_index][channel - 1]
        previous = self._measurement_from_raw(device_index, channel, previous_raw)
        if previous is None:
            return random.uniform(green_min, green_max)

        target = self.modeler_excursions[device_index][channel - 1]
        if target is None and random.random() < MODELER_EXCURSION_CHANCE:
            if random.random() < 0.8:
                target = self._high_excursion_target(device_index, channel)
            else:
                target = self._low_excursion_target(device_index, channel)
            self.modeler_excursions[device_index][channel - 1] = target

        if target is not None:
            next_value = self._move_toward(previous, target)
            if next_value == target:
                if green_min <= target <= green_max:
                    self.modeler_excursions[device_index][channel - 1] = None
                else:
                    self.modeler_excursions[device_index][channel - 1] = random.uniform(green_min, green_max)
            return next_value

        if green_min <= previous <= green_max:
            return min(green_max, max(green_min, previous + random.uniform(-MODELER_GREEN_JITTER, MODELER_GREEN_JITTER)))
        return self._move_toward(previous, random.uniform(green_min, green_max))

    def _emulated_response(self, slave_addr: int, device_index: int, channel: int) -> bytes:
        measurement = self._next_modeled_measurement(device_index, channel)
        raw_temperature = self._raw_from_measurement(device_index, channel, measurement)
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
