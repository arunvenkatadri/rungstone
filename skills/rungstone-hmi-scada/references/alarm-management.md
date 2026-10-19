# Alarm management (ISA-18.2 / IEC 62682 / EEMUA 191)

Read for the alarm cleanup workflow and whenever priorities, nuisance alarms or response text come up.
Rungstone analyses exported history and proposes changes; the site alarm philosophy owner and the
rationalization team decide. Never present a suggestion as a decision.

## Lifecycle (ISA-18.2)

Philosophy -> identification -> **rationalization** (is it an alarm? consequence, time to respond, priority,
response) -> detailed design (setpoint, deadband, delays, suppression) -> implementation -> operation ->
maintenance -> **monitoring and assessment** (this workflow) -> management of change -> audit.

An alarm must indicate an abnormal condition that **requires a timely operator action**. If no action is
required, it is an event or a status message, not an alarm.

## KPIs and typical targets

| KPI | Target / guidance |
|---|---|
| Average alarm rate per operator | ~6/hour acceptable (≈1 per 10 min); up to ~12/hour maximum manageable |
| Alarm floods | > 10 alarms in 10 min per operator; < ~1 % of 10-min periods in flood |
| Peak alarms in 10 min | ≤ 10 |
| Top 10 most frequent alarms | < 1–5 % of the total load |
| Chattering / fleeting alarms | 0 — fix them |
| Stale (standing) alarms | fewer than 5 on any day; plan to remove each |
| Priority distribution (annunciated) | ~80 % low, ~15 % medium, ~5 % high; critical/urgent < 1 % |
| Unauthorized suppression / changes | 0 |

These are the commonly quoted ISA-18.2 values; the site philosophy may set its own. Say which you used.

## Nuisance categories (as detected by `alarm_analysis.py`)

| Category | Default rule | Typical fix to propose |
|---|---|---|
| Chattering | ≥ 3 activations of the same alarm within 60 s | on-delay 2–5 s, deadband/hysteresis, fix instrument or setpoint |
| Fleeting | active < 2 s | on-delay; or demote to event if no action needed |
| Stale / standing | active > 24 h | fix cause/instrument; remove under MOC; shelving is temporary only |
| Consequential / duplicate | B follows A within 2 s in ≥ 80 % of A's activations | state-based suppression, first-out grouping, keep only the cause alarm |
| Flood | > 10 per 10 min | find the first-out alarm; suppress consequentials; check if one chattering alarm drives it |
| Bad actor | top 10 by count | rationalize these first — biggest load reduction for least effort |

## Priority matrix (example — replace with the site's)

| Consequence severity \ Max time to respond | > 30 min | 10–30 min | 3–10 min | < 3 min |
|---|---|---|---|---|
| Minor (no safety or environmental impact, small loss) | No alarm | Low | Low | Medium |
| Major (equipment damage, quality loss, reportable) | Low | Medium | Medium | High |
| Severe (injury potential, major environmental, large loss) | Medium | High | High | Critical |

Alarms acting as safeguards (layers of protection) need the safety/PHA team's input; Rungstone does not
re-prioritize safety-related alarms — list them for that team.

## Response text template (per alarm)

```
Alarm:        <tag / name>          Priority: <proposed>   (current: <current>)
Cause:        <what makes it go active - from the PLC logic, cite rung/line>
Consequence:  <what happens if the operator does nothing, and how fast>
Action:       <the specific operator action(s), in order>
Confirm:      <how the operator knows the action worked>
Time to respond: <minutes>          Basis: comment | io_map | inferred | unknown
```

Mark every cause/consequence that you inferred from logic as **inferred**.

## Terms

- **Shelving**: operator temporarily hides an alarm (time-limited, logged).
- **Suppression by design**: logic suppresses an alarm in a state (e.g. equipment stopped).
- **Out of service**: maintenance removes an alarm (permit / MOC).
