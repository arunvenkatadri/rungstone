# Rockwell: L5X, neutral text, and RSLogix 500 / PLC-5 addressing

Read this when the source or target is Allen-Bradley: Studio 5000 / RSLogix 5000
(.L5X, .ACD), RSLogix 500 (SLC 500, MicroLogix), or RSLogix 5 (PLC-5).

## Contents
1. Getting the right export
2. L5X structure (what to read, what to strip)
3. Rung neutral text
4. Structured Text, FBD, SFC in L5X
5. Add-On Instructions (AOIs) and UDTs
6. Tags, aliases, scope, and I/O module tags
7. Tasks and execution order
8. RSLogix 500 / PLC-5 data-table addressing
9. Writing an importable L5X (translation target)

---

## 1. Getting the right export

| Have | Ask for |
|---|---|
| `.ACD` (Studio 5000 binary) | File > Save As > `.L5X`. Or right-click a program/routine/AOI > Export for a partial `.L5X`. |
| `.RSS` (RSLogix 500 binary) | File > Save As > `.SLC` (ASCII library) or Report > print ladder + data table + address/symbol database to text. A `.CSV` of the address/symbol/description database is the I/O map. |
| `.RSP` (RSLogix 5) | Save As `.PC5` (ASCII) or a ladder report. |
| Screenshots | Usable, but transcription errors are likely; say so and ask for an export. |

`parse_project.py` refuses binaries and explains how to export.

## 2. L5X structure

```
RSLogix5000Content  (TargetType = Controller | Program | Routine | Rung | AddOnInstructionDefinition | DataType)
└─ Controller  (Use="Target" for a full export, Use="Context" for a partial one; ProcessorType, MajorRev)
   ├─ RedundancyInfo, Security, SafetyInfo        (SafetyInfo with SafetySignatureExists -> GuardLogix)
   ├─ DataTypes/DataType/Members/Member            (UDTs)
   ├─ Modules/Module                               (I/O tree: CatalogNumber, ParentModule, Ports/Port Address = slot)
   │    └─ Communications/Connections/Connection/InputTag|OutputTag/Comments/Comment Operand=".DATA.3"   <- I/O point comments
   ├─ AddOnInstructionDefinitions/AddOnInstructionDefinition
   │    ├─ Parameters/Parameter (Usage = Input | Output | InOut, Required, Visible)
   │    ├─ LocalTags/LocalTag
   │    └─ Routines/Routine (Logic, Prescan, Postscan, EnableInFalse)
   ├─ Tags/Tag                                     (controller scope)
   ├─ Programs/Program (Class="Safety" for GuardLogix safety programs, MainRoutineName)
   │    ├─ Tags/Tag                                (program scope)
   │    └─ Routines/Routine Type = RLL | ST | FBD | SFC
   └─ Tasks/Task (Type = CONTINUOUS | PERIODIC | EVENT, Rate, Priority, Class="Safety")
        └─ ScheduledPrograms/ScheduledProgram
```

**Strip:** every `<Data>`, `<DataValue>`, `<DataValueMember>`, `<Structure>`, `<ArrayMember>`, `<DefaultData>`
and `L5K` CDATA. Tag data is usually 60–90% of an L5X and carries no logic. The parser does
this; never paste raw tag data into the analysis. Exception: if the user asks what a preset or
setpoint *was*, read just that tag's `<Data>` on purpose and say where the value came from.

**Source protection:** a `<Routine>` or AOI containing `<EncodedData>` is source-protected.
You cannot read it. Report it as unreadable; do not guess its behavior from its name.

## 3. Rung neutral text

Each `<Rung>` has `<Text>` (logic) and optional `<Comment>`. Rung `Type` is `N` (normal) or
`I`/`R`/`D` for pending online edits (insert/replace/delete) — mention if present: the export
contains uncommitted edits.

Syntax:
- Instructions in series: `XIC(A)XIO(B)OTE(C);`
- Branches: `[` opens, `,` separates legs, `]` closes: `[XIC(Start) ,XIC(Run) ]XIO(Stop)OTE(Run);`
- `?` is an unset operand (typically TON/CTU preset/accum shown in the timer tag instead).
- Rung ends with `;`. Operands may be tags, members (`T1.DN`), bits (`Word.5`), array
  elements (`Arr[Idx]`), module tags (`Local:1:I.Data.3`), or expressions (`CPT`, `CMP`).

Instruction cheat-sheet (operand index written in brackets):

