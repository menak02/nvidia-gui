"""Application layer — ports (interfaces) and use cases (orchestrators).

Ports are abstract: they describe the side *effects* the application needs
(read a GPU snapshot, persist a profile, swap a DLL). Adapters in
``nvidia_gui.adapters`` implement them concretely (nvidia-smi subprocess, TOML
files, filesystem copy). The UI talks only to use cases, never to adapters
directly — so swapping an adapter (e.g. mocking nvidia-smi for tests) is one
line at the composition root.
"""
