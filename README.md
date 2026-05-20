# Element TM5104 Modbus Checker

Python/Tkinter utility for Modbus RTU requests and TM5104 telemetry.

The main window contains connection controls, status, and three compact TM5104 telemetry panels. Port settings, shared Modbus settings, and the three device slave addresses are configured from the Settings window.

Logs are shown in a separate Logs window.

The Settings window also contains manual Elemer baudrate controls. The app reads device register `0409` with function `03` to check the current speed code and writes the same register with function `06` to set a new speed. Supported device speed codes are `4` = 2400, `5` = 4800, `6` = 9600, `7` = 19200, `8` = 38400, `9` = 57600, `10` = 115200 bit/s.

The main window also has an Engineering menu. It contains 48 channel buttons grouped by Elemer device and an expandable editor for channel activity, naming, sensor type, limit setpoints, and calibration settings.
Engineering channel parameters are saved to `channel_settings.json` after confirmation.

On startup the app reads `element_checker_settings.xlsx` from the repository folder. Sensor names are taken from `Name`; sensors where `Used` is not `1` are disabled. `Tmin` and `Tmax` define the cell indicator color: blue below minimum, green in range, red above maximum.

For temperature sensors, the stored value is corrected with `T = a * raw + b`. For heat flux sensors, the stored value is converted to W/m² with the Stefan-Boltzmann formula `q = epsilon * sigma * (raw + 273.15)^4`, where `sigma = 5.670374419e-8`.
The original decoded device value before conversion is stored in `raw_temperature`.

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

Each measurement row stores `sensor_kind_code` from the Engineering channel type: `1` for a temperature sensor, `2` for a heat flux sensor.
It also stores the used conversion settings in `calibration_a`, `calibration_b`, and `emissivity`; unused settings are stored as `NULL`.

Automatic temperature polling is configured in the main window. During automatic polling each active sensor is scheduled for polling twice per second. The app reads each TM5104 telemetry block in one Modbus request, removes artificial inter-sensor delays, and polls as fast as the Modbus line allows.

During polling, every sensor response is immediately saved to PostgreSQL/TimescaleDB table `sensor_measurements`.
The app reads temperature and measurement error from `0520 + 4 * (channel - 1)` and sensor type from `0860 + channel - 1`.

When automatic measurement is stopped with `Stop measurement`, the app exports all database rows collected during that measurement session to `measurements/*.xlsx` and shows a save confirmation. The main window also has an Excel export panel for the last hour, last 6 hours, today, or a custom `dd.mm.yy HH:MM` period.

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