| Group | Instructions |
|---|---|
| Bit | XIC, XIO (read); OTE [0] destructive; OTL/OTU [0] retentive set/reset; ONS [0 storage]; OSR/OSF [0 storage, 1 output] |
| Timer/counter | TON, TOF, RTO [0] (TIMER: PRE, ACC DINT ms; EN, TT, DN); CTU, CTD [0] (COUNTER: PRE, ACC; CU, CD, DN, OV, UN); RES [0] |
| Compare | EQU NEQ GRT GEQ LES LEQ LIM MEQ CMP (read) |
| Move/math | MOV [1], MVM [2], COP/CPS [1], FLL [1], CLR [0], CPT [0], ADD SUB MUL DIV MOD [2], SQR NEG ABS [1], BTD [2] |
| Program control | JSR (call), SBR/RET (params), JMP/LBL, MCR (zone), TND, AFI, NOP, EVENT, IOT (immediate output) |
| File/sequencer | BSL BSR FFL FFU LFL LFU SQO SQI SQL FAL FSC (CONTROL structure: LEN, POS, DN, EN, ER...) |
| Process | PID (PID structure); SCP [5] scale-with-parameters; ALMD/ALMA alarms |
| Comms/system | MSG [0] (MESSAGE structure); GSV [3] / SSV; motion MSO MSF MAJ MAM MAS MAH MAG MCD ... (axis, motion-control tag [1]) |

AOI calls in a rung look like `MyAOI(InstanceTag, RequiredParam1, RequiredParam2, ...)`:
operand 0 is the backing tag; the rest are the **required** parameters in definition order
(excluding EnableIn/EnableOut). Direction comes from each parameter's `Usage`.

## 4. ST, FBD, SFC in L5X

- **ST:** `<STContent><Line Number="n">` — one CDATA per line. Logix ST is IEC-like but:
  `:=` assignment, `IF..THEN..ELSIF..ELSE..END_IF;`, `CASE..OF..END_CASE;`, `//` and `(* *)`
  comments, timers are called as `TONR(Timer_FBD)` (FBD_TIMER structures, not TIMER), and
  ladder-style instructions can be called as functions (e.g. `MOV(a,b);` is not valid; but `JSR` is).
- **FBD:** `<FBDContent><Sheet>` with `IRef`/`ORef` (tag in/out), `ICon`/`OCon` (sheet
  connectors), `Block Type="TONR" Operand="Tmr1"` and `Wire` elements. The parser lists sheet
  contents and in/out references; wiring order is approximate — say so when it matters.
- **SFC:** `<SFCContent>` with `Step`, `Transition` (condition is embedded ST), `Action`
  (ST bodies, qualifiers N/S/R/P/L/D), `Branch`, `DirectedLink`. Summarize as a step table.

## 5. AOIs and UDTs

AOI parameters are the AOI's interface; `LocalTags` are internal state. An AOI is called per
instance, and each instance's backing tag holds its parameters and locals. When translating,
an AOI becomes a Function Block (Siemens FB with instance DB / IEC FUNCTION_BLOCK). Prescan /
Postscan / EnableInFalse routines have no direct IEC equivalent — flag them.

UDT `Member`s with `Hidden="true"` (e.g. `ZZZZZZZZZZ...`) are bit-host SINTs for BOOL members —
ignore them.

## 6. Tags, aliases, scope, I/O

- `TagType`: `Base`, `Alias` (`AliasFor` points at the real tag, often a module point),
  `Produced`, `Consumed` (inter-controller over EtherNet/IP/ControlNet — see protocols pitfall).
- Scope: controller tags are global; program tags are local to their program (HMI reaches
  them as `Program:ProgramName.Tag`).
- `ExternalAccess` = `Read/Write`, `Read Only`, `None`: tells you whether HMI/SCADA *can*
  touch the tag. `Constant="true"` means it is not writable in logic.
- **Module I/O tag names:** local chassis `Local:<slot>:I.Data.<bit>` / `Local:<slot>:O.Data.<bit>`
  (1756 analog: `Local:3:I.Ch0Data`); remote adapter `<AdapterName>:<slot>:I.Data` or, for
  modules with their own connection, `<ModuleName>:I.Data`. The module's Port `Address` is its
  slot. I/O point comments live on the module connection tags (`Comment Operand=".DATA.3"`).
  This is the "I/O map" source of truth when present.
- `Class="Safety"` on a tag or program = GuardLogix safety. See `safety-exclusion.md`.

## 7. Tasks and execution order

- One optional **continuous** task (runs back-to-back, lowest priority), **periodic** tasks
  (fixed rate, priority 1–15; lower number = higher priority, can interrupt others),
  **event** tasks (triggered by input change, motion group, consumed tag, or EVENT).
- Within a task, programs run in `ScheduledPrograms` order; within a program only
  `MainRoutineName` runs unless other routines are `JSR`-called; rungs run top to bottom.
- **I/O is asynchronous in ControlLogix/CompactLogix:** input tags update at the module RPI,
  independent of the scan. An input can change between two rungs of the same scan. SLC/PLC-5
  and S7 use an input/output image updated between scans. This matters when translating —
  see `translation-pitfalls.md` § Scan order.
