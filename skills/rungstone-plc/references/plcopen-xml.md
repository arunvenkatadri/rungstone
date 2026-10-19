# PLCopen XML (CODESYS, Beckhoff TwinCAT, Beremiz) and IEC 61131-3 Structured Text

Read when the source or target is CODESYS-based (CODESYS, Schneider Machine Expert, WAGO
e!COCKPIT, ifm, Festo, Eaton…), Beckhoff TwinCAT, or plain IEC ST.

## Contents
1. Getting the right export
2. PLCopen XML structure
3. Graphical bodies (FBD, LD, SFC)
4. CODESYS and TwinCAT specifics
5. IEC ST conventions for generated code
6. Writing IEC ST / PLCopen output (translation target)

---

## 1. Getting the right export

| Have | Ask for |
|---|---|
| `.project` (CODESYS) | Project > Export PLCopenXML (select application/POUs) |
| TwinCAT solution | The `.TcPOU`/`.TcGVL`/`.TcDUT` files from the PLC project folder (plain XML), or PLCopenXML export |
| TwinSAFE (`.sal`, `.splcProj`) | Do not request — safety project, excluded |
| Text `.st`/`.exp` exports | Fine; parser reads PROGRAM/FUNCTION_BLOCK/FUNCTION/TYPE blocks |

## 2. PLCopen XML structure (TC6 v2.00 / v2.01)

```
project (xmlns http://www.plcopen.org/xml/tc6_0200 or tc6_0201)
├─ fileHeader productName productVersion
├─ contentHeader name
├─ types
│   ├─ dataTypes/dataType name -> baseType (struct/variable..., enum, array, derived)
│   └─ pous/pou name pouType=program|functionBlock|function
│        ├─ interface
│        │    ├─ inputVars | outputVars | inOutVars | localVars | tempVars | externalVars | globalVars
│        │    │    (attributes retain="true" / persistent="true" / constant="true")
│        │    │    └─ variable name address="%IX0.0"? -> type (BOOL | INT | derived name="TON" | array ...)
│        │    │                                         initialValue (data, strip) documentation (comment)
│        │    └─ returnType (functions)
│        ├─ actions/action, transitions/transition   (SFC)
│        ├─ body -> ST | IL | FBD | LD | SFC         (ST/IL text inside xhtml:p or xhtml)
│        └─ addData (vendor extensions: CODESYS methods, properties, line ids)
└─ instances/configurations/configuration/resource
     ├─ task name interval priority -> pouInstance typeName
     └─ globalVars
```

Comments are `documentation` (xhtml) on POUs and variables, and `(* *)` / `//` inside ST.

## 3. Graphical bodies

Every element has `localId`. Connections point backwards: `connectionPointIn/connection
refLocalId="7" formalParameter="Q"` means "this input is fed by element 7's output Q".
- FBD: `inVariable`/`outVariable`/`inOutVariable` (with `expression` = the variable),
  `block typeName instanceName` with `inputVariables`/`outputVariables`, `executionOrderId`.
- LD: `leftPowerRail`, `contact` (`negated`, `edge`, `variable`), `coil` (`negated`,
  `storage="set"|"reset"`), `block`s inline, `rightPowerRail`.
- SFC: `step` (`initialStep`), `transition` (`condition` inline ST or `reference`),
  `actionBlock`/`action` (`qualifier` N, S, R, P, L, D...), `selectionDivergence`,
  `simultaneousDivergence`, `jumpStep`.

The parser renders blocks as `TYPE<instance>(IN:=source, ...)`, outVariables as
`target := source`, and steps/transitions as lines. Execution order inside a graphical body
follows `executionOrderId` when present — otherwise state that order is approximate.

## 4. CODESYS and TwinCAT specifics

