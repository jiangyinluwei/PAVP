"""PAVP entry point for PyInstaller bundle.

Supports two modes:
  - Default: launches the Streamlit UI (replaces run.ps1).
  - --headless: starts the proxy server only (used by auto-start on boot).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _ensure_settings() -> None:
    """Create settings.json template if it doesn't exist."""
    settings_path = Path.home() / ".pavp" / "settings.json"
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        template = {
            "litellm_master_key": "sk-pavp-local",
            "proxy_port": 5401,
            "plan_0": {"model": "", "openai_api": "", "openai_base_url": "", "anthropic_api": "", "anthropic_base_url": ""},
            "act_0": {"model": "", "openai_api": "", "openai_base_url": "", "anthropic_api": "", "anthropic_base_url": ""},
            "current_plan_id": 0,
            "current_act_id": 0,
            "cc_bin": "claude",
            "act_max_budget": 3.0,
            "act_max_turns": 40,
            "act_timeout": 600,
            "loop_mode": "auto",
            "auto_start": True,
            "auto_start_ui": False,
        }
        settings_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8-sig",
        )
        print(f"[PAVP] Settings template created at {settings_path}")


def main() -> None:
    """Main entry point."""
    _ensure_settings()

    # Headless mode: start proxy server only (used by auto-start).
    if "--headless" in sys.argv:
        from pavp.proxy_server import main as proxy_main

        # Strip --headless, pass through --host and --port to proxy_server.
        args = [a for a in sys.argv[1:] if a != "--headless"]
        sys.argv = [sys.argv[0]] + args
        proxy_main()
        return

    # Default: launch Streamlit UI.
    from streamlit.web import cli as stcli

    # In a PyInstaller onedir bundle the running entry_point sits at
    # <_internal>/entry_point.py while ui.py is shipped as data under
    # <_internal>/pavp/ui.py. When running from source they share the
    # same pavp/ directory.
    if getattr(sys, "frozen", False):
        bundle_dir = Path(getattr(sys, "_MEIPASS", "."))
        ui_path = bundle_dir / "pavp" / "ui.py"
    else:
        ui_path = Path(__file__).resolve().parent / "ui.py"
    sys.argv = [
        "streamlit", "run", str(ui_path),
        # PyInstaller bundles Streamlit outside site-packages, which makes
        # global.developmentMode default to True and forbid setting
        # server.port. Run in production mode so the explicit port works.
        "--global.developmentMode", "false",
        "--server.port", "8501",
    ]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()