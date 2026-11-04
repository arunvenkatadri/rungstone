# Rungstone in Grok Bot

Grok Bot has no way to upload a skill folder. Instead the bot downloads the Rungstone package onto
its cloud computer, and you give it a brief and a few skills written as text that point at the
package. This page has everything to paste in. It takes about 15 minutes.

**Not for use on a production system without review and testing by a qualified engineer.**

## 0. Get Grok Bot

1. You need a plan that includes Grok Bot: SuperGrok, SuperGrok Plus or Heavy, X Premium+, an
   X Subscription Premium/Super/Ultra, a Grok Business seat, or a paid Cursor plan. Details:
   https://docs.x.ai/grok-bot/get-started
2. Download the desktop app from https://x.ai/bot (macOS, Windows, Linux; phones: App Store or
   Google Play) and open it.
3. Click **Sign in** and finish in your browser. With a SuperGrok or X Premium+ subscription, link
   it when asked (desktop: **Settings → Usage & Billing → Get Access with Grok**).
4. Click through the short intro. It creates a first bot called "Grok Bot"; leave that one for
   other work.

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

1. Click **+** at the top of the sidebar (or `Cmd/Ctrl+N`; on a team, choose **Create a Bot**).
2. Name it **Rungstone**, pick any shape and color, and click **Create**. A chat with it opens.
3. Paste this as its first message and send it:

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

4. Check its reply says it will remember these rules.

## 2. Install the package on the bot's computer

In the same chat, send this (the bot runs it in its cloud computer's terminal; approve it if asked):

```
In the terminal: mkdir -p ~/rungstone ~/rungstone-work && cd ~/rungstone &&
curl -fL -o rungstone-grok.zip https://github.com/arunvenkatadri/rungstone/releases/download/v0.1.7/rungstone-grok-0.1.7.zip &&
unzip -o rungstone-grok.zip. Then check which Python 3 version is installed, read
~/rungstone/rungstone/README.md and tell me which Rungstone version is installed.
```

The skills need Python 3.9 or later. If it isn't there, ask the bot what's available before
installing anything. To update later, change the version in the URL.

## 3. Create the skills

Send each of the four blocks below as its own message, starting with
**Save this as a skill called "<name>":** and then the text. For example:

```
Save this as a skill called "Rungstone: PLC code": When the engineer asks to explain, ...
```

Each skill points at the package, so updating the package updates the skills. To check they are
there: open **Connect apps** in the sidebar, click the connected count next to the title, and look
under **Private skills**. Skills are shared by all your bots; if this bot can't find one, enable it
for this bot there.

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

Download a sample from the repo's `fixtures/plc/` folder
(https://github.com/arunvenkatadri/rungstone/tree/main/fixtures/plc), attach it to a message in the
Rungstone chat, and ask, for example:

- "Write a lock-in report for Palletizer_PLCopen.xml"
- "Check FB_BatchMixer.scl for likely bugs"
- "What changed between Lift_Station_7.L5X and Lift_Station_7_v2.L5X?"
- "Connect to the PLC at 10.20.7.5 and change the setpoint": it should refuse.

## Sharing

1. Click the bot's name at the top of the chat and choose **Share**, then **Create a copy**. The bot
   posts a template card in the chat. (Phone: the bot's menu, **Share as Template**.)
2. On the card choose **Publish**, then **Copy link**. That is the `https://x.ai/bot/...` link
   people open to choose **Add to Grok Bot**.

A public link shows the bot's name, description, skills and routines. Check there are no secrets,
internal URLs or customer data in them first; with the setup above there shouldn't be.
Routines aren't needed: Rungstone jobs start when an engineer uploads files.
