# Siemens: SimaticML, SCL source, S7-1500 conventions, TIA Portal import

Read when the source or target is Siemens (TIA Portal, S7-1200/1500, S7-300/400 migrated
into TIA). Version-specific details (namespaces, attribute names) change between TIA
versions; when something here disagrees with the file in front of you, trust the file.

## Contents
1. Getting the right export
2. SimaticML structure
3. SCL source files
4. S7-1500 conventions for generated code
5. Data types and addressing
6. Timers, counters, edges
7. Analog values
8. Organization blocks, startup, faults
9. Writing SCL for TIA Portal import (translation target)
10. Safety (F-) program

---

## 1. Getting the right export

| Want | How the engineer gets it |
|---|---|
| SCL blocks as text | Program blocks > select > right-click **Generate source from blocks** -> `.scl` (exact text, best input) |
| LAD/FBD/STL, DBs, tag tables, UDTs | TIA Openness export or Version Control Interface -> SimaticML `.xml` (one file per object) |
| PLC tag table | PLC tags > tag table > Export -> `.xlsx` (convert to CSV or read with openpyxl) or SimaticML |
| S7-300/400 classic (STEP 7 v5.x) | `.awl` (STL source) / `.scl` source export; symbol table `.sdf`/`.asc` |
| `.ap17`…`.ap20` / `.zap` archives | Binary project — ask for the exports above |

## 2. SimaticML structure

```
Document
├─ Engineering version="V17"
├─ DocumentInfo
└─ SW.Blocks.FB | SW.Blocks.FC | SW.Blocks.OB | SW.Blocks.GlobalDB | SW.Blocks.InstanceDB
   | SW.Types.PlcStruct | SW.Tags.PlcTagTable
   ├─ AttributeList
   │   ├─ Name, Number, ProgrammingLanguage (LAD | FBD | SCL | STL | GRAPH | F_LAD | F_FBD ...)
   │   ├─ MemoryLayout (Optimized | Standard), IsRetain? , HeaderAuthor/Version
   │   └─ Interface/Sections (ns .../SW/Interface/v3|v4|v5)
   │        └─ Section Name=Input|Output|InOut|Static|Temp|Constant|Return|None
   │             └─ Member Name Datatype Remanence? Accessibility?  (nested Member/Sections for Structs)
   │                  ├─ Comment/MultiLanguageText Lang="en-US"
   │                  └─ StartValue          <- data, strip
   └─ ObjectList
       ├─ MultilingualText CompositionName="Comment"|"Title"   (block comment/title)
       └─ SW.Blocks.CompileUnit   (one per network)
           ├─ AttributeList/NetworkSource
           │    ├─ StructuredText (SCL tokens: Token, Blank, NewLine, LineComment, Access ...)
           │    ├─ FlgNet (LAD/FBD: Parts {Access, Part, Call} + Wires {NameCon, IdentCon, Powerrail})
           │    └─ StatementList (STL)
           └─ ObjectList/MultilingualText Title/Comment (network title/comment)
```

`Access Scope=` `LocalVariable` (render `#name`), `GlobalVariable` (`"name".member`),
`LiteralConstant`, `TypedConstant`, `LocalConstant`, `GlobalConstant`, `Call`.
PLC tag tables: `SW.Tags.PlcTagTable/ObjectList/SW.Tags.PlcTag` with `Name`, `DataTypeName`,
`LogicalAddress` (`%I0.0`, `%IW64`, `%Q4.1`, `%MW10`) and a `Comment` — this is the I/O map.

The parser reconstructs SCL text from tokens and summarizes LAD/FBD networks as
`Part(pin=operand) -> Part(...)` in wire order. The summary loses exact series/parallel
topology; when it matters (a translated rung looks wrong), ask for a screenshot of that
network or the `.scl`/STL source.

## 3. SCL source files

```
FUNCTION_BLOCK "FB_Tank"
{ S7_Optimized_Access := 'TRUE' }
VERSION : 0.1
   VAR_INPUT
      Start : Bool;   // start command
   END_VAR
   VAR_OUTPUT  Run : Bool; END_VAR
   VAR
      tonDelay {InstructionName := 'TON_TIME'; LibVersion := '1.0'} : TON_TIME;
   END_VAR
   VAR_TEMP  tmp : Int; END_VAR
BEGIN
   #tonDelay(IN := #Start, PT := T#5S);
   #Run := #tonDelay.Q;
END_FUNCTION_BLOCK
```

