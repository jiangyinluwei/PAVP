---
name: "streamlit-ui"
description: "Streamlit UI design and debugging guidance. Activated when modifying pavp/ui.py, encountering Streamlit state/cache/rerun issues, or needing UI design suggestions. Covers session_state pitfalls, rerun model, cache migration, error handling, performance optimization, and production checklist."
---

# Streamlit UI Debugging and Design Guidance

This skill provides debugging and design guidance for the PAVP project's Streamlit UI (`pavp/ui.py`). Covers the Streamlit execution model, common pitfalls, debugging patterns, and best practices.

---

## Core Concept: Streamlit Execution Model

**Streamlit re-executes the script from top to bottom on every user interaction.** This is the most important thing to understand:

- Every button click, slider move, or text input triggers a full rerun
- Variables at the top of the script are recreated each time
- State not explicitly preserved is lost after each interaction
- State preservation tools: `st.session_state` (values) and `@st.cache_data`/`@st.cache_resource` (computations)

```python
# Wrong: count resets to 0 on every rerun
count = 0
if st.button("Increment"):
    count += 1  # Always stays at 1
st.write(count)

# Correct: use session_state for persistence
if "count" not in st.session_state:
    st.session_state.count = 0
if st.button("Increment"):
    st.session_state.count += 1
st.write(st.session_state.count)
```

---

## Common Pitfalls and Fixes

### Pitfall 1: session_state KeyError

**Symptom:** `KeyError: 'st.session_state has no key "results"'`

**Cause:** session_state not initialized before use.

**Fix:** Always initialize with `if "key" not in st.session_state` before use.

```python
# Wrong
result = st.session_state.result  # KeyError

# Correct
if "result" not in st.session_state:
    st.session_state.result = None
result = st.session_state.result
```

### Pitfall 2: Widgets in Conditional Blocks Don't Trigger Reruns

**Symptom:** Widgets in conditional blocks or containers appear "frozen," with no response to interaction.

**Cause:** Streamlit only triggers reruns for widgets rendered in the current execution path. If a widget is inside an `if` block where the condition is False, or inside a closed expander, Streamlit cannot detect its changes.

**Fix:** Always render the widget, use the `disabled` parameter to control behavior instead of conditionally controlling existence.

```python
# Wrong: Widget inside conditional block
if st.checkbox("Show advanced options"):
    threshold = st.slider("Threshold", 0, 100, 50)  # slider doesn't exist when checkbox unchecked

# Correct: Always render, disable as needed
show_advanced = st.checkbox("Show advanced options")
threshold = st.slider("Threshold", 0, 100, 50, disabled=not show_advanced)
```

### Pitfall 3: st.rerun() in Callbacks Causes Loops

**Symptom:** Page refreshes infinitely or DeltaGenerator errors appear after form submission.

**Cause:** Calling `st.rerun()` inside except blocks or callbacks can cause rerun loops or state conflicts.

**Fix:** Use `st.stop()` instead of `st.rerun()` to halt execution, letting the natural rerun redraw the page.

```python
# Wrong: calling st.rerun() in except block
try:
    result = call_api()
except Exception as e:
    st.session_state.error_message = str(e)
    st.rerun()  # May cause infinite loop

# Correct: use st.stop() to halt execution
try:
    result = call_api()
except Exception as e:
    st.session_state.error_message = str(e)
    st.error(f"Operation failed: {e}")
    st.stop()  # Halts execution, next interaction redraws naturally
```

### Pitfall 4: @st.cache Is Deprecated

**Symptom:** `DeprecationWarning: st.cache is deprecated.`

**Cause:** `@st.cache` was removed in Streamlit 1.18 (February 2023).

**Fix:** Migrate to the new caching API:

```python
# Old (deprecated)
@st.cache
def load_data():
    return pd.read_csv("data.csv")

# New: cache data objects (DataFrame, list, dict, etc. - immutable data)
@st.cache_data
def load_data():
    return pd.read_csv("data.csv")

# New: cache shared resources (model instances, DB connections)
@st.cache_resource
def get_db_connection():
    return sqlite3.connect("database.db")
```

| Decorator | Use Case | Characteristics |
|---|---|---|
| `@st.cache_data` | DataFrame, list, dict etc. data | Deep copy of return value, avoids mutation |
| `@st.cache_resource` | ML models, DB connections, global resources | Returns singleton, shared across sessions |

### Pitfall 5: Directly Setting Values After Widget Key

**Symptom:** `StreamlitAPIException` or widget value not updating.

**Cause:** Directly setting `st.session_state[widget_key]` after widget render causes conflicts.

**Fix:** Initialize the key before rendering the widget, or use callbacks.

```python
# Wrong: setting after render
val = st.text_input("Value", key="my_input")
st.session_state.my_input = "forced"  # Conflict

# Correct: initialize before render
if "my_input" not in st.session_state:
    st.session_state.my_input = "default"
val = st.text_input("Value", key="my_input")
```

---

## PAVP UI-Specific Debugging Guide

### ui.py Architecture Key Points

PAVP's `ui.py` is a **control panel**, not a data display application:

1. **Proxy process management**: Tracks background proxy process via PID file
2. **Config read/write**: Directly reads/writes `~/.pavp/settings.json`
3. **Health check**: Calls proxy `/health` endpoint via httpx
4. **Log viewer**: Reads `log/pavp_proxy.log` file

