# -*- coding: utf-8 -*-
"""
recording.py -- Replay79 Phase 2.2/2.3A: Recording Lifecycle + Event
Normalization

Responsible for:
    * The Start Recording / Stop Recording state machine:
          STOPPED -> ARMING -> RECORDING -> STOPPING -> STOPPED
    * A recording-session clock measuring ACTIVE recording time (not
      arbitrary wall-clock time), structured so a future PAUSED state can
      exclude paused time without rewriting this module.
    * A single in-memory RecordingSession: a baseline scene snapshot taken
      at Start, plus a bounded log of NORMALIZED events (see events.py)
      -- each with a stable sequence number, a recording-relative
      timestamp, and the affected object's/mesh's persistent identity.py
      UID where available, including for OBJECT_DELETED (Phase 2.3A --
      see "Session identity maps" below).
    * (Phase 2.3A) Resolving each raw probe.py event's transient
      obj.as_pointer()/mesh.as_pointer() into a persistent identity.py
      UID via small, session-local pointer->UID maps, and handing the
      result to events.py for normalization + conservative coalescing.

Responsible for NOT doing (explicitly out of scope for Phase 2.2/2.3A --
see docs/Phase2_Architecture.md for where these belong):
    * SQLite / .rm79 file creation.
    * Mesh delta/topology encoding, checkpoint serialization.
    * Replay reconstruction / VirtualScene / camera / FFmpeg.
    * A second scene_update_post handler -- this module is a *subscriber*
      to probe.py's existing detection pipeline (see probe.add_event_listener),
      never an independent detector.

-------------------------------------------------------------------------
Session identity maps (Phase 2.3A)
-------------------------------------------------------------------------
P2.2's `_on_probe_event` resolved an object's UID with
`bpy.data.objects.get(obj_name)` -- which cannot work for OBJECT_DELETED,
because the object no longer exists to look up by name by the time the
event arrives. Phase 2.3A fixes this with two small, session-local
dicts, `_object_uid_by_pointer` / `_mesh_uid_by_pointer`, keyed by the
*transient* `obj.as_pointer()`/`mesh.as_pointer()` values that
snapshot.diff_snapshots() now includes on every event (see snapshot.py).
These pointers are SESSION-LOCAL LOOKUP KEYS ONLY -- never written into
a normalized event; the normalized event only ever carries the resolved
persistent `object_uid`/`mesh_uid` string (or None).

The maps are populated once from the baseline at Start (every object/mesh
present when recording begins), and kept up to date incrementally:
    * OBJECT_CREATED is the only event type allowed to run the
      comparatively expensive `identity.ensure_all_identities()` repair
      scan (a cheap, custom-ID-property-only scan -- no geometry -- but
      still O(all objects/meshes), so it must not run on every event).
      Immediately afterwards a cheap property-only pass refreshes both
      maps from the now-correct (collision-repaired) state. This is what
      makes a duplicated object (which starts out holding a *colliding*
      copy of the original's UID) end up correctly mapped to its own,
      newly-repaired UID rather than the original's.
    * Every other event type resolves via an O(1) pointer dict lookup
      first; only if that misses does it fall back to a single, cheap,
      geometry-free `bpy.data.objects.get(name)` / `.meshes.get(name)` +
      `identity.get_object_uid()/get_mesh_uid()` call (property reads
      only), backfilling the map for next time. OBJECT_DELETED never
      takes this fallback path at all (the object is already gone), so
      its UID can ONLY come from the pointer map -- which is exactly why
      the map must be populated proactively rather than lazily.
    * On OBJECT_DELETED, once the delete event has been normalized, the
      object's entry is removed from `_object_uid_by_pointer` (the mesh
      entry is deliberately NOT removed -- deleting an object does not
      delete its mesh datablock in Blender; it may still be referenced
      elsewhere or simply linger as a 0-user datablock).

-------------------------------------------------------------------------
Relationship to Phase 1 (probe.py) and Phase 2.1 (identity.py)
-------------------------------------------------------------------------
"Probe: Monitoring" (Phase 1) and "Recording: RECORDING" (Phase 2.2) are
related but distinct concepts, and this module deliberately keeps them
that way:

    * Phase 1's probe can run on its own, purely as a diagnostic, with no
      recording session active at all ("Probe: Monitoring / Recording:
      STOPPED" is a perfectly valid, expected state).
    * A recording session, however, is USELESS without Phase 1's detector
      actually running (no detector -> no events -> nothing ever reaches
      the session). So start_recording() ensures the probe is running
      (calling probe.start(), which is a safe no-op if it already is)
      rather than inventing a second detection path. This is the "reuse
      the existing probe/detection flow" requirement.
    * stop_recording() deliberately does NOT stop the probe -- a user may
      want diagnostic monitoring to keep running after a recording ends.
      A known, accepted consequence: if the user manually presses "Stop
      Probe" while a recording session is RECORDING, the session's state
      stays RECORDING but silently stops receiving new events (the
      handler feeding it is gone). The UI surfaces both states
      side-by-side specifically so this situation is visible rather than
      hidden -- see ui.py and the Known Limitations note in README.md.

-------------------------------------------------------------------------
Why identity.ensure_all_identities() + validate_scene_identities() first
-------------------------------------------------------------------------
A recording session's baseline and every subsequent event need a stable,
persistent way to refer to "this object"/"this mesh" across the whole
session (and, later, across Pause/Resume and Blender restarts). That is
exactly what Phase 2.1 provides. Recording therefore refuses to start if
identity assignment/repair could not make every currently relevant
datablock's identity valid and unique -- starting anyway would mean
silently recording against unreliable identities.
"""

