# -*- coding: utf-8 -*-
"""
probe.py -- ReplayProbe79

Responsible for:
    * Monitoring lifecycle (start / stop / clear).
    * Blender handler registration (scene_update_pre / scene_update_post).
    * A modal-operator timer used as a periodic fallback/heartbeat.
    * Collecting observations (counters + bounded event log) produced by
      snapshot.diff_snapshots().
    * Exposing a generic add_event_listener()/remove_event_listener() hook
      (added for Phase 2.2) so other modules -- currently recording.py --
      can observe the same detected events without a second handler.

Design notes / Blender 2.79 constraints honoured here:

    * Nothing in this module touches bpy.context.scene. Handlers receive
      the relevant `scene` as an argument; the timer-driven fallback runs
      inside an Operator's modal()/execute(), where bpy.context is a full,
      non-restricted context (never during addon registration).
    * bpy.app.timers does NOT exist in Blender 2.79 (it was added in
      2.80). The periodic fallback is implemented with the pre-2.80
      pattern instead: wm.event_timer_add() + a modal operator.
    * All module-level state is plain Python (dicts/deque/lists), never
      Blender IDs, so start()/stop()/clear() can run any number of times
      without leaking or depending on stale bpy state.
    * Handler and timer code paths are defensive: any exception is
      caught and logged to the console (prefixed) rather than allowed to
      propagate, because an uncaught exception inside scene_update_post
      can make Blender silently drop the handler or spam the console on
      every subsequent update.
"""

import time
from collections import deque

import bpy

from . import snapshot


ADDON_TAG = "ReplayProbe79"

COUNTER_KEYS = [
    'scene_updates',          # raw handler/timer invocations (cheap count)
    'processed_updates',      # invocations where a full diff actually ran
    'object_changes',         # created + deleted + renamed + data-changed
    'transform_changes',
    'mesh_changes',           # vertex/geometry movement, topology unchanged
    'topology_changes',
    'material_changes',
    'modifier_changes',
    'mode_changes',
    'selection_changes',
    'visibility_changes',
    'active_object_changes',
]

EVENT_COUNTER_MAP = {
    'OBJECT_CREATED': 'object_changes',
    'OBJECT_DELETED': 'object_changes',
    'OBJECT_RENAMED': 'object_changes',
    'OBJECT_DATA_CHANGED': 'object_changes',
    'TRANSFORM_CHANGED': 'transform_changes',
    'MESH_GEOMETRY_CHANGED': 'mesh_changes',
    'TOPOLOGY_CHANGED': 'topology_changes',
    'MATERIAL_CHANGED': 'material_changes',
    'MODIFIER_CHANGED': 'modifier_changes',
    'MODE_CHANGED': 'mode_changes',
    'SELECTION_CHANGED': 'selection_changes',
    'VISIBILITY_CHANGED': 'visibility_changes',
    'ACTIVE_OBJECT_CHANGED': 'active_object_changes',
}

# Minimum wall-clock seconds between two *expensive* full diffs. Raw
# handler calls are always counted, but the (potentially costly) mesh
# signature computation + diff is throttled to this interval to keep
# CPU usage reasonable during continuous operations (e.g. dragging a
# transform fires scene_update_post many times per second).
MIN_PROCESS_INTERVAL = 0.15

DEFAULT_LOG_LIMIT = 200


# ---------------------------------------------------------------------------
# Module-level (pure Python) state
# ---------------------------------------------------------------------------

_running = False
_start_time = 0.0
_last_process_time = 0.0
_last_update_elapsed = 0.0
_counters = dict((k, 0) for k in COUNTER_KEYS)
_log = deque(maxlen=DEFAULT_LOG_LIMIT)
_log_limit = DEFAULT_LOG_LIMIT
_last_event = "(none)"
_last_object = "(none)"
_last_mesh = "(none)"
_scene_snapshots = {}   # scene_name -> snapshot dict
_busy = False           # re-entrancy guard

