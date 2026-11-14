"""BLE UUID constants for the BeechLane RV leveler (derived from pyrvleveler, MIT)."""

# 128-bit service UUID present in advertisements (identifies a leveler).
ADV_SERVICE = "fb860f65-5683-b6b1-e740-faa035d58c29"

# Services; the device exposes the value as the first characteristic of each.
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
TEMPERATURE_SERVICE = "f5d838dc-8849-4bb5-85ae-797bced7e6e3"
LEVELER_SERVICE = "c003c564-6026-4014-b5ca-9e2bd2ed9917"
RENAME_SERVICE = "b5e00092-ffd9-4be4-8846-f60b4f0c21f6"

# Characteristics of LEVELER_SERVICE
DEVICE_ORIENTATION = "40895209-b14b-4d2f-841f-f4e0eaa36254"
SENSITIVITY = "d56674ba-84b3-4d1c-a29a-a71f805fb59a"
VEHICLE_GEOMETRY = "cdd8cd31-b442-4498-8d5e-565be5dd3899"
XYZ_CALIBRATE_OFFSET = "9885ee27-7022-48aa-8736-7c000a72fb54"
XYZ_OUT = "953ea754-b09f-4c05-b9bb-c07bfb352c29"
CALIBRATE = "5104a3ce-5a22-47c5-83d1-35b8e34ebe78"
