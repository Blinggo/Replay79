# -*- coding: utf-8 -*-
"""
snapshot.py -- ReplayProbe79

Responsible for:
    * Building lightweight, read-only snapshots of scene/object/mesh state.
    * Comparing two snapshots and producing a list of "diagnostic events".

This module intentionally contains NO Blender handler/timer/operator
registration logic and NO global mutable probe state. It is pure
data-in / data-out so it can be unit-tested and reused later by the
real recorder.

Blender 2.79 API notes (see README.md for the full discussion):

    * Object.mode exists per-object in 2.79 and reflects OBJECT / EDIT /
      SCULPT / etc. for that object specifically.
    * While an object is in Edit Mode, bpy.types.Mesh data (obj.data)
      is NOT live-updated from the BMesh editing cage. We must call
      Object.update_from_editmode() before reading mesh.vertices /
      mesh.edges / mesh.polygons, or the probe will see stale geometry.
      update_from_editmode() is available in Blender 2.79 (it predates
      2.80's view-layer API).
    * mesh.vertices / mesh.edges support foreach_get() for fixed-size
      per-element data (co, vertices). mesh.polygons has variable-length
      vertex lists, so topology for polygons is reconstructed from
      mesh.loops (vertex_index) + mesh.polygons (loop_total), both of
      which DO support foreach_get().
    * We never rely on a simple sum/average of vertex coordinates --
      equal-and-opposite vertex movement would cancel out under a sum.
      Instead we CRC32 the raw packed float bytes of every vertex
      coordinate, which changes if ANY coordinate changes, regardless
      of whether other coordinates move in a way that would cancel a
      naive sum.
"""

import array
import zlib


# ---------------------------------------------------------------------------
# Mesh signature
# ---------------------------------------------------------------------------

def mesh_signature(mesh):
    """Build a lightweight signature dict describing a mesh datablock.

    Returns a dict with vertex/edge/polygon counts plus CRC32 based
    checksums for vertex coordinates and for topology (edge + polygon
    connectivity). Returns None if `mesh` is falsy.
    """
    if mesh is None:
        return None

    vcount = len(mesh.vertices)
    ecount = len(mesh.edges)
    pcount = len(mesh.polygons)

    # --- vertex coordinate checksum -------------------------------------
    # CRC32 over the raw packed float bytes. This is sensitive to ANY
    # change in ANY coordinate, including equal-and-opposite movement
    # of two different vertices (which a sum/average would miss).
    if vcount:
        co = array.array('f', [0.0]) * (vcount * 3)
        mesh.vertices.foreach_get('co', co)
        vert_checksum = zlib.crc32(co.tobytes()) & 0xffffffff
    else:
        vert_checksum = 0

    # --- edge connectivity checksum --------------------------------------
    if ecount:
        edge_verts = array.array('i', [0]) * (ecount * 2)
        mesh.edges.foreach_get('vertices', edge_verts)
        edge_checksum = zlib.crc32(edge_verts.tobytes()) & 0xffffffff
    else:
        edge_checksum = 0

    # --- polygon connectivity checksum ------------------------------------
    # Reconstruct from loops because polygons[i].vertices is variable
    # length and does not support foreach_get directly.
    loop_count = len(mesh.loops)
    if pcount and loop_count:
        loop_total = array.array('i', [0]) * pcount
        mesh.polygons.foreach_get('loop_total', loop_total)

        loop_vidx = array.array('i', [0]) * loop_count
        mesh.loops.foreach_get('vertex_index', loop_vidx)

        poly_checksum = zlib.crc32(loop_total.tobytes())
        poly_checksum = zlib.crc32(loop_vidx.tobytes(), poly_checksum) & 0xffffffff
    else:
        poly_checksum = 0

    # Combined topology identity: counts + edge + polygon checksums.
    # NOTE: this is ORDER-SENSITIVE. If Blender reorders vertices/edges
    # /polygons internally without an actual topological change, this
    # checksum can still change. That is a known, documented limitation
    # (see README "Known Limitations").
    topo_src = array.array('i', [vcount, ecount, pcount])
    topo_checksum = zlib.crc32(topo_src.tobytes())
    topo_checksum = zlib.crc32(
        array.array('I', [edge_checksum, poly_checksum]).tobytes(),
        topo_checksum
    ) & 0xffffffff

    return {
        'vcount': vcount,
        'ecount': ecount,
        'pcount': pcount,
        'vert_checksum': vert_checksum,
        'edge_checksum': edge_checksum,
        'poly_checksum': poly_checksum,
        'topo_checksum': topo_checksum,
    }


def _sync_edit_mesh_if_needed(obj):
    """If `obj` is a mesh currently in Edit Mode, pull the live BMesh
    editing data back into obj.data so mesh.vertices/edges/polygons
    reflect the in-progress edit.

    This is wrapped defensively: update_from_editmode() can only be
    called in contexts where it is valid, and we must never let a
    diagnostic probe raise an exception that reaches Blender's handler
    chain (that can disable the handler silently or spam the console).
    """
    try:
        if obj.type == 'MESH' and getattr(obj, 'mode', 'OBJECT') == 'EDIT':
            obj.update_from_editmode()
    except Exception:
        # Known limitation: in rare cases (no active 3D view / wrong
        # context) update_from_editmode() can fail. We simply fall back
        # to whatever is currently stored in obj.data.
        pass


