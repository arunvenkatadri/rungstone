# Siemens WinCC (TIA Portal Comfort/Advanced/Unified, WinCC V7/V8 Professional)

Read when the HMI is a Siemens panel (Basic/Comfort/Unified panels), WinCC RT Advanced/Unified PC, or WinCC
V7/V8 (classic SCADA).

## Getting exports

| Platform | Tags | Alarms | Screens | Scripts |
|---|---|---|---|---|
| TIA WinCC Comfort/Advanced | HMI tags > Export (xlsx; sheet "Hmi Tags") | Discrete/Analog alarms > Export (xlsx sheets "DiscreteAlarms", "AnalogAlarms") | exported via Openness XML | VBScript in the project (copy text / Openness) |
| TIA WinCC Unified | Tag export (xlsx/Openness) | Alarm export (xlsx/Openness) | Openness or screen export (XML) | JavaScript (`HMIRuntime.Tags("x").Read()`, `.Write(v)`) |
| WinCC V7/V8 | Tag Management export (CSV/xlsx) or WinCC Configuration Studio | exported from Alarm Logging | Graphics Designer (PDL, binary - ask for screenshots + object property lists) | VBS (`HMIRuntime.Tags("x").Write`) and C (`SetTagDWord`, `GetTagBit`) |

`.xlsx` exports are read with openpyxl when it is installed; otherwise ask the user to save each sheet as CSV.
CSV exports are usually `;`-separated.

## HMI tags

Columns that matter: `Name`, `Connection` (the PLC connection), `PLC tag` (symbolic, e.g. `"Tank_DB".Level`),
`Address` (absolute, e.g. `%MW10`, `%DB10.DBW2` - used when "Absolute access"), `DataType`, `Access Method`
(Symbolic/Absolute), `Acquisition cycle`, `Comment`, scaling (linear scaling PLC <-> HMI range).
Internal tags have no connection.

Symbolic access requires the PLC DB to be **optimized** and the member to be HMI accessible/visible.
Absolute access requires a **non-optimized** (standard) DB. A retarget from absolute to symbolic changes both.

## Alarms

- **Discrete alarms** trigger on a **bit of a Word/Int trigger tag** (`Trigger tag` + `Trigger bit`). A BOOL tag
  cannot trigger a discrete alarm directly on Comfort/Advanced: pack alarm bits into a Word in the PLC, or use
  an analog alarm on the BOOL. Bit numbering of Word tags is byte-swapped relative to many other systems
  (bit 0 of the Word is in the high byte when accessed as bytes) - verify.
- **Analog alarms**: trigger tag + limit + limit mode (higher/lower), optional delay and hysteresis.
- Alarm classes: Errors (must acknowledge), Warnings, System, plus project classes. Map priorities onto classes and
  write the mapping into the review queue.
- PLC-based alarms on S7-1500 (ProDiag, Program_Alarm) live in the PLC project.

## Scripts

VBScript (Comfort/Advanced, V7), JavaScript (Unified), C (V7). Read as text. Writes: `SmartTags("x") = v`,
`HMIRuntime.Tags("x").Write v`, `SetTag...`. Reads: `SmartTags("x")`, `.Read`, `GetTag...`.