### Common PAVP UI Issues

#### Issue: Proxy Status Display Inconsistent

**Cause:** `_is_proxy_running()` checks PID file + process existence but doesn't do a health check.

**Debug steps:**
1. Check if `~/.pavp/proxy.pid` exists
2. Check if the PID's process is alive
3. Call `http://localhost:{port}/health` to verify
4. Delete PID file and restart if necessary

#### Issue: settings_cache Not Updating

**Cause:** `st.session_state.settings_cache` is cached after first load, doesn't auto-refresh when the file changes.

**Fix:** Use the refresh button to force reload:

```python
def _refresh_settings():
    st.session_state.settings_cache = load_settings()
    st.rerun()
```

#### Issue: Port Still Shows Stopped After Proxy Start

**Cause:** `time.sleep(2)` after `_start_proxy()` may not be enough; the proxy hasn't fully started.

**Fix:** Add retry logic or extend wait:

```python
# Wait for proxy to be ready (max 10 seconds)
for _ in range(10):
    time.sleep(1)
    if _proxy_health(port):
        break
st.rerun()
```

#### Issue: Loop Mode Toggle Doesn't Take Effect

**Cause:** `save_field()` writes to settings.json but doesn't refresh `settings_cache`.

**Fix:** Ensure reload and rerun after save:

```python
if _new_mode != s.get("loop_mode", "auto"):
    save_field("loop_mode", _new_mode)
    st.session_state.settings_cache = load_settings()
    st.rerun()
```

---

## Design Best Practices

### Layout Conventions

```python
# 1. set_page_config must be at the very top
st.set_page_config(page_title="PAVP Config", page_icon=":gear:", layout="wide")

# 2. Custom CSS for compact layout
st.markdown("""
<style>
.block-container { padding-top: 2rem !important; }
h1 { margin-top: 0 !important; }
</style>
""", unsafe_allow_html=True)

# 3. Clear sections: title -> sidebar -> main area -> expander
```

### Error Handling Pattern

```python
# Standard error handling in PAVP UI
try:
    result = risky_operation()
    st.success("Operation successful")
except SpecificError as e:
    st.error(f"Specific error: {e}")
    logger.error(f"Specific error: {e}")
except Exception as e:
    st.error("An unexpected error occurred. Please try again.")
    st.exception(e)  # Show full stack trace in dev mode
```

### Performance Optimization

| Technique | Use Case | Example |
|---|---|---|
| `@st.cache_data` | Data loading (files, API) | Cache settings.json reads |
| `@st.cache_resource` | Global resources (connections, models) | Cache httpx client |
| `st.form` | Batch input, reduce reruns | Config edit form |
| `st.fragment` | Partial rerun | Log refresh area |
| Lazy loading | Non-fold content | Detailed config in expanders |

### Log Display

PAVP UI's log viewer uses a custom HTML container for scrolling:

```python
st.markdown(
    f'<div class="pavp-log-box">'
    f'<pre style="margin:0;white-space:pre-wrap;word-break:break-all">'
    f'{_html_escape(log_text)}'
    f'</pre></div>',
    unsafe_allow_html=True,
)
```

**Note:** Log content must be processed through `_html_escape()` to prevent XSS.

---

## Production Checklist

After modifying `ui.py`, check each item:

```
[ ] st.set_page_config() at the very top of the file
[ ] All session_state keys initialized before use
[ ] Widgets always rendered (use disabled to control, not conditional wrapping)
[ ] External calls (httpx/file reads) have try-except
[ ] Error messages displayed via st.error/st.warning
[ ] Logs/user input escaped with _html_escape() before embedding in HTML
[ ] Proxy status check includes health check
[ ] Config changes refresh settings_cache and rerun
[ ] streamlit run pavp/ui.py runs without error
[ ] Page layout works correctly at different browser widths
```

---

## Debugging Tools and Tips

### 1. Dev Mode Debugging

```python
# Temporarily add in code
st.write("Debug:", st.session_state)  # View full session_state
st.write("Debug:", local_var)          # View variable value
```

### 2. AppTest Automated Testing

```python
from streamlit.testing.v1 import AppTest

at = AppTest.from_file("pavp/ui.py", default_timeout=10)
at.run()
assert not at.exception  # No exception
assert at.title[0].value == "PAVP Proxy"  # Title correct
```

### 3. Clear Cache Testing

```python
# Temporarily add a clear button in code
if st.button("Clear Cache"):
    st.cache_data.clear()
    st.cache_resource.clear()
    st.rerun()
```

### 4. Streamlit Config Debugging

```toml
# .streamlit/config.toml
[server]
runOnSave = true          # Auto-rerun on file save
maxUploadSize = 200

[browser]
gatherUsageStats = false  # Disable telemetry

[logger]
level = "DEBUG"           # Debug log level
```

---

## Related Documentation

- [Streamlit Official Docs](https://docs.streamlit.io/)
- [Session State Guide](https://docs.streamlit.io/develop/concepts/architecture/session-state)
- [Caching Guide](https://docs.streamlit.io/develop/concepts/architecture/caching)
- [Widget Behavior](https://docs.streamlit.io/develop/concepts/architecture/widget-behavior)
- [AppTest Testing](https://docs.streamlit.io/develop/api-reference/testing)
