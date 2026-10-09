# -*- coding: utf-8 -*-
"""
events.py -- Replay79 Phase 2.3A: Normalized Event Model

Responsible for:
    * The normalized, plain-Python-data event schema (see NORMALIZED
      EVENT SCHEMA below) that recording.py's in-memory session log
      stores.
    * A small, conservative, pure-Python coalescer that merges a short
      run of consecutive TRANSFORM_CHANGED observations for the same
      object into a single stored event (see Coalescer).

Responsible for NOT doing (explicitly out of scope for Phase 2.3A -- see
docs/Phase2_Architecture.md):
    * SQLite / .rm79 persistence -- the normalized event dict produced
      here is *suitable* for future serialization (plain data only, no
      bpy/RNA references) but nothing in this module writes to disk.
    * Any Blender handler/operator/UI code, and no bpy import at all --
      this module is pure data-in/data-out so it can be unit-tested
      without Blender, exactly like snapshot.py.
    * UID resolution -- turning a transient obj.as_pointer()/mesh pointer
      into a persistent identity.py UID is recording.py's job (it needs
      bpy.data access that this module deliberately avoids). By the time
      an event reaches this module, object_uid/mesh_uid are already
      either resolved strings or None.
    * Replay reconstruction, VirtualScene, mesh delta/topology encoding,
      cameras, FFmpeg.

-------------------------------------------------------------------------
NORMALIZED EVENT SCHEMA (schema version 1)
-------------------------------------------------------------------------
Every normalized event is a plain dict (never a bpy/RNA object, never a
custom class with un-picklable internals) with at least these top-level
keys:

    schema         : int, EVENT_SCHEMA_VERSION
    seq            : int, strictly increasing within one recording
                     session, identifying this STORED record (not every
                     raw detector observation -- see Coalescer)
    t              : float, recording-relative ACTIVE time in seconds
                     (from recording.py's RecordingClock, i.e. excludes
                     time before Start and any future paused time)
    scene_name     : str or None
    type           : str, one of the Phase 1 event-type constants (e.g.
                     "TRANSFORM_CHANGED")
    object_uid     : str or None -- identity.py persistent Object UID
    mesh_uid       : str or None -- identity.py persistent Mesh UID
    object_name    : str or None -- descriptive only, NOT identity
    mesh_name      : str or None -- descriptive only, NOT identity
    payload        : dict, type-specific (see snapshot.diff_snapshots()
                     for what each event type's payload contains)

Plus one additive field, not part of the minimum set above but always
present for schema consistency:

    sample_count   : int, >= 1. How many raw detector observations this
                     one stored record represents. Always 1 unless the
                     coalescer merged several consecutive TRANSFORM_CHANGED
                     observations into it (see Coalescer).

Names are explicitly DESCRIPTIVE METADATA ONLY -- never used as the
primary identity. `object_uid`/`mesh_uid` are the only fields a future
reconstruction layer should rely on to mean "the same thing across time".
"""

EVENT_SCHEMA_VERSION = 1

# How close in time (recording-relative seconds) two consecutive
# TRANSFORM_CHANGED observations for the SAME object must be for the
# second one to be merged into the first, rather than stored as its own
# record. Deliberately small and conservative -- see Coalescer docstring.
TRANSFORM_COALESCE_WINDOW = 0.25

# Event types eligible for coalescing at all. Everything else is always
# stored one-for-one, per the Phase 2.3A spec's explicit "do not coalesce"
# list (creation/deletion/rename/mesh-topology/material/modifier/mode/
# selection/visibility/active-object changes).
COALESCABLE_TYPES = frozenset(('TRANSFORM_CHANGED',))


def make_event(seq, t, scene_name, etype, object_uid, mesh_uid,
               object_name, mesh_name, payload):
    """Build one normalized event dict. `seq` may be None when the event
    is still a *candidate* that hasn't been decided yet to be a new
    stored record (see Coalescer.offer()) -- the caller is responsible
    for assigning the real sequence number once that is known.

    Always returns a fresh, plain-data dict (own copy of `payload`) --
    never a reference to any Blender/RNA object.
    """
    return {
        'schema': EVENT_SCHEMA_VERSION,
        'seq': seq,
        't': t,
        'scene_name': scene_name,
        'type': etype,
        'object_uid': object_uid,
        'mesh_uid': mesh_uid,
        'object_name': object_name,
        'mesh_name': mesh_name,
        'payload': dict(payload) if payload else {},
        'sample_count': 1,
    }


