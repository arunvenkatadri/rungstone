# FactoryTalk View (ME / SE) and PanelView Plus

Read when the HMI is Rockwell FactoryTalk View Machine Edition (PanelView Plus,
runtime .MER) or Site Edition (distributed SCADA).

## Getting exports

| Have | Ask for |
|---|---|
| `.MER` runtime file | The development application (`.APA` backup / `.MED` project). `.MER` is compiled; it may be convertible with the Application Manager only if it was created with conversion allowed. Never assume. |
| Displays | Graphics > Displays > right-click > Export (Graphics Import Export Wizard) -> one XML per display. |
| Tags | Tools > Tag Import and Export Wizard -> CSV (ME HMI tags, SE HMI tag database). |
| Alarms | ME: Alarm Setup export (XML). SE with FactoryTalk Alarms and Events: alarm server export (XML) or the ALMD/ALMA instructions in the controller (tag-based vs device-based). |
| SE VBA | Display code: VBA editor > export modules (`.bas` / `.cls`) or copy text. |
| Global objects / parameter files | Export the global object display (XML) and the `.par` parameter files. |

## Tag references

- **Direct reference:** `{[Shortcut]TagName}`, program scope `{[Shortcut]Program:MainProgram.Tag}`,
  members `{[Shortcut]Timer1.ACC}`, bits `{[Shortcut]Word.3}`.
  `::` prefix (`{::[Shortcut]Tag}`) appears in SE for the local area; `{/Area::[Shortcut]Tag}` for a named area.
  The **shortcut** is the device (RSLinx Enterprise / FT Linx path). Retargeting may only need the shortcut renamed.
- **HMI tag:** a tag in the HMI tag database whose `Address` is a direct reference (or memory). Screens then
  reference the HMI tag by name (`Fill\ValveOpen`). Rebind the database row, not every screen.
- **Placeholders:** `#1`, `#2`... come from parameter files or global object parameters. They are not tags —
  resolve them from the `.par` file / global object instance, otherwise report "unresolved placeholder".
- **Expressions:** connections can hold expressions (`{[PLC]A} AND NOT {[PLC]B}`, `IF ... THEN ... ELSE`).
  Every embedded reference is read.

## Display XML structure (simplified)

```
<gfx>
  <displaySettings .../>
  <group name=...> ... </group>                     objects may be grouped
  <momentaryPushButton name=...> <states><state stateId caption/></states>
      <connections><connection name="Value" expression="{[PLC]Tag}"/>
                   <connection name="Indicator" .../></connections>
  <numericDisplay> / <numericInput[Enable]> / <multistateIndicator> / <text caption=...>
  <animations><animateColor expression=...> <animateVisibility ...>
  <gotoDisplayButton display="..."/>
</gfx>
```

Object meaning (what an operator action does):

| Object | Writes | Notes |
|---|---|---|
| Momentary push button | 1 while pressed, 0 on release | If comms drop while pressed, the PLC may see a stuck 1. PLC logic should one-shot it. |
| Maintained push button | toggles 0/1 | State persists in the PLC. |
| Latched push button | 1; PLC must reset via handshake | Check the PLC resets it. |
| Interlocked / multistate push button | a fixed value per button/state | Mode selectors. |
| Numeric input (enable / cursor point) | operator value, limits min/max | Check limits vs PLC clamp; check engineering units vs raw. |
| Numeric / string display, multistate indicator, bar, gauge | read | Multistate captions are the operator's meaning of each value. |
| Goto / close display, macros, ME commands (`Set`, `Toggle`, `Display`) | navigation or writes | ME press/release actions can write tags. |

Labels: an indicator's meaning comes from its own caption, its state captions, or the text object next to it
(position-based association - mark it **inferred**).

## Alarms

- ME: digital (bit) and analog (threshold) HMI alarms on HMI tags; message text per trigger value.
- SE / FactoryTalk Alarms & Events: severity **1–1000** (low 1–250, medium 251–500, high 501–750,
  urgent 751–1000), priorities configured in the alarm server; device-based alarms live in the controller
  (ALMD/ALMA) — then the alarm list is in the PLC project, not the HMI export.

## Scripts

SE display code is VBA (`gTagGroup.Item("[PLC]Tag")`, `Tags.Item`, `.Value =` writes, `LogDiagnosticsMessage`).
ME has no scripting; logic lives in macros and the PLC. Read scripts as text; never run them.
