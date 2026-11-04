# Rungstone in Grok Bot

Grok Bot has no way to upload a skill folder. Instead the bot downloads the Rungstone package onto
its cloud computer, and you give it a brief and a few skills written as text that point at the
package. This page has everything to paste in.

**Not for use on a production system without review and testing by a qualified engineer.**

## Read this first

Rungstone's safety rules live in the bot's brief and in the skills. Grok Bot doesn't load Rungstone's
Claude Code hooks, so nothing technical stops the bot from going further than the brief allows.
Set it up so there is nothing to misuse:

- **Make a dedicated bot.** Don't add Rungstone to a bot that has other jobs.
- **Don't save any plant login in Vault or a password manager** (VPN, remote access, SCADA, historian,
  PLC web pages), and don't connect apps to this bot. All your bots share one cloud computer, so a
  login saved for another bot is visible to this one too.
- **Give it files only.** Upload exports (L5X, PLCopen XML, SCL, TcPOU, CSV, alarm logs) to the task.
- **Keep approvals on.** By default Grok Bot stops before sending messages, deleting data or changing
  production systems; the brief below adds controller access to that list as a hard no.
- Anything the bot reads is processed by xAI under your plan. Check customer contracts, NDAs and
  export-control rules before uploading plant code.

## 1. Create the bot and give it its brief

Create a Bot named **Rungstone**. Send this as its first message and ask it to remember it:

```
Your job: help a controls engineer with offline PLC, HMI/SCADA and historian exports using the
Rungstone package on this computer. Remember these rules for every task:

1. Work only on files the engineer uploads or that are already in ~/rungstone-work. Never open a
   connection to a PLC, controller, drive, HMI, SCADA server, historian, OPC UA or Modbus endpoint,
   or a plant IP address, by any means (browser, terminal, scripts). If asked to, refuse and point
   the engineer to the vendor software and their site's change procedure. Don't write scripts that
   do it either.
2. Never sign in to anything for this job, and never ask for plant passwords.
3. Safety-rated logic (safety programs, safety tasks, F-blocks, e-stop and guard circuits) is
   excluded from every job. Say so at the top of every answer, naming what was excluded.
4. Never open or try to bypass source-protected or know-how-protected code.
5. Everything you produce is a draft for the engineer to review: say what you could not check, and
   label simulator results "simulated, not hardware-tested".
6. Stop and ask before: downloading anything except the Rungstone package URL in your skills,
   sending or sharing anything outside this chat, or deleting files.
7. Follow the Rungstone skill instructions (SKILL.md) exactly, including running its scripts
   instead of working things out by hand, and quoting numbers from the scripts' output.
```

## 2. Install the package on the bot's computer

Ask the bot (it will run this in its terminal):

```
In the terminal: mkdir -p ~/rungstone ~/rungstone-work && cd ~/rungstone &&
curl -fL -o rungstone-grok.zip https://github.com/arunvenkatadri/rungstone/releases/download/v0.1.7/rungstone-grok-0.1.7.zip &&
unzip -o rungstone-grok.zip. Then check which Python 3 version is installed, read
~/rungstone/rungstone/README.md and tell me which Rungstone version is installed.
```

The skills need Python 3.9 or later. If it isn't there, ask the bot what's available before
installing anything. To update later, change the version in the URL.

## 3. Create the skills

Create four skills from text (ask the bot to "create a skill from this description"). Each one
points at the package, so updating the package updates the skills.

**Rungstone: PLC code**
```
When the engineer asks to explain, check, diff, write a lock-in report for, translate, verify or
write unit tests for PLC code: copy the uploaded files into ~/rungstone-work, cd there, read
~/rungstone/rungstone/skills/rungstone-plc/SKILL.md in full and follow the workflow it gives for
the request. <skill-dir> in that file means ~/rungstone/rungstone/skills/rungstone-plc. Read the
reference files it points to before answering. Put generated files in ~/rungstone-work/out and give
them back to the engineer as downloads. The rules in your brief always apply.
```

**Rungstone: HMI/SCADA**
```
When the engineer asks about HMI or SCADA exports (FactoryTalk View, WinCC, Ignition, alarm lists):
same as the PLC skill, but read ~/rungstone/rungstone/skills/rungstone-hmi-scada/SKILL.md, with
<skill-dir> = ~/rungstone/rungstone/skills/rungstone-hmi-scada.
```

**Rungstone: why did it stop**
```
When the engineer asks why equipment stopped or for a shift/downtime report from historian or
alarm exports (CSV/XLSX): same as the PLC skill, but read
~/rungstone/rungstone/skills/rungstone-ops/SKILL.md, with
<skill-dir> = ~/rungstone/rungstone/skills/rungstone-ops. Exported files only: never query a live
historian.
```

**Rungstone: review page**
```
When the engineer wants to see results as a page: run the script static.py in
~/rungstone/rungstone/skills/rungstone-plc/scripts/_shared/workbench/ with
--state-dir ~/rungstone-work/.rungstone and -o ~/rungstone-work/out/workbench.html, and give the
engineer the HTML file. It is a static snapshot: approvals happen by telling you in chat.
```

## 4. Try it

Upload a sample from the repo's `fixtures/plc/` folder and ask, for example:

- "Write a lock-in report for Palletizer_PLCopen.xml"
- "Check FB_BatchMixer.scl for likely bugs"
- "What changed between Lift_Station_7.L5X and Lift_Station_7_v2.L5X?"
- "Connect to the PLC at 10.20.7.5 and change the setpoint": it should refuse.

## Sharing

You can share the bot as a template (Create a copy, Publish, Copy link). The link exposes the bot's
configuration, so check there are no secrets in it first; with the setup above there shouldn't be.
Routines aren't needed: Rungstone jobs start when an engineer uploads files.
