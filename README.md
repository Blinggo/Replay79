## Addendum: Phase 2.2 — Recording State Machine (`recording.py`)

A new module, `recording.py`, adds a minimal **Start/Stop recording
lifecycle** around Phase 1's existing detector, now registered and wired
into `__init__.py` and `ui.py`. **This is not the full future recorder**
(no `.rm79` file, no SQLite, no persisted events, no replay — see
`docs/Phase2_Architecture.md`): it only tracks, in memory, "is a
recording session currently active, and what has it observed so far."

**State machine:** one authoritative state value —
`STOPPED -> ARMING -> RECORDING -> STOPPING -> STOPPED` — never scattered
booleans. **Start Recording** calls `identity.ensure_all_identities()`
then `identity.validate_scene_identities()`; if identities are invalid it
reports why and returns safely to `STOPPED` without ever claiming a
recording started. On success it captures a one-time, in-memory baseline
(reusing `snapshot.snapshot_scene()` plus a cheap persistent-UID lookup
per object/mesh via `identity.py` — no new geometry-scanning code), resets
the session's sequence counter/clock/statistics, makes sure Phase 1's
probe is actually running (`probe.start()`, a no-op if already running —
recording without an active detector would silently capture nothing),
and subscribes to Phase 1's events via a new generic
`probe.add_event_listener()` hook (**not** a second `scene_update_post`
handler). **Stop Recording** unsubscribes, freezes the recording clock,
and keeps the finished session's statistics/log available for diagnostics
until the next recording starts or "Clear Last Session" is pressed.

**Recording Time** is measured by a small `RecordingClock` abstraction
using `time.monotonic()` (not `bpy.app.timers`, which is 2.80+, and not
raw `time.time()` calls scattered around, which could jump if the system
clock changes) — `recording_time = now - start_time`, frozen once
stopped. It already has working (if currently unused) `pause()`/`resume()`
methods so a future `PAUSED` state can be added without reworking
time-keeping.

**"Probe: Monitoring" and "Recording: STOPPED" are independent, and both
valid at once** — the UI panel deliberately shows them as two separate
boxes. A known, accepted limitation: if you manually press "Stop Probe"
while a recording session is `RECORDING`, the session silently stops
receiving new events (nothing feeds it anymore) but its state does not
automatically change; the UI's probe/recording split is intended to make
that situation visible rather than hidden.

Explicitly **not** implemented in P2.2 (future work, see
`docs/Phase2_Architecture.md`): SQLite/`.rm79` persistence, mesh delta or
topology encoding, checkpoints, replay reconstruction, camera, FFmpeg.

See `recording.py`'s module docstring for full design rationale.

---

## Addendum: Phase 2.1 — Persistent Identity Manager (`identity.py`)

A new, self-contained module, `identity.py`, was added alongside the
Phase 1 files. It introduces no new classes to register and changes no
Phase 1 behavior. **As of Phase 2.2 it is actively consumed**: every
Start Recording call runs `identity.ensure_all_identities()` and
`identity.validate_scene_identities()` (see the addendum above), and
every in-memory recording event attaches the affected object's/mesh's
persistent UID where available.

**What it does:** assigns a persistent UUID to every Blender `Object`
(`obj["replay79_uid"]`) and every `Mesh` datablock
(`mesh["replay79_mesh_uid"]`) using ordinary custom ID properties, so the
IDs are saved into the `.blend` file and survive rename and reload — unlike
Phase 1's `obj.as_pointer()`, which is only a valid identity for the
lifetime of the current Blender process and is **not** used for anything
persistent.

**Why this is needed:** Blender's `obj.copy()` / `mesh.copy()` duplicate
*all* custom properties, including our own identity property. Immediately
after a duplicate/copy, both the original and the copy report the same
UID — so merely having a `replay79_uid` is not proof of a valid, unique
identity. `identity.py` builds a whole-file registry of currently-known
UIDs on every scan and repairs collisions: the first datablock encountered
keeps its UID, any later datablock found holding the same UID is
reassigned a fresh one. Existing, non-colliding UIDs are always preserved
exactly as-is (the manager is idempotent).

