"""xysy_progress — XYSY run instrumentation for the Hermes Agent harness.

WHY THIS EXISTS
---------------
XYSY's Run Theater renders from two things a run produces on disk:

  runs/<runId>/progress.json   the step ledger the MODEL writes (status per step)
  a live event feed            what the harness is doing right now

On Claude Code the second one came free: `claude -p --output-format stream-json`
emits a line per tool call and `cliEventLine()` in xysy.html parses it. Hermes's
`-z/--oneshot` deliberately prints ONLY the final response text, so there is
nothing to tail. This plugin supplies the missing feed from Hermes's OWN
observer-hook contract (`hermes.observer.v1`) instead of by scraping stdout —
which is strictly better: the payloads carry real correlation IDs
(session/turn/tool_call), timings, statuses and errors.

CONTRACT
--------
Activated only when XYSY_RUN_DIR points at a run directory. Writes:

  <XYSY_RUN_DIR>/hermes-events.ndjson   one JSON object per line, append-only
  <XYSY_RUN_DIR>/hermes-status.json     rewritten snapshot: liveness + counters

It NEVER writes progress.json. That file is the model's honest ledger, and the
XYSY honesty rules forbid anything else marking a step "done" — a plugin that
"helpfully" closed out steps would fake exactly the outcome those rules exist to
prevent. The status snapshot is how XYSY can still show life when the model has
gone quiet, and how it can tell "still working" from "died".

Every callback is fail-open and self-silencing: Hermes catches exceptions and
logs a warning, but a telemetry plugin that throws on every tool call would
bury the log, so failures here disable the plugin for the rest of the process.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

SCHEMA = "xysy.hermes.events.v1"

_LOCK = threading.Lock()
_DEAD = False           # set True after a write failure; stops all further work
_RUN_DIR = None
_EVENTS_PATH = None
_STATUS_PATH = None

# XY-SHOTCOPY -----------------------------------------------------------------
# Where this run's viewport captures should land, and how to recognise one. An app's
# capture tool answers with a path into the harness cache (rhinomcp returns
# "MEDIA:~/.xysy/hermes-runs/<run>/cache/images/img_*.png"); the step was then asked to
# copy that file itself and could not, because its only writing tool writes text.
_SHOT_PATH = None
_SHOT_N = 0
_MEDIA_RE = re.compile(r"(?:MEDIA:\s*)?((?:~|/)[^\s\"'\\,;)\]}]+\.(?:png|jpg|jpeg|webp))", re.I)


def _shot_candidates(value):
    """Every image path mentioned in a tool result, best (MEDIA:-tagged) first."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        return []
    tagged, plain = [], []
    for m in _MEDIA_RE.finditer(text):
        (tagged if m.group(0).upper().startswith("MEDIA") else plain).append(m.group(1))
    return tagged + plain


_LAST_MEDIA = None
_PENDING_SHOT = None
_SHOTFILE_RE = re.compile(r"((?:~|/)[^\s\"'\\,;)\]}]*shots/[^\s\"'\\,;)\]}]+\.(?:png|jpg|jpeg))", re.I)


def _remember_media(result):
    """Hold on to the newest real capture an app returned, whoever asked for it."""
    global _LAST_MEDIA
    for cand in _shot_candidates(result):
        src = os.path.abspath(os.path.expanduser(cand))
        try:
            if os.path.isfile(src) and os.path.getsize(src) > 0:
                _LAST_MEDIA = src
                return
        except Exception:
            continue


def _note_shot_target(args):
    """pre_tool_call is the hook that reliably carries args -- remember the path here."""
    global _PENDING_SHOT
    _PENDING_SHOT = None
    try:
        text = args if isinstance(args, str) else json.dumps(args, default=str)
    except Exception:
        return
    m = _SHOTFILE_RE.search(text or "")
    if m:
        _PENDING_SHOT = os.path.abspath(os.path.expanduser(m.group(1)))


