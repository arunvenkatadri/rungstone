---
description: Analyse alarm history (ISA-18.2 style) - floods, chattering and nuisance alarms, standing alarms, duplicates, priority and response-text suggestions
argument-hint: "<alarm history CSV> [alarm config export]"
---
Use the **rungstone-hmi-scada** skill, alarm cleanup workflow, on: $ARGUMENTS

Run alarm_analysis.py on the history, then present KPIs, bad actors, nuisance alarms (chattering, fleeting, standing), duplicates/consequential alarms, and suggested priorities and operator response text. Everything suggested is for the alarm philosophy owner to approve.
