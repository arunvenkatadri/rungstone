# Ignition (Inductive Automation)

Read when the SCADA/HMI is Ignition 8.x (Perspective or Vision) or 7.9.

## Getting exports

| Want | Ask for |
|---|---|
| Tags + tag alarms | Tag Browser > select provider/folder > Export > JSON (8.x) or XML (7.9). |
| Perspective screens | Project export (`.zip` of the project) or the `com.inductiveautomation.perspective/views/<View>/view.json` files. |
| Vision windows | Project export; Vision windows are serialized XML inside the project - scanned generically (references found, roles inferred). |
| Scripts | Project library (`script-python/<pkg>/code.py`), gateway event scripts, tag event scripts - all text. |
| Alarm history | Alarm Journal query export (CSV) or the journal database table - see `alarm-management.md`. |

## Tags

- Tag paths: `[provider]Folder/Sub/Tag` (e.g. `[default]Fill/TankFull`). These are **Ignition tags**, not PLC tags.
- OPC tags point at the PLC through `opcServer` + `opcItemPath`:
  - Allen-Bradley Logix driver: `ns=1;s=[DeviceName]TagName`, program scope `ns=1;s=[Dev]Program:Main.Tag`.
  - Siemens drivers: the **Symbolic** driver uses symbolic paths (DB and member names); the legacy S7 drivers use
    absolute addressing (`[Dev]DB10,INT2`, `[Dev]I0.0`). Syntax depends on the driver and its version - confirm
    before import; Rungstone writes the mapping symbol as given and flags it.
  - Modbus driver: `[Dev]HR1` style addresses; zero/one-based offsets per device setting.
- `valueSource`: `opc` (PLC), `memory`, `expression`, `derived`, `reference`, `query`. Only `opc` (and `derived`/
  `reference` resolving to an opc tag) touch the PLC. Memory tags are HMI-internal - list as `not_plc`.
- `readOnly` / security on the tag decides whether screens can write.
- UDT definitions/instances: parameters like `{InstanceName}` appear in OPC paths — resolve from the instance
  parameters or report "unresolved parameter".

Retargeting = rewrite `opcItemPath` (and the device name). Views and scripts that use `[default]...` paths do not
change when the Ignition tag keeps its name.

## Perspective view.json

```
root.children[]: { type: "ia.display.label" | "ia.input.button" | "ia.input.numeric-entry-field" | ...,
                   meta.name, props.text, position,
                   propConfig["props.value"].binding = {type: "tag", config: {tagPath, bidirectional}}
                                                    | {type: "expression", config: {expression: "{[default]A} > 5"}}
                   events.component.onActionPerformed = {type: "script", config: {script: "..."}} | {type: "nav", ...} }
```

A **bidirectional** tag binding on an input component is a write. Button scripts usually write with
`system.tag.writeBlocking([paths], [values])` / `writeAsync`; reads use `readBlocking`.

## Alarms on tags

Modes: Equality, Not Equal, Above/Below Setpoint (AboveValue/BelowValue), Between/Outside, Out of Range,
Bad Quality, Any Change, Bit State, On Condition. Priorities: Diagnostic, Low, Medium, High, Critical (0–4 in the
journal). Useful properties: `setpointA/B`, `deadband`, `timeOnDelaySeconds`, `timeOffDelaySeconds`, `label`,
`displayPath`, `notes`, `ackMode`, shelving.

Journal export columns typically include `eventtime`, `source`, `displaypath`, `priority`, `eventtype`
(0 active, 1 clear, 2 ack), `eventflags`. `alarm_analysis.py` maps the numeric event types.

## Scripts

Jython 2.7. Read as text. Flag side effects that matter for migration and safety review: `system.db.*`
(database writes), `system.net.*`, `system.util.execute` (runs programs), `system.file.write*`,
`system.opc.write*` (direct OPC writes that bypass tags).