def _fill_written_shot():
    """The model just wrote a shots/*.png with its TEXT writer. Put the real image there."""
    global _PENDING_SHOT
    dst, _PENDING_SHOT = _PENDING_SHOT, None
    if not _LAST_MEDIA or not dst:
        return None
    if dst == _LAST_MEDIA:
        return None
    try:
        # Only step in when what landed is not already a real image -- never clobber a
        # step that genuinely managed to save its own capture.
        if os.path.isfile(dst) and os.path.getsize(dst) >= os.path.getsize(_LAST_MEDIA):
            return None
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        shutil.copyfile(_LAST_MEDIA, tmp)
        os.replace(tmp, dst)
        return {"from": _LAST_MEDIA, "to": dst, "bytes": os.path.getsize(dst)}
    except Exception:
        return None


def _copy_shot(result):
    """Copy the capture an app just produced into the run's shots/ file. Never raises."""
    global _SHOT_N
    if not _SHOT_PATH:
        return None
    for cand in _shot_candidates(result):
        src = os.path.abspath(os.path.expanduser(cand))
        if src == _SHOT_PATH:
            return None
        try:
            if not os.path.isfile(src) or os.path.getsize(src) <= 0:
                continue
            tmp = _SHOT_PATH + ".part"
            shutil.copyfile(src, tmp)
            os.replace(tmp, _SHOT_PATH)
            _SHOT_N += 1
            return {"from": src, "to": _SHOT_PATH, "bytes": os.path.getsize(_SHOT_PATH)}
        except Exception:
            continue
    return None


_STARTED_AT = time.time()
_COUNTS = {"api_calls": 0, "tool_calls": 0, "tool_errors": 0, "subagents": 0}
_TOOL_T0 = {}           # tool_call_id -> start monotonic
_LAST = {"event": None, "tool": None, "at": None}
# XY-HEARTBEAT ---------------------------------------------------------------
# What the run is waiting for right now, and since when. Without this a status
# snapshot can only say "nothing has happened lately", which is equally true of a
# model thinking hard and a process that died.
_WAITING = {"on": None, "since": None}
_FINISHED_AT = None
# XY-SUBEND: the run's own session id. A delegated sub-agent opens and closes a
# session of its own mid-run, and only the root session ending means the run is over.
_ROOT_SESSION = None
_BEAT = None
_BEAT_SECONDS = float(os.environ.get("XYSY_HEARTBEAT_SECONDS", "3") or 3)

# Tool args/results can be enormous (a whole RhinoScript body, a base64 PNG from
# capture_viewport). The console only ever shows a one-line preview, so truncate
# at the source rather than writing megabytes per step to disk.
_PREVIEW_CHARS = int(os.environ.get("XYSY_EVENT_PREVIEW_CHARS", "400") or 400)


def _preview(value):
    """A short, always-JSON-safe rendering of an arbitrary payload."""
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        try:
            text = str(value)
        except Exception:
            return "<unrenderable>"
    text = " ".join(text.split())
    if len(text) > _PREVIEW_CHARS:
        return text[:_PREVIEW_CHARS] + "…"
    return text


def _init():
    """Resolve the run directory. Returns False when XYSY isn't driving this run."""
    global _RUN_DIR, _EVENTS_PATH, _STATUS_PATH
    if _RUN_DIR is not None:
        return True
    run_dir = os.environ.get("XYSY_RUN_DIR", "").strip()
    if not run_dir:
        return False
    run_dir = os.path.abspath(os.path.expanduser(run_dir))
    try:
        os.makedirs(run_dir, exist_ok=True)
    except Exception:
        return False
    _RUN_DIR = run_dir
    _EVENTS_PATH = os.path.join(run_dir, "hermes-events.ndjson")
    global _SHOT_PATH
    shot = os.environ.get("XYSY_SHOT_PATH", "").strip()
    if shot:
        _SHOT_PATH = os.path.abspath(os.path.expanduser(shot))
        try:
            os.makedirs(os.path.dirname(_SHOT_PATH), exist_ok=True)
        except Exception:
            _SHOT_PATH = None
    _STATUS_PATH = os.path.join(run_dir, "hermes-status.json")
    _start_beat()
    return True



def _beat():
    """Refresh the snapshot on a timer, so waiting looks different from stopping."""
    while not _DEAD and _FINISHED_AT is None:
        time.sleep(_BEAT_SECONDS)
        try:
            with _LOCK:
                if _FINISHED_AT is None:
                    _write_status()
        except Exception:
            return