**Shared mesh datablocks are explicitly not a collision.** If two
different Objects legitimately reference the *same* Mesh datablock (e.g. a
linked duplicate), that one Mesh keeps exactly one `replay79_mesh_uid`,
while the two Objects still get two different `replay79_uid` values. The
manager distinguishes "two objects, one shared mesh" (valid) from "two
different meshes that happen to hold the same UID string because one was
copied from the other" (a collision to repair) by checking datablock
identity, not just the property value.

See `identity.py`'s module docstring and `Phase2_Architecture.md` §3 for
further detail, and the end of this file is unaffected — Phase 1's
diagnostic probe, counters, and UI are unchanged by this addition.

---

# ReplayProbe79 — Phase 1 Diagnostic Probe

Part of the long-term **ReplayMod79** project: a Blender addon, conceptually
similar to Minecraft's Replay Mod, that will eventually let you record a
modelling session and later scrub/replay it with an independent cinematic
camera and render it out via FFmpeg.

**This addon is NOT that recorder.** This is Phase 1: a diagnostic probe
whose only job is to experimentally determine which Blender 2.79 scene/
object/mesh state changes can be reliably observed from Python, and by
which mechanism. Nothing here writes a replay file, plays anything back,
or touches rendering.

It never records mouse movement, keyboard input, or raw UI events. It only
observes **data state** (object transforms, mesh geometry/topology,
materials, modifiers, mode, selection, visibility) before and after
Blender tells us something happened.

---

## 1. Compatibility

- Target: **Blender 2.79b** exactly.
- Python: the Python 3.5.x interpreter bundled with 2.79b. No f-strings,
  no `pathlib`-only idioms, no 2.80+ API (`bpy.app.timers`,
  `view_layer`, `obj.select_get()/select_set()`, `depsgraph_update_post`,
  etc. are deliberately **not used**).
- Uses only 2.79-era APIs: `obj.select`, `obj.hide`, `scene.objects.active`,
  `bpy.app.handlers.scene_update_post` / `scene_update_pre`,
  `Object.update_from_editmode()`, `wm.event_timer_add()` + modal
  operators (there is no `bpy.app.timers` in 2.79 — that API was added in
  2.80).

---

## 2. Folder structure

```
ReplayProbe79/
    __init__.py     addon registration only (bl_info, register/unregister)
    probe.py        monitoring lifecycle, handlers, timer/modal operator,
                     counters, bounded event log
    snapshot.py      pure-data scene/object/mesh snapshotting + diffing
                     (no bpy.context dependency, no Blender handler code)
    identity.py      (Phase 2.1) persistent object/mesh UUIDs via custom
                     ID properties, collision detection/repair
    recording.py     (Phase 2.2) Start/Stop recording state machine,
                     in-memory session baseline/clock/event log
    ui.py            the diagnostic Panel (Probe status + Recording status)
    README.md        this file
```

Responsibilities are deliberately separated so that `snapshot.py` can be
unit-tested without Blender at all (it only uses `array`/`zlib`), and so
the eventual recorder can reuse the snapshot/diff logic largely as-is.

---

## 3. Installation (Blender 2.79b)

1. Zip the `ReplayProbe79` folder so that `__init__.py` sits at the root
   of the zip (i.e. zipping the folder itself, not its *contents*):
   the deliverable `ReplayProbe79_Phase1.zip` is already built this way.
2. In Blender 2.79b: **File > User Preferences > Add-ons > Install
   Add-on from File...**
3. Select `ReplayProbe79_Phase1.zip`.
4. Enable the checkbox next to **"ReplayProbe79 (Phase 1 Diagnostic
   Probe)"**.
5. (Optional) Click "Save User Settings" if you want it enabled on next
   launch.
6. Confirm there is **no traceback** printed in the system console
   (Window > Toggle System Console on Windows; just watch the terminal
   you launched Blender from on Linux/macOS).

