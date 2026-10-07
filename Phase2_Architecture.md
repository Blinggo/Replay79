# ReplayMod79 — Phase 2 Architecture Proposal
### (Recording / Reconstruction Design — Pre-Implementation Review)

Status: **Design only.** No Phase 2 code exists yet. Phase 1 (`ReplayProbe79`)
remains unmodified. This document is the basis for deciding what Phase 2
should actually build.

Correction for the record: Blender 2.79b bundles **Python 3.5.3**, not
Python 2.x. `array`, `zlib`, `sqlite3`, and `uuid` are all standard-library
modules available in that interpreter; Phase 1 already relies on `array`
and `zlib` successfully.

---

## 1. Recommended Architecture

**Hybrid checkpoint + event-delta recording (Approach C), persisted
incrementally to a single SQLite file per recording, with a pure-Python
"VirtualScene" reconstruction layer that is decoupled from both the live
Blender scene and any future camera/render system.**

Five architectural layers, each independently testable (mirroring how
Phase 1's `snapshot.py` was tested without Blender at all):

1. **Identity layer** — stable, persistent UIDs for objects and mesh
   datablocks (replacing Phase 1's `as_pointer()`, which is session-local
   and reuse-prone — see Risk #1).
2. **Detection layer** — Phase 1's proven mechanisms (`scene_update_post`
   + modal timer + throttle), extended with debouncing/coalescing before
   anything is written to disk.
3. **Encoding layer** — turns a detected change into a compact event or
   checkpoint record (full vs. sparse mesh representation, chosen by a
   cheap byte-cost heuristic, not by trying to identify the operator).
4. **Storage layer** — a single-file SQLite container, written
   incrementally and transactionally, so the file on disk *is* the
   durable recording at all times (no separate "unsaved in-RAM session"
   that a crash or a mistimed Stop could destroy).
5. **Reconstruction layer** — a pure-Python `reconstruct_scene(time)`
   function operating only on the stored data, producing an in-memory
   scene-state description that a later "materializer" pushes into real
   Blender datablocks only when something actually needs to look at it
   (scrubbing UI, rendering). This layer has zero knowledge of how data
   was captured, and the camera/FFmpeg work (Phases 3/4) only ever talks
   to this layer.

---

## 2. Full Snapshots vs. Deltas vs. Hybrid

### Approach A — Full scene snapshot at every recording point

**How it would work:** every time something changes, serialize a
complete, independently-reconstructable copy of every object and every
mesh in the scene.

**Advantages:**
- Trivial, bullet-proof reconstruction: load the one record nearest to
  time T, done — no replay logic, no accumulated-error risk, no need to
  track "current state" across any sequence of edits.
- Naturally robust to corruption: every record is self-contained; losing
  one doesn't invalidate any other.
- Doesn't care what changed or how — works identically for any Blender
  operation, including ones we can't categorize.

**Disadvantages:**
- Storage cost scales with **(mesh size) × (number of recorded points)**,
  which for "every recording point" interpreted as every detected change
  is catastrophic — demonstrated below.
- Every write is proportional to full scene size, so write latency (and
  therefore main-thread stalls, since Blender's Python API isn't safely
  usable off the main thread) scales with mesh size too — directly
  conflicts with the low-end-hardware requirement.
- No meaningful ability to distinguish "nothing happened" from "a lot
  happened" — storage grows even during idle/thinking time if snapshots
  are taken on a fixed timer.

**Storage estimates** (vertex position data only, float32, ignoring
edges/polygons/loops/materials — i.e. a *lower bound*):

| Mesh size | Bytes/vertex-array | Per-snapshot cost* | 1 snapshot/sec × 1 hour | 1 snapshot/sec × 2 hours |
|---|---|---|---|---|
| Small (cube, 8 verts) | 96 B | ~0.5–1 KB (incl. edges/faces/overhead) | ~1.8–3.6 MB | ~3.6–7 MB |
| 100,000 verts | 1.2 MB | ~4–6 MB (incl. edges/loops) | ~14–21 GB | ~29–43 GB |
| 1,000,000 verts | 12 MB | ~40–60 MB (incl. edges/loops) | ~144–216 GB | ~288–432 GB |

\* "Per-snapshot cost" includes a rough allowance for edge (`~2×` vertex
count × 8 bytes) and loop/polygon data (`~1×`–`2×` vertex count × ~8
bytes) typical of a quad/tri mesh — real ratios vary by topology.

Even at a **much** sparser interval (once per 60 seconds instead of once
per second), a single 1M-vertex mesh edited continuously for 2 hours
would still cost on the order of **4.8–7.2 GB** just for that one object,
with every single checkpoint write stalling the UI for however long it
takes to pull 12MB+ out of `foreach_get` and flush it to disk. And if
"every recording point" is read literally as "every detected change"
(which, per Phase 1, can fire many times per second during an interactive
drag), the numbers above explode by another 1–2 orders of magnitude into
clearly absurd territory. **Conclusion: Approach A cannot be the primary
mechanism.** It is, however, exactly what we want for the infrequent
"checkpoint" in Approach C (see below) — just not for every change.

### Approach B — Pure event/delta recording

**How it would work:** never store a full scene; store only the specific
field(s) that changed, every time something changes.

Representative event shapes (conceptual, not final schema — see §4):

```
OBJECT_CREATED      { uid, name, type, initial_transform, mesh_uid }
OBJECT_DELETED      { uid }
TRANSFORM_CHANGED   { uid, location, rotation, scale }
OBJECT_RENAMED      { uid, new_name }
MATERIAL_CHANGED    { uid, material_slot_names }
MODIFIER_CHANGED    { uid, modifier_stack }
VISIBILITY_CHANGED  { uid, hide, hide_render }
MODE_CHANGED        { uid, mode }
MESH_VERTEX_DELTA   { mesh_uid, [(vertex_index, x, y, z), ...] }
MESH_TOPOLOGY_FULL  { mesh_uid, vertices[], edges[], loop_total[], loop_vertex_index[], material_index[] }
```

**Advantages:**
- Storage proportional to *actual edit activity*, not wall-clock time or
  mesh size — a session where the user mostly stares at the viewport
  costs almost nothing.
- Naturally captures "net effect" rather than operator identity, which
  is exactly the constraint we're working under (no keyboard/mouse/menu
  recording, no assumption about which tool was used).