def _start_beat():
    global _BEAT
    if _BEAT is not None or _BEAT_SECONDS <= 0:
        return
    try:
        _BEAT = threading.Thread(target=_beat, name="xysy-heartbeat", daemon=True)
        _BEAT.start()
    except Exception:
        pass

def _emit(kind, **fields):
    """Append one event and refresh the status snapshot. Never raises."""
    global _DEAD
    if _DEAD or not _init():
        return
    event = {"schema": SCHEMA, "ts": round(time.time(), 3), "kind": kind}
    for key, value in fields.items():
        if value is not None:
            event[key] = value
    try:
        with _LOCK:
            with open(_EVENTS_PATH, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, default=str) + "\n")
            _LAST["event"] = kind
            _LAST["at"] = event["ts"]
            if kind in ("tool_start", "tool_end"):
                _LAST["tool"] = fields.get("tool")
            _write_status()
    except Exception:
        # One bad write (full disk, deleted run dir) means every subsequent one
        # fails too. Go quiet instead of logging a warning per tool call.
        _DEAD = True


def _write_status():
    """Rewrite the snapshot atomically — XYSY may poll it mid-write."""
    snapshot = {
        "schema": SCHEMA,
        "runId": os.environ.get("XYSY_RUN_ID") or None,
        "harness": "hermes",
        "model": os.environ.get("HERMES_INFERENCE_MODEL") or None,
        "pid": os.getpid(),
        "startedAt": round(_STARTED_AT, 3),
        "updatedAt": round(time.time(), 3),
        "elapsedSec": round(time.time() - _STARTED_AT, 1),
        "counts": dict(_COUNTS),
        "last": dict(_LAST),
        # XY-HEARTBEAT: what is outstanding, and for how long. `finishedAt` is what
        # lets a reader tell "the work is over" from "the process is still winding down".
        "waitingOn": _WAITING["on"],
        "waitingSec": (round(time.time() - _WAITING["since"], 1)
                       if _WAITING["since"] else None),
        "finishedAt": _FINISHED_AT,
        "state": "finished" if _FINISHED_AT else ("waiting" if _WAITING["on"] else "working"),
    }
    tmp = _STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, default=str)
    os.replace(tmp, _STATUS_PATH)


# ---------------------------------------------------------------- hook bodies
# Every callback takes **kwargs: the observer contract is explicitly additive,
# so naming parameters positionally would break on the next Hermes release.

def on_session_start(**kwargs):
    global _ROOT_SESSION
    if _ROOT_SESSION is None and kwargs.get("session_id"):
        _ROOT_SESSION = kwargs.get("session_id")     # XY-SUBEND: first one wins
    _emit("session_start",
          session_id=kwargs.get("session_id"),
          task_id=kwargs.get("task_id"),
          model=kwargs.get("model"),
          provider=kwargs.get("provider"))


def on_session_end(**kwargs):
    # XY-SUBEND: this fired for a delegated sub-agent too, and setting _FINISHED_AT
    # both froze the snapshot at "finished" and killed the heartbeat thread. XYSY read
    # that as the session ending, took its verdict from a progress.json that still said
    # "running" with nothing done, and told the person the run FAILED - while the parent
    # session went on working for another 74 seconds. Measured on run_1786864518322,
    # Diyara Demo, nemotron-3.5-lightning, 2026-08-16.
    global _FINISHED_AT
    sid = kwargs.get("session_id")
    is_root = (sid is None) or (_ROOT_SESSION is None) or (sid == _ROOT_SESSION)
    if is_root:
        _FINISHED_AT = round(time.time(), 3)
        _WAITING["on"], _WAITING["since"] = None, None
    _emit("session_end",
          session_id=sid,
          root=is_root,
          reason=kwargs.get("reason") or kwargs.get("status"))


def on_session_finalize(**kwargs):
    _emit("session_finalize", session_id=kwargs.get("session_id"))


def pre_api_request(**kwargs):
    _WAITING["on"], _WAITING["since"] = "model", time.time()
    _COUNTS["api_calls"] += 1
    _emit("api_start",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          api_request_id=kwargs.get("api_request_id"),
          model=kwargs.get("model"),
          provider=kwargs.get("provider"),
          api_call_count=kwargs.get("api_call_count"))


