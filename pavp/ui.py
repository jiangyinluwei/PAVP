"""PAVP Streamlit UI - Configuration panel + proxy launcher

Proxy persists in background (tracked by PID file).
UI is a control panel: start/stop proxy, view config, check logs.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st

try:
    from .settings import load as load_settings, settings_path, save_field, DEFAULT_PORT
    from .auto_start import set_auto_start
except ImportError:
    from pavp.settings import load as load_settings, settings_path, save_field, DEFAULT_PORT
    from pavp.auto_start import set_auto_start

import httpx


def _html_escape(text: str) -> str:
    """Escape text for safe HTML embedding."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ============================================================================
# PID file for proxy persistence
# ============================================================================
_PID_FILE = Path.home() / ".pavp" / "proxy.pid"
_LOG_FILE = Path(_PROJECT_ROOT) / "log" / "pavp_proxy.log"
# 手动停止标记文件：用户点击 Stop Proxy 时写入时间戳，防止页面刷新后 auto_start 重新启动
_MANUAL_STOP_FILE = Path.home() / ".pavp" / "manual_stop.txt"


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID exists. Platform-safe.

    On Unix: os.kill(pid, 0) is safe (checks existence).
    On Windows: os.kill(pid, 0) calls TerminateProcess — KILLS the process!
               So we use ctypes to OpenProcess with SYNCHRONIZE only.
    """
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    else:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED (5) = process exists but can't open with SYNCHRONIZE
        return kernel32.GetLastError() == 5


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.1):
    """Wait until *predicate* returns a truthy value, or *timeout* elapses.

    Polls *predicate* every *interval* seconds. Returns the truthy result
    on success, or ``None`` on timeout.  Use this instead of fixed
    ``time.sleep()`` so that the wait ends as soon as the condition is met.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return None


def _is_proxy_running(port: int | None = None) -> tuple[bool, bool]:
    """Check if proxy is running: (process_alive, models_ready).

    process_alive: PID file exists, process running, health check passes.
    models_ready: proxy /info reports ready=true (model configs present).

    If port is provided, use it for health/info checks.
    Otherwise read from settings (may differ from UI port).
    """
    if not _PID_FILE.exists():
        return False, False
    try:
        pid = int(_PID_FILE.read_text().strip())
        # Check if process exists (platform-safe: os.kill(pid, 0)
        # TERMINATES the process on Windows, so use ctypes instead.)
        if not _pid_alive(pid):
            _PID_FILE.unlink(missing_ok=True)
            return False, False
        # Determine port to check
        if port is None:
            try:
                s = load_settings()
                port = s.get("proxy_port", DEFAULT_PORT)
            except Exception:
                return False, False
        # Verify via health + info endpoints
        if not _proxy_health(port):
            return False, False
        info = _proxy_info(port)
        models_ready = info.get("ready", False) if info else False
        return True, models_ready
    except (ValueError, IOError):
        return False, False


def _read_pid() -> int:
    try:
        return int(_PID_FILE.read_text().strip())
    except (ValueError, IOError):
        return 0


def _is_recent_manual_stop(timeout: float = 60.0) -> bool:
    """检查用户是否在最近 timeout 秒内手动停止了代理。

    用于防止页面刷新后 auto_start 重新启动手动停止的代理。
    """
    try:
        if not _MANUAL_STOP_FILE.exists():
            return False
        mtime = _MANUAL_STOP_FILE.stat().st_mtime
        return (time.time() - mtime) < timeout
    except Exception:
        return False


def _clear_manual_stop():
    """清除手动停止标记——用户主动启动/重启代理时调用。"""
    _MANUAL_STOP_FILE.unlink(missing_ok=True)


