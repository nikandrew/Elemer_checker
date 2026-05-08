# Element TM5104 Modbus Checker

Python/Tkinter utility for Modbus RTU requests and TM5104 telemetry.

The main window contains connection controls, status, and three compact TM5104 telemetry panels. Port settings, shared Modbus settings, and the three device slave addresses are configured from the Settings window.

On startup the app reads `element_checker_settings.xlsx` from the repository folder. Sensor names are taken from `Name`; sensors where `Used` is not `1` are disabled. `Tmin` and `Tmax` define the cell indicator color: blue below minimum, green in range, red above maximum.

Automatic temperature polling is configured in the main window. The polling interval can be set from `100 ms` to `100 s`. During automatic polling valid temperature measurements are written to PostgreSQL/TimescaleDB table `temperature_measurements`.

PostgreSQL DSN and Grafana URL are configured in the Settings window. Defaults can also be supplied through `ELEMER_DB_DSN` and `ELEMER_GRAFANA_URL`.

## Run

```powershell
cd c:\soft\TERMU_sxTelemetry\Element_checker
python -m pip install -r requirements.txt
python element_checker.py
```

Each device panel has 16 telemetry buttons. Buttons read sensor channels from holding registers:

- Sensor 1: `0500`, quantity `2`
- Sensor 2: `0502`, quantity `2`
- ...
- Sensor 16: `051E`, quantity `2`

Each sensor value is decoded as big-endian IEEE 754 `float`.
