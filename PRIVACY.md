# Rungstone privacy policy

Last updated: 2026-10-20

Rungstone is an open-source plugin for AI coding assistants (Claude and Codex). It is published by
Arun Venkatadri. This page explains what happens to your data when you use it.

## What Rungstone itself collects

Nothing. Rungstone has no servers, accounts, analytics or telemetry. Its scripts run on your computer,
read the files you give them, and write results to your project folder (`.rungstone/`, `out/` and
`export/`). The scripts make no network connections. The one exception is the optional historian
connector in the Claude plugin, which stays off unless you configure it, and then only queries the
historian you point it at.

The workbench is a local web page served from your own computer on `127.0.0.1`. It is not reachable
from other machines and sends nothing anywhere.

## What your AI assistant receives

Rungstone runs inside your AI assistant. Any file content, script output or report that the assistant
reads becomes part of your conversation and is processed by the assistant's provider under the terms
of your plan:

- in Claude (Claude Code, claude.ai): Anthropic
- in Codex or ChatGPT: OpenAI

Rungstone does not control or receive that data. Before you use Rungstone on a project, check that
sharing it with your assistant's provider is allowed by your customer contracts, NDAs, site rules and
any export-control obligations. If it isn't, don't load those files.

## Contact

Questions about this policy: open an issue at https://github.com/arunvenkatadri/rungstone/issues.