def _start_proxy(port: int):
    """Start proxy as detached background process.

    The proxy auto-finds a free port (starting from *port*) and updates
    settings.json. After startup, we re-read settings to get the actual port.
    """
    # 用户主动启动代理时，清除手动停止标记
    _clear_manual_stop()

    # Stop old proxy by PID if one is running
    old_pid = _read_pid()
    if old_pid:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(old_pid), "/F", "/T"],
                               capture_output=True, text=True)
            else:
                os.kill(old_pid, signal.SIGTERM)
        except Exception:
            pass
        _PID_FILE.unlink(missing_ok=True)
        _wait_until(lambda: not _pid_alive(old_pid), timeout=3.0, interval=0.1)

    # Remove stale PID file before starting
    _PID_FILE.unlink(missing_ok=True)

    # Use python.exe (not pythonw.exe) for proxy subprocess: pythonw has no
    # stdout by default, which breaks sys.stdout.reconfigure in proxy_server.
    python_exe = sys.executable
    if sys.platform == "win32":
        python_dir = Path(python_exe).parent
        python_normal = python_dir / "python.exe"
        if python_normal.exists():
            python_exe = str(python_normal)

    # Ensure log directory exists before opening log file
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(_LOG_FILE, "w")

    # On Windows, CREATE_NO_WINDOW (0x08000000) prevents a blank console
    # window from popping up when launching python.exe as a subprocess.
    _creationflags = 0
    if sys.platform == "win32":
        _creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000

    try:
        proc = subprocess.Popen(
            [python_exe, "-m", "pavp.proxy_server",
             "--host", "0.0.0.0", "--port", str(port)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=_PROJECT_ROOT,
            creationflags=_creationflags,
        )
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(proc.pid))

        # Wait for process to either exit (failed) or start serving (success).
        # Poll up to 3s — break as soon as the process exits or health responds.
        if _wait_until(lambda: proc.poll() is not None, timeout=3.0, interval=0.1):
            # Process exited immediately — startup failed.
            # Read the log to show the actual error.
            _PID_FILE.unlink(missing_ok=True)
            log_file.close()
            if _LOG_FILE.exists():
                log_tail = _LOG_FILE.read_text(encoding="utf-8", errors="replace").strip()
                if log_tail:
                    st.session_state.proxy_start_error = log_tail[-500:]
                else:
                    st.session_state.proxy_start_error = f"Proxy exited with code {proc.returncode} (no log output)"
            else:
                st.session_state.proxy_start_error = f"Proxy exited with code {proc.returncode}"
            return  # Don't raise; error stored in session state for display

        # Clear any previous startup error on successful process launch
        st.session_state.proxy_start_error = None

        # Re-read settings to get the actual port (proxy may have auto-incremented)
        try:
            new_settings = load_settings()
            actual_port = new_settings.get("proxy_port", port)
            if actual_port != port:
                st.session_state.proxy_port = actual_port
                port = actual_port
                st.session_state.settings_cache = new_settings
        except Exception:
            pass

        # Wait for proxy HTTP server to become ready (up to 10s)
        _http_ready = False
        for _ in range(20):
            if _proxy_health(port):
                _http_ready = True
                break
            time.sleep(0.5)
        if not _http_ready:
            _stop_proxy()
            log_file.close()
            st.session_state.proxy_start_error = (
                f"Proxy process started but HTTP server did not become "
                f"reachable within 10s on port {port}. Check log at {_LOG_FILE}"
            )
            return

        # Verify model connectivity: make a test chat completion call.
        # This catches the common boot scenario where the network is not
        # yet ready for outbound API calls. Retry for up to 30 seconds.
        _model_ok = _wait_model_ready(port, timeout=30)
        if not _model_ok:
            st.session_state.proxy_start_error = (
                "Proxy started but model API is unreachable after 30s. "
                "This may happen on boot before the network is fully up. "
                "Try 'Restart Proxy' once the network is ready."
            )
            # Don't kill the proxy — let it run; user can restart later.
    except Exception as e:
        _PID_FILE.unlink(missing_ok=True)
        log_file.close()
        st.session_state.proxy_start_error = f"Failed to start proxy: {e}"


def _stop_proxy():
    """Stop proxy by PID"""
    # 记录手动停止时间戳，防止页面刷新后 auto_start 重新启动
    _MANUAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MANUAL_STOP_FILE.write_text(str(time.time()))

    pid = _read_pid()
    result_parts = []
    if pid:
        result_parts.append(f"Killing PID={pid}")
        try:
            if sys.platform == "win32":
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True, text=True,
                )
                result_parts.append(f"exit={r.returncode}")
                out = (r.stdout or "").strip()
                if out:
                    result_parts.append(out)
                err = (r.stderr or "").strip()
                if err:
                    result_parts.append(f"err: {err}")
            else:
                os.kill(pid, signal.SIGTERM)
                result_parts.append("SIGTERM sent")
        except Exception as e:
            result_parts.append(f"exception: {e}")
    else:
        result_parts.append("No PID in file")

    _PID_FILE.unlink(missing_ok=True)

    if pid:
        # Wait for process to actually terminate (poll up to 3s)
        _wait_until(lambda: not _pid_alive(pid), timeout=3.0, interval=0.1)
        if _pid_alive(pid):
            result_parts.append(f"⚠️ PID {pid} still alive")
        else:
            result_parts.append(f"PID {pid} terminated")

    result = " | ".join(result_parts)
    print(f"[StopProxy] {result}", flush=True)
    st.session_state.proxy_stop_result = result


