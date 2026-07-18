---
name: code-governance
description: Enforces codebase governance, static security gating (similar to VibeGuard), automated refactoring, reverse engineering guidelines, and secure, autonomous development workflows.
---

# Code Governance & Agent Security (VibeGuard Mode)

This skill governs the security, structure, and quality standards for automated agent workflows inside the `nvidia-gui` workspace. It acts as an inline audit checker to ensure all generated code is clean, safe, and production-ready.

---

## 1. Automated Quality & Code Improvements

### Code Stripping & Dead Code Elimination
- Proactively identify and delete unused functions, dead helper methods, or duplicate card builders.
- When refactoring, strip out legacy comments that are no longer accurate while keeping developer-provided context docstrings.

### Decoupling & Modular Architecture
- **Adapter Decoupling**: Backend hardware-access layers (e.g., calling `nvidia-smi`, writing to `modprobe.d`, or querying `xrandr`/`vibrant`) must be fully decoupled from the UI widgets.
- **Dependency Injection**: Maintain the composition pattern established in `composition_root.py`. Never instantiate use cases or adapters directly inside UI presentation views.

---

## 2. Reverse Engineering & Auditing Protocols

### Configuration File Tracking
- When investigating external game configurations (like Steam's `localconfig.vdf` or `appmanifest.acf` files):
  - Always trace keys defensively (keys can be case-sensitive, optionally quoted, or contain control escape sequences).
  - Use our robust parsing abstractions (`vdf_lite.py`, `acf_parser.py`) rather than raw regular expression replacement.

### Driver & Environment Overrides
- Proactively audit env keys mapped to game launches. Ensure any new environment override aligns with the environment sanitizer loop in `launch_option_steam.py` to prevent duplication or override clashes.

---

## 3. Real-Time Security & "VibeGuard" Gating

### Preventing Command Injection
- Never invoke raw commands via shell execution (`shell=True` in subprocesses) unless absolutely unavoidable. Always pass arguments as a structured array (`list[str]`).
- All user-supplied inputs (like custom Steam launch paths or manual modprobe variables) must be parsed, stripped of control characters, and validated before execution.

### Secret & Credential Scanning
- Ensure that no developer access tokens (PATs), custom tokens, or system passwords are written or logged to disk during agent workflows.
- Exclude settings files (like `settings.toml` or custom configurations containing system-specific secrets) from git commits.

### Sandboxed Execution
- Before running test commands or launching the UI wrapper, verify that the environment does not override root permissions.
- Gracefully handle system missing resources (e.g., when run on a system without a display server or without the `nvidia-smi` binary).