- Prescan (on transition to Run): OTE outputs are cleared, timers reset, ONS storage set.
  Tag values are otherwise retained through power cycles (Logix memory is retentive).

## 8. RSLogix 500 / PLC-5 addressing

| File | Type | Example | Notes |
|---|---|---|---|
| O0 | Output image | `O:2/15`, `O:4.0` | `O:slot.word/bit`; word 0 may be omitted for bits (`O:2/15` = `O:2.0/15`). |
| I1 | Input image | `I:1/14`, `I:3.0` | Analog channels are words: `I:3.0` = slot 3 channel 0 on a 1746-NI4. |
| S2 | Status | `S:1/15` first-scan bit, `S:4` free-running clock, `S:5/0` overflow trap, `S:13/S:14` math register | No direct equivalents in IEC; translate intent. |
| B3 | Bit | `B3:0/5`, `B3/21` (= `B3:1/5`) | Word/bit or flat bit numbering. |
| T4 | Timer | `T4:0.ACC`, `T4:0/DN` | ACC/PRE are 16-bit INT in **time-base** units (1.0 s or 0.01 s; 0.001 on MicroLogix). |
| C5 | Counter | `C5:0.ACC`, `C5:0/DN`, `/OV`, `/UN` | 16-bit, -32768..32767. |
| R6 | Control | `R6:0.POS`, `.LEN`, `/DN`, `/EN` | For FIFO/LIFO/bit-shift/sequencer/file instructions. |
| N7 | Integer | `N7:0` | 16-bit signed. `#N7:0` is a file (array) reference; `N7:[N7:1]` indirect. |
| F8 | Float | `F8:0` | 32-bit REAL. |
| ST, A, L, MG, PD | String, ASCII, Long (MicroLogix), Message, PID | | |

**RSLogix 500 ASCII rung format:** `SOR <instr> <operands...> ... EOR`, branches `BST`
(start) / `NXB` (next leg) / `BND` (end). Operands are space-separated; timers are
`TON T4:0 1.0 100 0` (timer, time base, preset, accum). Example:
`SOR BST XIC I:1/14 NXB XIC O:2/15 BND XIC I:1/15 OTE O:2/15 EOR` = start-or-sealed, AND stop (normally closed), drives master coil.

**SLC analog raw ranges (1746-NI4 / NO4I, typical defaults — confirm module and jumpers):**
0–10 V → 0..16383; 4–20 mA → 3277..16384 (≈ 819.2 counts/mA); 0–20 mA → 0..16383 (NO4I output:
0..32764 for 0–21 mA). Newer 1746-NI8/NI16 are configurable. Treat every scaling constant as
"needs confirmation" unless the module catalog number and range are stated.

## 9. Writing an importable L5X (when Rockwell is the target)

Emit a **partial export** the engineer imports with right-click > Import Routine/Program in
Studio 5000 (never a full controller file, never anything for direct download):

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!-- GENERATED BY Rungstone - DRAFT FOR REVIEW. Not approved. Import into Studio 5000, review, verify offline. Not for direct download to a running controller. -->
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="33.00" TargetName="TwoTank" TargetType="Program"
    ContainsContext="true" ExportOptions="References NoRawData L5KData DecoratedData Context Dependencies ForceProtectedEncoding AllProjDocTrans">
<Controller Use="Context" Name="Translated">
<DataTypes Use="Context"/>
<Tags Use="Context">
  <Tag Name="Tank1_Level_Raw" TagType="Base" DataType="INT" Radix="Decimal" Constant="false" ExternalAccess="Read/Write">
    <Description><![CDATA[Tank 1 level, raw (was I:3.0). INFERRED - confirm]]></Description>
  </Tag>
</Tags>
<Programs Use="Context">
<Program Use="Target" Name="TwoTank" TestEdits="false" MainRoutineName="Main" Disabled="false" UseAsFolder="false">
<Tags/>
<Routines>
<Routine Name="Main" Type="RLL">
<RLLContent>
<Rung Number="0" Type="N">
<Comment><![CDATA[Master start/stop seal-in (was rung 0)]]></Comment>
<Text><![CDATA[[XIC(Start_PB) ,XIC(Master_Run) ]XIC(Stop_PB)OTE(Master_Run);]]></Text>
</Rung>
</RLLContent>
</Routine>
</Routines>
</Program>
</Programs>
</Controller>
</RSLogix5000Content>
```

Rules: declare every tag used (Context tags) with descriptions carrying the old address; use
DINT unless 16-bit is required for comms; keep one rung per source rung where possible and
put the source rung number in the comment so the mapping is traceable. Do not invent module
definitions — list required I/O modules in the review queue instead.