def post_api_request(**kwargs):
    _WAITING["on"], _WAITING["since"] = None, None
    _emit("api_end",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          api_request_id=kwargs.get("api_request_id"),
          status=kwargs.get("status"),
          duration_ms=kwargs.get("duration_ms"),
          input_tokens=kwargs.get("input_tokens"),
          output_tokens=kwargs.get("output_tokens"))


def api_request_error(**kwargs):
    _WAITING["on"], _WAITING["since"] = None, None
    _emit("api_error",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          error=_preview(kwargs.get("error") or kwargs.get("message")))


def pre_tool_call(**kwargs):
    # XY-SHOTCOPY: this is the hook that carries the arguments. If the model is about to
    # write a shots/*.png with its text writer, remember where, so the real capture can be
    # dropped in behind it the moment the call returns.
    try:
        _note_shot_target(kwargs.get("args"))
    except Exception:
        pass
    _WAITING["on"], _WAITING["since"] = (kwargs.get("tool_name") or "a tool"), time.time()
    _COUNTS["tool_calls"] += 1
    call_id = kwargs.get("tool_call_id")
    if call_id:
        _TOOL_T0[call_id] = time.monotonic()
    _emit("tool_start",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          tool_call_id=call_id,
          tool=kwargs.get("tool_name"),
          args=_preview(kwargs.get("args")))
    # Returning None leaves the call alone. This hook CAN block a tool
    # ({"action": "block"}) — deliberately unused: XYSY runs with --yolo and
    # policy belongs in the launcher's server allowlist, not in telemetry.
    return None


def post_tool_call(**kwargs):
    # XY-SHOTCOPY: do this BEFORE the event is written, so the copied byte count is in the
    # same feed line that reports the capture -- that is what makes the fix provable.
    copied = None
    try:
        _remember_media(kwargs.get("result"))
        copied = _copy_shot(kwargs.get("result"))
        if not copied:
            copied = _fill_written_shot()
    except Exception:
        copied = None
    if copied:
        _emit("shot_saved", tool=kwargs.get("tool_name"),
              path=copied["to"], source=copied["from"], bytes=copied["bytes"])
    _WAITING["on"], _WAITING["since"] = None, None
    call_id = kwargs.get("tool_call_id")
    started = _TOOL_T0.pop(call_id, None) if call_id else None
    status = kwargs.get("status")
    if status and str(status).lower() not in ("ok", "success", "succeeded", "completed"):
        _COUNTS["tool_errors"] += 1
    _emit("tool_end",
          session_id=kwargs.get("session_id"),
          turn_id=kwargs.get("turn_id"),
          tool_call_id=call_id,
          tool=kwargs.get("tool_name"),
          status=status,
          duration_ms=round((time.monotonic() - started) * 1000, 1) if started else None,
          error=_preview(kwargs.get("error")),
          result=_preview(kwargs.get("result")))


def subagent_start(**kwargs):
    _COUNTS["subagents"] += 1
    _emit("subagent_start",
          parent_session_id=kwargs.get("parent_session_id"),
          child_session_id=kwargs.get("child_session_id"),
          task=_preview(kwargs.get("task") or kwargs.get("prompt")))


def subagent_stop(**kwargs):
    _emit("subagent_stop",
          parent_session_id=kwargs.get("parent_session_id"),
          child_session_id=kwargs.get("child_session_id"),
          status=kwargs.get("status"))


def register(ctx):
    """Hermes plugin entry point."""
    if not _init():
        return          # not an XYSY run — stay completely inert
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("pre_api_request", pre_api_request)
    ctx.register_hook("post_api_request", post_api_request)
    ctx.register_hook("api_request_error", api_request_error)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("subagent_start", subagent_start)
    ctx.register_hook("subagent_stop", subagent_stop)
    _emit("plugin_loaded", plugin="xysy_progress", version="0.1.0")
    # XY-RHINOLEAK: something the PERSON needs to read, not the model. The launcher measures
    # it before the run starts and hands it over here, because the event feed is the only
    # channel that reaches the run console.
    _notice = os.environ.get("XYSY_NOTICE", "").strip()
    if _notice:
        _emit("notice", text=_notice)
