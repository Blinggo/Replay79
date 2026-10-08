# -*- coding: utf-8 -*-
"""
ui.py -- ReplayProbe79

Responsible for:
    * The Blender UI panel.
    * Displaying diagnostic information collected by probe.py.

This module defines the WindowManager.replayprobe79_log_limit property.
Adding a property to a class (bpy.types.WindowManager.x = ...) is safe to
do at register() time -- it does NOT read bpy.context, it only mutates
the WindowManager *class*, which is always available.
"""

import bpy

from . import probe
from . import recording


COUNTER_LABELS = (
    ('scene_updates', 'Scene updates (raw)'),
    ('processed_updates', 'Scene updates (processed)'),
    ('object_changes', 'Object changes'),
    ('transform_changes', 'Transform changes'),
    ('mesh_changes', 'Mesh changes'),
    ('topology_changes', 'Topology changes'),
    ('material_changes', 'Material changes'),
    ('modifier_changes', 'Modifier changes'),
    ('mode_changes', 'Mode changes'),
    ('selection_changes', 'Selection changes'),
    ('visibility_changes', 'Visibility changes'),
    ('active_object_changes', 'Active object changes'),
)

MAX_LOG_LINES_SHOWN = 15


class REPLAYPROBE79_PT_panel(bpy.types.Panel):
    bl_label = "Replay Probe 79"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Replay Probe"

    def draw(self, context):
        layout = self.layout
        status = probe.get_status()

        row = layout.row(align=True)
        row.operator("replayprobe79.start", text="Start Probe")
        row.operator("replayprobe79.stop", text="Stop Probe")
        layout.operator("replayprobe79.clear", text="Clear")

        wm = context.window_manager
        if hasattr(wm, 'replayprobe79_log_limit'):
            layout.prop(wm, 'replayprobe79_log_limit', text="Log Limit")

        layout.separator()

        box = layout.box()
        box.label(text="Status")
        box.label(text="Probe: %s" % ("Monitoring" if status['running'] else "Stopped"))
        box.label(text="Elapsed: %.1fs" % status['elapsed'])
        box.label(text="Scenes tracked: %d" % status['scene_count'])

        layout.separator()

        # Recording (Phase 2.2) -- deliberately its own, visually distinct
        # box. "Probe: Monitoring" above and "Recording: STOPPED" below is
        # a valid, expected combination: Phase 1 diagnostics can run with
        # no recording session active at all.
        rec_status = recording.get_status()

        rbox = layout.box()
        rbox.label(text="Recording (Phase 2.2)")
        row = rbox.row(align=True)
        row.operator("replayprobe79.start_recording", text="Start Recording")
        row.operator("replayprobe79.stop_recording", text="Stop Recording")
        rbox.operator("replayprobe79.clear_recording", text="Clear Last Session")

        rbox.label(text="Recording: %s" % rec_status['state'])
        rbox.label(text="Recording Time: %.1fs" % rec_status['elapsed'])
        rbox.label(text="Sequence: %d" % rec_status['sequence'])
        rbox.label(text="Events logged: %d" % rec_status['log_count'])
        rbox.label(text=rec_status['last_message'])
        if not rec_status['probe_running'] and rec_status['state'] == recording.STATE_RECORDING:
            rbox.label(text="Warning: Probe is stopped -- no new events "
                             "are being captured!", icon='ERROR')

        rbox2 = layout.box()
        rbox2.label(text="Recording counters (current/last session):")
        for key, label in COUNTER_LABELS:
            rbox2.label(text="%s: %d" % (label, rec_status['counters'].get(key, 0)))

        box = layout.box()
        box.label(text="Counters:")
        for key, label in COUNTER_LABELS:
            box.label(text="%s: %d" % (label, status['counters'].get(key, 0)))

        box = layout.box()
        box.label(text="Last detected event:")
        box.label(text=status['last_event'])
        box.label(text="Last detected object:")
        box.label(text=status['last_object'])
        box.label(text="Last detected mesh:")
        box.label(text=status['last_mesh'])

        box = layout.box()
        box.label(text="Event log (most recent last, showing up to %d):"
                  % MAX_LOG_LINES_SHOWN)
        log_lines = status['log'][-MAX_LOG_LINES_SHOWN:]
        if not log_lines:
            box.label(text="(empty)")
        else:
            for line in log_lines:
                box.label(text=line)


def _register_properties():
    if not hasattr(bpy.types.WindowManager, 'replayprobe79_log_limit'):
        bpy.types.WindowManager.replayprobe79_log_limit = bpy.props.IntProperty(
            name="Log Limit",
            description="Maximum number of diagnostic events retained in memory",
            default=probe.DEFAULT_LOG_LIMIT,
            min=10,
            max=2000,
        )


def _unregister_properties():
    if hasattr(bpy.types.WindowManager, 'replayprobe79_log_limit'):
        try:
            del bpy.types.WindowManager.replayprobe79_log_limit
        except Exception:
            pass


UI_CLASSES = (
    REPLAYPROBE79_PT_panel,
)
