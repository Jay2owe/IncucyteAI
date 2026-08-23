"""Saved credentials and the bearer token that comes with them.

The Incucyte never sees a plaintext password: it is hashed by the vendor's own
.NET assembly before it leaves the machine, and only that hash is stored.  The
token it returns is good for about a day, so we keep it and its expiry and only
re-authenticate when it has actually run out.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

from . import engine
from .errors import NotLoggedInError

#: Refresh this many seconds before the token really expires.
TOKEN_SAFETY_MARGIN = 60


@dataclass
class Credentials:
    """One device login, as persisted to disk."""

    host: str = engine.DEFAULT_HOST
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
            host=self.host, username=self.username,
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
    """Reads and writes the saved-credentials file."""

    def __init__(self, path=None):
        self.path = Path(path) if path else engine.CONFIG_FILE

    def load(self):
        """Return saved credentials (an empty record if none are saved)."""
        if not self.path.exists():
            return Credentials()
        try:
            return Credentials.from_dict(
                json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return Credentials()

    def require(self):
        """Return saved credentials, or raise if the user has never logged in."""
        creds = self.load()
        if not (creds.can_refresh or creds.token_valid):
            raise NotLoggedInError(
                f"No saved Incucyte login in {self.path}. Run 'pyincucyte login' "
                f"or log in from the GUI first.")
        return creds

    def save(self, credentials):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(credentials.to_dict(), indent=2), encoding="utf-8")
        return credentials

    def clear(self):
        """Forget the saved login."""
        try:
            self.path.unlink()
        except OSError:
            pass

    def __repr__(self):
        return f"<ConfigStore {self.path}>"


#: The default store, pointing at the per-user config folder.
default_store = ConfigStore()


__all__ = ["Credentials", "ConfigStore", "default_store", "TOKEN_SAFETY_MARGIN"]