import time

import bpy

from . import probe
from . import snapshot
from . import identity
from . import events


ADDON_TAG = "ReplayProbe79"

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------
STATE_STOPPED = 'STOPPED'
STATE_ARMING = 'ARMING'
STATE_RECORDING = 'RECORDING'
STATE_STOPPING = 'STOPPING'
# Reserved for a future milestone. Not entered by this implementation yet,
# but RecordingClock.pause()/resume() already exist so adding it later does
# not require reworking the clock or the session bookkeeping below.
STATE_PAUSED = 'PAUSED'

SESSION_LOG_LIMIT = 500  # bounded in-memory event log per session


# ---------------------------------------------------------------------------
# Recording clock
# ---------------------------------------------------------------------------

class RecordingClock(object):
    """Measures ACTIVE recording time using time.monotonic() (Python 3.5
    standard library -- available in Blender 2.79b's bundled interpreter),
    not time.time(), so the recording clock cannot jump if the system
    wall clock changes mid-session.

    pause()/resume() are implemented now (cheap: a couple of floats) even
    though Phase 2.2's state machine never calls them, specifically so a
    future PAUSED state is a small addition to the state machine rather
    than a rewrite of time-keeping.
    """

    def __init__(self):
        self._start_mono = None
        self._stop_mono = None
        self._pause_started_mono = None
        self._paused_total = 0.0
        self._running = False

    def start(self):
        now = time.monotonic()
        self._start_mono = now
        self._stop_mono = None
        self._pause_started_mono = None
        self._paused_total = 0.0
        self._running = True

    def stop(self):
        if not self._running:
            return
        now = time.monotonic()
        if self._pause_started_mono is not None:
            self._paused_total += now - self._pause_started_mono
            self._pause_started_mono = None
        self._stop_mono = now
        self._running = False

    def pause(self):
        # Unused by the P2.2 state machine (no PAUSED state yet); kept
        # functional for forward compatibility.
        if self._running and self._pause_started_mono is None:
            self._pause_started_mono = time.monotonic()

    def resume(self):
        if self._pause_started_mono is not None:
            self._paused_total += time.monotonic() - self._pause_started_mono
            self._pause_started_mono = None

    def elapsed(self):
        """Active recording time in seconds: wall time since start(),
        minus any paused time, frozen at whatever it was when stop() was
        called.
        """
        if self._start_mono is None:
            return 0.0
        end = self._stop_mono if self._stop_mono is not None else time.monotonic()
        paused = self._paused_total
        if self._pause_started_mono is not None:
            paused += time.monotonic() - self._pause_started_mono
        return max(0.0, (end - self._start_mono) - paused)

    def is_running(self):
        return self._running


_clock = RecordingClock()


# ---------------------------------------------------------------------------
# Session container
# ---------------------------------------------------------------------------

