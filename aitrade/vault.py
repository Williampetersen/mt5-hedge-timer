"""MT5 passwords in the Windows password vault (Credential Manager).

Nothing here writes a password to a file. Windows encrypts the entries with
the signed-in user's own key; they show up in Control Panel > Credential
Manager > Windows Credentials under "Aitrade MT5".
"""

from __future__ import annotations

from typing import Optional

SERVICE = "Aitrade MT5"


class VaultError(RuntimeError):
    """The Windows password vault could not be used. The message is written for the user."""


def entry_name(side: str, login: int) -> str:
    """One entry per side and account number, e.g. 'buy:12345678'."""
    return f"{side}:{int(login)}"


class Vault:
    def __init__(self, backend=None):
        self._backend = backend

    @property
    def backend(self):
        if self._backend is None:
            try:
                import keyring
            except ImportError as exc:
                raise VaultError("The 'keyring' package is missing, so passwords cannot be saved.") from exc
            self._backend = keyring
        return self._backend

    def get(self, side: str, login: int) -> str:
        """The saved password, or '' when there is none."""
        if login <= 0:
            return ""
        try:
            return self.backend.get_password(SERVICE, entry_name(side, login)) or ""
        except VaultError:
            raise
        except Exception as exc:
            raise VaultError(f"Could not read the saved password from Windows: {exc}") from exc

    def has(self, side: str, login: int) -> bool:
        return bool(self.get(side, login))

    def set(self, side: str, login: int, password: str) -> None:
        if login <= 0 or not password:
            raise VaultError("An account number and a password are needed to save a password.")
        try:
            self.backend.set_password(SERVICE, entry_name(side, login), password)
        except Exception as exc:
            raise VaultError(f"Could not save the password in Windows: {exc}") from exc

    def delete(self, side: str, login: int) -> None:
        """Remove the entry. Missing entries are fine."""
        if login <= 0:
            return
        try:
            if self.backend.get_password(SERVICE, entry_name(side, login)) is not None:
                self.backend.delete_password(SERVICE, entry_name(side, login))
        except Exception as exc:
            raise VaultError(f"Could not remove the saved password from Windows: {exc}") from exc


class MemoryBackend:
    """Stand-in for the Windows vault (tests, and screenshots of the app)."""

    def __init__(self, initial: Optional[dict] = None):
        self.items: dict[tuple[str, str], str] = dict(initial or {})

    def get_password(self, service: str, name: str) -> Optional[str]:
        return self.items.get((service, name))

    def set_password(self, service: str, name: str, password: str) -> None:
        self.items[(service, name)] = password

    def delete_password(self, service: str, name: str) -> None:
        del self.items[(service, name)]