# ---------------------------------------------------------------------------
# Object snapshot
# ---------------------------------------------------------------------------

def snapshot_object(obj):
    """Build a read-only snapshot dict describing a single object."""

    _sync_edit_mesh_if_needed(obj)

    try:
        loc = tuple(round(v, 6) for v in obj.location)
    except Exception:
        loc = None

    try:
        if obj.rotation_mode == 'QUATERNION':
            rot = tuple(round(v, 6) for v in obj.rotation_quaternion)
        elif obj.rotation_mode == 'AXIS_ANGLE':
            rot = tuple(round(v, 6) for v in obj.rotation_axis_angle)
        else:
            rot = tuple(round(v, 6) for v in obj.rotation_euler)
    except Exception:
        rot = None

    try:
        scale = tuple(round(v, 6) for v in obj.scale)
    except Exception:
        scale = None

    data = obj.data
    try:
        data_pointer = data.as_pointer() if data is not None else None
    except Exception:
        data_pointer = None
    data_name = getattr(data, 'name', None) if data is not None else None

    try:
        materials = tuple(
            (slot.material.name if slot.material else None)
            for slot in obj.material_slots
        )
    except Exception:
        materials = ()

    try:
        modifiers = tuple((m.name, m.type) for m in obj.modifiers)
    except Exception:
        modifiers = ()

    mesh_sig = None
    if obj.type == 'MESH' and data is not None:
        try:
            mesh_sig = mesh_signature(data)
        except Exception:
            mesh_sig = None

    try:
        mode = obj.mode
    except Exception:
        mode = 'UNKNOWN'

    try:
        hide = bool(obj.hide)
    except Exception:
        hide = False

    try:
        hide_render = bool(obj.hide_render)
    except Exception:
        hide_render = False

    try:
        select = bool(obj.select)
    except Exception:
        select = False

    try:
        pointer = obj.as_pointer()
    except Exception:
        pointer = id(obj)

    return {
        'pointer': pointer,
        'name': obj.name,
        'type': obj.type,
        'location': loc,
        'rotation': rot,
        'scale': scale,
        'hide': hide,
        'hide_render': hide_render,
        'select': select,
        'mode': mode,
        'data_pointer': data_pointer,
        'data_name': data_name,
        'materials': materials,
        'modifiers': modifiers,
        'mesh': mesh_sig,
    }


# ---------------------------------------------------------------------------
# Scene snapshot
# ---------------------------------------------------------------------------