class RecordingSession(object):
    """A single in-memory recording session's bookkeeping. Nothing here is
    serialized to disk in Phase 2.2 -- see docs/Phase2_Architecture.md for
    the future .rm79/SQLite design this is intentionally compatible with.
    """

    def __init__(self):
        self.sequence = 0
        self.baseline = None
        self.started_wall = None     # time.time(), for human-readable display only
        self.stopped_wall = None
        self.final_elapsed = None    # frozen clock reading, set at Stop
        self.log = []                # bounded list of NORMALIZED event dicts (events.py)
        self.counters = dict((k, 0) for k in probe.COUNTER_KEYS)
        self.error = None            # last internal error, if any (diagnostics)

        # --- Phase 2.3A: normalized-event bookkeeping ---------------------
        self.coalescer = events.Coalescer()
        # Raw: every detector observation handed to _on_probe_event, before
        # coalescing. Stored: how many of those became their own normalized
        # log record (session.sequence advances exactly this many times).
        # Coalesced: raw observations that were merged into an existing
        # stored record instead of creating a new one.
        # raw_events_received == normalized_events_stored + coalesced_events.
        self.raw_events_received = 0
        self.normalized_events_stored = 0
        self.coalesced_events = 0
        self.last_event_type = None
        self.last_object_uid = None

    def append_event(self, entry):
        self.log.append(entry)
        if len(self.log) > SESSION_LOG_LIMIT:
            del self.log[0:len(self.log) - SESSION_LOG_LIMIT]


# ---------------------------------------------------------------------------
# Module state (one authoritative state value; one current/last session)
# ---------------------------------------------------------------------------

_state = STATE_STOPPED
_session = None   # current RecordingSession while RECORDING; last one otherwise
_last_message = "(no recording session yet)"

# Phase 2.3A session identity maps: transient obj/mesh pointer -> persistent
# identity.py UID. Rebuilt from the baseline at every Start; see the module
# docstring ("Session identity maps") for the full rationale, especially
# why this is required for OBJECT_DELETED to still carry a UID.
_object_uid_by_pointer = {}
_mesh_uid_by_pointer = {}


def get_state():
    return _state


def is_recording():
    return _state == STATE_RECORDING


def get_status():
    """Read-only, plain-data status for UI drawing / diagnostics."""
    probe_running = probe.is_running()
    if _session is None:
        return {
            'state': _state,
            'probe_running': probe_running,
            'elapsed': 0.0,
            'sequence': 0,
            'counters': dict((k, 0) for k in probe.COUNTER_KEYS),
            'log_count': 0,
            'last_message': _last_message,
            'started_wall': None,
            'stopped_wall': None,
            'raw_events': 0,
            'stored_events': 0,
            'coalesced_events': 0,
            'last_event_type': None,
            'last_object_uid': None,
        }
    elapsed = _session.final_elapsed if _session.final_elapsed is not None else _clock.elapsed()
    return {
        'state': _state,
        'probe_running': probe_running,
        'elapsed': elapsed,
        'sequence': _session.sequence,
        'counters': dict(_session.counters),
        'log_count': len(_session.log),
        'last_message': _last_message,
        'started_wall': _session.started_wall,
        'stopped_wall': _session.stopped_wall,
        'raw_events': _session.raw_events_received,
        'stored_events': _session.normalized_events_stored,
        'coalesced_events': _session.coalesced_events,
        'last_event_type': _session.last_event_type,
        'last_object_uid': _session.last_object_uid,
    }


# ---------------------------------------------------------------------------
# Baseline capture (reuses snapshot.py + identity.py; no new geometry code)
# ---------------------------------------------------------------------------

def _build_baseline():
    """Capture the scene state at the exact start of a recording session.

    Deliberately reuses snapshot.snapshot_scene() as-is for everything it
    already computes well (names, types, transforms, visibility,
    selection, mode, active object, materials, modifiers, and -- for mesh
    objects -- the same mesh signature Phase 1 already proved affordable:
    ~25ms/100k verts in Object Mode). This function does NOT recompute or
    re-implement any of that; it only adds a thin, cheap layer of
    persistent-identity lookups (plain property reads, not a new scan
    mechanism) on top, keyed the same way snapshot.py already keys
    objects (obj.as_pointer()) so the two can be cross-referenced.

    Called exactly ONCE per recording start -- never per scene update.
    """
    baseline = {
        'captured_wall': time.time(),
        'scenes': {},
    }
    for scn in bpy.data.scenes:
        try:
            snap = snapshot.snapshot_scene(scn)
        except Exception as exc:
            print(ADDON_TAG + ": baseline snapshot failed for scene '%s': %s" % (
                getattr(scn, 'name', '?'), exc))
            continue

        object_uids = {}
        mesh_uids = {}
        for obj in scn.objects:
            try:
                ptr = obj.as_pointer()
            except Exception:
                continue
            object_uids[ptr] = identity.get_object_uid(obj)

            if getattr(obj, 'type', None) == 'MESH' and obj.data is not None:
                try:
                    mesh_ptr = obj.data.as_pointer()
                except Exception:
                    mesh_ptr = None
                if mesh_ptr is not None:
                    mesh_uids[mesh_ptr] = identity.get_mesh_uid(obj.data)

        baseline['scenes'][scn.name] = {
            'snapshot': snap,
            'object_uids': object_uids,
            'mesh_uids': mesh_uids,
        }
    return baseline