If you prefer manual install: copy the whole `ReplayProbe79` folder into
your Blender `scripts/addons/` directory, then enable it the same way.

---

## 4. Where the UI shows up

Open the 3D Viewport, press **N** to open the right-hand sidebar, and
look for a tab named **"Replay Probe"**. You will see:

```
[ Start Probe ] [ Stop Probe ]
[ Clear ]
Log Limit: 200

Status
Monitoring: YES/NO
Elapsed: ...
Scenes tracked: ...

Counters:
Scene updates (raw): X
Scene updates (processed): X
Object changes: X
Transform changes: X
Mesh changes: X
Topology changes: X
Material changes: X
Modifier changes: X
Mode changes: X
Selection changes: X
Visibility changes: X
Active object changes: X

Last detected event: ...
Last detected object: ...
Last detected mesh: ...

Event log (most recent last, showing up to 15):
[12.31] OBJECT_CREATED Cube
[15.72] TRANSFORM_CHANGED Cube
...

Recording (Phase 2.2)
[ Start Recording ] [ Stop Recording ]
[ Clear Last Session ]
Recording: STOPPED / ARMING / RECORDING / STOPPING
Recording Time: ...s
Sequence: ...
Events logged: ...
(status message)

Recording counters (current/last session):
... (same categories as the Probe counters above, scoped to the
    recording session instead of since Start Probe)
```

See the "Phase 2.2" addendum at the top of this file for what the
Recording section does and does not do.

---

## 5. Starting / stopping the probe

- **Start Probe**: builds a baseline snapshot of every open scene
  (`bpy.data.scenes`, not `bpy.context.scene`), installs the
  `scene_update_post` handler, and starts a background modal timer
  (0.5s tick) as a fallback/heartbeat. Click it once; the button itself
  is a modal operator, so Blender's UI stays fully interactive while it
  runs — you are not blocked and the viewport is not "stuck in an
  operator".
- **Stop Probe**: removes the handler and lets the modal timer self-
  cancel. Counters and the log are preserved until you hit "Clear" or
  start again.
- **Clear**: resets counters and empties the log without touching
  monitoring state.
- **Log Limit**: how many lines the in-memory event log retains (10–2000,
  default 200). Applied when you next click "Start Probe".

Re-enabling the addon (disable → enable) never errors and never leaves a
duplicate handler installed — `register()` defensively removes the
handler if present, and `unregister()` forces `stop()` first.

---

## 6. How detection actually works

Two independent mechanisms are used, deliberately, because the task
explicitly requires us **not** to assume one mechanism (e.g.
`scene_update_post`) is sufficient on its own:

### 6.1 `bpy.app.handlers.scene_update_post`
Registered only while the probe is running (never at `register()` time).
Blender calls our function with the `scene` that was updated — we never
read `bpy.context.scene` inside the handler. Every call increments
"Scene updates (raw)". To keep CPU usage bounded (this handler can fire
dozens of times per second during an interactive transform or sculpt
stroke), the actually expensive step — rebuilding mesh signatures and
diffing — is throttled to at most once per 0.15s via a simple
timestamp check. Each time the expensive path runs, "Scene updates
(processed)" increments.