**Disadvantages:**
- Reconstructing *any* point in time requires replaying the *entire*
  event stream from the beginning — unacceptable for scrubbing in a
  multi-hour session (seeking to minute 90 would mean replaying 90
  minutes of events every time).
- A corrupted or missing event anywhere in the stream invalidates every
  state *after* it, with no recovery point.
- Long sessions produce an ever-growing, monotonically-replayed log with
  no bounded "worst case" seek cost.

**Mesh reconstruction without knowing the operation — the core idea:**
We never need to know "the user pressed E" (extrude). We only need two
mesh states, A (before) and B (after), and *enough information to turn A
into B*. Concretely, this means every mesh event is one of exactly two
practical shapes, chosen by *outcome*, not by *tool*:

1. **`MESH_VERTEX_DELTA`** — topology (vertex/edge/polygon counts *and*
   Phase 1's topology checksum) is unchanged, only coordinates moved.
   We already have the previous vertex array resident in memory (needed
   to compute Phase 1's CRC32 anyway); a plain index-by-index comparison
   against the new array tells us exactly which vertices changed and by
   how much, with zero knowledge of *why*. This is what covers: moving
   one vertex, moving many vertices, proportional editing, equal-and-
   opposite moves, sculpting strokes, armature-driven deformation (if
   ever applied to mesh data), etc.
2. **`MESH_TOPOLOGY_FULL`** — topology checksum (or counts) changed.
   Rather than attempting to compute a minimal structural patch (e.g.
   "insert vertices 8–11, insert these new edges/faces"), we store the
   **entire new vertex/edge/loop/polygon arrays** as the event payload.
   This deliberately avoids a hard, tool-specific, and arguably
   ill-posed problem: Blender gives no API guarantee of *stable element
   indices* across a topology operation (extrude, inset, loop cut,
   dissolve, delete, and even some non-destructive cleanup can all
   renumber elements), so a generic "correct" minimal diff is a
   structural/graph-isomorphism problem with no reliable, cheap, tool-
   agnostic solution. Storing the full new topology is simple, always
   correct, and — because topology-changing edits are comparatively rare
   relative to continuous vertex dragging in a typical modelling
   session — an acceptable cost in practice. This is covered further in
   §3 (large changes).

Because Approach B has no efficient random-access seek story, it cannot
be the sole mechanism either.

### Approach C — Hybrid checkpoints + deltas (Recommended)

Combine A and B: **infrequent full checkpoints bound the worst-case
reconstruction/seek cost; event deltas between checkpoints keep
continuous storage/CPU cost low.** This is architecturally identical to
how video codecs mix I-frames and P-frames, and it is the natural
answer to every sub-requirement the brief lists:

| | Approach A (full) | Approach B (deltas only) | Approach C (hybrid) |
|---|---|---|---|
| Storage growth | huge, scales with mesh size × frequency | small, scales with edit activity | small, bounded by checkpoint interval |
| Reconstruction cost at arbitrary time | O(1) | O(session length) | O(checkpoint interval) |
| Crash/corruption resilience | excellent (self-contained records) | poor (one gap breaks everything after it) | good (checkpoints are recovery anchors) |
| CPU cost per detected change | high (full serialize) | low (diff only) | low (diff only; checkpoint cost is rare/amortized) |
| Implementation complexity | low | medium | medium-high (worth it) |
| Fits "disk can be bigger than RAM, RAM must stay small" | no (also stresses RAM during capture) | yes | yes |

**Detailed hybrid design:**

- **Checkpoint frequency:** time-based *and* size-based, whichever comes
  first — e.g. every **3–5 minutes of active recording time**, OR
  whenever accumulated event-payload bytes since the last checkpoint
  exceed roughly **10–20 MB**, OR at explicit session boundaries (Record
  transition out of Pause, Stop, Save). No checkpoint is written purely
  because time passed if nothing changed — an idle multi-minute gap costs
  nothing.
- **Event storage:** append-only, strictly ordered by a monotonically
  increasing sequence number plus a recording-relative timestamp
  (float seconds since the recording's own clock started — see §12 on
  pause-aware clocks).
- **Reconstruction process:** see §8, dedicated section.
- **Seeking/scrubbing:** worst-case reconstruction cost is bounded by one
  checkpoint load + at most "checkpoint interval's worth" of events —
  this is the direct lever for tuning scrub responsiveness vs. storage
  cost. Small forward seeks within the same interval can incrementally
  advance an already-materialized state instead of reloading from the
  checkpoint (see §8).
- **Crash recovery:** because the working file is written incrementally
  with periodic committed transactions (not accumulated in RAM awaiting
  a manual Save — see §7), a crash loses at most the still-open
  transaction (bounded to a few seconds / a few dozen events by the
  commit cadence), never the session.
- **Corruption recovery:** SQLite's own atomic-commit semantics already
  prevent partial-row corruption; a crash mid-write simply means the
  last *uncommitted* transaction never appears on reopen — the database
  itself remains structurally valid. Each event/checkpoint payload blob
  additionally carries its own CRC32 (same primitive already proven in
  Phase 1's mesh signatures) so the reader can detect payload-level
  corruption from the (much lower-probability) case of underlying
  storage media bit-rot, independent of SQLite's own guarantees.
- **Memory usage:** bounded by "the last known full state of every
  currently-tracked mesh" (needed to compute the next delta against) —
  proportional to *live scene complexity*, not to *session duration*.
  No history is ever retained in RAM.
- **Disk usage:** proportional to actual edit activity + a roughly fixed
  per-checkpoint-interval overhead; can be large for long/active
  sessions, which the brief explicitly accepts.

---

## 3. Proposed Mesh Representation

This is the most load-bearing part of the design, so it's specified
concretely.

### Identity

> **Status update (P2.1, implemented):** the identity scheme described in
> this subsection has been implemented in `identity.py` and verified
> against synthetic (non-Blender) tests covering: new-object UID
> assignment, rename stability, `obj.copy()`/`mesh.copy()` collision
> repair, shared-mesh non-collision, idempotency across repeated scans,
> and manually-forced duplicate-UID repair. Manual verification inside a
> real Blender 2.79b session (including an actual `.blend` save/reload
> round-trip) is still pending — see the P2.1 final report. No recording,
> SQLite, event, checkpoint, or reconstruction code exists yet; this
> remains strictly the identity layer described below.

- Every `Mesh` datablock gets a **persistent UID** the first time the
  recorder sees it: a `uuid.uuid4().hex` string stored as a Blender
  **custom ID property** directly on the datablock, e.g.
  `mesh["replaymod79_uid"] = "a1b2c3..."`. This is a 2.79-compatible,
  completely standard mechanism for any `ID` type (meshes, objects,
  materials all support custom properties), and — bonus — it **survives
  `.blend` file save/reload**, which matters directly for "Resume after
  Blender restart" (§7).
- Objects get the same treatment: `obj["replaymod79_uid"]`.
- This replaces Phase 1's `as_pointer()` keying, which is only valid for
  the lifetime of the in-memory C struct and is vulnerable to address
  reuse after deletion (see Risk #1) — unacceptable for a format that
  must remain correct across minutes/hours and across restarts.

### Per-mesh stored state (baseline, materialized at a checkpoint or
after a full-replace event)
```
mesh_uid            : str (persistent UID)
vcount, ecount, pcount
vertices            : packed float32[vcount*3]      (co, via foreach_get)
edges               : packed int32[ecount*2]         (vertex index pairs)
loop_total          : packed int32[pcount]            (per-polygon loop length)
loop_vertex_index   : packed int32[sum(loop_total)]   (reconstructs polygon→vertex connectivity)
material_index      : packed int32[pcount]            (per-polygon material slot index)
```
This is a direct extension of the arrays Phase 1 already proved it could
pull via `foreach_get` for checksum purposes — Phase 2 simply *retains*
the arrays themselves (compressed) instead of discarding them after
hashing. UV/vertex-color/normal data is explicitly **out of scope for
Phase 2** (geometry + topology + per-face material only); noted as a
future extension, not silently dropped.

### Per-object stored state
```
object_uid, name, type
location, rotation (+ rotation_mode), scale
hide, hide_render, select, mode
mesh_uid            : reference only — mesh data is never embedded inline
material_slots      : tuple of material names (object-level slot assignment)
modifiers           : tuple of (name, type [, future: key params])
```
Keeping `mesh_uid` as a *reference* rather than embedding mesh data in
every object record is what makes shared/linked-duplicate mesh
datablocks fall out naturally: two objects with the same `mesh_uid`
always resolve to the same mesh-state entry during reconstruction,
exactly mirroring Blender's own data model.

### Geometry-only changes (example: one vertex moves)
Detected via the Phase 1 pattern (topology checksum unchanged, vertex
checksum changed) → compute a plain index-by-index comparison against
the last retained vertex array → emit:
```
MESH_VERTEX_DELTA { mesh_uid, entries: [(index, x, y, z), ...] }
```
Only the differing indices are included. Cost is `O(changed vertices)`
in the payload and `O(total vertices)` in the one-time comparison scan
(mitigated by the cheap CRC32 early-out — if the vertex checksum didn't
change at all, skip the scan entirely, exactly as Phase 1 already does).

### Topology changes (example: extrusion, inset, loop cut, dissolve,
vertex/edge/face deletion)
Detected via topology checksum/count change → **do not** attempt a
structural patch → emit:
```
MESH_TOPOLOGY_FULL { mesh_uid, vertices[], edges[], loop_total[], loop_vertex_index[], material_index[] }
```
the complete new arrays, zlib-compressed. This is the practical,
non-theoretical answer to "reconstruct the mesh without knowing which
operation was performed": we don't patch, we **replace**, whenever
topology itself isn't guaranteed index-stable. This is simple, always
correct, and acceptable because (a) topology edits are comparatively
infrequent relative to continuous dragging in real modelling sessions,
and (b) generic zlib compression on structured integer/float arrays
typically achieves meaningful size reduction for free.

### Large changes (example: a destructive Subdivide/Smooth touching
most of the mesh)
No special-cased detection of "this was a big operation" — instead, a
**byte-cost heuristic applied uniformly** at encode time:
- A sparse `MESH_VERTEX_DELTA` entry costs ~16 bytes/changed vertex
  (4-byte index + 3×4-byte floats).
- A dense full-array replace costs ~12 bytes/vertex (no index overhead).
- **Crossover point: once more than ~43% of vertices in a topology-
  unchanged event have moved, a dense replacement is already cheaper
  than the sparse list** — at that point, emit a `MESH_GEOMETRY_FULL`
  event (same shape as `MESH_TOPOLOGY_FULL` but counts/topology
  unchanged, so the reconstructor just overwrites coordinates in bulk)
  instead of a giant sparse list. This is a mechanical, derivable rule,
  not a tuned magic number, and requires no knowledge of what operation
  caused the change.
- Destructive operations that *also* change topology (e.g. a destructive
  Subdivide) already fall into `MESH_TOPOLOGY_FULL` by the topology-
  checksum rule above — no separate handling needed.

---

## 4. Proposed Event Representation

A single, uniform envelope for every event, so the storage/reader code
doesn't need per-type special cases beyond payload parsing:

```
Event {
    seq           : int (monotonic, per recording)
    t             : float (recording-relative seconds, pause-excluded — see §12)
    type          : str (one of the event type constants, e.g. "TRANSFORM_CHANGED")
    target_uid    : str (object_uid or mesh_uid depending on type)
    payload       : bytes (type-specific; zlib-compressed where it contains arrays)
    checksum      : uint32 (CRC32 of payload, for corruption detection)
}
```

Event type catalogue (directly derived from Phase 1's confirmed/implemented
categories, nothing invented beyond what Phase 1 already demonstrated it
can detect):

`OBJECT_CREATED`, `OBJECT_DELETED`, `OBJECT_RENAMED`, `TRANSFORM_CHANGED`,
`VISIBILITY_CHANGED`, `SELECTION_CHANGED`, `MODE_CHANGED`,
`OBJECT_DATA_CHANGED` (mesh swapped on an object), `MATERIAL_CHANGED`,
`MODIFIER_CHANGED`, `ACTIVE_OBJECT_CHANGED`, `MESH_VERTEX_DELTA`,
`MESH_GEOMETRY_FULL`, `MESH_TOPOLOGY_FULL`.

Small, non-array payloads (transform floats, mode strings, material-name
tuples) are stored as plain small binary/JSON-in-a-blob — the volume is
low enough that format elegance doesn't matter there; only the mesh
array payloads need the compact binary + compression treatment.

---

## 5. Proposed Checkpoint Strategy

A checkpoint is a **complete, Approach-A-style snapshot** of the scene at
one instant — every tracked object's full state (per §3) and every
referenced mesh's full arrays — serialized as one record.

- **Trigger conditions** (first one reached wins): elapsed active-
  recording time since last checkpoint ≥ ~3–5 minutes; OR accumulated
  event payload bytes since last checkpoint ≥ ~10–20 MB; OR an explicit
  lifecycle boundary (Resume-from-Pause, Stop, Save).
- **No checkpoint during true idle** — if zero events occurred since the
  last checkpoint, skip it; nothing has changed, so a new checkpoint
  would be byte-identical and wasteful.
- **Checkpoints are the only seek anchors** — every `reconstruct_scene`
  call starts by finding the nearest checkpoint ≤ requested time.
- **Tuning tension (explicit trade-off, not hidden):** shorter interval
  → faster scrubbing, more disk usage, more frequent (small) UI stalls
  while writing; longer interval → slower worst-case scrubbing, less
  disk usage, fewer stalls. The 3–5 minute / 10–20MB defaults above are
  a starting point to be empirically tuned in Milestone M7 (§16) on
  actual low-end hardware, not a final answer.

---

## 6. Proposed Replay File Format

**Recommendation: a single SQLite database file per recording**
(extension e.g. `.rm79`), not hand-rolled binary, not JSON, not a
directory of chunk files.

**Why SQLite over the alternatives:**
- **`sqlite3` is Python standard library** — available in Blender's
  bundled CPython 3.5.3 with no extra dependency (to be confirmed
  empirically as the very first Phase 2 task, per the "don't hide
  uncertainty" principle — see Risk list).
- **Transactional, crash-safe by construction** — exactly the corruption/
  crash-recovery guarantee a hand-rolled binary format would have to
  reimplement from scratch (length-prefixing, torn-write detection,
  journal/undo logic). This directly serves the "don't lose a 2-hour
  session" requirement.
- **Random access / indexed seeking** — `checkpoints`/`events` tables
  with an indexed `timestamp` (or `seq`) column give O(log n) lookup for
  "nearest checkpoint ≤ T" without reading the whole file, which a flat
  append-only binary log or a single big JSON document cannot do without
  a hand-built index alongside it anyway (at which point you've built a
  worse SQLite).
- **Incremental append without full-file rewrite** — a single large JSON
  file would need to be fully re-parsed and re-serialized on every save,
  which is exactly the "rewrite a huge file over and over" problem we're
  trying to avoid for multi-hour/multi-GB recordings.
- **Human-inspectable with off-the-shelf tools** — any standard `sqlite3`
  CLI or GUI browser can open a `.rm79` file for debugging without any
  addon code at all, useful during Phase 2 development itself.

**Proposed schema (conceptual):**
```
meta (key TEXT PRIMARY KEY, value TEXT)
    -- format_version, blender_version, created_at, session_uid,
    -- ended_at (NULL until Stop), finalized (0/1)

checkpoints (
    id INTEGER PRIMARY KEY,
    t REAL,                  -- recording-relative seconds
    payload BLOB,            -- compressed full-scene snapshot
    checksum INTEGER
)
CREATE INDEX idx_checkpoints_t ON checkpoints(t);

events (
    seq INTEGER PRIMARY KEY,
    t REAL,
    type TEXT,
    target_uid TEXT,
    payload BLOB,
    checksum INTEGER
)
CREATE INDEX idx_events_t ON events(t);

objects_index (uid TEXT PRIMARY KEY, last_name TEXT, last_type TEXT)
mesh_index    (uid TEXT PRIMARY KEY, last_name TEXT)
    -- small lookup tables for UI display (e.g. a scrub-bar object list)
    -- without needing a full reconstruction just to show names.
```

JSON is used only for the tiny `meta` values (strings/numbers), never
for bulk vertex/edge/loop arrays — at the sizes discussed in §2, JSON's
text overhead and parse cost would be a 3–5× regression for no benefit.
A hand-rolled binary container was considered and rejected as the
*primary* format for the reasons above, though the same binary-array
encoding (`array.array(...).tobytes()` + optional zlib) is used *inside*
SQLite BLOB columns regardless — SQLite is the container, not a
replacement for compact binary encoding of the payloads themselves.

---

## 7. Save / Auto-save / Resume Design

Key design decision: **the working file is created immediately at "New
Recording" and written to incrementally and durably from that point
on.** There is no large in-RAM-only buffer that "Save" flushes for the
first time — by the time the user clicks Save, the data is already
durable. This single decision is what satisfies "we do not want a
two-hour session to disappear" directly, rather than through a bolt-on
recovery mechanism.

- **New Recording:** create a new working `.rm79` file (in an addon-
  managed autosave directory, e.g. next to the `.blend` if saved, or a
  temp/addon-data directory if not yet saved), write `meta` row
  (`created_at`, `blender_version`, `format_version`, `session_uid`,
  `finalized=0`), write checkpoint #0 (baseline full snapshot). No
  capture yet.
- **Record:** (re)install the `scene_update_post` handler + (re)start
  the modal timer; begin appending events to the *existing* working
  file. Never creates a new file.
- **Pause:** stop capturing (flag off / handler detached), but — first —
  **force-flush any pending debounced/coalesced in-flight change** (see
  §9) so nothing mid-drag is silently dropped. The working file and
  timer heartbeat may keep running for UI purposes; no new events are
  written while paused.
- **Resume:** re-enable capture against the same working file / same
  `session_uid` (not a new file). Recommended: force a checkpoint at
  resume as a convenient scrub/debug anchor marking "session was paused
  here," even though zero events occurred during the pause itself.
- **Stop:** finalize capture (detach handler, cancel timer), flush any
  pending in-flight change, write a final checkpoint (fast "jump to
  end" anchor), set `meta.ended_at` — but explicitly **do not delete or
  discard the working file**. The recording is complete but still sits
  in its working/autosave location until the user does something
  deliberate with it.
- **Save:** because the working file is already a valid, continuously-
  committed SQLite database, Save is essentially **a filesystem copy/
  move to the user-chosen permanent path** plus setting
  `meta.finalized=1` — not a slow re-serialization of the whole session.
  Fast even for multi-GB recordings.
- **Auto-save:** not a separate "copy the whole file periodically" step —
  that would itself not scale to multi-GB files. Instead, "auto-save"
  *is* the SQLite commit cadence: every ~2–5 seconds or ~50 events
  (whichever first), driven by the existing 0.5s modal timer, call
  `connection.commit()`. This bounds the at-risk window on crash to that
  small interval, not the whole session.
- **Resume after Blender restart:** on addon `register()`, scan the
  known autosave/working directory for `.rm79` files where
  `meta.finalized = 0` (i.e., a recording that never reached a clean
  Stop+Save — most likely due to a crash or forced quit). Surface these
  in the panel as "Unfinished recording found (last event at T) —
  Resume / Discard?". Resuming: open the file, run `reconstruct_scene()`
  up to the last valid event (tolerating a possibly-truncated final
  transaction — SQLite simply won't show uncommitted rows, which is the
  correct behavior), re-derive the in-memory "last known mesh baselines"
  needed for future diffing, re-attach the handler, and continue
  appending with correctly continued `seq`/`t` values.
- **Clear/Delete:** a distinct, explicitly confirmation-gated action
  (e.g. Blender's `wm.invoke_confirm` / a two-click "Really delete?"
  pattern in the real UI) that deletes the file from disk. This is the
  **only** path that destroys data. It must never be an implicit side
  effect of Stop, Pause, New Recording, disabling the addon, or closing
  Blender — all of those preserve the file.

---

## 8. Replay Reconstruction Algorithm — `reconstruct_scene(time)`

Conceptual algorithm (no implementation yet):

```
function reconstruct_scene(replay_file, time):
    checkpoint = SELECT * FROM checkpoints
                 WHERE t <= time ORDER BY t DESC LIMIT 1
    virtual_scene = deserialize(checkpoint.payload)
        # virtual_scene.objects : { object_uid -> object-state dict }
        # virtual_scene.meshes  : { mesh_uid   -> mesh-state dict }

    events = SELECT * FROM events
             WHERE t > checkpoint.t AND t <= time
             ORDER BY seq ASC

    for ev in events:
        apply(virtual_scene, ev)   # pure in-memory mutation, no bpy calls

    return virtual_scene
```

`apply()` per event type:
- **`OBJECT_CREATED`** → insert a new object-state entry keyed by `uid`.
- **`OBJECT_DELETED`** → remove the object-state entry (its referenced
  `mesh_uid` entry is left alone — it may still be referenced by another
  object, per shared-mesh handling below).
- **`TRANSFORM_CHANGED`** → overwrite `location`/`rotation`/`scale`.
- **`OBJECT_RENAMED`** → overwrite display `name` only (identity is the
  UID, unaffected).
- **`VISIBILITY_CHANGED` / `MODE_CHANGED` / `SELECTION_CHANGED` /
  `MATERIAL_CHANGED` / `MODIFIER_CHANGED` / `OBJECT_DATA_CHANGED`** →
  overwrite the corresponding field(s) on the object-state entry.
- **`MESH_VERTEX_DELTA`** → look up `virtual_scene.meshes[mesh_uid]`,
  overwrite only the listed vertex indices.
- **`MESH_GEOMETRY_FULL` / `MESH_TOPOLOGY_FULL`** → replace the entire
  vertex/edge/loop/polygon/material-index arrays for that `mesh_uid`.

**Shared mesh datablocks** fall out for free: since mesh-state is keyed
by `mesh_uid` independent of any object, every object-state whose
`mesh_uid` matches automatically reflects the same geometry — exactly
mirroring Blender's own linked-duplicate behavior, with no special-case
code required.

**Materialization (a separate, later step):** `reconstruct_scene()`
itself never touches `bpy.data` — it returns a plain Python structure.
A separate materializer pushes that structure into a **dedicated set of
replay-only Blender objects/meshes** (never the user's live working
scene) only when something needs to actually see it: a scrub-preview
viewport or a render frame. For mesh data this uses the standard
2.79-compatible bulk-assignment APIs (`mesh.vertices.add()` +
`foreach_set('co', ...)`, etc.) for full replacement, or a per-index
`foreach_set` for an incremental patch when advancing by a small scrub
step on an already-materialized mesh (avoiding a full rebuild for small
forward seeks).

**Seeking/scrubbing performance:** a small forward seek within the same
checkpoint interval can incrementally advance an already-held
`virtual_scene` by applying only the newly-crossed events, instead of
re-running the whole algorithm from the checkpoint. A backward seek, a
large forward jump, or the first seek after opening the file always
goes through the full checkpoint-then-replay path. This gives a bounded
worst case (checkpoint interval) and a cheap common case (small
scrub nudges), directly justifying the checkpoint-frequency trade-off
in §5.

---

## 9. Recording-Frequency Strategy

**Is Phase 1's `scene_update_post` + 0.5s timer + 0.15s throttle
suitable as-is for the final recorder? Partially — the detection
cadence is fine; the *write* cadence needs an additional coalescing
layer on top of it.**

- **Event-driven capture** (`scene_update_post`) remains the correct
  primary signal — it's the only mechanism that reacts promptly to an
  actual change rather than guessing on a fixed clock.
- **Periodic sampling** (the 0.5s modal timer) remains useful as a
  safety-net sweep (multi-scene coverage, catching anything
  `scene_update_post` might miss — still an open question per Phase 1)
  and is repurposed in Phase 2 to also drive the SQLite commit cadence
  and checkpoint-trigger checks.
- **The existing ~0.15s detection throttle is kept** for *noticing*
  something changed (cheap counter/checksum compares) — this part is
  proven to keep CPU reasonable and shouldn't change.
- **What's missing in Phase 1 and must be added for Phase 2: a
  debouncing/coalescing layer between "a change was detected" and "an
  event is written to disk."** Without it, one continuous 3-second mouse
  drag could still produce ~20 separate on-disk events (one per
  throttled detection tick), which is wasteful and produces far more
  granularity than scrubbing needs. Proposed policy:
  - Hold the *net* pending change for a given target (object or mesh) in
    memory rather than writing immediately.
  - Flush it to the event log when **any** of: (a) an idle timeout
    elapses (~400ms) with no further change to that same target: the
    drag is probably over; (b) a hard cap elapses since the change
    started (~2s): bounds worst-case latency/size even for a very long
    continuous interaction, so scrubbing granularity doesn't degrade to
    "one event per 10-minute sculpt session"; (c) a state transition
    forces a flush regardless — mode change, object deselected/
    different object becomes active, Pause/Stop pressed. These forced
    flush points guarantee nothing in-flight is ever lost at a boundary.
  - If the net change after debounce is zero (user nudged something and
    then undid it within the window), **no event is written at all.**
- **High-frequency / sculpt-like continuous geometry changes** are the
  hard case: a sustained sculpt stroke may never go idle for the 400ms
  debounce window to fire, and for a huge mesh, diffing on every
  throttled tick is itself non-trivial CPU cost. Recommended policy:
  during a sustained continuous change to a mesh, additionally emit an
  intermediate `MESH_VERTEX_DELTA`/`MESH_GEOMETRY_FULL` at a **capped
  rate** (e.g. at most once every ~0.5–1s) even if the stroke hasn't
  gone idle, so (a) a very long stroke still gets *some* scrub
  granularity instead of one giant event at the end, and (b) the
  in-memory "pending net change" buffer doesn't grow unboundedly. This
  is the same idea as "I-frame cadence" applied to the delta stream
  itself, independent of the checkpoint cadence.
- **Avoiding huge files**, concretely: debounce/coalesce (above) + the
  sparse-vs-dense byte-cost crossover (§3) + unconditional zlib
  compression of array payloads + skipping empty/no-op flushes +
  skipping idle checkpoints (§5). None of these require knowing what
  tool the user used.

**Recommended practical initial strategy for a low-end 2.79 machine:**
keep Phase 1's detection stack unchanged (it's proven and cheap), add
the debounce/coalesce layer in front of the disk writer only, cap
intermediate flushes during sustained changes to ~1–2/sec, commit SQLite
transactions every ~2–5s, and checkpoint every ~3–5 minutes or ~10–20MB
of accumulated deltas. Treat all specific numbers here as starting
points for empirical tuning in Milestone M7 (§16), not final constants.

---

## 10. Memory / Disk Considerations

- **Memory (bounded by live scene complexity, not session length):**
  the recorder only ever needs, resident in RAM: (a) the last-known full
  array state of every *currently tracked* mesh (required to diff the
  next change against — unavoidable, same requirement Phase 1 already
  has), (b) small pending-flush buffers for the debounce layer, (c) a
  small open-SQLite-connection write buffer. A session with a single 1M-
  vertex mesh costs roughly ~12–20MB resident for that mesh's baseline,
  regardless of whether the session has run for 5 minutes or 5 hours.
  No history, no snapshot list, no growing structure is kept in RAM.
- **Disk (allowed to be larger, per the brief):** grows with actual edit
  activity, bounded above by the checkpoint cadence and below (i.e. for
  idle periods) by essentially nothing. A long but mostly-idle modelling
  session (lots of thinking, little editing) should produce a small
  file; a long, continuously active sculpting session on a huge mesh
  will legitimately produce a large file, and that is accepted as
  correct behavior rather than a problem to "fix" architecturally.
- **I/O pattern:** periodic small-to-medium committed writes (events)
  interleaved with rare larger writes (checkpoints). On an old
  mechanical HDD specifically, the checkpoint writes are the ones most
  likely to be perceptible as a stall — worth measuring directly in
  Milestone M7 rather than assuming.

---

## 11. Crash Recovery Strategy

Already threaded through §7 and §5, summarized as one coherent story:

1. The working file is durable from the moment "New Recording" is
   pressed — not just from "Save."
2. SQLite transactions are committed on a short, fixed cadence
   (~2–5s / ~50 events), bounding the data-at-risk window on any crash
   (Blender crash, OS crash, power loss) to that small interval.
3. SQLite's atomic-commit guarantee means a crash mid-write cannot leave
   the *database structure* corrupted — at worst, the last incomplete
   transaction is simply absent on reopen.
4. Per-record CRC32 checksums catch the separate, lower-probability
   failure mode of storage-media-level corruption (bit rot, bad
   sectors) independent of SQLite's own guarantees, allowing the reader
   to stop cleanly at the last verified-good record instead of crashing
   or silently returning garbage.
5. On next Blender launch, `register()` scans for any `finalized=0`
   file and offers Resume — turning "Blender crashed" into "pick up
   where you left off" rather than "lost two hours of work."
6. Data is **only** ever destroyed by the explicit, confirmation-gated
   Clear/Delete action (§7) — never as a side effect of any other
   button, crash, or addon lifecycle event.

---

## 12. Blender 2.79 Compatibility Concerns

- **Python is 3.5.3**, not 2.x — `array`, `zlib`, `uuid` confirmed usable
  (Phase 1 already uses two of these); `sqlite3` availability in
  Blender's specific bundled interpreter should be the **first thing
  verified** in Phase 2 (Milestone M1/M2) rather than assumed, in
  keeping with "don't hide uncertainty."
- **Custom ID properties** (`obj["key"] = value`, `mesh["key"] = value`)
  are long-standing, stable 2.7x API, safe to rely on for persistent UID
  storage, and they survive `.blend` save/reload "for free," which is
  directly useful for the resume-after-restart story — but note they
  are **visible to the user** in the N-panel "Custom Properties" section
  and are saved into the user's `.blend` file itself; this should be
  flagged to the user (e.g. in documentation) as a side effect of
  recording, not treated as invisible plumbing.
- **No `bpy.app.timers`** (2.80+ only) — Phase 2 continues using the
  `wm.event_timer_add` + modal operator pattern already proven in
  Phase 1.
- **No `bpy.msgbus`** (introduced later) — no granular property-change
  notification API exists in 2.79; polling/diffing via handler+timer
  remains the only option, reinforcing why the debounce/coalesce layer
  (§9) matters so much for keeping that polling affordable.
- **`Object.update_from_editmode()` dependency carries over unchanged**
  from Phase 1, including its documented silent-failure risk — Phase 2
  should specifically load-test this call against large meshes in
  Edit/Sculpt mode (Milestone M7) since it is now on the critical path
  for *every* geometry-changing event, not just a diagnostic counter.
- **Single-threaded constraint:** Blender's Python API is not safely
  usable from a background thread for `bpy.data` access. All detection,
  diffing, and encoding must remain synchronous, small-bounded-cost
  operations on the main thread/modal tick, as Phase 1 already does.
  A background thread *could* safely perform the final disk I/O/SQLite
  commit step on already-extracted plain bytes (no `bpy` access) as a
  future optimization to decouple slow disk latency from the UI — noted
  as a possible later refinement, not required initially.

---

## 13. Future Camera Architecture (Not Implemented Yet)

**Core principle: the recording timeline and the camera/video timeline
are two separate time domains, connected only by an explicit,
user-authored mapping function — never implicitly coupled.**

What the architecture above must already preserve to make this possible
later, without changes to the recording format itself:

- Every event/checkpoint carries an **absolute recording-relative
  timestamp** (already designed in §4/§5) — this is the fixed "source"
  timeline, authored once by capture and never altered afterward.
- `reconstruct_scene(time)` (§8) is designed as a **pure function of an
  arbitrary float time**, not assumed to be called in increasing order
  or exactly once per value. This matters because a future camera-driven
  renderer will call it according to whatever the time-mapping function
  below dictates — potentially non-monotonic, repeated, or holding on
  one value while the camera moves.
- A **separate camera/cinematic track**, logically and physically
  independent of the recording file, expressed in its own **video time**
  domain (e.g. 0–30 seconds) using Blender's own native keyframe/F-Curve
  system on a dedicated camera object — no need to invent a new
  keyframe format, since by the camera-authoring phase we're back in
  normal interactive Blender, not inside the capture pipeline.
- A **time-mapping function** `video_time → replay_time`: itself just
  data (e.g. a small ordered list of `(video_time, replay_time)` anchor
  pairs with interpolation, or an F-Curve), stored and edited
  independently of both the recording file and the camera keyframes.
  This is what lets 2 hours of recording compress into 30 seconds of
  video non-linearly — spending more video-seconds on an interesting
  passage and skipping uneventful stretches — purely by reshaping this
  mapping, with zero changes to the recorded data.
- **Render-time composition** (future, not now): for each output video
  frame, compute `video_time = frame / fps`; map to `replay_time` via
  the mapping function; call `reconstruct_scene(replay_time)` to
  materialize the modelling state; independently evaluate the camera's
  own F-Curves at `video_time`; render the combination. The two
  computations never need to know about each other.

No camera code is written in Phase 2 — this section exists purely to
make sure Phase 2's data model doesn't accidentally foreclose it (e.g.
by baking camera/viewport state into the recording, which is explicitly
avoided: the recorder never captures the user's own viewport camera).

---

## 14. Future FFmpeg Architecture (Not Implemented Yet)

FFmpeg sits **entirely outside and downstream of** the reconstruction
system, with no awareness of recording/event/checkpoint internals:

1. A future renderer drives `reconstruct_scene(replay_time)` per output
   frame (per §13's composition algorithm), materializes it into the
   replay-only Blender scene, positions the independently-keyframed
   camera, and renders that single frame via Blender's normal render
   pipeline (e.g. to a PNG image sequence).
2. FFmpeg is invoked afterward as a **separate, generic, swappable**
   post-process — most simply via Python's `subprocess` calling the
   system `ffmpeg` binary — to encode the rendered image sequence (and,
   out of scope, any audio track) into a final video container.
3. Because this stage only ever consumes already-rendered frames, it has
   zero coupling to the recording format, the event schema, or the
   camera-mapping design — it could be swapped for a different encoder
   entirely without touching anything described in this document.

---

## 15. Major Technical Risks

1. **Identity reuse (`as_pointer()`) is a real correctness risk**, not
   just a style concern: Blender/Python may reuse a freed object's or
   mesh's memory address for a newly created datablock within the same
   session. Phase 1's pointer-keyed diffing is theoretically exposed to
   misattributing a delete+create as a continuation of the same entity.
   Phase 2's switch to persistent UID custom properties (§3) is the
   fix, but UID assignment must happen synchronously at first detection
   — before any ambiguity window — and this needs explicit testing
   (e.g. rapid delete-then-create-of-same-type in one throttle window).
2. **`scene_update_post` coverage gaps are still an open question** from
   Phase 1 (specifically around pure selection changes) — Phase 2 must
   remain defensive via the existing periodic timer sweep, and any
   newly-discovered gap should be documented, not silently assumed away.
3. **Per-event diff cost at scale**: even the cheap CRC32 early-out
   still requires touching every vertex's bytes once per detected
   change; for a very large mesh under continuous high-frequency editing
   (sculpting) on a low-end CPU, this could become a measurable per-tick
   cost. May need an additional adaptive back-off (check less often) for
   meshes above a size threshold — to be measured, not assumed.
4. **`update_from_editmode()` cost/reliability at scale** is now on the
   critical path for every geometry event (not just a diagnostic
   counter) — needs direct performance measurement with large meshes in
   Edit/Sculpt mode on representative low-end hardware.
5. **SQLite commit stalls on slow/old storage** could manifest as
   perceptible UI hitches if the commit cadence is tuned too
   aggressively for the target hardware — needs empirical tuning.
6. **"Full replace" topology strategy has an accepted scaling limit**:
   a workflow that repeatedly performs small topology edits on an
   already-huge mesh (e.g. many sequential loop cuts on a 500k-vert
   mesh) will repeatedly pay the full-array cost with no sparse
   topological patch — a known, deliberate trade-off (§3), not a bug,
   but worth monitoring in real usage and potentially revisiting later
   if it proves problematic in practice.
7. **Clock semantics across Pause/Resume and restart** must be nailed
   down precisely up front (recording-relative elapsed *active* seconds,
   excluding paused duration) since both the scrubbing UI and the future
   camera time-mapping function depend on a single, unambiguous
   definition of "replay time."
8. **Materialization cost during scrubbing** for very large meshes
   (rebuilding a 1M-vertex Blender mesh datablock from Python arrays
   isn't free) may make scrub UX sluggish on low-end hardware for huge
   meshes specifically — flagged as a future concern for the
   materializer design, not solved by this document.
9. **Very large SQLite files over very long sessions** (potentially
   multi-GB) need real-world testing of query/write performance at that
   scale on aged hardware/disks; a future maintenance operation
   (compacting/archiving old checkpoints) may be needed eventually.
10. **`sqlite3` module availability in Blender's specific bundled
    Python build is assumed, not yet verified** — must be the literal
    first check performed in Phase 2, with a documented fallback plan
    (a hand-rolled length-prefixed binary container with its own
    journal) if it turns out to be unavailable or restricted.

---

## 16. Recommended Phase 2 Implementation Milestones

Staged so each milestone is independently testable (continuing Phase
1's practice of testing pure-Python logic without needing Blender
wherever possible), and so no camera/FFmpeg work begins before the
recorder itself is proven:

- **M1 — Identity layer.** Implement UID assignment via custom ID
  properties on objects/mesh datablocks, replacing pointer-based keying.
  Verify UIDs survive rename, duplication, and `.blend` save/reload.
  Verify `sqlite3` import succeeds inside Blender 2.79b's bundled
  Python (Risk #10) — first concrete go/no-go check for the whole format
  decision in §6.
- **M2 — Replay file format.** Implement the `.rm79` SQLite schema
  (`meta`/`checkpoints`/`events`), writer and reader primitives,
  checksum validation, version field. Unit-test write→read round-trips
  with synthetic data, no Blender required (same approach as Phase 1's
  `snapshot.py` tests).
- **M3 — Recording pipeline.** Wire Phase 1's detection stack (handler +
  timer + throttle) into the new encoder/writer; implement the
  debounce/coalescing layer and the sparse-vs-full mesh byte-cost
  heuristic (§3/§9). Still read-only with respect to reconstruction —
  just prove correct, bounded-size capture.
- **M4 — Lifecycle state machine.** Implement New/Record/Pause/Resume/
  Stop/Save/Auto-save/Clear exactly per §7, plus the crash-recovery scan
  in `register()`. Re-run Phase 1's manual test matrix, now additionally
  verifying a real, inspectable `.rm79` file results after each test
  (e.g. via the standalone `sqlite3` CLI, independent of the addon).
- **M5 — Reconstruction.** Implement `reconstruct_scene(time)` (§8)
  against synthetic recordings, unit-tested without Blender, mirroring
  how Phase 1 validated `diff_snapshots()`.
- **M6 — Materializer + basic scrub UI.** Push reconstructed state into
  a dedicated replay-only set of Blender objects/meshes with a simple
  time-slider UI, proving the whole pipeline visually in Blender 2.79b.
  First real, measured performance numbers (not estimates) on the
  target low-end hardware across small/medium/large meshes.
- **M7 — Stress testing & tuning.** Long-duration synthetic session
  (scripted edits simulating hours), large-mesh tests (100k/1M verts),
  old/slow-disk I/O testing, and empirical tuning of every constant
  proposed in this document (checkpoint interval, commit cadence,
  debounce timeouts, sparse/dense crossover) based on measured data.

**Only after M7** should Phase 3 (camera keyframe authoring, per §13)
and Phase 4 (FFmpeg rendering pipeline, per §14) begin.