# ---------------------------------------------------------------------------
# Identity-failure reporting
# ---------------------------------------------------------------------------

def _describe_identity_problems(report):
    parts = []
    if report.get('objects_missing_uid'):
        parts.append("%d object(s) missing a UID" % len(report['objects_missing_uid']))
    if report.get('objects_duplicate_uid'):
        parts.append("%d duplicate object UID(s)" % len(report['objects_duplicate_uid']))
    if report.get('meshes_missing_uid'):
        parts.append("%d mesh(es) missing a UID" % len(report['meshes_missing_uid']))
    if report.get('meshes_duplicate_uid'):
        parts.append("%d duplicate mesh UID(s)" % len(report['meshes_duplicate_uid']))
    if not parts:
        return "Identity validation failed for an unspecified reason."
    return "Identity validation failed: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Phase 2.3A: UID resolution (pointer map first, cheap name fallback second)
# ---------------------------------------------------------------------------

def _refresh_identity_maps_full():
    """Cheap, property-reads-only pass refreshing BOTH pointer->UID maps
    from the live bpy.data.objects/meshes. No geometry is touched.

    Only ever called right after identity.ensure_all_identities(), and
    only on OBJECT_CREATED events (see module docstring / Phase 2.3A
    performance requirements) -- never on every event.
    """
    for obj in bpy.data.objects:
        try:
            ptr = obj.as_pointer()
        except Exception:
            continue
        uid = identity.get_object_uid(obj)
        if uid is not None:
            _object_uid_by_pointer[ptr] = uid

    for mesh in bpy.data.meshes:
        try:
            ptr = mesh.as_pointer()
        except Exception:
            continue
        uid = identity.get_mesh_uid(mesh)
        if uid is not None:
            _mesh_uid_by_pointer[ptr] = uid


def _resolve_object_uid(pointer, name):
    """Resolution priority: (1) session pointer map -- O(1), works even
    after the object is deleted; (2) if the object still exists, a cheap
    direct name lookup + property read (also backfills the map). Returns
    None if neither resolves (expected for e.g. non-object events).
    """
    if pointer is not None and pointer in _object_uid_by_pointer:
        return _object_uid_by_pointer[pointer]
    if name:
        try:
            obj = bpy.data.objects.get(name)
        except Exception:
            obj = None
        if obj is not None:
            uid = identity.get_object_uid(obj)
            if uid is not None and pointer is not None:
                _object_uid_by_pointer[pointer] = uid
            return uid
    return None


def _resolve_mesh_uid(pointer, name):
    """Same priority order as _resolve_object_uid(), for Mesh datablocks."""
    if pointer is not None and pointer in _mesh_uid_by_pointer:
        return _mesh_uid_by_pointer[pointer]
    if name:
        try:
            mesh = bpy.data.meshes.get(name)
        except Exception:
            mesh = None
        if mesh is not None:
            uid = identity.get_mesh_uid(mesh)
            if uid is not None and pointer is not None:
                _mesh_uid_by_pointer[pointer] = uid
            return uid
    return None


# ---------------------------------------------------------------------------
# Event ingestion (connected to probe.py via the listener hook, not a
# second handler)
# ---------------------------------------------------------------------------