Blocks: `FUNCTION_BLOCK`, `FUNCTION "x" : Void|Int|...`, `ORGANIZATION_BLOCK`, `DATA_BLOCK`
(`"FB" ` for instance DBs, `NON_RETAIN`/`RETAIN` keywords), `TYPE ... STRUCT ... END_STRUCT END_TYPE`.
`#` marks locals, `"..."` marks global symbols. `REGION name ... END_REGION` groups code.

## 4. S7-1500 conventions for generated code

- **Optimized block access** (`S7_Optimized_Access := 'TRUE'`) for all new blocks; no absolute
  offsets into DBs (`DB1.DBW2`), use symbols.
- One FB per machine function / former program or routine; instance as a multi-instance in
  a parent FB's `Static` section or a single instance DB. OB1 calls the top FBs in the same
  order the source executed its programs/routines.
- Field I/O stays in the **PLC tag table** with `%I`/`%Q` addresses; FBs receive I/O through
  `VAR_INPUT`/`VAR_OUTPUT` or read global symbols — pick one and keep it consistent. For
  translation, global symbols with the old address in the comment make the mapping table
  easiest to check.
- Naming: PascalCase or the plant's standard; never reuse the old address as a symbol name
  (`I_1_14`) unless the engineer asks — meaning first (`Start_PB`), old address in the comment.
- Comments on every block, region, and declaration: source location (`was rung 3`, `was I:1/14`).
- Constants in `VAR CONSTANT` instead of magic numbers.

## 5. Data types and addressing

| S7 type | Size | Notes vs Rockwell |
|---|---|---|
| Bool | bit | |
| Byte/Word/DWord/LWord | 8/16/32/64 | bit strings, no sign |
| SInt/Int/DInt/LInt | 8/16/32/64 signed | Logix SINT/INT/DINT/LINT; SLC N file = Int |
| USInt/UInt/UDInt/ULInt | unsigned | no Logix equivalent for most |
| Real/LReal | 32/64 | Logix REAL/LREAL; SLC F file = Real |
| Time / LTime | 32-bit ms / 64-bit ns | Logix uses DINT ms |
| S5Time | legacy BCD time | appears in migrated S7-300 code |
| String[n] (max 254) / WString | | Logix STRING is 82 chars + LEN |
| Array[0..n] of T, Struct, UDT ("PLC data type") | | Logix arrays are 0-based only |

**Byte order:** S7 is big-endian in the process image and memory areas: `%MW0` = `%MB0`
(high byte) + `%MB1` (low byte), so `%M0.0` is bit 0 of the *high* byte of `%MW0`. Bit-level
overlays of words translated from Rockwell (little-endian bit numbering of DINT `.0..31`) get
this wrong easily — flag every `AT` overlay or `%M` bit/word mix as high risk.

Peripheral (immediate) access: `%IW64:P` / `%QW64:P` reads/writes the module directly,
bypassing the process image — the S7 analogue of `IIN`/`IOT` / Logix immediate.

## 6. Timers, counters, edges

- IEC timers on S7-1500: `TON`, `TOF`, `TP`, `TONR` (retentive) with `IN`, `PT` (Time), `Q`, `ET`
  (Time). Instance types `IEC_TIMER` / `TON_TIME` / `TOF_TIME` (multi-instance). `ET` stops at
  `PT`; `ET` resets when `IN` goes false (TON). A timer instance that is not called does not
  update. **Call timers unconditionally** (outside IF) and drive `IN` with the condition.
- Counters: `CTU`, `CTD`, `CTUD` (`CU`, `CD`, `R`, `LD`, `PV`, `Q`/`QU`/`QD`, `CV`). `CV` is Int
  for `CTU` unless the instance is `CTU_DINT`/`CTU_UDINT`. IEC counters do not wrap — they stop
  at the type limit. No `.OV/.UN` bits.
- Edges: `R_TRIG`/`F_TRIG` instances (`CLK`, `Q`) or `P_TRIG`/`N_TRIG`/`P`/`N` LAD contacts with
  an edge memory bit. One instance per edge, called every scan.
- Legacy `S_ODT`/`S5Time` timers in migrated S7-300 code: keep them only if the engineer asks.

## 7. Analog values (S7-1500 standard analog modules)

Nominal range raw value: **0..27648** unipolar (0–10 V, 0–20 mA, 4–20 mA all map to 0..27648 at
the module), **-27648..27648** bipolar. Overrange up to 32511, overflow 32767 (0x7FFF), underflow
-32768 (0x8000) — used for wire-break/underrange diagnostics on 4–20 mA. Scaling:

```
#Level_pct := SCALE_X(MIN := 0.0, VALUE := NORM_X(MIN := 0, VALUE := "AI_Tank1_Level", MAX := 27648), MAX := 100.0);
```

Every translation of analog scaling goes into the review queue with the source raw range,
the target raw range, the engineering range, and whether the module is the same type.
Never assume the target module range; state it as inferred and ask.

## 8. Organization blocks, startup, faults

| OB | Purpose | Rockwell analogue |
|---|---|---|
| OB1 | Main cycle | continuous task |
| OB30–OB38 | Cyclic interrupt (fixed rate; PID_Compact belongs here) | periodic task |
| OB40 | Hardware interrupt | event task |
| OB100 | Startup (warm restart) | first-scan bit `S:FS` / `S:1/15`, power-up handler |
| OB80 | Time error (cycle overrun) | watchdog major fault |
| OB82 | Diagnostic interrupt (module diagnostics, wire break) | module fault / I/O fault handling |
| OB83 / OB86 | Pull/plug, rack/station failure | connection fault, `GSV` module status |
| OB121 / OB122 | Programming / I/O access error | major fault routine (array index, etc.) |

Fault reactions differ by CPU family and firmware (whether a missing error OB stops the CPU,
whether a block with local error handling via `GET_ERR_ID` continues). **Do not state a fault
reaction as fact** — list the source behavior, the target design you propose, and put the
item in the review queue for the engineer to confirm on their CPU/firmware.

Retentivity: memory is **non-retentive by default**. Set retentive per member in optimized DBs
(`Retain` checkbox / `RETAIN` in source), in instance DBs via the FB interface, and for `%M`
in the CPU's retentive memory range. Rockwell tags are retentive; translate every latch, counter,
accumulator, sequence step and setpoint with an explicit retain decision.

## 9. Writing SCL for TIA Portal import

Produce `.scl` source files the engineer adds via **External source files > Add new external
file**, then **Generate blocks from source**. Rules that keep import clean:

- One file may contain several blocks; declare called blocks/UDTs **before** callers, UDTs first.
- Every global symbol used must exist in the tag table or a DB — also deliver the tag table as
  CSV/XLSX-ready rows (Name, Path, Data Type, Logical Address, Comment, Retain) and any global DBs
  as `DATA_BLOCK` source.
- Header comment on every file:
  ```
  // GENERATED BY Rungstone - DRAFT FOR ENGINEER REVIEW - NOT APPROVED
  // Source: two_tank_slc.txt (SLC 5/04, file 2). Review queue: R1-R7 open.
  // Import into TIA Portal as an external source, compile, and review offline.
  // Not for direct download to a running controller.
  ```
- Use `REGION` per source routine or rung group, with `// was rung N` comments.
- Instance timers/counters as `Static` multi-instances; call them unconditionally.
- No absolute DB addressing, no pointers/ANY unless the source used indirection (then flag).

Template:

```
// GENERATED BY Rungstone - DRAFT FOR ENGINEER REVIEW - NOT APPROVED
FUNCTION_BLOCK "FB_TwoTank"
{ S7_Optimized_Access := 'TRUE' }
VERSION : 0.1
   VAR_INPUT
      Start_PB : Bool;    // was I:1/14 (NO pushbutton) - from comment
      Stop_PB  : Bool;    // was I:1/15 (normally-closed pushbutton, TRUE = not pressed) - inferred from XIC use
   END_VAR
   VAR_OUTPUT
      Master_Run : Bool;  // was O:2/15
   END_VAR
   VAR RETAIN
      LowCutoff : Int;    // was N7:1 - set by HMI? (open question Q2)
   END_VAR
BEGIN
   REGION Master start/stop (was rung 0)
      #Master_Run := (#Start_PB OR #Master_Run) AND #Stop_PB;
   END_REGION
END_FUNCTION_BLOCK
```

## 10. Safety (F-) program

F-blocks (`ProgrammingLanguage` `F_LAD`/`F_FBD`, blocks in an F-runtime group, `Main_Safety*`,
F-I/O DBs, safety library instructions `ESTOP1`, `TWO_H_EN`, `MUTING`, `FDBACK`, `SFDOOR`,
`ACK_GL`, `EV1oo2DI`) are excluded from every workflow — see `safety-exclusion.md`. Standard
code may read F-I/O values or F-DB outputs (e.g. `"F_Global".EStop_OK`); keep those reads,
flag them, and never generate code that writes into F-data.
