# Element TM5104 Modbus Checker

Python/Tkinter utility for Modbus RTU requests and TM5104 telemetry.

The main window contains connection controls, status, and three compact TM5104 telemetry panels. Port settings, shared Modbus settings, and the three device slave addresses are configured from the Settings window.

Logs are shown in a separate Logs window.

The main window also has an Engineering menu. It contains 48 channel buttons grouped by Elemer device and an expandable editor for channel activity, naming, polling, zone, limit setpoints, calibration, and calculation settings.
Engineering channel parameters are saved to `channel_settings.json` after confirmation.

On startup the app reads `element_checker_settings.xlsx` from the repository folder. Sensor names are taken from `Name`; sensors where `Used` is not `1` are disabled. `Tmin` and `Tmax` define the cell indicator color: blue below minimum, green in range, red above maximum.

Channel limits are stored as separate JSON fields: `tmin`, `tmax`, `twar`, `tcrit`, and `temerg`. They are also written to PostgreSQL columns `limit_tmin`, `limit_tmax`, `limit_twar`, `limit_tcrit`, and `limit_temerg` with every measurement row.

Each measurement row also stores `color_level`:

- `-1`: sensor error, gray
- `0`: below `limit_tmin`, blue
- `1`: `limit_tmin` to `limit_tmax`, green
- `2`: above `limit_tmax`, yellow
- `3`: above `limit_twar`, orange
- `4`: above `limit_tcrit`, purple
- `5`: above `limit_temerg`, red

Empty limit values are ignored when `color_level` is calculated.

Automatic temperature polling is configured in the main window. During automatic polling each active sensor is polled according to its channel polling period from the Engineering menu.

During polling, every sensor response is also saved to PostgreSQL/TimescaleDB table `sensor_measurements`.
The app reads temperature and measurement error from `0520 + 4 * (channel - 1)` and sensor type from `0860 + channel - 1`.

Default database settings:

- database: `elemer_tvi`
- host: `localhost`
- port: `5432`
- user: `postgres`
- password: `pass`

Override them with environment variables:

```powershell
$env:ELEMER_DB_NAME="elemer_tvi"
$env:ELEMER_DB_HOST="localhost"
$env:ELEMER_DB_PORT="5432"
$env:ELEMER_DB_USER="postgres"
$env:ELEMER_DB_PASSWORD="pass"
```

The app also reads the same settings from local `.env` in the application folder. Keep real passwords in `.env`; use `.env.example` as a template.

## Run

```powershell
cd C:\Nikitin\Soft\Elemer_checker
python -m pip install -r requirements.txt
python element_checker.py
```

Each device panel has 16 telemetry buttons. Buttons read sensor channels from holding registers:

- Sensor 1: `0520`, quantity `4`
- Sensor 2: `0524`, quantity `4`
- ...
- Sensor 16: `055C`, quantity `4`

Each response contains big-endian IEEE 754 `float` temperature, measurement error code, and timer code.