def _on_probe_event(scene, event):
    """Registered with probe.add_event_listener() only while RECORDING.

    Normalizes the raw probe.py/snapshot.py event into the Phase 2.3A
    event schema (events.py) and runs it through the session's
    conservative coalescer before deciding whether it becomes a new
    stored record or is merged into the currently-open one.

    Deliberately cheap for the common case: OBJECT_CREATED is the only
    event type allowed to run identity.ensure_all_identities() (an
    O(all objects/meshes), geometry-free repair scan); everything else
    resolves via an O(1) pointer-map lookup, with at most one cheap
    name-based fallback lookup on a miss. No scene scan, no mesh
    geometry access -- that work already happened once inside Phase 1's
    own detection pass before this callback runs at all.
    """
    if _state != STATE_RECORDING or _session is None:
        return
    try:
        _session.raw_events_received += 1

        etype = event.get('type')
        scene_name = getattr(scene, 'name', None)
        obj_name = event.get('object')
        mesh_name = event.get('mesh_name')
        object_pointer = event.get('object_pointer')
        mesh_pointer = event.get('mesh_pointer')
        payload = event.get('payload') or {}

        if etype == 'OBJECT_CREATED':
            # The one case allowed to perform a full, collision-aware
            # identity repair scan -- see module docstring. This is what
            # makes a freshly duplicated object (which starts out
            # holding a *colliding* copy of the original's UID) resolve
            # to its own, newly-repaired UID rather than the original's.
            try:
                identity.ensure_all_identities()
            except Exception as exc:
                print(ADDON_TAG + ": identity repair scan failed: %s" % exc)
            _refresh_identity_maps_full()

        object_uid = None
        if object_pointer is not None or obj_name:
            object_uid = _resolve_object_uid(object_pointer, obj_name)

        mesh_uid = None
        if mesh_pointer is not None or mesh_name:
            mesh_uid = _resolve_mesh_uid(mesh_pointer, mesh_name)

        t = _clock.elapsed()

        candidate = events.make_event(
            seq=None,
            t=t,
            scene_name=scene_name,
            etype=etype,
            object_uid=object_uid,
            mesh_uid=mesh_uid,
            object_name=obj_name,
            mesh_name=mesh_name,
            payload=payload,
        )

        stored, was_coalesced = _session.coalescer.offer(candidate)

        if was_coalesced:
            _session.coalesced_events += 1
        else:
            _session.sequence += 1
            stored['seq'] = _session.sequence
            _session.append_event(stored)
            _session.normalized_events_stored += 1

            counter_key = probe.EVENT_COUNTER_MAP.get(etype)
            if counter_key:
                _session.counters[counter_key] = _session.counters.get(counter_key, 0) + 1

        _session.last_event_type = etype
        _session.last_object_uid = object_uid

        # The object is gone -- stop remembering its pointer so it can
        # never be confused with an unrelated future datablock that
        # happens to get the same (now-freed) memory address. The mesh
        # entry is deliberately left alone (see module docstring).
        if etype == 'OBJECT_DELETED' and object_pointer is not None:
            _object_uid_by_pointer.pop(object_pointer, None)

    except Exception as exc:
        # Never let a bookkeeping error here propagate into probe.py's
        # detection loop (it is already guarded there too, belt-and-braces).
        _session.error = str(exc)
        print(ADDON_TAG + ": error recording event (session kept alive): %s" % exc)


# ---------------------------------------------------------------------------
# Lifecycle: start / stop
# ---------------------------------------------------------------------------

def start_recording():
    """STOPPED -> ARMING -> RECORDING (or back to STOPPED on failure).

    Returns (ok, message).
    """
    global _state, _session, _last_message

    if _state == STATE_RECORDING:
        _last_message = "Already recording."
        return False, _last_message
    if _state in (STATE_ARMING, STATE_STOPPING):
        _last_message = "Recording is currently %s; try again in a moment." % _state
        return False, _last_message

    _state = STATE_ARMING
    try:
        # Step 2+3: ensure and validate persistent identities (Phase 2.1).
        identity.ensure_all_identities()
        report = identity.validate_scene_identities()
        if not report.get('valid', False):
            _state = STATE_STOPPED
            _last_message = _describe_identity_problems(report)
            return False, _last_message

        # Step 5: baseline (reuses snapshot.py; one-time cost only).
        session = RecordingSession()
        session.baseline = _build_baseline()
        session.started_wall = time.time()

        # Phase 2.3A: (re)populate the session identity maps from the
        # baseline we just captured -- every object/mesh present right
        # now gets a known pointer->UID entry before a single event can
        # occur, which is what makes OBJECT_DELETED resolution reliable
        # even for objects that existed before recording started.
        _object_uid_by_pointer.clear()
        _mesh_uid_by_pointer.clear()
        for scene_data in session.baseline['scenes'].values():
            for ptr, uid in scene_data['object_uids'].items():
                if uid is not None:
                    _object_uid_by_pointer[ptr] = uid
            for ptr, uid in scene_data['mesh_uids'].items():
                if uid is not None:
                    _mesh_uid_by_pointer[ptr] = uid

        # Steps 6-8: sequence counter / clock / per-session stats are all
        # fresh because `session` is a brand-new RecordingSession object.
        _clock.start()

        # Make sure the detector is actually running -- recording without
        # an active probe would silently capture nothing (see module
        # docstring). probe.start() is a safe no-op if already running.
        probe.start()
        probe.add_event_listener(_on_probe_event)

        # Step 9+10: commit the new session and enter RECORDING.
        _session = session
        _state = STATE_RECORDING
        _last_message = "Recording started."
        return True, _last_message

    except Exception as exc:
        # Never remain stuck in ARMING.
        _state = STATE_STOPPED
        _last_message = "Failed to start recording: %s" % exc
        print(ADDON_TAG + ": " + _last_message)
        return False, _last_message


