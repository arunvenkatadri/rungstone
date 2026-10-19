# Historian, alarm and event export formats

All scripts auto-detect layout (`ops_csv.sniff`). Use this file when a file is not
detected correctly, or to tell the user how to export what is needed.

## Layouts the scripts read

| Layout | Shape | Detected by |
|---|---|---|
| **wide** | `Timestamp, TagA, TagB, ...` one row per time | no tag/value columns, no message column |
| **long** | `Timestamp, Tag, Value[, Quality]` one row per sample | a tag-like column (`Tag`, `TagName`, `Name`, `Point`, `Item`, `Path`) **and** a `Value` column |
| **events** | `Time, Source, Message[, Priority, State]` | a message-like column (`Message`, `Text`, `Description`, `Event`, `Alarm Text`) |

- Delimiters `,` `;` tab `|` are sniffed. Leading comment lines (`#`, `;`, `//`) and
  title lines without delimiters are skipped. UTF-8 (with or without BOM) and UTF-16.
- Quality: a `Quality`/`Status` column (long) or `<tag>_Q` / `<tag>.Quality` columns
  (wide). Anything other than Good/192/0/OK is counted as bad quality in `summary`.
- Bad or missing values (`Bad`, `NaN`, `No Data`, `I/O Timeout`, empty) are skipped,
  not treated as zero.
- Booleans may be `0/1`, `True/False`, `On/Off`; multi-state digitals may be text
  (`Running`, `Stopped`) and are treated as states.

## Timestamps

ISO 8601 (`2025-06-12T02:29:44`, `2025-06-12 02:29:44.123`, with `Z` or `+02:00`),
`MM/DD/YYYY hh:mm:ss[.fff] [AM|PM]`, `DD.MM.YYYY hh:mm:ss`, `YYYY/MM/DD ...`,
`DD-Mon-YYYY hh:mm:ss` (PI), epoch seconds or milliseconds.

- Ambiguous `01/02/2025`: the US order (MM/DD) is tried first. If the site uses
  DD/MM, the dates will be wrong for days <= 12 - check `summary.start/end` against
  what the user expects and say which order you assumed.
- Times with a zone are converted to UTC; times without a zone are left as written
  ("file time"). Every script output carries a `time_note`; repeat it in the answer
  when times are compared across files.

## Exporting from common systems (what to ask the user for)

| System | Export | Notes |
|---|---|---|
| **AVEVA PI** | PI DataLink "Sampled Data" (wide, interpolated) or "Compressed Data" (long, recorded values); PI SMT Archive Editor export; PI Vision CSV | Prefer **recorded/compressed** values for troubleshooting - interpolated values invent intermediate states. PI timestamps often `DD-Mon-YYYY hh:mm:ss`. |
| **Ignition** | Tag History via Easy Chart/Power Chart "Export CSV" (wide), `system.tag.queryTagHistory` dataset export, or the `sqlth_*` tables (long: `tagid, intvalue/floatvalue, t_stamp` epoch ms) | `t_stamp` is epoch ms. Tag paths look like `[default]Pumps/P101/Run`; the last path element usually matches the PLC tag. Alarm journal export: `eventtime, source, displaypath, priority, eventtype` (0=active,1=clear,2=ack). |
| **FactoryTalk Historian** (PI-based) | Excel Add-In or FactoryTalk Historian DataLink, same as PI | Same as PI. |
| **FactoryTalk View SE** datalog | ODBC/CSV datalog: `Date, Time, Millitm, Tagname, Value, Status, Marker` (long) | Date and time are separate columns - join them before analysis or ask for a combined export. `Millitm` adds milliseconds. |
| **FactoryTalk Alarms & Events** | Alarm history from the A&E SQL log (`AllEvent` table) or an export from the Alarm and Event Log Viewer | Columns include `EventTimeStamp, SourceName, Message, Severity, ConditionName, Active, Acked`. |
| **WinCC (Classic/Unified)** | Tag logging export (CSV via Archive export or WinCC OLE-DB), Alarm logging export (CSV from the alarm view) | German exports use `;` delimiters and `DD.MM.YYYY` dates, decimal comma (`12,5`). The parser handles `;` but values with decimal commas need conversion - check `summary` types; analog columns read as strings mean decimal commas. |
| **Canary, Aspen IP.21, GE Proficy/Historian, OSIsoft AF** | CSV export of raw values | Map to wide or long as above. |
| **Generic SQL** | `SELECT timestamp, tag, value FROM ... WHERE timestamp BETWEEN ...` to CSV | Long layout. |

## Size and scope

- The scripts stream files; tens of MB are fine. Ask for the narrowest useful
  window (the event +/- 1 h) and the relevant tags rather than whole-plant exports.
- Run `historian.py summary` first and quote its scope (tags, time range, sample
  interval, gaps) before analysis, so the user can see what the data can and
  cannot show.
