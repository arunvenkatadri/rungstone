# Lock-in report (portability.py)

Read before presenting a lock-in report.

## What it answers

"What in this project only works on CODESYS (or TwinCAT), and what would it take to move it to
OpenPLC/Beremiz, plain IEC ST, S7-1500 or Logix?" It is a planning tool: it sizes the job and
finds the hard parts before anyone translates anything. It does not translate.

## Sources covered

CODESYS-based projects (CODESYS, Schneider Machine Expert, WAGO e!COCKPIT, ifm, Festo, Eaton...
via PLCopen XML export), Beckhoff TwinCAT (.TcPOU/.TcGVL/.TcDUT or PLCopen XML) and IEC 61131-3
ST text. Rockwell and Siemens sources return `supported: false`; their platform-specific items
show up in Translate's review queue instead.

Never ask for, open or decode `.project`, `.library`, `.compiled-library` or other binary files.
The PLCopen XML export (Project > Export PLCopenXML) is the only input; see `plcopen-xml.md`.

## Ratings

| Rating | Meaning |
|---|---|
| as-is | moves without code changes (renames aside) |
| rework | the target has an equivalent; the code is rewritten around it |
| no equivalent | the target has nothing comparable: the function moves (to the HMI, a historian, a drive or motion controller) or the design changes |

A POU "moves as-is" for a target when none of its findings is rework or no equivalent. I/O
re-mapping and compiler-hint pragmas add hours but don't stop a POU moving as-is: every migration
re-maps I/O.

## Effort

Each rule carries an hours range per POU, per occurrence or per project. The totals are
deliberately wide. Say what they cover (code changes) and what they don't (testing,
commissioning, I/O checkout, documentation, safety). Never turn the range into a single number,
a price or a schedule.

## Things to say when they apply

- Motion: moving axes is never just code. The axes are re-configured and re-commissioned on the
  target, and for OpenPLC/plain IEC the motion has to live in a drive or motion controller.
- File logging, sockets, dynamic memory: these usually mean the PLC is doing work that belongs
  in the HMI, a historian or an edge device. Say that moving them out is often the cleaner fix.
- Object orientation: flattening EXTENDS/interfaces/methods is mechanical but touches every caller.
  Count the callers in the dependency map before sizing it.
- Libraries: the export only shows the calls. The library code isn't there, and its license may
  not allow use on another platform.
- PERSISTENT/RETAIN: what survives a power cycle and a download differs on every platform. Always
  a review item when translating.
- OpenPLC/Beremiz suits machines, test rigs and education. It is not a safety platform and has no
  motion control; say so if the project has either.

## Limits

Static analysis of the export only: no visualisations, device trees, fieldbus configuration or
library contents. Rules are rules of thumb; the engineer confirms each rating.
