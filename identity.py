# -*- coding: utf-8 -*-
"""
identity.py -- Replay79 Phase 2.1: Persistent Object/Mesh Identity Manager

Responsible for:
    * Generating persistent UUIDs for Objects and Mesh datablocks.
    * Assigning those UUIDs via Blender custom ID properties so they
      survive rename and .blend save/reload.
    * Detecting and repairing UUID collisions caused by Blender copy
      operations (obj.copy() / mesh.copy() duplicate custom properties,
      including our own identity property -- see "Why as_pointer() is
      not identity" below).
    * Keeping Object identity and Mesh identity strictly separate, while
      correctly allowing multiple Objects to legitimately share one Mesh
      datablock.

This module does NOT do any of the following (explicitly out of scope
for Phase 2.1 -- see Phase2_Architecture.md for where they belong):
    * SQLite / .rm79 files / event recording / checkpoints / deltas.
    * Mesh geometry analysis of any kind (no vertex arrays, no CRCs, no
      topology signatures). Identity checking only ever reads/writes two
      small custom ID properties -- it must stay orders of magnitude
      cheaper than snapshot.py's mesh signature work.
    * Replay reconstruction, cameras, or FFmpeg.

-------------------------------------------------------------------------
Why as_pointer() is NOT used as persistent identity
-------------------------------------------------------------------------
Phase 1 (snapshot.py) uses `obj.as_pointer()` / `mesh.as_pointer()` as a
*temporary, in-memory* diagnostic key -- it is the memory address of the
underlying C struct for the current Blender process only. It is not
written anywhere, does not survive object deletion (the address can be
reused by a later, unrelated datablock), and has no meaning after a
save/reload or in a different Blender session. That is fine for a
live diagnostic counter, but it is unusable as a persistent identity for
a recording system that must survive hours of editing, Pause/Resume, and
Blender restarts.

This module uses Blender custom ID properties instead (`obj["replay79_uid"]`,
`mesh["replay79_mesh_uid"]`), which are ordinary datablock data: they are
saved into the .blend file and survive rename/reload like any other
property. `as_pointer()` is still used here, but ONLY as a transient,
call-local dictionary/set key during a single scan (to tell "is this the
same underlying datablock I already saw in this pass" when a Python RNA
wrapper's `id()` is not guaranteed stable across separate attribute
accesses in Blender's API) -- never stored, never treated as identity.

-------------------------------------------------------------------------
The duplicate-property collision problem
-------------------------------------------------------------------------
Blender's `obj.copy()` / `mesh.copy()` copy ALL custom properties,
including ours. So immediately after:

    dup = obj.copy()

both `obj` and `dup` report the SAME `replay79_uid` value. The mere
presence of a `replay79_uid` property is therefore NOT sufficient
evidence of a valid, unique identity -- it must be cross-checked against
every other datablock's property to detect collisions. This module does
that via a registry built during each scan (see `_scan_and_repair`).

Collision repair policy (documented, as required; revised after the P2.1
Test 4 bug fix): bpy.data iteration order is NOT used to decide who keeps
a colliding UID -- real Blender 2.79b testing showed a freshly duplicated
object can be visited BEFORE the object it was duplicated from, which
would incorrectly repair the original instead of the duplicate if order
were the deciding factor. Instead, this module keeps a small, in-memory,
session-scoped "ownership" registry (`_object_uid_owner` /
`_mesh_uid_owner`, see `_scan_and_repair`) that persists ACROSS calls for
the lifetime of the running Blender process. The first time a UID is ever
seen as unique, its owning datablock's pointer is recorded and that
record sticks. On every later scan, whichever datablock still matches the
recorded pointer keeps the UID; any OTHER datablock presenting the same
UID is the interloper and is repaired -- independent of iteration order,
because the decision is "does this object match who we already know owns
this UID", not "who did we see first in this particular scan". The only
case this cannot help with is a UID collision that already exists the
very first time Replay79 ever scans a file (no ownership has been
recorded yet for either side); there, no historical information exists
to prefer one over the other, so the first one encountered in that scan
keeps the UID and the second is repaired -- a documented, accepted
limitation, not a claimed general solution.
"""

import uuid

import bpy


OBJECT_UID_KEY = "replay79_uid"
MESH_UID_KEY = "replay79_mesh_uid"