def stop_recording():
    """RECORDING -> STOPPING -> STOPPED.

    Returns (ok, message). Session data (baseline, log, counters) is kept
    available (in `_session`) after this returns -- it is only replaced
    by a subsequent start_recording() or explicitly cleared by clear_session().
    """
    global _state, _last_message

    if _state == STATE_STOPPED:
        _last_message = "Not recording."
        return False, _last_message
    if _state == STATE_ARMING:
        # Arming is synchronous in this implementation, so this should be
        # unreachable in practice, but handle it defensively rather than
        # leaving the state machine stuck.
        _state = STATE_STOPPED
        _last_message = "Recording was still arming; cancelled safely."
        return False, _last_message
    if _state == STATE_STOPPING:
        _last_message = "Already stopping."
        return False, _last_message

    _state = STATE_STOPPING
    try:
        probe.remove_event_listener(_on_probe_event)
        _clock.stop()
        if _session is not None:
            _session.stopped_wall = time.time()
            _session.final_elapsed = _clock.elapsed()
        _state = STATE_STOPPED
        _last_message = "Recording stopped."
        return True, _last_message
    except Exception as exc:
        # Guarantee we never remain falsely stuck in RECORDING/STOPPING.
        try:
            probe.remove_event_listener(_on_probe_event)
        except Exception:
            pass
        _state = STATE_STOPPED
        _last_message = "Error while stopping recording (state forced to STOPPED): %s" % exc
        print(ADDON_TAG + ": " + _last_message)
        return False, _last_message


def clear_session():
    """Explicitly discard the last completed session's statistics/log.
    Only valid while STOPPED -- never clears an active recording.
    """
    global _session, _last_message
    if _state != STATE_STOPPED:
        _last_message = "Cannot clear while %s." % _state
        return False, _last_message
    _session = None
    _last_message = "Session data cleared."
    return True, _last_message


def shutdown():
    """Called from __init__.unregister() (and defensively from register()).
    Forces the state machine back to a safe STOPPED state and detaches the
    event listener. Never touches bpy.context.
    """
    global _state
    try:
        probe.remove_event_listener(_on_probe_event)
    except Exception:
        pass
    try:
        _clock.stop()
    except Exception:
        pass
    _object_uid_by_pointer.clear()
    _mesh_uid_by_pointer.clear()
    _state = STATE_STOPPED


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class REPLAYPROBE79_OT_start_recording(bpy.types.Operator):
    bl_idname = "replayprobe79.start_recording"
    bl_label = "Start Recording"
    bl_description = "Start a Replay79 recording session"
    bl_options = {'REGISTER'}

    def execute(self, context):
        ok, message = start_recording()
        self.report({'INFO'} if ok else {'WARNING'}, message)
        return {'FINISHED'}


class REPLAYPROBE79_OT_stop_recording(bpy.types.Operator):
    bl_idname = "replayprobe79.stop_recording"
    bl_label = "Stop Recording"
    bl_description = "Stop the current Replay79 recording session"
    bl_options = {'REGISTER'}

    def execute(self, context):
        ok, message = stop_recording()
        self.report({'INFO'} if ok else {'WARNING'}, message)
        return {'FINISHED'}


class REPLAYPROBE79_OT_clear_recording(bpy.types.Operator):
    bl_idname = "replayprobe79.clear_recording"
    bl_label = "Clear Recording"
    bl_description = "Discard the last completed Replay79 recording session's statistics"
    bl_options = {'REGISTER'}

    def execute(self, context):
        ok, message = clear_session()
        self.report({'INFO'} if ok else {'WARNING'}, message)
        return {'FINISHED'}


RECORDING_CLASSES = (
    REPLAYPROBE79_OT_start_recording,
    REPLAYPROBE79_OT_stop_recording,
    REPLAYPROBE79_OT_clear_recording,
)