def snapshot_scene(scene):
    """Build a snapshot dict for an entire scene.

    Keyed by the object's stable C-struct pointer (obj.as_pointer()),
    NOT by object name, because names can change (rename) and we must
    still be able to match "the same object" across two snapshots.
    """
    objects = {}
    for obj in scene.objects:
        try:
            rec = snapshot_object(obj)
        except Exception:
            # Never let one bad object abort the whole snapshot.
            continue
        objects[rec['pointer']] = rec

    active = scene.objects.active
    active_pointer = None
    active_name = None
    if active is not None:
        try:
            active_pointer = active.as_pointer()
            active_name = active.name
        except Exception:
            pass

    return {
        'scene_name': scene.name,
        'frame_current': scene.frame_current,
        'active_pointer': active_pointer,
        'active_name': active_name,
        'objects': objects,
    }


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def diff_snapshots(old, new):
    """Compare two scene snapshots and return a list of event dicts.

    Each event dict has at least: 'type', 'object'. It also carries (added
    for Phase 2.3A, see events.py / recording.py):

        'object_pointer' -- obj.as_pointer() of the affected object, or
                             None. SESSION-LOCAL / TRANSIENT ONLY -- this
                             is a lookup key for resolving a persistent
                             identity.py UID while the object still (or
                             very recently) existed; it is never meant to
                             be stored as a persistent identity itself.
                             For OBJECT_DELETED specifically, this is the
                             pointer the object had *before* it vanished,
                             which is exactly what lets a subscriber like
                             recording.py resolve "which UID was this"
                             from a pointer->UID map populated while the
                             object was still alive -- a name lookup
                             against bpy.data.objects would fail for a
                             deleted object, which is the whole reason
                             this field exists.
        'mesh_pointer'    -- same idea, for the affected Mesh datablock
                              (mesh.as_pointer()), or None for non-mesh
                              events/objects.
        'payload'         -- a small, type-specific plain dict containing
                              enough already-computed state (from the
                              snapshot records below) to build a useful
                              normalized event payload one layer up, in
                              events.py / recording.py. This deliberately
                              reuses data snapshot_object()/mesh_signature()
                              already computed -- nothing here re-reads
                              bpy or recomputes a mesh signature.

    It may also have 'detail' (human readable extra info, used only for
    probe.py's plain-text event log line) and 'mesh_name'.
    """
    events = []

    old_objects = old.get('objects', {})
    new_objects = new.get('objects', {})

    old_ptrs = set(old_objects.keys())
    new_ptrs = set(new_objects.keys())

    created = new_ptrs - old_ptrs
    deleted = old_ptrs - new_ptrs
    common = old_ptrs & new_ptrs

    for p in created:
        rec = new_objects[p]
        events.append({
            'type': 'OBJECT_CREATED',
            'object': rec['name'],
            'object_pointer': rec.get('pointer'),
            'mesh_name': rec.get('data_name'),
            'mesh_pointer': rec.get('data_pointer'),
            'payload': {
                'object_type': rec.get('type'),
                'name': rec.get('name'),
                'location': rec.get('location'),
                'rotation': rec.get('rotation'),
                'scale': rec.get('scale'),
                'hide': rec.get('hide'),
                'hide_render': rec.get('hide_render'),
                'select': rec.get('select'),
                'mode': rec.get('mode'),
                'data_name': rec.get('data_name'),
                'materials': rec.get('materials'),
                'modifiers': rec.get('modifiers'),
                'mesh_signature': rec.get('mesh'),
            },
        })

    for p in deleted:
        rec = old_objects[p]
        events.append({
            'type': 'OBJECT_DELETED',
            'object': rec['name'],
            'object_pointer': rec.get('pointer'),
            'mesh_name': rec.get('data_name'),
            'mesh_pointer': rec.get('data_pointer'),
            'payload': {
                'object_type': rec.get('type'),
                'old_name': rec.get('name'),
                'location': rec.get('location'),
                'rotation': rec.get('rotation'),
                'scale': rec.get('scale'),
                'hide': rec.get('hide'),
                'hide_render': rec.get('hide_render'),
                'data_name': rec.get('data_name'),
            },
        })

    for p in common:
        o = old_objects[p]
        n = new_objects[p]
        obj_ptr = n.get('pointer')
        mesh_ptr = n.get('data_pointer')

        if o['name'] != n['name']:
            events.append({
                'type': 'OBJECT_RENAMED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'detail': 'was ' + o['name'],
                'payload': {'old_name': o['name'], 'new_name': n['name']},
            })

        if o['location'] != n['location'] or o['rotation'] != n['rotation'] or o['scale'] != n['scale']:
            events.append({
                'type': 'TRANSFORM_CHANGED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'payload': {
                    'location': n['location'],
                    'rotation': n['rotation'],
                    'scale': n['scale'],
                },
            })

        if o['hide'] != n['hide'] or o['hide_render'] != n['hide_render']:
            events.append({
                'type': 'VISIBILITY_CHANGED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'payload': {'hide': n['hide'], 'hide_render': n['hide_render']},
            })

        if o['select'] != n['select']:
            events.append({
                'type': 'SELECTION_CHANGED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'payload': {'select': n['select']},
            })

        if o['mode'] != n['mode']:
            events.append({
                'type': 'MODE_CHANGED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'detail': o['mode'] + ' -> ' + n['mode'],
                'payload': {'old_mode': o['mode'], 'new_mode': n['mode']},
            })

        if o['data_pointer'] != n['data_pointer']:
            events.append({
                'type': 'OBJECT_DATA_CHANGED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'payload': {
                    'data_name': n.get('data_name'),
                    'mesh_signature': n.get('mesh'),
                },
            })

        if o['materials'] != n['materials']:
            events.append({
                'type': 'MATERIAL_CHANGED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'payload': {'materials': n['materials']},
            })

        if o['modifiers'] != n['modifiers']:
            events.append({
                'type': 'MODIFIER_CHANGED',
                'object': n['name'],
                'object_pointer': obj_ptr,
                'mesh_name': n.get('data_name'),
                'mesh_pointer': mesh_ptr,
                'payload': {'modifiers': n['modifiers']},
            })

        om = o.get('mesh')
        nm = n.get('mesh')
        if om is not None and nm is not None:
            if (om['vcount'] != nm['vcount'] or om['ecount'] != nm['ecount']
                    or om['pcount'] != nm['pcount']
                    or om['topo_checksum'] != nm['topo_checksum']):
                events.append({
                    'type': 'TOPOLOGY_CHANGED',
                    'object': n['name'],
                    'object_pointer': obj_ptr,
                    'mesh_name': n.get('data_name'),
                    'mesh_pointer': mesh_ptr,
                    'payload': {'mesh_signature': nm},
                })
            elif om['vert_checksum'] != nm['vert_checksum']:
                events.append({
                    'type': 'MESH_GEOMETRY_CHANGED',
                    'object': n['name'],
                    'object_pointer': obj_ptr,
                    'mesh_name': n.get('data_name'),
                    'mesh_pointer': mesh_ptr,
                    'payload': {'mesh_signature': nm},
                })

    if old.get('active_pointer') != new.get('active_pointer'):
        events.append({
            'type': 'ACTIVE_OBJECT_CHANGED',
            'object': new.get('active_name') or '(none)',
            'object_pointer': new.get('active_pointer'),
            'mesh_name': None,
            'mesh_pointer': None,
            'payload': {'active_object_name': new.get('active_name')},
        })

    return events