# ---------------------------------------------------------------------------
# Session-scoped ownership registries (P2.1 Test 4 bug fix)
# ---------------------------------------------------------------------------
# uid -> pointer (as_pointer(), transient) of the datablock Replay79 has
# already confirmed owns that uid. These intentionally persist ACROSS calls
# to _scan_and_repair() (unlike everything else in this module, which reads
# live bpy state fresh every call) -- this sticky memory is the only way to
# correctly tell "an object we already established" apart from "a newly
# introduced duplicate" once a collision scan runs again later, regardless
# of what order bpy.data happens to iterate in during that later scan.
#
# This is deliberately NOT persisted to the .blend file and NOT treated as
# identity itself -- it resets naturally on Blender restart / addon module
# reload, which is correct: the .blend custom properties remain the only
# persistent identity (Section 12 of the Phase 2 architecture doc).
_object_uid_owner = {}   # uid -> object pointer
_mesh_uid_owner = {}     # uid -> mesh pointer


def _reset_session_identity_cache():
    """Clear the in-memory ownership registries. Not part of the public
    Replay79 identity API -- intended for test harnesses that simulate
    multiple separate Blender sessions in one process, and for addon
    disable/enable cycles if a future caller wants a clean slate.
    """
    _object_uid_owner.clear()
    _mesh_uid_owner.clear()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _generate_uid():
    """Generate a new persistent UID string."""
    return str(uuid.uuid4())