def _proxy_health(port: int) -> bool:
    """Check if proxy responds to health endpoint"""
    try:
        r = httpx.get(f"http://localhost:{port}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _proxy_info(port: int) -> dict | None:
    """Check proxy /info endpoint, returns info dict or None if unreachable."""
    try:
        r = httpx.get(f"http://localhost:{port}/info", timeout=2)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _wait_model_ready(port: int, timeout: float = 30.0) -> bool:
    """Test proxy model connectivity by making a real chat completion call.

    Retries until a successful response or timeout is reached.
    Returns True if the model API is reachable, False otherwise.
    Stores the last error detail in st.session_state.proxy_last_test for display.
    """
    s = load_settings()
    key = s.get("litellm_master_key", "sk-pavp-local")
    url = f"http://localhost:{port}/v1/chat/completions"
    payload = {
        "model": "pavp",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    deadline = time.time() + timeout
    attempt = 0
    last_error = ""
    while time.time() < deadline:
        attempt += 1
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=min(15, deadline - time.time()),
            )
            if r.status_code == 200:
                st.session_state.proxy_last_test = f"OK (attempt {attempt})"
                return True
            # Capture the actual response for diagnostics
            try:
                body = r.json()
                last_error = f"HTTP {r.status_code}: {body.get('error', {}).get('message', str(body))}"
            except Exception:
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            # Non-200: auth/config error — not a transient network issue
            if r.status_code in (401, 502):
                st.session_state.proxy_last_test = last_error
                return False
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        time.sleep(min(0.5, deadline - time.time()))
    st.session_state.proxy_last_test = f"Timeout after {timeout}s ({attempt} attempts). Last: {last_error}"
    return False


# ============================================================================
# Page config
# ============================================================================


def _run_health_check(port: int):
    """Run combined health check + model test, store results in session state."""
    results = {"health": None, "model": None, "timestamp": time.time()}
    health_ok = _proxy_health(port)
    results["health"] = health_ok
    if health_ok:
        s2 = load_settings()
        key = s2.get("litellm_master_key", "sk-pavp-local")
        try:
            r = httpx.post(
                f"http://localhost:{port}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "pavp", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=30,
            )
            if r.status_code == 200:
                results["model"] = "OK — proxy working correctly"
            else:
                try:
                    err = r.json().get("error", {}).get("message", r.text[:200])
                except Exception:
                    err = r.text[:200]
                results["model"] = f"HTTP {r.status_code}: {err}"
        except Exception as e:
            results["model"] = f"Connection failed: {type(e).__name__}: {e}"
    else:
        results["model"] = "Skipped (proxy not healthy)"
    st.session_state.health_results = results


st.set_page_config(page_title="PAVP Config", page_icon="⚙️", layout="wide")

# --- Custom CSS: running animation + log scroll ---
st.markdown("""
<style>
/* --- Compact layout: reduce top spacing --- */
.block-container {
    padding-top: 2rem !important;
    padding-bottom: 1rem !important;
}
h1 {
    padding-top: 0 !important;
    margin-top: 0 !important;
}
h2, h3 {
    margin-top: 0.6rem !important;
    margin-bottom: 0.4rem !important;
}
hr {
    margin: 0.5rem 0 !important;
}
.element-container {
    margin-bottom: 0.3rem !important;
}

/* Replace default "Running" indicator with rotating dots */
div[data-testid="stStatusWidget"] svg {
    display: none !important;
}
div[data-testid="stStatusWidget"] > div > div {
    width: 24px;
    height: 24px;
    position: relative;
    animation: pavp-spin 1.5s linear infinite;
}
div[data-testid="stStatusWidget"] > div > div::before,
div[data-testid="stStatusWidget"] > div > div::after {
    content: "";
    position: absolute;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #ff4b4b;
    top: 50%;
    left: 50%;
    margin: -3px 0 0 -3px;
}
div[data-testid="stStatusWidget"] > div > div::before {
    transform: translateY(-9px);
}
div[data-testid="stStatusWidget"] > div > div::after {
    transform: translateY(9px);
}
@keyframes pavp-spin {
    to { transform: rotate(360deg); }
}

/* Proxy log scrollable container */
.pavp-log-box {
    max-height: 420px;
    overflow-y: scroll !important;
    border: 1px solid rgba(128,128,128,0.25);
    border-radius: 6px;
    padding: 8px;
    background: rgba(0,0,0,0.03);
}

/* URL truncation in sidebar captions */
 .sidebar-url {
     display: inline-block;
     max-width: 180px;
     overflow: hidden;
     text-overflow: ellipsis;
     white-space: nowrap;
     vertical-align: middle;
 }

/* Fixed-width sidebar — fully disable CSS resize property + hide any resize handle */
section[data-testid="stSidebar"] {
    min-width: 280px !important;
    max-width: 280px !important;
    width: 280px !important;
    resize: none !important;
}
section[data-testid="stSidebar"]::before,
section[data-testid="stSidebar"]::after {
    display: none !important;
}
/* Ensure collapsed sidebar has zero width */
section[data-testid="stSidebar"][aria-expanded="false"] {
    min-width: 0 !important;
    max-width: 0 !important;
    width: 0 !important;
}
/* Explicitly suppress col-resize cursors within the entire app shell container */
div[data-testid="stAppViewContainer"] *,
div[data-testid="stAppViewContainer"] *::before,
div[data-testid="stAppViewContainer"] *::after {
    cursor: default !important;
}
/* Restore interactive cursors for clickable/input elements */
div[data-testid="stAppViewContainer"] button,
div[data-testid="stAppViewContainer"] a,
div[data-testid="stAppViewContainer"] input,
div[data-testid="stAppViewContainer"] select,
div[data-testid="stAppViewContainer"] textarea,
div[data-testid="stAppViewContainer"] [role="button"],
div[data-testid="stAppViewContainer"] [role="tab"],
div[data-testid="stAppViewContainer"] [role="link"],
div[data-testid="stAppViewContainer"] label,
div[data-testid="stAppViewContainer"] summary,
div[data-testid="stAppViewContainer"] [contenteditable="true"] {
    cursor: auto !important;
}
div[data-testid="stAppViewContainer"] button,
div[data-testid="stAppViewContainer"] a,
div[data-testid="stAppViewContainer"] [role="button"],
div[data-testid="stAppViewContainer"] [role="tab"],
div[data-testid="stAppViewContainer"] [role="link"] {
    cursor: pointer !important;
}
div[data-testid="stAppViewContainer"] input,
div[data-testid="stAppViewContainer"] textarea,
div[data-testid="stAppViewContainer"] [contenteditable="true"] {
    cursor: text !important;
}

/* Compact sidebar layout: reduce padding, margins, and line spacing */
section[data-testid="stSidebar"] .block-container {
    padding-top: 0.5rem !important;
    padding-bottom: 0 !important;
}
section[data-testid="stSidebar"] hr {
    margin: 0.25rem 0 2rem 0 !important;
}
section[data-testid="stSidebar"] .element-container {
    margin-bottom: 0.1rem !important;
}
section[data-testid="stSidebar"] .stCaption {
    margin-bottom: 0 !important;
    padding-bottom: 0 !important;
}
section[data-testid="stSidebar"] .stMarkdown {
    margin-bottom: 0 !important;
}
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
    gap: 0.15rem !important;
}
section[data-testid="stSidebar"] .stButton {
    margin-bottom: 0.15rem !important;
}

/* --- PAVP orchestrator status indicator --- */
.pavp-header {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 0.5rem;
}
.pavp-caption {
    color: var(--text-color);
    opacity: 0.75;
}
.pavp-status {
    font-size: 0.8em;
    white-space: nowrap;
}
.pavp-loop {
    color: var(--text-color);
    opacity: 0.7;
    font-weight: 500;
}
.pavp-phase {
    font-weight: 700;
    letter-spacing: 0.3px;
}

/* Animated dots: reveals "... " character by character in a 3s loop */
.pavp-dots {
    display: inline-block;
    overflow: hidden;
    white-space: nowrap;
    vertical-align: bottom;
    width: 0.4em;
    animation: pavp-dot-cycle 3s steps(3, end) infinite;
    font-weight: 700;
    letter-spacing: 0.3px;
}
@keyframes pavp-dot-cycle {
    0%   { width: 0.4em; }
    33%  { width: 0.8em; }
    66%  { width: 1.2em; }
    100% { width: 1.4em; }
}
 </style>""", unsafe_allow_html=True)

st.title("PAVP Proxy")

# ============================================================================
# Orchestrator status indicator (reads from state file written by orchestrator)
# ============================================================================
_STATE_FILE = Path.home() / ".pavp" / "current_state.json"
_API_CALL_TIMEOUT = 30  # seconds — 匹配 LLM 超时 (30s)，防止思考超时误显示 Standby



def _read_orchestrator_state() -> dict | None:
    """Read orchestrator state file. Returns None if proxy is not running or last API call is stale."""
    try:
        if not _STATE_FILE.exists():
            return None

        # 检查代理进程是否存活 — 如果代理未运行，状态文件是残留的旧数据
        if not _PID_FILE.exists():
            return None
        try:
            pid = int(_PID_FILE.read_text().strip())
            if not _pid_alive(pid):
                return None
        except (ValueError, OSError):
            return None

        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        # Use last_api_call if available, otherwise fall back to updated_at
        ts = data.get("last_api_call") or data.get("updated_at", "")
        if not ts:
            return None
        age = time.time() - datetime.fromisoformat(ts).timestamp()
        if age > _API_CALL_TIMEOUT:  # 30 seconds
            return None
        return data
    except Exception:
        return None


def _status_html() -> str:
    """Build the HTML for the PAVP status indicator."""
    state = _read_orchestrator_state()
    if state is None:
        st.session_state.pop("_pavp_prev_fsm", None)
        st.session_state.pop("_pavp_prev_iter", None)
        return '<span class="pavp-status">Standby...</span>'

    fsm = state.get("fsm_state", "")
    iteration = state.get("iteration", 0)
    task_key_count = state.get("task_key_count", 0)
    task_key_sample = state.get("task_key_sample", "")

    # DONE / FAILED 表示请求真正完成
    if fsm in ("DONE", "FAILED"):
        st.session_state.pop("_pavp_prev_fsm", None)
        st.session_state.pop("_pavp_prev_iter", None)
        return '<span class="pavp-status">Standby...</span>'

    # IDLE: 模型可能在思考/状态切换中，保留上次活跃状态
    if fsm == "IDLE":
        prev_fsm = st.session_state.get("_pavp_prev_fsm")
        prev_iter = st.session_state.get("_pavp_prev_iter", 0)
        if prev_fsm and prev_fsm not in ("IDLE", "DONE", "FAILED"):
            fsm = prev_fsm
            iteration = prev_iter
        else:
            # 没有上次活跃状态，确实处于空闲
            return '<span class="pavp-status">Standby...</span>'

    # Store current state for next call
    st.session_state["_pavp_prev_fsm"] = fsm
    st.session_state["_pavp_prev_iter"] = iteration

    # fsm_state → display label + color
    _M = {
        "PLANNING":     ("Plan",   "#00BFFF"),  # cyan-blue
        "ACTING":       ("Act",    "#00CC66"),  # green
        "VERIFYING":    ("Verify", "#F5DEB3"),  # beige/wheat
        "AWAITING_USER":("Verify", "#F5DEB3"),  # also part of Verify
    }
    display, color = _M.get(fsm, (fsm, "inherit"))

    # 构建 task_key 前缀：&N TK-xxx （仅当有 task_key 时显示）
    task_key_prefix = ""
    if task_key_count > 0 and task_key_sample:
        task_key_prefix = f'<span class="pavp-loop">&{task_key_count}</span> <span class="pavp-phase" style="color:#888;">{task_key_sample}</span> '

    return (
        f'<span class="pavp-status">'
        f'{task_key_prefix}'
        f'<span class="pavp-loop">#{iteration}</span> '
        f'<span class="pavp-phase" style="color:{color};">{display}</span>'
        f'</span>'
    )


# Render the status indicator as an isolated fragment so it can refresh
# independently without re-rendering the entire page.
# Only this fragment re-runs every 2 seconds, keeping buttons and other
# UI elements fully responsive.
@st.fragment(run_every=2.0)
def _render_status_fragment():
    """Render the PAVP status indicator as an isolated fragment.

    Uses @st.fragment so that the periodic re-render only affects this
    small section of the page, not the entire UI. This prevents the
    "page busy / buttons unresponsive" problem caused by full-page reruns.
    """
    # Compute dots color matching _status_html() color
    _state = _read_orchestrator_state()
    _dots_html = ""
    if _state:
        _fsm = _state.get("fsm_state", "")
        # 与 _status_html() 保持一致的 IDLE 回退逻辑
        if _fsm == "IDLE":
            _prev_fsm = st.session_state.get("_pavp_prev_fsm")
            if _prev_fsm and _prev_fsm not in ("IDLE", "DONE", "FAILED"):
                _fsm = _prev_fsm
        _M = {
            "PLANNING":      "#00BFFF",
            "ACTING":        "#00CC66",
            "VERIFYING":     "#F5DEB3",
            "AWAITING_USER": "#F5DEB3",
        }
        if _fsm in _M:
            _dots_html = f'<span class="pavp-dots" style="color:{_M[_fsm]};">...</span>'

    st.markdown(
        f'<div class="pavp-header">'
        f'<small class="pavp-caption">Plan → Act → Verify → (DebugPlan / NewPlan Loop)</small>'
        f'<br><br>'
        f'{_status_html()}'
        f'{_dots_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


_render_status_fragment()

# ============================================================================
# State init
# ============================================================================
if "settings_cache" not in st.session_state:
    st.session_state.settings_cache = load_settings()

# Auto-migrate old default port 4000 → DEFAULT_PORT
if st.session_state.settings_cache.get("proxy_port") == 4000:
    save_field("proxy_port", DEFAULT_PORT)
    st.session_state.settings_cache = load_settings()

if "proxy_port" not in st.session_state:
    st.session_state.proxy_port = st.session_state.settings_cache.get("proxy_port", DEFAULT_PORT)

# Check real proxy status (not session state)
proxy_alive, proxy_ready = _is_proxy_running(st.session_state.proxy_port)

# Auto-sync port: if the running proxy is on a different port than settings,
# update settings.json and session state to match the actual port.
_PORT_FILE = Path.home() / ".pavp" / "proxy_port.txt"
try:
    if _PORT_FILE.exists():
        _actual_port = int(_PORT_FILE.read_text().strip())
        _cfg_port = st.session_state.settings_cache.get("proxy_port", DEFAULT_PORT)
        if _actual_port != _cfg_port:
            save_field("proxy_port", _actual_port)
            st.session_state.settings_cache = load_settings()
            st.session_state.proxy_port = _actual_port
            proxy_alive, proxy_ready = _is_proxy_running(_actual_port)
except Exception:
    pass

# Sync auto-start setting to registry (always sync to ensure command is up-to-date)
_auto_start_val = st.session_state.settings_cache.get("auto_start", True)
_auto_start_ui_val = st.session_state.settings_cache.get("auto_start_ui", False)
set_auto_start(_auto_start_val, st.session_state.proxy_port, _auto_start_ui_val)

# Auto-start proxy on boot if auto-start is enabled and proxy is not running
if "auto_start_attempted" not in st.session_state:
    st.session_state.auto_start_attempted = False

if _auto_start_val and not proxy_alive and not st.session_state.auto_start_attempted and not _is_recent_manual_stop():
    st.session_state.auto_start_attempted = True
    # Double-check: if the proxy port responds to health check but PID file
    # is missing or stale (e.g. cleaned up by system), the proxy is already
    # running — do NOT restart it, just show its status.
    port = st.session_state.proxy_port
    if _proxy_health(port):
        pass  # Proxy is already running, nothing to do
    else:
        _start_proxy(port)
        # Wait for proxy to become healthy (up to 10s) instead of fixed sleep
        _wait_until(lambda: _proxy_health(port), timeout=10.0, interval=0.3)
        st.rerun()


def _refresh_settings():
    """Force reload settings"""
    st.session_state.settings_cache = load_settings()
    st.rerun()


# ============================================================================
# Sidebar
# ============================================================================
sp = str(settings_path())
st.sidebar.caption(f"Config: `{sp}`")
if st.sidebar.button("🔄 Refresh", use_container_width=True):
    _refresh_settings()

# --- Model config in sidebar ---
s = st.session_state.settings_cache

plan_model = s.get("plan_model", "")
plan_api = s.get("plan_api", "")
plan_url = s.get("plan_base_url", "")
act_model = s.get("act_model", "")
act_api = s.get("act_api", "")
act_url = s.get("act_base_url", "")

plan_ok = bool(plan_model and plan_api and plan_url)
act_ok = bool(act_model and act_api and act_url)

st.sidebar.divider()
st.sidebar.markdown("**Plan/Verify**")
st.sidebar.caption(f"Model: `{plan_model or '—'}`")
_plan_url_display = _html_escape(plan_url or '—')
st.sidebar.markdown(
    f'URL: <code class="sidebar-url">{_plan_url_display}</code>  {"✅" if plan_ok else "⬜"}',
    unsafe_allow_html=True,
)

st.sidebar.divider()
st.sidebar.markdown("**Act**")
st.sidebar.caption(f"Model: `{act_model or '-'}`")
_act_url_display = _html_escape(act_url or '—')
st.sidebar.markdown(
    f'URL: <code class="sidebar-url">{_act_url_display}</code>  {"✅" if act_ok else "⬜"}',
    unsafe_allow_html=True,
)

# --- Loop mode toggle (bottom-left) ---
st.sidebar.divider()
st.sidebar.markdown("**Loop Mode**")
_current_auto = s.get("loop_mode", "auto") == "auto"
_spacer, _lcol1, _lcol2 = st.sidebar.columns([0.04, 0.2, 0.76], vertical_alignment="center", gap="small")
with _lcol1:
    _auto_mode = st.toggle("", value=_current_auto, key="loop_mode_toggle", label_visibility="collapsed")
with _lcol2:
    st.markdown(f"**{'Auto' if _auto_mode else 'Manual'}**")
_new_mode = "auto" if _auto_mode else "manual"
if _new_mode != s.get("loop_mode", "auto"):
    save_field("loop_mode", _new_mode)
    st.session_state.settings_cache = load_settings()
    st.rerun()

# --- Auto-start toggle (bottom-left) ---
st.sidebar.divider()
st.sidebar.markdown("**Auto Start**")
_current_auto_start = s.get("auto_start", True)
_s_spacer, _s_lcol1, _s_lcol2 = st.sidebar.columns([0.04, 0.2, 0.76], vertical_alignment="center", gap="small")
with _s_lcol1:
    _auto_start_on = st.toggle("", value=_current_auto_start, key="auto_start_toggle", label_visibility="collapsed")
with _s_lcol2:
    st.markdown(f"**{'On' if _auto_start_on else 'Off'}**")
if _auto_start_on != _current_auto_start:
    save_field("auto_start", _auto_start_on)
    set_auto_start(_auto_start_on, st.session_state.proxy_port, _auto_start_ui_val)
    st.session_state.settings_cache = load_settings()
    st.rerun()

# --- Auto-start UI toggle (only visible when Auto Start is ON) ---
_current_auto_start_ui = s.get("auto_start_ui", False)
_sui_spacer, _sui_lcol1, _sui_lcol2 = st.sidebar.columns([0.04, 0.2, 0.76], vertical_alignment="center", gap="small")
with _sui_lcol1:
    _auto_start_ui_on = st.toggle("", value=_current_auto_start_ui, key="auto_start_ui_toggle", label_visibility="collapsed", disabled=not _auto_start_on)
with _sui_lcol2:
    st.markdown(f"**UI {'On' if _auto_start_ui_on else 'Off'}**")
if _auto_start_ui_on != _current_auto_start_ui:
    save_field("auto_start_ui", _auto_start_ui_on)
    set_auto_start(_auto_start_on, st.session_state.proxy_port, _auto_start_ui_on)
    st.session_state.settings_cache = load_settings()
    st.rerun()


# ============================================================================
# Proxy control
# ============================================================================
st.divider()
st.header("Proxy API")

port = st.number_input(
    "Proxy Port", min_value=1000, max_value=9999,
    value=st.session_state.proxy_port, step=1,
    disabled=proxy_alive,
)

proxy_url = f"http://localhost:{port}/v1"

# Determine button state
_has_error = bool(st.session_state.get("proxy_start_error"))

if proxy_alive:
    # Running: only show Stop + Health
    col_stop, col_health = st.columns(2)
    if col_stop.button("■ Stop Proxy", use_container_width=True):
        _stop_proxy()
        st.session_state.proxy_stop_time = time.time()
        # Wait for proxy to actually stop (up to 5s) instead of fixed sleep
        _wait_until(lambda: not _is_proxy_running(port)[0], timeout=5.0, interval=0.1)
        st.rerun()
    if col_health.button("🔍 Health", use_container_width=True):
        _run_health_check(port)
        st.rerun()
elif _has_error:
    # Failed: show Restart + Health
    col_restart, col_health = st.columns(2)
    if col_restart.button("↻ Restart Proxy", type="primary", use_container_width=True):
        _stop_proxy()
        st.session_state.pop("proxy_stop_time", None)
        # Wait for stop to complete (poll up to 5s), then start
        _wait_until(lambda: not _is_proxy_running(port)[0], timeout=5.0, interval=0.1)
        _start_proxy(port)
        # Wait for proxy to become healthy (up to 10s) instead of fixed sleep
        _wait_until(lambda: _proxy_health(port), timeout=10.0, interval=0.3)
        st.rerun()
    if col_health.button("🔍 Health", use_container_width=True):
        _run_health_check(port)
        st.rerun()
else:
    # Stopped: show Start + Health
    col_start, col_health = st.columns(2)
    _start_disabled = not (plan_ok and act_ok)
    if col_start.button("▶ Start Proxy", type="primary", use_container_width=True,
                        disabled=_start_disabled):
        _start_proxy(port)
        st.session_state.pop("proxy_stop_time", None)
        # Wait for proxy to become healthy (up to 10s) instead of fixed sleep
        _wait_until(lambda: _proxy_health(port), timeout=10.0, interval=0.3)
        st.rerun()
    if col_health.button("🔍 Health", use_container_width=True):
        _run_health_check(port)
        st.rerun()


# ============================================================================
# Status display
# ============================================================================
proxy_alive, proxy_ready = _is_proxy_running(port)

# The status indicator (Plan/Act field) is auto-refreshed via the
# @st.fragment(run_every=2.0) decorator on _render_status_fragment() above.
# That fragment re-renders independently without blocking the rest of the page,
# keeping buttons and other UI elements fully responsive.

# Show temporary stop result (auto-dismiss after 5 seconds)
_stop_time = st.session_state.get("proxy_stop_time", 0)
if _stop_time and time.time() - _stop_time < 5:
    _stop_result = st.session_state.get("proxy_stop_result", "")
    if _stop_result:
        st.info(f"🛑 **Stop result:** {_stop_result}")
elif "proxy_stop_time" in st.session_state:
    # Clear stale state after 5 seconds
    del st.session_state.proxy_stop_time

if proxy_alive:
    if proxy_ready:
        st.success(f"🟢 Running — Proxy API at `{proxy_url}`")
        api_key = s.get("litellm_master_key", "sk-pavp-local")
        st.code(
            f"base_url = \"{proxy_url}\"\n"
            f"api_key  = \"{api_key}\"\n"
            f"model    = \"pavp\"",
            language="python",
        )
    else:
        st.warning(
            f"🟡 Proxy process running, but model configs are incomplete. "
            f"Check settings: Plan model={plan_ok}, Act model={act_ok}. "
            f"The proxy will return 502 for all chat requests."
        )
else:
    st.error("🔴 Proxy stopped")

# Show proxy startup error if present (persisted across reruns)
if st.session_state.get("proxy_start_error"):
    st.error(f"**Startup error:**\n```\n{st.session_state.proxy_start_error}\n```")
    # Also show proxy log tail for diagnostics
    if _LOG_FILE.exists():
        try:
            log_text = _LOG_FILE.read_text(encoding="utf-8", errors="replace")
            if log_text.strip():
                with st.expander("📜 Proxy log (last 30 lines)"):
                    lines = log_text.splitlines()
                    st.code("\n".join(lines[-30:]), language="text")
        except Exception:
            pass
    if st.button("Clear error"):
        del st.session_state.proxy_start_error
        st.rerun()


# ============================================================================
# How it works
# ============================================================================
st.divider()
with st.expander("How PAVP works"):
    st.markdown("""
    **PAVP = Plan - Act - Verify - (Loop)**

    1. Agent (CC/Codex/Trae) sends prompt to the proxy API
    2. **Plan**: high-power model analyzes requirement, outputs structured plan
    3. **Act**: execution model generates code/solution based on plan
    4. **Verify**: high-power model audits the Act output, including:
       - Bug/compile checks (DO-NOT-SHIP -> DebugPlan)
       - Task completion check (INCOMPLETE -> NewPlan)
    5. **Loop**: if Verify produces DebugPlan or NewPlan:
       - **Auto mode**: framework automatically continues the loop
       - **Manual mode**: user decides whether to continue or stop

    The proxy does NOT auto-loop. Each request = one Plan->Act->Verify cycle.
    Loop mode applies to the orchestrator (CLI) workflow.

    **Proxy persists in background** - closing this page does NOT stop the proxy.
    Use the Stop button above to terminate it.
    """)

# ============================================================================
# Diagnostics (moved below How PAVP works)
# ============================================================================
st.divider()
_pid_exists = _PID_FILE.exists()
_pid_val = _read_pid() if _pid_exists else 0
_settings_port = load_settings().get("proxy_port", DEFAULT_PORT)
with st.expander("🔧 Diagnostics", expanded=False):
    st.markdown(
        f"**PID file:** `{_PID_FILE}` exists={_pid_exists} pid={_pid_val}  \n"
        f"**UI port:** `{port}`  \n"
        f"**Settings port:** `{_settings_port}`  \n"
        f"**_is_proxy_running():** alive={proxy_alive}, ready={proxy_ready}  \n"
        f"**Plan OK:** {plan_ok} ({plan_model} / {'***' if plan_api else '(empty)'})  \n"
        f"**Act OK:** {act_ok} ({act_model} / {'***' if act_api else '(empty)'})  \n"
        f"**Auto-start:** {_auto_start_val}  \n"
        f"**Auto-start UI:** {_auto_start_ui_val}"
    )
    # Show health check results if available
    health_results = st.session_state.get("health_results")
    if health_results:
        st.markdown("---")
        st.markdown("**Health Check Log:**")
        st.markdown(f"- Health: {'✅ OK' if health_results['health'] else '❌ Failed'}")
        st.markdown(f"- Model test: {health_results['model']}")


# ============================================================================
# Proxy log viewer
# ============================================================================
if proxy_alive:
    with st.expander("📜 Proxy Log"):
        if _LOG_FILE.exists():
            if st.button("🔄 Refresh Log"):
                st.rerun()
            lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            recent = lines[-80:] if len(lines) > 80 else lines
            log_text = "\n".join(recent)
            st.markdown(
                f'<div class="pavp-log-box"><pre style="margin:0;white-space:pre-wrap;word-break:break-all">{_html_escape(log_text)}</pre></div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("No log file yet")
