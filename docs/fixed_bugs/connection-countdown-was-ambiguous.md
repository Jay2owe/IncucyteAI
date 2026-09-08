# Connection countdown did not name what expires
**Date**: 2026-09-03
**Files changed**: `pyincucyte/gui/app.py`
**Guard**: `tests/test_gui_threading.py::ConnectionCountdownTests`

## What went wrong
When a saved bearer token was valid, the connection badge showed the user name followed only by a duration, such as `Jamie · 23.4h`. A person could not tell whether this described login expiry, instrument availability, or another time limit.

## The broken pattern
```python
self.conn_var.set(f"{credentials.username or 'connected'} · {unit}")
# The duration had no label identifying what it measured.
```

## The fix
```python
self.conn_var.set(
    f"{credentials.username or 'connected'} · token expires in {unit}")
```

## Why it matters
The token can be refreshed independently of the broader signed-in state. Naming it prevents people from mistaking a credential countdown for the end of their login or instrument session.
