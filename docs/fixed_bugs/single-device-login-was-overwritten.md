# Single-device login was overwritten
**Date**: 2026-09-03
**Files changed**: `pyincucyte/config.py`, `pyincucyte/client.py`, `pyincucyte/cli.py`, `pyincucyte/gui/app.py`, `pyincucyte/gui/dialogs.py`, `README.md`
**Guard**: `DeviceProfileTests` in `tests/test_device_profiles.py`

## What went wrong
PyIncucyte stored one flat credential record and one active address. Signing into another instrument replaced the first instrument's username, encrypted password, token, and expiry, so there was no safe way to move between devices.

## The broken pattern
```python
def save(self, credentials):
    self.path.write_text(json.dumps(credentials.to_dict()))
    # Every login replaced the complete file.
```

## The fix
The credential file now contains a host-keyed collection of named device profiles and records which host is active. A legacy flat record is read as the first profile and migrates automatically when the store is next written.

## Why it matters
Tokens are valid only for the instrument that issued them. Keeping profiles isolated prevents a device switch from sending one instrument's token to another and lets users return to saved devices without re-entering credentials.
