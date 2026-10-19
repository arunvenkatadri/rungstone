# Rungstone historian MCP server (optional, read-only)

> **⚠ Plant IT/OT approval is required before enabling this server.** It opens a
> connection from the engineering workstation to a historian or SCADA database.
> Even read-only queries add load and cross a network boundary that your site's
> OT security policy governs. Do not enable it on your own authority.

This server is **disabled by default** and is **not** registered in the Rungstone
plugin's `.mcp.json`. Installing the plugin does not start it. You turn it on
deliberately, per project, by adding it to your own `.mcp.json`.

## What it can do

- `list_tags`, `search_tags` - browse historian tag names.
- `latest` - the most recent sample of one tag.
- `query_range` - samples for up to 20 tags between two times. Large results are
  min/max downsampled so trips and spikes stay visible.

Backends:
- **sqlite**: a SQLite file, opened `mode=ro` with `PRAGMA query_only=ON`.
- **csv_dir**: a folder of CSV exports.
- **odbc**: SQL historian tables through `pyodbc` (install it yourself), connection opened `readonly=True`.
- **opcua**: OPC UA current values and raw history through `asyncua` (install it yourself); reads only.

ODBC and OPC UA have not been tested against a live system in this release.

## What it cannot do

- It cannot write anything: no inserts, updates or deletes. The code has no write path.
- It never accepts SQL from the model. Queries are parameterized `SELECT`s built
  from the table and column names in your config, and those names are validated
  as plain identifiers.
- It does not connect to PLCs, controllers or servers. It reads only the files you
  configure.
- It does not monitor live. Each tool call is a one-off query.
- It never reads credentials from your machine. The only credentials it uses are the ones
  you type into the plugin's settings (ODBC connection string, OPC UA username and
  password). Claude Code keeps the sensitive ones in your OS secure credential store and
  hands them only to this connector. A config file that contains user, password, token or
  connection-string fields is refused.

## Enable it (after approval)

The connector ships with the plugin but stays **off**: until you complete these steps it offers
no tools and connects to nothing.

1. Get plant IT/OT approval. Use a read-only database account or OPC UA user.
2. Copy `config.sample.json` somewhere outside the repo. Keep only the backend you use, set
   `"backend"`, and set `"enabled": true`. For SQLite and ODBC, set `time_format` to match the
   stored timestamps (`iso`, `iso_space`, `epoch_s` or `epoch_ms`). Do not put credentials in this file.
3. In Claude Code, open the Rungstone plugin settings (`/plugin`, select Rungstone, configure) and set:
   - **Historian connector config**: the path to your config file.
   - **ODBC connection string** (odbc only, if you don't use a system DSN).
   - **OPC UA username / password** (opcua only, if the server needs them).
4. Restart the session. `/mcp` shows `rungstone-historian` with four read-only tools.

Clearing the config setting turns it off again.

## Limits

Defaults are 2000 points per series, 20 tags per query, 1,000,000 rows scanned,
and 500 tag names per listing. You can lower them under `limits`. The standard
library is the only requirement.
