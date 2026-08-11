#!/usr/bin/env python3
"""xysy_hermes_run — launch a XYSY workflow run on the Hermes Agent harness.

This is the Hermes counterpart of `tRunCli` in local-agent/mcpb/server/index.js.
Same job, same return shape ({ok, pid, runId, ...} as one line of JSON on stdout),
so wiring it up is a new branch in the agent's run tool rather than a new runtime.

    xysy_hermes_run.py start  --dir <project> --run-id <id> [--servers a,b] [...]
    xysy_hermes_run.py status --dir <project> --run-id <id>
    xysy_hermes_run.py stop   --dir <project> --run-id <id>

WHAT MAPS TO WHAT (verified against Hermes v0.20.0)

    claude -p <prompt>                      hermes -z <prompt>
    --add-dir <dir>                         --in <dir>
    --model <m>                             -m <m>
    --permission-mode bypassPermissions     --yolo   (+ --accept-hooks, headless)
    --mcp-config runs/<id>/mcp.json         HERMES_HOME=<per-run home>   <-- see below
    --output-format stream-json --verbose    the xysy_progress plugin's NDJSON feed
    (no equivalent)                         -t <toolsets>, --worktree, --usage-file

WHY A PER-RUN HERMES_HOME
    Hermes has no per-invocation MCP config flag; servers come from
    $HERMES_HOME/config.yaml. Pointing HERMES_HOME at a directory this script
    generates buys three things at once:
      1. the per-variant env isolation XYSY needs (OS_MCP_APP, RHINO_MCP_PORT …),
      2. an allowlist — only the servers THIS run needs get started, so a run
         never fires up `autodesk` (npx mcp-remote → an OAuth browser window) or
         six uvx app servers it has no use for,
      3. a private plugins/ dir, so the instrumentation is scoped to XYSY runs
         and cannot alter the user's own `hermes` sessions.

Python 3.9+, standard library only. Runs on the user's machine next to the
Local Agent, so it must not assume anything is installed but hermes itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
XYSY_ROOT = os.environ.get("XYSY_ROOT") or os.path.join(HOME, ".xysy")
REGISTRY = os.path.join(XYSY_ROOT, "registry.json")
RUN_HOMES = os.path.join(XYSY_ROOT, "hermes-runs")
PLUGIN_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xysy_progress")

# The Local Agent's own MCP server is never handed to a run: the run is what the
# agent launched, and re-entering it invites a loop.
SELF_SERVERS = {"xysy-agent", "openstudio-agent", "xysy", "openstudio"}

# Hermes refuses to start on a model reporting under 64k context, and a XYSY run
# carries plan.json + a brief + skills before it does any work, so the ceiling
# matters more here than in chat. Overridable per run.
MIN_CONTEXT = 65536

# A 13-phase arch-viz run makes hundreds of tool calls. Hermes ships
# code_execution.max_tool_calls: 50, which would strand a real run mid-way.
DEFAULT_MAX_TOOL_CALLS = 2000


def die(message: str, **extra):
    print(json.dumps({"ok": False, "error": message, **extra}))
    sys.exit(1)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {} if default is None else default


def find_hermes():
    """GUI-launched parents inherit a stripped PATH — look where the installer puts it.

    Windows needs its own list AND the .exe/.cmd suffixes: a bare 'hermes' matches nothing there,
    so XYSY would report Hermes missing on a machine that has it — and, because the agent IS
    reachable on Windows, it would say so with confidence rather than admitting it cannot tell.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
        names = ["hermes.exe", "hermes.cmd", "hermes.bat", "hermes"]
        candidates = [os.environ.get("HERMES_CLI")]
        candidates += [shutil.which(n) for n in names]
        for base in (os.path.join(HOME, ".local", "bin"),
                     os.path.join(HOME, ".hermes", "hermes-agent", "venv", "Scripts"),
                     os.path.join(local, "Programs", "Python", "Scripts"),
                     os.path.join(appdata, "Python", "Scripts"),
                     os.path.join(local, "Microsoft", "WindowsApps")):
            candidates += [os.path.join(base, n) for n in names]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return ""
    candidates = [
        os.environ.get("HERMES_CLI"),
        shutil.which("hermes"),
        os.path.join(HOME, ".local", "bin", "hermes"),
        os.path.join(HOME, ".hermes", "hermes-agent", "venv", "bin", "hermes"),
        "/usr/local/bin/hermes",
        "/opt/homebrew/bin/hermes",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def enriched_env():
    env = dict(os.environ)
    extra = [os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    path = env.get("PATH", "")
    for entry in extra:
        if entry not in path.split(os.pathsep):
            path = path + os.pathsep + entry
    env["PATH"] = path
    return env


def select_servers(names, mcp_env):
    """Pick this run's servers out of the XYSY registry and apply env overrides.

    `names is None` means "not specified" → every app server in the registry.
    An empty LIST means "this run needs no app servers" and must stay empty:
    collapsing the two is how `--servers ''` ends up launching `autodesk` and
    opening an OAuth browser window on a run that wanted a text tool.
    """
    registry = load_json(REGISTRY).get("servers", {}) or {}
    if names is None:
        wanted = [n for n in registry.keys() if n not in SELF_SERVERS]
    else:
        wanted = [n for n in names if n]
    selected, missing = {}, []
    for name in wanted:
        entry = registry.get(name)
        if not entry:
            missing.append(name)
            continue
        # transport:'builtin' servers have no launch command — the same class of
        # bug that leaked ~/.xysy paths into exported packages. Skip, don't guess.
        if not entry.get("command"):
            continue
        server = {"command": entry["command"], "args": list(entry.get("args") or [])}
        env = dict(entry.get("env") or {})
        override = (mcp_env or {}).get(name) or (mcp_env or {}).get("*")
        if isinstance(override, dict):
            env.update(override)
        if env:
            server["env"] = env
        selected[name] = server
    return selected, missing


def build_home(run_id, servers, model, provider, base_url, context_length,
               max_tool_calls, project_dir):
    """Create $HERMES_HOME for this run: config, plugin, skills bridge."""
    home = os.path.join(RUN_HOMES, run_id)
    os.makedirs(os.path.join(home, "plugins"), exist_ok=True)
    os.makedirs(os.path.join(home, "skills"), exist_ok=True)

    model_cfg = {"default": model} if model else {}
    if provider:
        model_cfg["provider"] = provider
    if base_url:
        model_cfg["base_url"] = base_url
    if context_length:
        model_cfg["context_length"] = int(context_length)
        # Two DIFFERENT numbers, and getting them confused is the whole trap.
        # `context_length` is what Hermes believes the model's window is.
        # `ollama_num_ctx` is what Ollama actually allocates at load time — its
        # default is far smaller, and Hermes refuses the run when the RUNTIME
        # window is under 64k even if the model's advertised window is fine.
        # Only meaningful for Ollama-backed providers; harmless elsewhere.
        if (provider or "").lower() in ("ollama", "custom", ""):
            model_cfg["ollama_num_ctx"] = int(context_length)

    config = {
        "model": model_cfg,
        # A run drives real applications; it must not stop at the chat default.
        "code_execution": {"max_tool_calls": int(max_tool_calls), "timeout": 900},
        "delegation": {"max_iterations": 200},
        "plugins": {"enabled": ["xysy_progress"]},
        "hooks_auto_accept": True,
        "mcp_servers": servers,
    }
    # Written as JSON on purpose: YAML is a superset of JSON, Hermes parses this
    # file with yaml.safe_load, and hand-rolling YAML quoting for Windows paths
    # and args like `--with mcp==1.28.1` is a bug farm.
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)

    # Ollama and friends need no key, but the OpenAI-compatible client wants one.
    env_path = os.path.join(home, ".env")
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write("OPENAI_API_KEY=%s\n" % (os.environ.get("XYSY_LOCAL_API_KEY") or "local"))
        os.chmod(env_path, 0o600)

    # Instrumentation. Copied, not symlinked: a symlink into the XYSY install
    # would break the moment the app is moved or updated mid-run.
    plugin_dst = os.path.join(home, "plugins", "xysy_progress")
    if os.path.isdir(PLUGIN_SRC):
        shutil.rmtree(plugin_dst, ignore_errors=True)
        shutil.copytree(PLUGIN_SRC, plugin_dst)

    # Skills bridge. stage_skills writes <project>/.claude/skills/<id>/SKILL.md,
    # which Hermes does not read; it loads $HERMES_HOME/skills plus AGENTS.md.
    staged = os.path.join(project_dir, ".claude", "skills")
    bridged = []
    if os.path.isdir(staged):
        for name in sorted(os.listdir(staged)):
            src = os.path.join(staged, name)
            if not os.path.isdir(src) or not os.path.exists(os.path.join(src, "SKILL.md")):
                continue
            dst = os.path.join(home, "skills", name)
            if os.path.islink(dst) or os.path.exists(dst):
                if os.path.islink(dst):
                    os.unlink(dst)
                else:
                    shutil.rmtree(dst, ignore_errors=True)
            os.symlink(src, dst)
            bridged.append(name)
    return home, bridged


MODEL_KEYS = ("default", "model", "provider", "base_url", "context_length", "ollama_num_ctx")


def read_user_model_config():
    """What model is Hermes ACTUALLY configured to use? Ask Hermes, don't guess.

    `hermes config get <key>` prints the RESOLVED value, which beats reading the file for two
    reasons: it accounts for layering we would otherwise have to reimplement, and it picks up an
    endpoint we would never have guessed — someone running vLLM on a custom port, or LM Studio
    moved off 1234. Falls back to scanning the file when the CLI is unavailable.
    """
    hermes = find_hermes()
    if hermes:
        found, env = {}, enriched_env()
        for key, alias in (("model.default", "default"), ("model.provider", "provider"),
                           ("model.base_url", "base_url"),
                           ("model.context_length", "context_length"),
                           ("model.ollama_num_ctx", "ollama_num_ctx")):
            try:
                proc = subprocess.run([hermes, "config", "get", key], capture_output=True,
                                      text=True, timeout=45, env=env)
                value = (proc.stdout or "").strip().splitlines()
                value = value[-1].strip() if value else ""
            except Exception:
                continue
            # An unset key prints a sentence, not a value. Anything with a space in it is prose.
            if value and "not set" not in value.lower() and " " not in value:
                found[alias] = value
        if found.get("default"):
            return found
    return _scan_model_config()


def _scan_model_config():
    """Fallback: pull the `model:` block out of $HERMES_HOME/config.yaml by hand.

    Deliberately a scan, not a YAML parse: the shipped config.yaml is a 92 KB commented
    example and the file must be readable without adding a YAML dependency to a helper that
    has to run on any machine with bare python3.
    """
    home = os.environ.get("HERMES_HOME") or os.path.join(HOME, ".hermes")
    path_ = os.path.join(home, "config.yaml")
    found = {}
    try:
        with open(path_, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return found
    inside = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():                  # a new top-level key ends the block
            inside = stripped.startswith("model:")
            continue
        if not inside or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in MODEL_KEYS and value:
            found[key] = value
    return found


def ollama_native_context(base_url, model):
    """The model's TRAINED context window, straight from Ollama — not what it was loaded with.

    This exists because the probe can be made to pass by a model that cannot really do the job:
    setting `ollama_num_ctx: 65536` makes Ollama allocate a 64k window for a model whose native
    limit is 40,960, Hermes' floor check is satisfied, and the readiness card goes green on a
    model that is rope-extended past its training and will degrade instead of failing. Returns
    None when the endpoint is not Ollama or does not say.
    """
    if not base_url or not model:
        return None
    try:
        import urllib.request
        root = base_url.rstrip("/")
        for suffix in ("/v1", "/api"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]
        req = urllib.request.Request(
            root + "/api/show",
            data=json.dumps({"model": model}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read().decode("utf-8")) or {}
    except Exception:
        return None
    # The key is namespaced by architecture: llama.context_length, qwen3.context_length, …
    for key, value in (info.get("model_info") or {}).items():
        if key.endswith(".context_length"):
            try:
                return int(value)
            except Exception:
                return None
    return None


def cmd_models(args):
    """List the local models actually installed on THIS machine, with their real limits.

    Feeds both model pickers — the Hermes setup screen (which sets the defaults for every CLI
    call) and a Sub-Agent's own model preference. It replaces a hardcoded list of invented
    names, so the rule is: report what is installed, and say plainly which ones Hermes can
    actually use and why.

    A model that does not qualify is INCLUDED and marked, never hidden. Silently omitting it
    turns "why isn't my model listed" into a support question; showing it greyed with
    "trained for 40,960 tokens, needs 65,536" answers itself.
    """
    # ASK HERMES FIRST. The assumption to make is that this person already has models — they
    # installed Hermes to use them. So the primary source is whatever endpoint Hermes is
    # actually pointed at, which may be on a port nobody would guess. The two well-known local
    # ports are probed afterwards as extras, and only to catch a second server they also run.
    endpoints = []
    if args.base_url:
        endpoints.append(("configured", args.base_url))
    else:
        cfg = read_user_model_config()
        if cfg.get("base_url"):
            endpoints.append((cfg.get("provider") or "configured", cfg["base_url"]))
        for provider, url in (("ollama", "http://127.0.0.1:11434/v1"),
                              ("lmstudio", "http://127.0.0.1:1234/v1")):
            if not any(u.rstrip("/") == url.rstrip("/") for _, u in endpoints):
                endpoints.append((provider, url))

    import urllib.request

    def get_json(url, payload=None, timeout=10):
        try:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    models, reachable = [], []
    for provider, base_url in endpoints:
        root = base_url.rstrip("/")
        for suffix in ("/v1", "/api"):
            if root.endswith(suffix):
                root = root[: -len(suffix)]

        listing = get_json(root + "/api/tags")            # Ollama
        names = []
        if listing and isinstance(listing.get("models"), list):
            for entry in listing["models"]:
                names.append((entry.get("name") or entry.get("model"), entry.get("size")))
        else:
            listing = get_json(base_url.rstrip("/") + "/models")   # OpenAI-compatible fallback
            if listing and isinstance(listing.get("data"), list):
                names = [(e.get("id"), None) for e in listing["data"]]
        if not names:
            continue
        reachable.append({"provider": provider, "base_url": base_url, "count": len(names)})

        for name, size in names:
            if not name:
                continue
            native = ollama_native_context(base_url, name)
            item = {
                "id": name, "provider": provider, "base_url": base_url,
                "sizeBytes": size,
                "sizeGB": round(size / 1e9, 1) if isinstance(size, (int, float)) else None,
                "nativeContext": native,
                "minContext": MIN_CONTEXT,
            }
            if native is None:
                # Unknown is not the same as too small. Let the person try it; the readiness
                # probe is the real gate and it fails honestly.
                item["qualifies"] = None
                item["reason"] = "context window unknown — Hermes will refuse it if it is under %s" % (
                    "{:,}".format(MIN_CONTEXT))
            elif native < MIN_CONTEXT:
                item["qualifies"] = False
                item["reason"] = "trained for %s tokens; Hermes needs %s" % (
                    "{:,}".format(native), "{:,}".format(MIN_CONTEXT))
            else:
                item["qualifies"] = True
                item["reason"] = "%s tokens of context" % "{:,}".format(native)
            models.append(item)

    usable = [m for m in models if m["qualifies"]]
    print(json.dumps({
        "ok": True,
        "endpoints": reachable,
        "models": models,
        "usable": [m["id"] for m in usable],
        "count": len(models),
        "usableCount": len(usable),
        "minContext": MIN_CONTEXT,
        # The UI needs to tell three states apart: nothing serving, models but none usable, and
        # ready. They have completely different next actions.
        "state": ("no_endpoint" if not reachable else
                  "none_qualify" if not usable else "ok"),
    }))


def cmd_probe(args):
    """Answer 'is Hermes ready to run a XYSY workflow' by actually making it reason.

    Reports the DISTINCT failure, because each one has a different fix:
      not_installed | no_runner | not_configured | context_too_small | endpoint_down | no_reason

    Runs against a scratch HERMES_HOME with NO mcp_servers. That matters: probing against the
    user's own home would start every server they have, and `autodesk` (npx mcp-remote) opens
    an OAuth browser window. A readiness check must never do that.
    """
    hermes = find_hermes()
    result = {"ok": True, "harness": "hermes", "installed": bool(hermes), "path": hermes or None,
              "runner": os.path.abspath(__file__), "ready": False, "state": "not_installed"}
    if not hermes:
        print(json.dumps(result))
        return

    env = enriched_env()
    try:
        result["version"] = subprocess.run(
            [hermes, "--version"], capture_output=True, text=True, timeout=60, env=env
        ).stdout.strip().splitlines()[0]
    except Exception:
        result["version"] = None

    cfg = read_user_model_config()
    model = args.model or cfg.get("default") or cfg.get("model")
    provider = args.provider or cfg.get("provider")
    base_url = args.base_url or cfg.get("base_url")
    context_length = args.context_length or cfg.get("context_length") or MIN_CONTEXT
    result.update({"model": model, "provider": provider, "base_url": base_url})
    if not model:
        result["state"] = "not_configured"
        result["hint"] = "no model set - run `hermes setup` or set model.default"
        print(json.dumps(result))
        return

    # Check the model's TRAINED window BEFORE spending two minutes proving it can emit a word.
    # A pass that was bought by rope-extending the model is not a pass.
    native = ollama_native_context(base_url, model)
    if native:
        result["nativeContext"] = native
        if native < MIN_CONTEXT:
            result["state"] = "context_too_small"
            result["minContext"] = MIN_CONTEXT
            result["hint"] = (
                "%s was trained for %s tokens of context; Hermes needs %s. Forcing a bigger "
                "window would run it past its training instead of failing honestly — pick a "
                "model whose NATIVE window is at least %s."
                % (model, "{:,}".format(native), "{:,}".format(MIN_CONTEXT),
                   "{:,}".format(MIN_CONTEXT))
            )
            print(json.dumps(result))
            return

    home = os.path.join(RUN_HOMES, "_probe")
    build_home("_probe", {}, model, provider, base_url, context_length,
               DEFAULT_MAX_TOOL_CALLS, HOME)
    env["HERMES_HOME"] = home
    env.pop("XYSY_RUN_DIR", None)         # keep probes out of the run event feed

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [hermes, "-z", "Reply with exactly the single word: READY",
             "--yolo", "--accept-hooks"],
            capture_output=True, text=True, timeout=args.timeout, env=env, cwd=home,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        result["state"] = "timeout"
        result["hint"] = "the model did not answer within %ss" % args.timeout
        print(json.dumps(result))
        return
    except Exception as exc:
        result["state"] = "no_reason"
        result["hint"] = str(exc)
        print(json.dumps(result))
        return

    result["elapsedSec"] = round(time.monotonic() - started, 1)
    result["reply"] = " ".join(text.split())[:400]

    low = text.lower()
    # Order matters: the context complaint also contains the word "context", and an auth
    # failure can mention the model, so test the most specific cause first.
    if "below the minimum" in low or "tokens of runtime context" in low:
        result["state"] = "context_too_small"
        result["minContext"] = MIN_CONTEXT
        result["hint"] = ("%s cannot serve %s tokens of context. Pick a model whose NATIVE window "
                          "is bigger, and set model.ollama_num_ctx as well as model.context_length."
                          % (model, MIN_CONTEXT))
    elif any(s in low for s in ("connection refused", "failed to connect", "could not connect",
                                "connection error", "name or service not known")):
        result["state"] = "endpoint_down"
        result["hint"] = "nothing is serving %s" % (base_url or "the configured endpoint")
    elif any(s in low for s in ("api key", "unauthorized", "401", "authentication")):
        result["state"] = "not_configured"
        result["hint"] = "the provider rejected the credentials"
    # The same rule as the Claude path: green ONLY when it actually reasoned. A naive /ok/i
    # test once matched the "ok" inside "tOKen" in "Invalid bearer token" and reported a
    # signed-in engine that could not answer. Require the word, and no error alongside it.
    elif "ready" in low and not any(s in low for s in ("error", "failed", "invalid", "unable")):
        result["ready"] = True
        result["state"] = "ready"
    else:
        result["state"] = "no_reason"
        result["hint"] = "Hermes ran but did not answer READY"
    print(json.dumps(result))



# ===== XY-HERMES-THINK - short reasoning, locally, with no tools =============================
# The sparkle features (ranking, classification, drafting a brief, proposing a step list) are
# short prompts over material XYSY already holds. Those a small local model can do. Anything
# needing the live web or big-model judgement is NOT routed here - index.js sends it to XYSY's
# own Anthropic API instead, which is the one thing Sean's no-Claude-CLI rule still allows.

# Hermes' CONFIGURABLE_TOOLSETS, verbatim. Everything here is switched off for a think, minus
# `web` in web mode. Kept as a literal list rather than asked of Hermes at runtime because a
# think must not pay an import of Hermes' toolset machinery on every call.
THINK_TOOLSETS = [
    "web", "browser", "terminal", "file", "code_execution", "vision", "video", "image_gen",
    "video_gen", "bfl", "x_search", "tts", "stt", "skills", "todo", "memory", "context_engine",
    "session_search", "clarify", "delegation", "cronjob", "homeassistant", "spotify", "discord",
    "discord_admin", "yuanbao", "computer_use",
]

# A local model at a 64k window has room for far more than this, but a prompt this long is a
# sign the caller wanted the big brain (a whole catalogue, a whole plan). Refuse and say why,
# so the router can send it to the API rather than getting a confidently truncated answer.
THINK_MAX_CHARS = 24000


def think_home(mode, model, provider, base_url, context_length):
    home = os.path.join(RUN_HOMES, "_think" if mode != "web" else "_think_web")
    os.makedirs(home, exist_ok=True)

    model_cfg = {"default": model} if model else {}
    if provider:
        model_cfg["provider"] = provider
    if base_url:
        model_cfg["base_url"] = base_url
    if context_length:
        model_cfg["context_length"] = int(context_length)
        if (provider or "").lower() in ("ollama", "custom", ""):
            model_cfg["ollama_num_ctx"] = int(context_length)

    disabled = [t for t in THINK_TOOLSETS if not (mode == "web" and t == "web")]
    config = {
        "database": {"journal_mode": "wal"},
        "model": model_cfg,
        "mcp_servers": {},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "agent": {"disabled_toolsets": disabled},
        "plugins": {"enabled": []},
        "hooks_auto_accept": True,
    }
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)

    env_path = os.path.join(home, ".env")
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write("OPENAI_API_KEY=%s\n" % (os.environ.get("XYSY_LOCAL_API_KEY") or "local"))
        os.chmod(env_path, 0o600)
    return home


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _read_arg(value, path_):
    if path_:
        try:
            with open(path_, "r", encoding="utf-8") as handle:
                return handle.read()
        except Exception:
            return ""
    return value or ""


def cmd_think(args):
    """One short reasoning turn on the local model. Returns text, or a REASON it could not."""
    prompt = _read_arg(args.prompt, args.prompt_file).strip()
    system = _read_arg(args.system, args.system_file).strip()
    if not prompt:
        print(json.dumps({"ok": False, "state": "no_prompt", "error": "nothing to think about"}))
        return

    text_in = (system + "\n\n" + prompt) if system else prompt
    if len(text_in) > THINK_MAX_CHARS:
        print(json.dumps({"ok": False, "state": "too_long", "chars": len(text_in),
                          "maxChars": THINK_MAX_CHARS,
                          "hint": "this prompt is bigger than a local model should be handed"}))
        return

    hermes = find_hermes()
    if not hermes:
        print(json.dumps({"ok": False, "state": "not_installed",
                          "hint": "Hermes is not installed on this computer"}))
        return

    cfg = read_user_model_config()
    model = args.model or cfg.get("default") or cfg.get("model")
    provider = args.provider or cfg.get("provider")
    base_url = args.base_url or cfg.get("base_url")
    context_length = args.context_length or cfg.get("context_length") or MIN_CONTEXT
    if not model:
        print(json.dumps({"ok": False, "state": "not_configured",
                          "hint": "no local model is set"}))
        return

    native = ollama_native_context(base_url, model)
    if native and native < MIN_CONTEXT:
        print(json.dumps({"ok": False, "state": "context_too_small", "model": model,
                          "nativeContext": native, "minContext": MIN_CONTEXT,
                          "hint": "%s was trained for %s tokens; Hermes needs %s"
                                  % (model, "{:,}".format(native), "{:,}".format(MIN_CONTEXT))}))
        return

    mode = "web" if args.web else "plain"
    home = think_home(mode, model, provider, base_url, context_length)
    env = enriched_env()
    env["HERMES_HOME"] = home
    env.pop("XYSY_RUN_DIR", None)          # a think is not a run; keep it out of the event feed

    argv = [hermes, "-z", text_in, "--yolo", "--accept-hooks"]
    if args.web:
        argv += ["-t", "web"]
    if args.reasoning:
        argv += ["--reasoning", args.reasoning]

    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=args.timeout, env=env, cwd=home)
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "state": "timeout", "model": model,
                          "hint": "the local model did not answer within %ss" % args.timeout}))
        return
    except Exception as exc:
        print(json.dumps({"ok": False, "state": "no_reason", "hint": str(exc)}))
        return

    out = _ANSI.sub("", (proc.stdout or "")).strip()
    err = _ANSI.sub("", (proc.stderr or "")).strip()
    elapsed = round(time.monotonic() - started, 1)
    low = (out + "\n" + err).lower()

    if not out:
        state = "no_reason"
        if any(s in low for s in ("connection refused", "failed to connect", "could not connect",
                                  "connection error", "name or service not known")):
            state = "endpoint_down"
        elif any(s in low for s in ("api key", "unauthorized", "401", "authentication")):
            state = "not_configured"
        elif "below the minimum" in low or "tokens of runtime context" in low:
            state = "context_too_small"
        print(json.dumps({"ok": False, "state": state, "model": model,
                          "elapsedSec": elapsed,
                          "hint": " ".join(err.split())[:300] or "Hermes returned nothing"}))
        return

    print(json.dumps({"ok": True, "text": out, "via": "hermes", "model": model,
                      "web": bool(args.web), "elapsedSec": elapsed}))