### 6.2 Modal timer fallback (`wm.event_timer_add` + modal operator)
Blender 2.79 has **no** `bpy.app.timers` (that API was added in 2.80).
Instead, clicking "Start Probe" runs an operator that calls
`context.window_manager.event_timer_add(0.5, context.window)` and
`wm.modal_handler_add(self)`, then returns `RUNNING_MODAL`. On every
`TIMER` event it calls the same diff logic across **every** scene in
`bpy.data.scenes`. This is a safety net for:
- multi-scene setups (the `scene_update_post` handler is called per
  updated scene, not proactively for every scene you aren't looking at),
- any change that, experimentally, does *not* reliably trigger
  `scene_update_post` (to be confirmed — see the test matrix below),
- giving the UI a guaranteed refresh cadence even if nothing happens.

Both paths funnel into the same `probe._process_scene()` →
`snapshot.snapshot_scene()` / `snapshot.diff_snapshots()` code, and both
are protected by a re-entrancy guard (`_busy`) and the same throttle
timestamp, so running both simultaneously never double-counts or
compounds CPU cost.

### 6.3 Snapshots (the actual "what changed" logic)
`snapshot.py` has no Blender-handler code at all — it is pure
input → output logic over plain Blender data:

**Per object** we record: name, type, location/rotation/scale (rounded
to 6 decimals to avoid float-noise false positives), `hide`,
`hide_render`, `select`, `mode` (`obj.mode`, read per-object — in 2.79
this correctly reflects OBJECT/EDIT/etc. for that specific object),
the *pointer identity* of `obj.data` (so a reassigned/replaced mesh
datablock is detected even if vertex counts happen to match), material
slot contents, modifier `(name, type)` list, and — for mesh objects — a
mesh signature (below).

Objects are keyed in the snapshot by `obj.as_pointer()` (a stable
per-session identity for the underlying C struct), **not** by name, so
a rename is detected as a rename (same key, different `name`) instead of
being misread as "delete + create".

**Per mesh** (`mesh_signature()`), we deliberately avoid the common
pitfall named explicitly in the brief — *summing vertex coordinates is
not sufficient*, because two vertices moving by equal and opposite
amounts would cancel out under a sum. Instead:
- vertex count, edge count, polygon count (cheap, always available),
- a **CRC32 over the raw packed float bytes** of every vertex's `co`
  (via `foreach_get('co', ...)` into an `array('f', ...)`). Any change
  to any coordinate changes this checksum, including equal-and-opposite
  moves (verified in the automated test suite, see §9).
- an **edge-topology checksum**: CRC32 over `foreach_get('vertices', ...)`
  for every edge (each edge stores exactly 2 vertex indices, so this is
  `foreach_get`-friendly).
- a **polygon-topology checksum**: polygons have a *variable* number of
  vertices, so `polygons[i].vertices` cannot be pulled with
  `foreach_get`. Instead we pull `polygons.foreach_get('loop_total', ...)`
  and `loops.foreach_get('vertex_index', ...)` — both fixed-size per
  element — and CRC32 the combination. This reconstructs full polygon
  connectivity.
- A change in vertex/edge/polygon **count**, or in the edge/polygon
  checksums, is reported as `TOPOLOGY_CHANGED`. A change in the vertex
  checksum *alone* (counts and topology checksums unchanged) is reported
  as `MESH_GEOMETRY_CHANGED` (pure vertex movement, e.g. "move one
  vertex").

**Edit Mode caveat (important finding):** while an object is in Edit
Mode, `obj.data` (the `Mesh` datablock) is generally **not** live-synced
to the in-progress BMesh editing cage in Blender 2.79. Before computing
a mesh signature we call `obj.update_from_editmode()` (present in 2.79;
it predates the 2.80 view-layer API) so that vertex moves, extrusions,
insets, etc. performed *while still in Edit Mode* are visible to the
probe without requiring you to exit to Object Mode first. This call is
wrapped in `try/except` because it is only valid in certain contexts; if
it fails we silently fall back to whatever is already in `obj.data`
(documented as a known limitation below).

---

## 7. What Blender 2.79 appears to expose reliably (expected, pending your test run)

Based on the detection design above, here is what we *expect* to see —
**you must run the test matrix in §9 and report actual results**, because
the brief explicitly forbids claiming detection without verification:

| Observation | Expected mechanism | Expected event |
|---|---|---|
| Object creation | scene_update_post / timer, pointer-set diff | `OBJECT_CREATED` |
| Object deletion | pointer-set diff | `OBJECT_DELETED` |
| Object duplication | appears as `OBJECT_CREATED` (new pointer + new/shared data pointer) | `OBJECT_CREATED` |
| Object transform | location/rotation/scale compare | `TRANSFORM_CHANGED` |
| Object rename | same pointer, name differs | `OBJECT_RENAMED` |
| Vertex movement | vertex CRC32 | `MESH_GEOMETRY_CHANGED` |
| Vert/edge/face creation | counts/topology CRC32 | `TOPOLOGY_CHANGED` |
| Extrude / Inset / Loop cut | counts/topology CRC32 change | `TOPOLOGY_CHANGED` |
| Vert/edge/face deletion | counts decrease | `TOPOLOGY_CHANGED` |
| Dissolve | counts/topology CRC32 change | `TOPOLOGY_CHANGED` |
| Modifier add/remove/param change | modifier tuple compare | `MODIFIER_CHANGED` |
| Material assign/change | material slot tuple compare | `MATERIAL_CHANGED` |
| Object/Edit mode toggle | `obj.mode` compare | `MODE_CHANGED` |
| Active object change | `scene.objects.active` pointer compare | `ACTIVE_OBJECT_CHANGED` |
| Selection change | `obj.select` compare | `SELECTION_CHANGED` *(see limitations — may be noisy or may not fire scene_update_post at all; the timer fallback is what is expected to actually catch this)* |
| Visibility/hide change | `obj.hide` / `obj.hide_render` compare | `VISIBILITY_CHANGED` |
| Object datablock swap (e.g. mesh linked from another object) | `obj.data` pointer compare | `OBJECT_DATA_CHANGED` |

---

## 8. Known limitations (acknowledged up front, not hidden)

1. **`scene_update_post` firing is not documented to be exhaustive.**
   Blender 2.79's docs do not guarantee it fires for every conceivable
   data change (e.g. a pure selection change with no other side effect
   may or may not trigger a depsgraph update). This is exactly why the
   probe also runs an independent 0.5s modal-timer sweep across all
   scenes — but a change that happens and is reverted entirely within
   one 0.5s window, and never triggers `scene_update_post`, could in
   theory be missed. Report back if this ever appears to happen.
2. **Topology checksums are order-sensitive.** If Blender internally
   reorders vertices/edges/polygons without a real topological change
   (this can happen after certain operators, e.g. "remove doubles" or
   mesh cleanup, even when the net shape is identical), the checksum
   will differ and will be reported as `TOPOLOGY_CHANGED` even though a
   human would call it a no-op. This is a false positive we accept for
   Phase 1; a production recorder would need a canonicalized/sorted
   topology representation if this turns out to matter in practice.
3. **No sub-operator granularity.** We cannot tell *which* vertices moved
   or *which* operator was used (extrude vs. move vs. inset can all
   produce the same observable signature pattern: topology count changes
   + geometry changes). The probe only tells you *that* a category of
   change happened, not the operator name. True operator identification
   would require `bpy.types.SpaceView3D` operator-redo-panel inspection
   or `bpy.context.window_manager.operators` history — out of scope for
   this phase but worth a future experiment (see §11).
4. **Linked/shared mesh datablocks**: if two objects share one `Mesh`
   datablock (e.g. via Alt-D "linked duplicate"), editing the mesh
   through *either* object will correctly show `TOPOLOGY_CHANGED` /
   `MESH_GEOMETRY_CHANGED` for **both** objects, because both point at
   the same `data_pointer`. This is correct behavior, not a bug, but it
   means "which object did the user actually edit" is ambiguous from
   mesh changes alone in that scenario — selection/active-object context
   is needed to disambiguate it later.
5. **Edit Mode geometry requires `update_from_editmode()`.** If this call
   ever fails silently in your Blender build/context (wrapped in
   try/except here), geometry/topology changes made while remaining in
   Edit Mode would not be seen until you return to Object Mode. Please
   report if any Edit-Mode test (TEST 08–16) shows **no** mesh/topology
   counter increase until you press Tab back to Object Mode — that is
   the specific failure mode to watch for.
6. **Throttling (0.15s) means very rapid back-to-back changes within
   that window are coalesced** into a single diff against the
   last-processed state, not one diff per intermediate step. This is
   intentional (CPU budget) but means the log shows "net effect," not a
   frame-by-frame record. The real recorder will need a deliberate
   policy decision here (Phase 2+ topic).
7. **Non-mesh objects** (Empty, Camera, Lamp, etc.) only get
   object-level diagnostics (transform/rename/visibility/mode/active/
   selection/material where applicable); mesh-specific fields are simply
   `None` and skipped — this is expected, not an error.
8. **We do not attempt to diff curves, armatures, lattices, or other
   non-mesh geometry data-blocks' internal content** — only `Mesh` is
   given a geometry/topology signature in Phase 1. Note if you need
   curve/armature editing detected; that is a candidate Phase 1.5/2
   experiment, not implemented here.
9. **Duplication vs. creation are not distinguished.** A duplicated
   object currently reports only as `OBJECT_CREATED` (new C-struct
   pointer). We do not yet attempt heuristics (e.g. matching `.001`
   name suffixes or shared `data_pointer`) to specifically label an
   event as "duplicate" — documented as a deliberate simplification,
   not an oversight.

---

## 9. Automated unit tests (already run, no Blender required)

Because `snapshot.py` has zero dependency on `bpy`, its diff logic was
unit-tested directly with fake Python stand-ins for Blender's
object/mesh collections (fake `foreach_get`-capable collections, fake
`Object`/`Scene`/`Material`/`Modifier`). All of the following passed:

- no-change snapshot produces zero events,
- object creation / deletion,
- transform change,
- rename (same identity, new name),
- single vertex move → `MESH_GEOMETRY_CHANGED`, not `TOPOLOGY_CHANGED`,
- **equal-and-opposite vertex movement is still detected** (this was
  checked explicitly, since a naive coordinate-sum approach would have
  missed it — the CRC32-based checksum correctly changes),
- topology change (added vertex/edge/polygon) → `TOPOLOGY_CHANGED`,
- material change, modifier change, mode change, selection change,
  visibility change, active-object change.

This gives confidence the *diffing logic itself* is correct. It does
**not** prove what real Blender 2.79 will actually report through
`scene_update_post` for each real user operation — that can only be
verified by you, running the test matrix below inside actual Blender
2.79b, which is why this remains a diagnostic phase.

---

## 10. Manual test matrix (run inside Blender 2.79b)

For every test: click **Start Probe**, perform the action, then read the
**counters**, the **Last detected event/object/mesh**, and the **event
log** in the panel. Report back, for each test number:
(a) which counter(s) incremented, (b) the exact event log line(s) that
appeared, (c) whether it matched the "Expected" column, and (d) anything
surprising (e.g. no event at all, a delayed event, or an unexpected
event type).

Start with a fresh default scene (File > New > General) for TEST 01–09,
then continue using the same cube for TEST 10 onward unless a test says
otherwise.

| # | Test | How to perform it | Expected signal |
|---|------|--------------------|------------------|
| 01 | Create object | Shift+A > Mesh > Cylinder | `OBJECT_CREATED` |
| 02 | Delete object | Select the new cylinder, press X > Delete | `OBJECT_DELETED` |
| 03 | Duplicate object | Select default Cube, Shift+D, click to place | `OBJECT_CREATED` (new pointer) |
| 04 | Move object | Select Cube, G, move mouse, click | `TRANSFORM_CHANGED` |
| 05 | Rotate object | Select Cube, R, move mouse, click | `TRANSFORM_CHANGED` |
| 06 | Scale object | Select Cube, S, move mouse, click | `TRANSFORM_CHANGED` |
| 07 | Rename object | Double-click object name in Outliner, retype, Enter | `OBJECT_RENAMED` |
| 08 | Move one vertex | Tab into Edit Mode, select 1 vertex, G, move, click | `MESH_GEOMETRY_CHANGED` (not `TOPOLOGY_CHANGED`) |
| 09 | Move multiple vertices | Edit Mode, box-select several verts, G, move, click | `MESH_GEOMETRY_CHANGED` |
| 10 | Extrude | Edit Mode, select a face, E, move, click | `TOPOLOGY_CHANGED` (count increase) |
| 11 | Inset | Edit Mode, select a face, I, move, click | `TOPOLOGY_CHANGED` |
| 12 | Loop cut | Edit Mode, Ctrl+R over an edge, click, Esc/click to confirm at center | `TOPOLOGY_CHANGED` |
| 13 | Delete face | Edit Mode, select a face, X > Faces | `TOPOLOGY_CHANGED` (count decrease) |
| 14 | Delete edge | Edit Mode, select an edge, X > Edges | `TOPOLOGY_CHANGED` |
| 15 | Delete vertex | Edit Mode, select a vertex, X > Vertices | `TOPOLOGY_CHANGED` |
| 16 | Dissolve | Edit Mode, select an edge, X > Dissolve Edges | `TOPOLOGY_CHANGED` (watch for false-positive note in §8.2) |
| 17 | Modifier | Object Mode, add a Subdivision Surface modifier via Properties > Modifiers | `MODIFIER_CHANGED` |
| 18 | Material | Object Mode, Properties > Material > New, change base color | `MATERIAL_CHANGED` (assigning new material slot); note whether a color tweak alone also triggers it (it may not — material *property* edits aren't explicitly diffed in Phase 1, only slot/material assignment identity; report what you observe) |
| 19 | Object/Edit mode | Press Tab repeatedly to toggle modes a few times | `MODE_CHANGED` each toggle |
| 20 | Selection | Object Mode, click between two different objects to change selection/active object | `SELECTION_CHANGED` and/or `ACTIVE_OBJECT_CHANGED` — report which one(s) actually appear, and whether there is a delay (this exercises the "selection may not be reliably/immediately detectable via scene_update_post" question directly) |

Also specifically try, and report on:
- **Empty scene**: Start Probe with zero objects in the scene — confirm
  no traceback and "Monitoring: YES" with all counters at 0.
- **Multiple scenes**: create a second scene (Scene > + New), switch
  between them, edit objects in each — confirm "Scenes tracked" reflects
  both and events are attributed correctly.
- **Non-mesh object**: add a Lamp or Empty, move/rename it — confirm
  `TRANSFORM_CHANGED`/`OBJECT_RENAMED` still work without mesh fields.
- **Linked duplicate**: Alt+D a mesh object, edit the mesh via one of the
  two instances in Edit Mode — confirm both objects show
  `MESH_GEOMETRY_CHANGED`/`TOPOLOGY_CHANGED`.
- **Enable/disable/re-enable** the addon 2–3 times in a row (User
  Preferences > Add-ons) and confirm no traceback appears at any point.

---

## 11. What to report back

For a useful Phase 1 result, please report:
1. The table from §10 filled in with actual observed events per test.
2. Any test where **nothing** was logged (a true detection gap).
3. Any test where the event type logged did not match "Expected".
4. Any console tracebacks (copy the full traceback text).
5. Rough perceived responsiveness/CPU impact while the probe is running
   during a normal modelling session (just a subjective note is fine —
   "felt instant", "noticeable lag while dragging a transform", etc.).
6. Whether the Log Limit control behaves as expected.

---

## 12. Recommended next Phase 1 follow-up experiment

Do **not** jump to building the full recorder next. Based on the open
questions this diagnostic is designed to surface, the recommended next
experiment is:

**Phase 1b — Operator-name correlation probe.** Add a second, equally
small diagnostic mechanism that listens to
`bpy.context.window_manager.operators` (the operator redo/history stack)
or hooks `bpy.types.Operator` globally in a safe, read-only way, purely
to log the **operator id string** (e.g. `mesh.extrude_region_move`,
`transform.translate`, `object.duplicate`) alongside the existing
state-diff events — without executing or altering any operator. The
goal is to determine whether we can reliably **correlate** "a
`TOPOLOGY_CHANGED` event happened" with "the user ran `mesh.inset`" vs.
inferring it purely from geometry deltas (which Phase 1 already shows
is ambiguous — see §8.3). That correlation, if reliable, would
meaningfully change the design of the real event-recording format in
the subsequent architecture phase.

Only after that experiment should the project move on to designing the
actual replay file format / timeline / camera system.