def _looks_like_uid(value):
    """Return True if `value` is a syntactically valid UID string.

    Guards against the Blender 2.79 reality that a custom property can
    hold almost any value (a user could have typed a number, or an old/
    unrelated string, into a property with this name). An invalid value
    is treated the same as "missing" -- a fresh UID is generated for it.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _safe_get_pointer(datablock):
    """Best-effort, failure-safe as_pointer() for transient scan bookkeeping
    only (see module docstring). Never used as persistent identity.
    """
    try:
        return datablock.as_pointer()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Object identity
# ---------------------------------------------------------------------------

def get_object_uid(obj):
    """Return the existing, valid Replay79 UID for `obj`, or None.

    Read-only. Never assigns or modifies anything.
    """
    if obj is None:
        return None
    try:
        value = obj.get(OBJECT_UID_KEY)
    except Exception:
        return None
    return value if _looks_like_uid(value) else None


def ensure_object_uid(obj):
    """Return a valid Replay79 UID for `obj`, assigning a new one only if
    none exists yet. Does NOT perform collision detection by itself --
    that requires comparing against other objects, which is the job of
    ensure_scene_identities()/ensure_all_identities(). Calling this alone
    on a single object is safe and idempotent, but cannot discover that
    the returned UID is a duplicate of another object's UID (e.g. right
    after obj.copy()).
    """
    if obj is None:
        return None
    existing = get_object_uid(obj)
    if existing is not None:
        return existing
    new_uid = _generate_uid()
    try:
        obj[OBJECT_UID_KEY] = new_uid
    except Exception:
        # Fail safe: e.g. linked/library object that can't be written to.
        return None
    return new_uid


def repair_object_uid_collision(obj):
    """Force-assign a brand-new UID to `obj`, unconditionally overwriting
    whatever (colliding) value is currently stored. Only call this once a
    collision has actually been confirmed against a registry -- it does
    not check anything itself.
    """
    if obj is None:
        return None
    new_uid = _generate_uid()
    try:
        obj[OBJECT_UID_KEY] = new_uid
    except Exception:
        return None
    return new_uid


# ---------------------------------------------------------------------------
# Mesh identity
# ---------------------------------------------------------------------------

def get_mesh_uid(mesh):
    """Return the existing, valid Replay79 UID for `mesh`, or None.
    Read-only. Never assigns or modifies anything.
    """
    if mesh is None:
        return None
    try:
        value = mesh.get(MESH_UID_KEY)
    except Exception:
        return None
    return value if _looks_like_uid(value) else None


def ensure_mesh_uid(mesh):
    """Return a valid Replay79 UID for `mesh`, assigning a new one only if
    none exists yet. Same caveat as ensure_object_uid(): no collision
    detection in isolation.
    """
    if mesh is None:
        return None
    existing = get_mesh_uid(mesh)
    if existing is not None:
        return existing
    new_uid = _generate_uid()
    try:
        mesh[MESH_UID_KEY] = new_uid
    except Exception:
        return None
    return new_uid


def repair_mesh_uid_collision(mesh):
    """Force-assign a brand-new UID to `mesh`, unconditionally overwriting
    the current (colliding) value. Only call after a collision has been
    confirmed against a registry.
    """
    if mesh is None:
        return None
    new_uid = _generate_uid()
    try:
        mesh[MESH_UID_KEY] = new_uid
    except Exception:
        return None
    return new_uid


# ---------------------------------------------------------------------------
# Scan / ensure / repair (the actual collision-aware entry points)
# ---------------------------------------------------------------------------

def _is_mesh_object(obj):
    try:
        return getattr(obj, 'type', None) == 'MESH' and obj.data is not None
    except Exception:
        return False


def _scan_and_repair(object_iterable, mesh_iterable):
    """Core worker shared by ensure_all_identities()/ensure_scene_identities().

    Collision ownership is tracked via the module-level, session-persistent
    `_object_uid_owner` / `_mesh_uid_owner` registries (see their
    definitions above) rather than a registry rebuilt from scratch on
    every call -- a purely per-call registry cannot distinguish "an object
    we already established earlier" from "a newly introduced duplicate"
    when both are visited in whatever order bpy.data happens to iterate in
    THIS call (this was the P2.1 Test 4 bug: a duplicate visited before
    its original would incorrectly keep the UID). Performs ONLY
    custom-property reads and writes; never touches geometry, transforms,
    materials, modifiers, selection, mode, or scene structure (Section 13
    requirement).

    Returns a small stats dict for logging/testing purposes.
    """
    stats = {
        'objects_scanned': 0,
        'objects_uid_assigned': 0,
        'objects_uid_repaired': 0,
        'meshes_scanned': 0,
        'meshes_uid_assigned': 0,
        'meshes_uid_repaired': 0,
        'errors': 0,
    }

    processed_mesh_pointers = set()

    # --- Pass 1: objects (and, in turn, the meshes they reference) -------
    for obj in object_iterable:
        if obj is None:
            continue
        try:
            stats['objects_scanned'] += 1
            had_uid = get_object_uid(obj) is not None
            uid = ensure_object_uid(obj)
            if uid is None:
                # Could not read/write this object (e.g. linked/library
                # data). Skip it silently rather than raising.
                pass
            else:
                if not had_uid:
                    stats['objects_uid_assigned'] += 1

                obj_ptr = _safe_get_pointer(obj)
                owner_ptr = _object_uid_owner.get(uid)
                if owner_ptr is not None and owner_ptr != obj_ptr:
                    # Duplicate UID on a DIFFERENT object -> collision.
                    uid = repair_object_uid_collision(obj)
                    stats['objects_uid_repaired'] += 1
                    obj_ptr = _safe_get_pointer(obj)
                if uid is not None:
                    _object_uid_owner[uid] = obj_ptr
        except Exception:
            stats['errors'] += 1

        # Mesh identity for this object's data, if it is a mesh object.
        try:
            if not _is_mesh_object(obj):
                continue
            mesh = obj.data
            mesh_ptr = _safe_get_pointer(mesh)
            if mesh_ptr is not None and mesh_ptr in processed_mesh_pointers:
                # Already handled this exact mesh datablock via another
                # object (legitimate shared-mesh case) -- do not touch it
                # again, and do NOT treat this as a new identity.
                continue

            stats['meshes_scanned'] += 1
            had_muid = get_mesh_uid(mesh) is not None
            muid = ensure_mesh_uid(mesh)
            if muid is not None:
                if not had_muid:
                    stats['meshes_uid_assigned'] += 1

                owner_ptr = _mesh_uid_owner.get(muid)
                if owner_ptr is not None and owner_ptr != mesh_ptr:
                    muid = repair_mesh_uid_collision(mesh)
                    stats['meshes_uid_repaired'] += 1
                    mesh_ptr = _safe_get_pointer(mesh)
                if muid is not None:
                    _mesh_uid_owner[muid] = mesh_ptr

            if mesh_ptr is not None:
                processed_mesh_pointers.add(mesh_ptr)
        except Exception:
            stats['errors'] += 1

    # --- Pass 2: orphan meshes (0 users / not referenced by any scanned
    # object right now, e.g. briefly after unlinking). Still identified
    # so they keep a stable UID if they become referenced again later. ---
    if mesh_iterable is not None:
        for mesh in mesh_iterable:
            if mesh is None:
                continue
            try:
                mesh_ptr = _safe_get_pointer(mesh)
                if mesh_ptr is not None and mesh_ptr in processed_mesh_pointers:
                    continue

                stats['meshes_scanned'] += 1
                had_muid = get_mesh_uid(mesh) is not None
                muid = ensure_mesh_uid(mesh)
                if muid is not None:
                    if not had_muid:
                        stats['meshes_uid_assigned'] += 1

                    owner_ptr = _mesh_uid_owner.get(muid)
                    if owner_ptr is not None and owner_ptr != mesh_ptr:
                        muid = repair_mesh_uid_collision(mesh)
                        stats['meshes_uid_repaired'] += 1
                        mesh_ptr = _safe_get_pointer(mesh)
                    if muid is not None:
                        _mesh_uid_owner[muid] = mesh_ptr

                if mesh_ptr is not None:
                    processed_mesh_pointers.add(mesh_ptr)
            except Exception:
                stats['errors'] += 1

    return stats


def ensure_all_identities():
    """Ensure persistent, collision-free Object and Mesh UIDs for the
    ENTIRE current .blend file (bpy.data.objects / bpy.data.meshes),
    independent of scenes.

    This is the recommended primary entry point: Objects and Meshes are
    Blender-global datablocks, so a UID collision between two objects
    that happen to live in different scenes can only be caught if both
    are checked against the same registry in the same call.
    """
    return _scan_and_repair(bpy.data.objects, bpy.data.meshes)


def ensure_scene_identities(scene=None):
    """Ensure persistent, collision-free Object and Mesh UIDs, scoped by
    API signature to a `scene` (as requested), but -- deliberately --
    NOT scoped internally to that scene's objects.

    Rationale (Section 10 requirement): Objects and Mesh datablocks are
    global, not scene-local. If this function only scanned `scene.objects`,
    calling it once for Scene A and later for Scene B would use two
    separate, short-lived registries and could miss a collision between
    an object in A and an unrelated object in B. To guarantee correct
    collision detection across any number of scenes, this function
    delegates to the same whole-file scan as ensure_all_identities().

    The `scene` argument is accepted (and safely ignored if None) purely
    to match the requested call-site signature and to leave room for a
    future, explicitly scene-restricted variant if one ever proves
    necessary -- it is not required for correctness today.
    """
    return ensure_all_identities()


def validate_scene_identities(scene=None):
    """Read-only diagnostic: report on the current state of Object/Mesh
    identities WITHOUT assigning or repairing anything.

    Useful for tests and for a future UI indicator ("N objects missing a
    Replay79 UID", "N duplicate UIDs detected") without side effects.
    Like ensure_scene_identities(), this inspects the whole file
    (bpy.data.objects / bpy.data.meshes) for the reasons given above;
    `scene` is accepted but not used to restrict scope.
    """
    report = {
        'objects_total': 0,
        'objects_missing_uid': [],
        'objects_duplicate_uid': {},   # uid -> [object names]
        'meshes_total': 0,
        'meshes_missing_uid': [],
        'meshes_duplicate_uid': {},    # uid -> [mesh names]
        'valid': True,
    }

    obj_uid_to_names = {}
    for obj in bpy.data.objects:
        if obj is None:
            continue
        try:
            report['objects_total'] += 1
            uid = get_object_uid(obj)
            if uid is None:
                report['objects_missing_uid'].append(obj.name)
                continue
            obj_uid_to_names.setdefault(uid, []).append(obj.name)
        except Exception:
            continue

    mesh_uid_to_names = {}
    for mesh in bpy.data.meshes:
        if mesh is None:
            continue
        try:
            report['meshes_total'] += 1
            uid = get_mesh_uid(mesh)
            if uid is None:
                report['meshes_missing_uid'].append(mesh.name)
                continue
            mesh_uid_to_names.setdefault(uid, []).append(mesh.name)
        except Exception:
            continue

    for uid, names in obj_uid_to_names.items():
        if len(names) > 1:
            report['objects_duplicate_uid'][uid] = names

    for uid, names in mesh_uid_to_names.items():
        if len(names) > 1:
            report['meshes_duplicate_uid'][uid] = names

    report['valid'] = (
        not report['objects_missing_uid']
        and not report['objects_duplicate_uid']
        and not report['meshes_missing_uid']
        and not report['meshes_duplicate_uid']
    )
    return report
