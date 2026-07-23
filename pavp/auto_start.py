"""Auto-start on boot support (Windows Registry Run key).

Registers/unregisters the PAVP Streamlit UI to start automatically
when Windows boots. The UI will then auto-start the proxy server
if the auto_start setting is enabled.
"""
from __future__ import annotations

import sys
from pathlib import Path


def set_auto_start(enabled: bool, port: int | None = None) -> None:
    """Enable or disable auto-start in Windows registry.

    Starts the Streamlit UI (control panel) on boot.
    The UI will then auto-start the proxy server if enabled in settings.
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
            # Use pythonw.exe if available for silent (no console window) startup
            python_dir = Path(python_exe).parent
            pythonw = python_dir / "pythonw.exe"
            if pythonw.exists():
                python_exe = str(pythonw)
            # Start the Streamlit UI (control panel) on boot.
            # The UI will auto-start the proxy server when auto_start is enabled.
            ui_path = Path(__file__).resolve().parent / "ui.py"
            cmd = f'"{python_exe}" -m streamlit run "{ui_path}" --server.port 8501'
            winreg.SetValueEx(key, "PAVP-Proxy", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "PAVP-Proxy")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)
