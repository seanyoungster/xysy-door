# XYSY door — a Hermes plugin

Lets [XYSY](https://xysy.ai) on the web drive the creative applications on this computer, without
XYSY installing an application of its own and without Claude Desktop needing to be open.

## Install

```
hermes plugins install seanyoungster/xysy-door --enable
hermes serve
```

That is the whole thing. The first command clones the plugin into `~/.hermes/plugins/xysy` and adds
it to `plugins.enabled`; the second runs Hermes' own server, which is what hosts the door. Then open
[xysy.ai](https://xysy.ai) → **Set up Hermes** and press **↻ Try again**.

To keep the server running after a reboot, run `hermes serve` from a login item — or on macOS, use
the LaunchAgent recipe at the end of this file.

Update later with `hermes plugins update xysy`. Remove with `hermes plugins remove xysy`.

## Why a door of its own

Hermes' own local server refuses `xysy.ai` by name — measured against a real `hermes serve`:

```
Origin: https://xysy.ai        ->  400  Disallowed CORS origin
Origin: http://localhost:5173  ->  200  allowed
```

That is a hardcoded rule with a security reason attached, not a setting. So this plugin opens a
second, much smaller listener on `127.0.0.1:4850` and answers only for XYSY.

## The four locks

1. **Loopback only.** The listener binds `127.0.0.1`; nothing off this machine can reach it.
2. **An origin allowlist**, enforced on the preflight *and* on the request.
3. **A bearer token, always** — including when there is no `Origin` header. "No origin means not a
   browser, so trust it" is the wrong way round and is deliberately not done here.
4. **Pairing.** The door belongs to exactly one XYSY account. The first key is verified upstream
   with xysy.ai, and compared with `hmac.compare_digest`, never `==`.

## What it can do

`hello` · `pair` · `system` · `inventory` · `apps` · `servers` / `server_verify` · `capture` ·
`projects` · `project_create` · `run_write` · `run_start` · `run_status` · `run_stop` ·
`read_output` · `stage_skills` · `think` · `open`

Deliberately **not** exposed: anything that runs a shell, and `verify_mcp` — the caller hands that
one a command line, and a door reachable from a web page does not get to run command lines on this
computer. `think` refuses anything needing the live web.

**Screenshots go through the application's own MCP connector**, not the screen: no Screen Recording
permission, and a dialog sitting over the window cannot corrupt the result. Connector definitions
come from Hermes' own `mcp_servers` config. A screen grab remains only as a fallback, and says so.

## Keeping the server up on macOS

```
cat > ~/Library/LaunchAgents/ai.xysy.hermes-serve.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.xysy.hermes-serve</string>
  <key>ProgramArguments</key><array>
    <string>PYTHON</string><string>HERMES</string><string>serve</string><string>--skip-build</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
PLIST
```

Replace `PYTHON` with `~/.hermes/hermes-agent/venv/bin/python` and `HERMES` with
`~/.hermes/hermes-agent/hermes` (absolute paths — launchd does not expand `~`), then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.xysy.hermes-serve.plist`.

## Checking it works

```
curl -s http://127.0.0.1:4850/xysy/hello
{"ok": true, "door": "xysy", "version": "0.4.2", "host": "hermes", "paired": false, "email": ""}
```
