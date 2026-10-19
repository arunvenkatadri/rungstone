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

Backends: a **SQLite** file (opened `mode=ro` with `PRAGMA query_only=ON`), a
folder of **CSV** exports, and, only if you install the driver yourself,
**ODBC** (`pyodbc`, a system DSN named in the config, connection opened `readonly=True`) or **OPC UA** (`asyncua`,
value and history reads only). ODBC and OPC UA have not been tested against a
live system in this release.

## What it cannot do

- It cannot write anything: no inserts, updates, deletes, OPC UA writes, or
  method calls. The code has no write path.
- It never accepts SQL from the model. Queries are parameterized `SELECT`s built
  from the table and column names in your config, and those names are validated
  as plain identifiers.
- It does not connect to PLCs or controllers. It talks only to the historian you
  configure.
- It does not monitor live. Each tool call is a one-off query.
- It never reads, stores or passes credentials. ODBC connects to a system DSN by
  name; any username or password lives in your OS ODBC configuration (prefer
  Windows integrated auth or a read-only database account). OPC UA uses anonymous
  read-only sessions. A config containing user, password, token or
  connection-string fields is refused.

## Enable it (after approval)

1. Copy `config.sample.json` somewhere outside the repo, fill in the backend
   section you use, and set `"enabled": true`.
2. For SQLite, set `time_format` to match the stored timestamps: `iso`
   (`2026-10-19T08:00:00`), `iso_space` (`2026-10-19 08:00:00`), `epoch_s`, or
   `epoch_ms`.
3. Add this to **your project's** `.mcp.json` (not the plugin's):

```json
{
  "mcpServers": {
    "rungstone-historian": {
      "command": "python3",
      "args": ["/absolute/path/to/rungstone/mcp/historian/server.py",
               "--config", "/absolute/path/to/historian-config.json"]
    }
  }
}
```

If `--config` is missing, or the config does not have
`"enabled": true`, the server prints why and exits without serving anything.

## Limits

Defaults are 2000 points per series, 20 tags per query, 1,000,000 rows scanned,
and 500 tag names per listing. You can lower them under `limits`. The standard
library is the only requirement; `pyodbc` and `asyncua` are optional.
