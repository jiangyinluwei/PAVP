"""Auto-start on boot support (Windows Registry Run key).

When auto-start is enabled:
- Always starts the PAVP proxy server (PAVP-Proxy) on boot.
- Optionally starts the Streamlit UI (PAVP-UI) if auto_start_ui is True.
"""
from __future__ import annotations

import sys
from pathlib import Path


def set_auto_start(enabled: bool, port: int | None = None, auto_start_ui: bool = False) -> None:
    """Enable or disable auto-start in Windows registry.

    When enabled, always starts the PAVP proxy server on boot.
    If *auto_start_ui* is True, also starts the Streamlit UI control panel.

    Registry entries:
      - PAVP-Proxy: always set when enabled, starts the proxy server.
      - PAVP-UI:    set only when enabled AND auto_start_ui is True.
    """
    if sys.platform != "win32":
        return

    if port is None:
        from .settings import load as _load, DEFAULT_PORT
        try:
            port = _load().get("proxy_port", DEFAULT_PORT)
        except Exception:
            port = DEFAULT_PORT

    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    )
    try:
        if enabled:
            python_exe = sys.executable
            python_dir = Path(python_exe).parent
            pythonw = python_dir / "pythonw.exe"
            if pythonw.exists():
                python_exe = str(pythonw)

            # Always start the proxy server on boot
            proxy_cmd = (
                f'"{python_exe}" -m pavp.proxy_server'
                f" --host 0.0.0.0 --port {port}"
            )
            winreg.SetValueEx(key, "PAVP-Proxy", 0, winreg.REG_SZ, proxy_cmd)

            # Optionally start the Streamlit UI
            if auto_start_ui:
                ui_path = Path(__file__).resolve().parent / "ui.py"
                ui_cmd = (
                    f'"{python_exe}" -m streamlit run "{ui_path}"'
                    f" --server.port 8501"
                )
                winreg.SetValueEx(key, "PAVP-UI", 0, winreg.REG_SZ, ui_cmd)
            else:
                try:
                    winreg.DeleteValue(key, "PAVP-UI")
                except FileNotFoundError:
                    pass
        else:
            # Remove both entries when auto-start is disabled
            for name in ("PAVP-Proxy", "PAVP-UI"):
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    finally:
        winreg.CloseKey(key)