# ---------------------------------------------------------------------------
# Event listener hook (added for Phase 2.2 -- see recording.py)
# ---------------------------------------------------------------------------
# Phase 2.2 needs to observe the same detected-change events Phase 1 already
# computes, WITHOUT installing a second scene_update_post handler. Rather
# than hard-coding a dependency on recording.py here (which would invert
# the module layering), probe.py exposes a tiny, generic observer list:
# any module may register a callback(scene, event_dict) and will be called
# once per detected event, right after Phase 1's own counters/log are
# updated. A listener exception is caught and logged here so a bug in a
# *subscriber* (e.g. the recording state machine) can never break Phase 1
# detection itself.
_event_listeners = []


def add_event_listener(callback):
    """Register `callback(scene, event_dict)` to be invoked once per
    detected change event. Safe to call multiple times with the same
    callback (it will not be added twice).
    """
    if callback not in _event_listeners:
        _event_listeners.append(callback)


def remove_event_listener(callback):
    """Unregister a previously added callback. Safe to call even if the
    callback was never registered / already removed.
    """
    try:
        _event_listeners.remove(callback)
    except ValueError:
        pass


def is_running():
    return _running


def set_log_limit(n):
    global _log_limit, _log
    try:
        n = int(n)
    except Exception:
        return
    n = max(10, min(2000, n))
    _log_limit = n
    _log = deque(_log, maxlen=n)


def get_status():
    """Return a plain-data snapshot of probe state for UI drawing."""
    return {
        'running': _running,
        'elapsed': (time.time() - _start_time) if _running else 0.0,
        'counters': dict(_counters),
        'log': list(_log),
        'log_limit': _log_limit,
        'last_event': _last_event,
        'last_object': _last_object,
        'last_mesh': _last_mesh,
        'last_update_elapsed': _last_update_elapsed,
        'scene_count': len(_scene_snapshots),
    }


def _reset_counters():
    global _counters
    _counters = dict((k, 0) for k in COUNTER_KEYS)


def _log_event(etype, obj_name, detail=None):
    global _last_event, _last_object
    elapsed = (time.time() - _start_time) if _start_time else 0.0
    line = "[%.2f] %s %s" % (elapsed, etype, obj_name)
    if detail:
        line += " (%s)" % detail
    _log.append(line)
    _last_event = etype
    _last_object = obj_name


def start():
    """Begin monitoring. Builds an initial baseline snapshot of every
    scene in bpy.data.scenes (NOT bpy.context.scene) and installs the
    scene_update_post handler. Safe to call multiple times (no-op if
    already running).
    """
    global _running, _start_time, _scene_snapshots, _last_process_time
    if _running:
        return

    _scene_snapshots = {}
    for scn in bpy.data.scenes:
        try:
            _scene_snapshots[scn.name] = snapshot.snapshot_scene(scn)
        except Exception as exc:
            print(ADDON_TAG + ": failed to baseline scene '%s': %s" % (scn.name, exc))

    _reset_counters()
    _log.clear()
    _start_time = time.time()
    _last_process_time = 0.0
    _running = True

    if on_scene_update_post not in bpy.app.handlers.scene_update_post:
        bpy.app.handlers.scene_update_post.append(on_scene_update_post)

    _log_event('PROBE_STARTED', '(session)')


def stop():
    """Stop monitoring and remove the scene_update_post handler. Safe to
    call multiple times / when not running.
    """
    global _running
    if not _running:
        # Still make sure the handler is not dangling.
        _remove_handler()
        return
    _running = False
    _log_event('PROBE_STOPPED', '(session)')
    _remove_handler()


def _remove_handler():
    try:
        if on_scene_update_post in bpy.app.handlers.scene_update_post:
            bpy.app.handlers.scene_update_post.remove(on_scene_update_post)
    except Exception as exc:
        print(ADDON_TAG + ": error removing handler: %s" % exc)


def clear():
    """Clear counters and log without stopping monitoring."""
    _log.clear()
    _reset_counters()
    global _last_event, _last_object, _last_mesh
    _last_event = "(none)"
    _last_object = "(none)"
    _last_mesh = "(none)"


