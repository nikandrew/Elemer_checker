# Element TM5104 Modbus Checker

Python/Tkinter utility for Modbus RTU requests and TM5104 telemetry.

The main window contains connection controls, status, and three compact TM5104 telemetry panels. Port settings, shared Modbus settings, and the three device slave addresses are configured from the Settings window.

Logs are shown in a separate Logs window. Live temperature plots are shown in a separate Graphs window with six small selectable graphs and one combined graph with selectable sensor series.

The main window also has an Engineering menu. It contains 48 channel buttons grouped by Elemer device and an expandable editor for channel activity, naming, polling, graph, zone, limit, line, and calculation settings.
Engineering channel parameters are saved to `channel_settings.json` after confirmation.

On startup the app reads `element_checker_settings.xlsx` from the repository folder. Sensor names are taken from `Name`; sensors where `Used` is not `1` are disabled. `Tmin` and `Tmax` define the cell indicator color: blue below minimum, green in range, red above maximum.

Automatic temperature polling is configured in the main window. During automatic polling each active sensor is polled according to its channel polling period from the Engineering menu. Valid temperature measurements are buffered and saved as CSV files in `measurements`.

CSV files are written when measurement is stopped or every 10 minutes of automatic operation. File names use `Elemer_<start>_<end>.csv`, where dates are formatted as `HHMM_DDMMYY`. Each CSV row contains one timestamp and columns for all 48 sensors.

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