REQUIRED_FIELDS = (
    'schema', 'seq', 't', 'scene_name', 'type', 'object_uid', 'mesh_uid',
    'object_name', 'mesh_name', 'payload',
)


def validate_event_shape(event):
    """Return (ok, missing_fields) -- a cheap structural check used by
    the test harness (and safe to use defensively elsewhere). Does not
    check value types beyond "payload must be a dict".
    """
    missing = [k for k in REQUIRED_FIELDS if k not in event]
    if 'payload' in event and not isinstance(event['payload'], dict):
        missing.append('payload(not a dict)')
    return (len(missing) == 0, missing)


class Coalescer(object):
    """Conservative, per-(object_uid, type) coalescing of consecutive
    TRANSFORM_CHANGED observations.

    Policy (deliberately simple -- see Phase2_Architecture.md "P2.3A"
    addendum for the full rationale):

      * Only TRANSFORM_CHANGED is ever coalesced. Every other event type
        is always stored one-for-one (OBJECT_CREATED/DELETED/RENAMED,
        mesh topology/geometry changes, material/modifier/mode/
        selection/visibility/active-object changes).
      * A new TRANSFORM_CHANGED observation for object_uid U merges into
        the currently "open" stored TRANSFORM_CHANGED record for U only
        if the previous observation for U (not necessarily the session's
        last event overall) happened within TRANSFORM_COALESCE_WINDOW
        seconds. This is a SLIDING window -- a continuous drag keeps
        extending the same open record for as long as consecutive
        detector ticks stay close together, even if the total span of
        the merged record exceeds the window.
      * Any OTHER event type observed for the same object_uid closes
        that object's open TRANSFORM_CHANGED record -- a later, separate
        transform starts a brand-new record rather than silently
        reopening an old one.
      * When merging: the record's ORIGINAL 't' (timestamp) and 'seq'
        are preserved; only 'payload' is replaced with the newest
        transform values, and 'sample_count' is incremented.

    This is intentionally a *safe first policy*, not an attempt to
    minimize stored event count aggressively -- no create/delete
    cancellation, no topology coalescing, no mesh delta compression.
    """

    def __init__(self, window=TRANSFORM_COALESCE_WINDOW):
        self.window = window
        self._open = {}   # (object_uid, type) -> {'event': dict, 'last_t': float}

    def reset(self):
        self._open = {}

    def offer(self, event):
        """Given a freshly built *candidate* normalized event (seq may
        still be None), decide whether it merges into a still-open
        coalescing group.

        Returns (stored_event, was_coalesced):
            stored_event  -- either `event` itself (a genuinely new
                              record the caller must assign a seq to and
                              append to its log), or the pre-existing
                              event dict that was just mutated in place
                              (the caller must NOT append a new log
                              entry or allocate a new seq for it).
            was_coalesced -- True exactly when `stored_event is not event`.
        """
        etype = event.get('type')
        uid = event.get('object_uid')

        if etype not in COALESCABLE_TYPES or uid is None:
            # Not a coalescable type (or no UID to key on). Also closes
            # any open TRANSFORM_CHANGED group this object had, since a
            # different kind of change just happened to it.
            self._close_transform_group(uid)
            return event, False

        key = (uid, etype)
        group = self._open.get(key)
        if group is not None and (event['t'] - group['last_t']) <= self.window:
            stored = group['event']
            stored['payload'] = event['payload']
            stored['sample_count'] = stored.get('sample_count', 1) + 1
            group['last_t'] = event['t']
            return stored, True

        # No open group (or it lapsed) -- this observation starts a new one.
        self._open[key] = {'event': event, 'last_t': event['t']}
        return event, False

    def _close_transform_group(self, uid):
        if uid is None:
            return
        key = (uid, 'TRANSFORM_CHANGED')
        if key in self._open:
            del self._open[key]
