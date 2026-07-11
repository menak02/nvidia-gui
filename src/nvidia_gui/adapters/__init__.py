"""Adapters — concrete, I/O-bound implementations of application ports.

Each module here is a *technology choice* (nvidia-smi for telemetry, TOML for
persistence, the filesystem for DLL swaps). None of them know about GTK; the UI
only reaches them through :class:`application.use_cases.UseCases`.
"""