- Libraries: `Standard` (TON/TOF/TP/CTU/CTD/R_TRIG/F_TRIG), `Util`, `CAA`, `SysTime`; Beckhoff
  `Tc2_Standard`, `Tc2_System`, `Tc2_MC2` (motion), `Tc2_ModbusSrv`, `Tc3_EventLogger`.
- Object orientation: methods, properties, interfaces, `EXTENDS`, `THIS^`, `SUPER^`. Pointers
  `POINTER TO`, `REFERENCE TO`, `ADR()`, `^`. Flag all of these when translating to Logix
  (no equivalent) or older S7 targets.
- `VAR RETAIN` survives warm restart/power loss (with retain hardware), `VAR PERSISTENT` also
  survives download/reset origin on most runtimes — check the runtime docs; flag the choice.
- TwinCAT I/O: variables declared `AT %I*` / `AT %Q*` are linked to I/O in the System Manager
  (the link is not in the PLC file — ask for the I/O mapping or `.tsproj` export if needed).
  Process image is copied at task start/end per task.
- TwinCAT `.TcPOU`: `<TcPlcObject><POU Name><Declaration>` (CDATA declaration text) and
  `<Implementation><ST>` (CDATA body); `Method`, `Action`, `Property/Get|Set` children.
  LD/FBD in TcPOU are stored as NWL XML — ask for a PLCopen export instead.
- Safety: CODESYS Safety SIL2/SIL3 apps, PLCopen safety FBs (`SF_EmergencyStop`, `SF_ESPE`,
  `SF_GuardMonitoring`, `SF_TwoHandControl...`, `SAFEBOOL` types) and TwinSAFE are excluded.

## 5. IEC ST conventions for generated code

- `PROGRAM` per former task/program, `FUNCTION_BLOCK` per reusable unit/AOI.
- Declarations with comments carrying the old address and meaning basis:
  `Start_PB AT %IX0.0 : BOOL; // was I:1/14 - from comment`.
- Standard library only (TON/TOF/TP/R_TRIG/F_TRIG/CTU/CTD/CTUD) unless the engineer names a
  target runtime. `TIME` literals `T#5S`.
- Call FB instances every cycle, outside conditional blocks.
- `VAR RETAIN` for every value the source kept through power cycles (all Rockwell tags), with
  a review-queue item when unsure.

## 6. Writing IEC ST / PLCopen output

Default deliverable: `.st` text (one PROGRAM/FUNCTION_BLOCK per file or one file with
TYPEs first) that can be pasted or imported into CODESYS/TwinCAT. If the engineer wants
PLCopen XML for import, wrap each POU:

```xml
<?xml version="1.0" encoding="utf-8"?>
<!-- GENERATED BY Rungstone - DRAFT FOR ENGINEER REVIEW - NOT APPROVED. Import via Project > Import PLCopenXML; not for direct download. -->
<project xmlns="http://www.plcopen.org/xml/tc6_0200">
  <fileHeader companyName="" productName="Rungstone" productVersion="1" creationDateTime="2026-01-01T00:00:00"/>
  <contentHeader name="Translated"><coordinateInfo><fbd><scaling x="1" y="1"/></fbd><ld><scaling x="1" y="1"/></ld><sfc><scaling x="1" y="1"/></sfc></coordinateInfo></contentHeader>
  <types><dataTypes/><pous>
    <pou name="PRG_TwoTank" pouType="program">
      <interface><localVars>
        <variable name="Start_PB" address="%IX0.0"><type><BOOL/></type>
          <documentation><xhtml xmlns="http://www.w3.org/1999/xhtml">was I:1/14 - from comment</xhtml></documentation></variable>
      </localVars></interface>
      <body><ST><xhtml xmlns="http://www.w3.org/1999/xhtml">Master_Run := (Start_PB OR Master_Run) AND Stop_PB;</xhtml></ST></body>
    </pou>
  </pous></types>
  <instances><configurations/></instances>
</project>
```

Escape `<`, `>`, `&` inside the ST text (`&lt;` etc.).
