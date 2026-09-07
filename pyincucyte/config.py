"""Saved device profiles and the bearer tokens that come with them.

The Incucyte never sees a plaintext password: it is hashed by the vendor's own
.NET assembly before it leaves the machine, and only that hash is stored.  The
Each token is good for about a day, so we keep it with the device that issued
it and only re-authenticate when it has actually run out.
"""

import json
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import engine
from .errors import NotLoggedInError

#: Refresh this many seconds before the token really expires.
TOKEN_SAFETY_MARGIN = 60
CONFIG_VERSION = 2
_STORE_LOCK = threading.RLock()


@dataclass
class Credentials:
    """One device login, as persisted to disk."""

    host: str = engine.DEFAULT_HOST
    device_name: str = ""
    username: str = ""
    encrypted_password: str = ""
    token: str = ""
    token_expires_at: str = ""
    login_time: str = ""

    # -- token state ------------------------------------------------------

    @property
    def expires_at(self):
        if not self.token_expires_at:
            return None
        try:
            return datetime.fromisoformat(self.token_expires_at)
        except ValueError:
            return None

    @property
    def token_valid(self):
        expiry = self.expires_at
        return bool(self.token) and expiry is not None and datetime.now() < expiry

    @property
    def token_seconds_left(self):
        expiry = self.expires_at
        if not expiry:
            return 0
        return max(0.0, (expiry - datetime.now()).total_seconds())

    @property
    def can_refresh(self):
        return bool(self.username and self.encrypted_password)

    def with_token(self, token, expires_in):
        """Return a copy carrying a freshly issued token."""
        expiry = (datetime.now().replace(microsecond=0)
                  + timedelta(seconds=max(0, int(expires_in) - TOKEN_SAFETY_MARGIN)))
        return Credentials(
            host=self.host, device_name=self.device_name,
            username=self.username,
            encrypted_password=self.encrypted_password,
            token=token, token_expires_at=expiry.isoformat(),
            login_time=self.login_time or datetime.now().isoformat(),
        )

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def __repr__(self):
        state = "valid" if self.token_valid else "expired"
        return f"<Credentials {self.username}@{self.host} token={state}>"


class ConfigStore:
    """Reads and writes isolated credentials for several devices."""

    def __init__(self, path=None):
        self.path = Path(path) if path else engine.CONFIG_FILE

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _devices_from(data):
        """Return host-keyed credentials from current or legacy JSON."""
        records = data.get("devices") if isinstance(data, dict) else None
        if isinstance(records, dict):
            devices = {}
            for host, record in records.items():
                if not isinstance(record, dict):
                    continue
                credentials = Credentials.from_dict(record)
                credentials.host = credentials.host or str(host).strip()
                if credentials.host:
                    devices[credentials.host] = credentials
            return devices

        credentials = Credentials.from_dict(data)
        if credentials.host and any((
                credentials.username, credentials.encrypted_password,
                credentials.token, credentials.device_name)):
            return {credentials.host: credentials}
        return {}

    def devices(self):
        """Return every saved device, with the active one first."""
        with _STORE_LOCK:
            data = self._read()
            devices = self._devices_from(data)
        active = data.get("active_host")
        return sorted(
            devices.values(),
            key=lambda item: (
                item.host != active,
                (item.device_name or item.host).casefold(),
                item.host.casefold(),
            ))

    def active_host(self):
        """Return the selected device address, if one has been saved."""
        with _STORE_LOCK:
            data = self._read()
            devices = self._devices_from(data)
        active = str(data.get("active_host") or "").strip()
        if active in devices:
            return active
        return next(iter(devices), "")

    def load(self, host=None):
        """Return one device's credentials, or an empty record for its host."""
        requested = str(host or "").strip()
        with _STORE_LOCK:
            data = self._read()
            devices = self._devices_from(data)
        selected = requested or str(data.get("active_host") or "").strip()
        if not selected and devices:
            selected = next(iter(devices))
        if selected in devices:
            return Credentials.from_dict(devices[selected].to_dict())
        return Credentials(host=selected or engine.DEFAULT_HOST)

    def require(self, host=None):
        """Return one saved login, or raise if that device has none."""
        creds = self.load(host)
        if not (creds.can_refresh or creds.token_valid):
            raise NotLoggedInError(
                f"No saved Incucyte login for {creds.host or 'this device'} in "
                f"{self.path}. Run 'pyincucyte login' or log in from the GUI "
                f"first.")
        return creds

    def save(self, credentials):
        """Save one device without overwriting any other device login."""
        if not credentials.host:
            raise ValueError("A device address is required before saving a login.")
        with _STORE_LOCK:
            devices = self._devices_from(self._read())
            previous = devices.get(credentials.host)
            if previous and not credentials.device_name:
                credentials.device_name = previous.device_name
            devices[credentials.host] = Credentials.from_dict(
                credentials.to_dict())
            payload = {
                "version": CONFIG_VERSION,
                "active_host": credentials.host,
                "devices": {
                    host: record.to_dict()
                    for host, record in devices.items()
                },
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        return credentials

    def select(self, host):
        """Make one saved device the default without changing its login."""
        host = str(host or "").strip()
        with _STORE_LOCK:
            devices = self._devices_from(self._read())
            if host not in devices:
                return Credentials(host=host)
            payload = {
                "version": CONFIG_VERSION,
                "active_host": host,
                "devices": {
                    address: record.to_dict()
                    for address, record in devices.items()
                },
            }
            self.path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
        return self.load(host)

    def clear(self, host=None):
        """Forget one device login, leaving other saved devices untouched."""
        with _STORE_LOCK:
            data = self._read()
            devices = self._devices_from(data)
            selected = str(host or data.get("active_host") or "").strip()
            devices.pop(selected, None)
            if not devices:
                try:
                    self.path.unlink()
                except OSError:
                    pass
                return
            active = next(iter(devices))
            payload = {
                "version": CONFIG_VERSION,
                "active_host": active,
                "devices": {
                    address: record.to_dict()
                    for address, record in devices.items()
                },
            }
            self.path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8")

    def __repr__(self):
        return f"<ConfigStore {self.path}>"


#: The default store, pointing at the per-user config folder.
default_store = ConfigStore()


__all__ = ["Credentials", "ConfigStore", "default_store", "TOKEN_SAFETY_MARGIN",
           "CONFIG_VERSION"]