def shutdown():
    """Called from unregister(). Forces monitoring off and clears all
    in-memory state. Never touches bpy.context.
    """
    global _running
    _running = False
    _remove_handler()
    clear()
    # Detach any Phase 2.2+ subscribers too -- on a full addon unregister
    # nothing should keep a stale reference into this module's state.
    _event_listeners[:] = []


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def on_scene_update_post(scene):
    """Blender calls this with the scene that was updated. We never use
    bpy.context.scene here -- `scene` is supplied directly by Blender.
    """
    global _busy
    if not _running or _busy:
        return
    _counters['scene_updates'] += 1
    now = time.time()
    if (now - _last_process_time) < MIN_PROCESS_INTERVAL:
        return
    _busy = True
    try:
        _process_scene(scene, now)
    except Exception as exc:
        print(ADDON_TAG + ": handler error on scene '%s': %s" % (
            getattr(scene, 'name', '?'), exc))
    finally:
        _busy = False


def process_all_scenes_tick():
    """Used by the modal timer fallback (runs inside an Operator's
    modal(), i.e. with a normal non-restricted bpy.context). Iterates
    every scene in bpy.data.scenes so multi-scene changes and any
    changes that do not reliably fire scene_update_post are still
    picked up eventually.
    """
    global _busy
    if not _running or _busy:
        return
    now = time.time()
    if (now - _last_process_time) < MIN_PROCESS_INTERVAL:
        return
    _busy = True
    try:
        for scn in bpy.data.scenes:
            _process_scene(scn, now)
    except Exception as exc:
        print(ADDON_TAG + ": timer tick error: %s" % exc)
    finally:
        _busy = False


def _process_scene(scene, now):
    global _last_process_time, _last_update_elapsed, _last_mesh
    _counters['processed_updates'] += 1
    _last_process_time = now
    _last_update_elapsed = now - _start_time if _start_time else 0.0

    old = _scene_snapshots.get(scene.name)
    new = snapshot.snapshot_scene(scene)
    _scene_snapshots[scene.name] = new

    if old is None:
        # First time we see this scene (e.g. scene created after start()).
        return

    events = snapshot.diff_snapshots(old, new)
    for ev in events:
        key = EVENT_COUNTER_MAP.get(ev['type'])
        if key:
            _counters[key] += 1
        mesh_name = ev.get('mesh_name')
        if mesh_name:
            _last_mesh = mesh_name
        _log_event(ev['type'], ev.get('object', '?'), ev.get('detail'))

        for listener in tuple(_event_listeners):
            try:
                listener(scene, ev)
            except Exception as exc:
                print(ADDON_TAG + ": event listener error (ignored): %s" % exc)


# ---------------------------------------------------------------------------
# Operators (lifecycle control). Kept in probe.py because they ARE the
# monitoring lifecycle, per the project's module-responsibility split.
# ---------------------------------------------------------------------------

class REPLAYPROBE79_OT_start(bpy.types.Operator):
    bl_idname = "replayprobe79.start"
    bl_label = "Start Probe"
    bl_description = "Start ReplayProbe79 diagnostic monitoring"
    bl_options = {'REGISTER'}

    _timer = None

    def modal(self, context, event):
        if not is_running():
            self._finish(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            process_all_scenes_tick()

        return {'PASS_THROUGH'}

    def execute(self, context):
        wm = context.window_manager
        try:
            limit = getattr(wm, 'replayprobe79_log_limit', DEFAULT_LOG_LIMIT)
            set_log_limit(limit)
        except Exception:
            pass

        start()

        self._timer = wm.event_timer_add(0.5, context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        wm = context.window_manager
        if self._timer is not None:
            try:
                wm.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None

    def cancel(self, context):
        self._finish(context)


class REPLAYPROBE79_OT_stop(bpy.types.Operator):
    bl_idname = "replayprobe79.stop"
    bl_label = "Stop Probe"
    bl_description = "Stop ReplayProbe79 diagnostic monitoring"
    bl_options = {'REGISTER'}

    def execute(self, context):
        stop()
        return {'FINISHED'}


class REPLAYPROBE79_OT_clear(bpy.types.Operator):
    bl_idname = "replayprobe79.clear"
    bl_label = "Clear"
    bl_description = "Clear ReplayProbe79 counters and event log"
    bl_options = {'REGISTER'}

    def execute(self, context):
        clear()
        return {'FINISHED'}


PROBE_CLASSES = (
    REPLAYPROBE79_OT_start,
    REPLAYPROBE79_OT_stop,
    REPLAYPROBE79_OT_clear,
)
