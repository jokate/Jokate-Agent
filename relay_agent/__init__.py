"""Relay agent server: stages pass a compact baton instead of the full conversation."""

from .envpath import claude_install_dirs, refresh

# tools installed after this process (or the terminal that started it) opened, e.g. claude or uv
refresh(claude_install_dirs())
