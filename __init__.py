# -*- coding: utf-8 -*-
"""
ReplayProbe79 -- Phase 1 Diagnostic Probe

Part of the long-term ReplayMod79 project (a Minecraft-Replay-Mod-style
instrumentation/playback system for Blender modelling sessions).

THIS ADDON ONLY IMPLEMENTS THE DIAGNOSTIC PROBE.
It does not record a replay, does not play anything back, and does not
touch rendering/FFmpeg. See README.md for full scope notes.

Registration safety:
    register()/unregister() below NEVER touch bpy.context.scene (or any
    other context-dependent attribute). Blender 2.79 can call register()
    with a restricted context (e.g. during startup / background mode),
    and bpy.context.scene is one of the attributes that is not guaranteed
    to exist at that time. Anything here that needs scene data uses
    bpy.data.scenes instead, and only from inside operator execution
    (normal context), never from register()/unregister() themselves.
"""

bl_info = {
    "name": "ReplayProbe79 (Phase 1 Diagnostic Probe)",
    "author": "ReplayMod79 project",
    "version": (0, 1, 0),
    "blender": (2, 79, 0),
    "location": "View3D > Tool Shelf / Sidebar (N) > Replay Probe",
    "description": (
        "Phase 1 diagnostic instrumentation for the future ReplayMod79 "
        "addon. Observes which Blender 2.79 scene/object/mesh changes can "
        "be reliably detected. Does NOT record or replay anything."
    ),
    "warning": "Diagnostic tool only. Not the final recorder.",
    "wiki_url": "",
    "category": "Development",
}

import bpy

from . import probe
from . import recording
from . import ui


ALL_CLASSES = tuple(probe.PROBE_CLASSES) + tuple(recording.RECORDING_CLASSES) + tuple(ui.UI_CLASSES)


def register():
    for cls in ALL_CLASSES:
        bpy.utils.register_class(cls)
    ui._register_properties()

    # Defensive: in case a previous session left the handler installed
    # (e.g. Blender crashed, or disable() was skipped), make sure we
    # start from a clean slate. This only touches bpy.app.handlers, a
    # plain list, never bpy.context.
    probe._remove_handler()
    # Same defensive reasoning for Phase 2.2: never start up already
    # believing a recording session is active.
    recording.shutdown()


def unregister():
    # Make sure monitoring is fully stopped and handlers detached before
    # classes disappear. Order does not matter: recording.shutdown() only
    # detaches its own probe listener/clock, it does not depend on probe
    # still being registered.
    recording.shutdown()
    probe.shutdown()

    ui._unregister_properties()

    for cls in reversed(ALL_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:
            print("ReplayProbe79: error unregistering %s: %s" % (cls, exc))


if __name__ == "__main__":
    register()
