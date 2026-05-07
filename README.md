# Element TM5104 Modbus Checker

Small Python/Tkinter utility for sending Modbus RTU requests to Element TM5104.

## Install

```powershell
cd c:\soft\TERMU_sxTelemetry\Element_checker
python -m pip install -r requirements.txt
```

## Run

```powershell
python element_checker.py
```

Defaults:

- COM port: `COM22`
- Baudrate: `115200`
- Stop bits: `2`
- Slave addr: `2`
- Scan Rate: `1000 ms`