def pidfile(project_dir, run_id):
    return os.path.join(project_dir, "runs", run_id, "hermes.pid")


def cmd_start(args):
    project_dir = os.path.abspath(os.path.expanduser(args.dir))
    if not os.path.isdir(project_dir):
        die("project folder not found: %s" % project_dir)
    run_dir = os.path.join(project_dir, "runs", args.run_id)
    os.makedirs(os.path.join(run_dir, "shots"), exist_ok=True)

    hermes = find_hermes()
    if not hermes:
        die("hermes CLI not found — install it, or set HERMES_CLI")

    prompt = args.prompt
    if args.prompt_file:
        with open(os.path.expanduser(args.prompt_file), "r", encoding="utf-8") as handle:
            prompt = handle.read()
    if not prompt:
        # Same default contract as the Claude path: RUN.md is the instruction set.
        prompt = (
            "Execute the XYSY workflow run in runs/%s/RUN.md (read that file first; "
            "you are already in its project folder). Follow its step instructions exactly, "
            "including its HONESTY RULES: never mark a step done that did not run in this "
            "invocation, and never reuse a previous run's outputs." % args.run_id
        )

    mcp_env = json.loads(args.mcp_env) if args.mcp_env else {}
    servers, missing = select_servers(
        None if args.servers is None else [s.strip() for s in args.servers.split(",")],
        mcp_env,
    )
    home, bridged = build_home(
        args.run_id, servers, args.model, args.provider, args.base_url,
        args.context_length, args.max_tool_calls, project_dir,
    )

    argv = [hermes, "-z", prompt, "--in", project_dir, "--yolo", "--accept-hooks",
            "--usage-file", os.path.join(run_dir, "usage.json")]
    if args.model:
        argv += ["-m", args.model]
    if args.provider:
        argv += ["--provider", args.provider]
    if args.reasoning:
        argv += ["--reasoning", args.reasoning]
    if args.toolsets:
        argv += ["-t", args.toolsets]
    if args.skills:
        argv += ["-s", args.skills]
    if args.worktree:
        argv.append("--worktree")

    env = enriched_env()
    env["HERMES_HOME"] = home
    env["XYSY_RUN_DIR"] = run_dir          # arms the instrumentation plugin
    env["XYSY_RUN_ID"] = args.run_id
    if args.model:
        env["HERMES_INFERENCE_MODEL"] = args.model

    log_path = os.path.join(run_dir, "hermes.log")
    try:
        log = open(log_path, "w")
    except Exception as exc:
        die("couldn't open log: %s" % exc)

    # Detached so the caller returns immediately; XYSY polls the run dir.
    popen_kwargs = {"cwd": project_dir, "env": env, "stdin": subprocess.DEVNULL,
                    "stdout": log, "stderr": subprocess.STDOUT}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    child = subprocess.Popen(argv, **popen_kwargs)

    with open(pidfile(project_dir, args.run_id), "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))

    print(json.dumps({
        "ok": True, "harness": "hermes", "pid": child.pid, "runId": args.run_id,
        "hermesHome": home, "model": args.model, "servers": sorted(servers.keys()),
        "missingServers": missing, "skillsBridged": bridged,
        "events": os.path.join(run_dir, "hermes-events.ndjson"), "log": log_path,
    }))


def read_pid(project_dir, run_id):
    try:
        with open(pidfile(project_dir, run_id), "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except Exception:
        return None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def cmd_status(args):
    project_dir = os.path.abspath(os.path.expanduser(args.dir))
    run_dir = os.path.join(project_dir, "runs", args.run_id)
    pid = read_pid(project_dir, args.run_id)
    events = []
    events_path = os.path.join(run_dir, "hermes-events.ndjson")
    try:
        with open(events_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-args.tail:]
        for line in lines:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    # XY-HEARTBEAT · "is there a process" is not "is the work still going".
    # Watched from a web page, a run that had ENDED at 26s still read alive=true at 60s,
    # because the child lingers after the session closes. Liveness is now the conjunction,
    # and the raw process fact is still reported separately rather than hidden.
    status = load_json(os.path.join(run_dir, "hermes-status.json"))
    process_alive = alive(pid)
    finished = bool((status or {}).get("finishedAt"))
    print(json.dumps({
        "ok": True, "alive": process_alive and not finished,
        "processAlive": process_alive, "finished": finished,
        "state": (status or {}).get("state") or ("finished" if finished else None),
        "pid": pid,
        "status": status,
        "usage": load_json(os.path.join(run_dir, "usage.json")),
        "progress": load_json(os.path.join(run_dir, "progress.json")),
        "events": events,
    }))


def cmd_stop(args):
    project_dir = os.path.abspath(os.path.expanduser(args.dir))
    pid = read_pid(project_dir, args.run_id)
    if not alive(pid):
        print(json.dumps({"ok": True, "stopped": False, "reason": "not running"}))
        return
    # Kill the whole TREE, not just the parent: Hermes launches the app MCP servers as its own
    # children, and killing only the parent leaves uvx/npx servers holding their ports — the next
    # run then fails to bind and looks like a XYSY bug.
    # os.killpg does not exist on Windows, so this used to raise AttributeError and the Stop
    # button did nothing at all there. Each platform gets the call it actually has.
    if os.name == "nt":
        # taskkill /T takes the children with it; /F because a headless agent has no console to
        # deliver a graceful signal through.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=60)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    for _ in range(20):
        if not alive(pid):
            break
        time.sleep(0.25)
    if alive(pid) and os.name != "nt":
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
    print(json.dumps({"ok": True, "stopped": True, "pid": pid}))


def main():
    parser = argparse.ArgumentParser(description="Run a XYSY workflow on the Hermes harness")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    def shared(sub):
        sub.add_argument("--dir", required=True, help="XYSY project folder")
        sub.add_argument("--run-id", required=True, dest="run_id")

    start = subparsers.add_parser("start")
    shared(start)
    start.add_argument("--prompt")
    start.add_argument("--prompt-file", dest="prompt_file")
    start.add_argument("--model", default=os.environ.get("XYSY_HERMES_MODEL"))
    start.add_argument("--provider", default=os.environ.get("XYSY_HERMES_PROVIDER"))
    start.add_argument("--base-url", dest="base_url",
                       default=os.environ.get("XYSY_HERMES_BASE_URL"))
    start.add_argument("--context-length", dest="context_length", type=int,
                       default=int(os.environ.get("XYSY_HERMES_CONTEXT", MIN_CONTEXT)))
    start.add_argument("--max-tool-calls", dest="max_tool_calls", type=int,
                       default=DEFAULT_MAX_TOOL_CALLS)
    start.add_argument("--servers", help="comma-separated registry server ids (default: all app servers)")
    start.add_argument("--mcp-env", dest="mcp_env", help="JSON {server: {ENV: val}}; '*' applies to all")
    start.add_argument("--toolsets", "-t")
    start.add_argument("--skills", "-s")
    start.add_argument("--reasoning")
    start.add_argument("--worktree", action="store_true")
    start.set_defaults(func=cmd_start)

    status = subparsers.add_parser("status")
    shared(status)
    status.add_argument("--tail", type=int, default=40)
    status.set_defaults(func=cmd_status)

    stop = subparsers.add_parser("stop")
    shared(stop)
    stop.set_defaults(func=cmd_stop)

    models = subparsers.add_parser("models")
    models.add_argument("--base-url", dest="base_url", default=None,
                        help="probe only this OpenAI-compatible endpoint")
    models.set_defaults(func=cmd_models)


    think = subparsers.add_parser("think")
    think.add_argument("--prompt")
    think.add_argument("--prompt-file", dest="prompt_file")
    think.add_argument("--system")
    think.add_argument("--system-file", dest="system_file")
    think.add_argument("--web", action="store_true")
    think.add_argument("--reasoning", default=None)
    think.add_argument("--model", default=None)
    think.add_argument("--provider", default=None)
    think.add_argument("--base-url", dest="base_url", default=None)
    think.add_argument("--context-length", dest="context_length", type=int, default=None)
    think.add_argument("--timeout", type=int, default=180)
    think.set_defaults(func=cmd_think)

    probe = subparsers.add_parser("probe")
    probe.add_argument("--model", default=None)
    probe.add_argument("--provider", default=None)
    probe.add_argument("--base-url", dest="base_url", default=None)
    probe.add_argument("--context-length", dest="context_length", type=int, default=None)
    probe.add_argument("--timeout", type=int, default=180)
    probe.set_defaults(func=cmd_probe)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
