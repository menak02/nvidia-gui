"""Domain layer — pure Python, no I/O.

Holds the core abstractions of nvidia-gui: the games a user owns, the per-game
and global settings they tune, the DLSS versions available to swap in, and the
live GPU snapshot surfaced for telemetry. Nothing here imports GTK, subprocess,
or the filesystem. Keeping it pure means it is trivially unit-testable and that
the persistence/telemetry adapters can be swapped without touching business
rules.
"""
