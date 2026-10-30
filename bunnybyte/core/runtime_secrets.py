"""Runtime secret redaction and shell environment helpers."""

import os

from .secret_vault import DEFAULT_VAULT

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"


class RuntimeSecretsMixin:
    @staticmethod
    def looks_sensitive_env_name(name):
        upper = str(name).upper()
        return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)

    def is_secret_env_name(self, name):
        upper = str(name).upper()
        return upper in self.secret_env_names or self.looks_sensitive_env_name(upper)

    def configured_secret_env_items(self):
        items = [(name, value) for name, value in os.environ.items() if str(name).upper() in self.secret_env_names and value]
        items.sort(key=lambda item: item[0])
        return items

    def detected_secret_env_items(self):
        items = [(name, value) for name, value in os.environ.items() if self.is_secret_env_name(name) and value]
        items.sort(key=lambda item: item[0])
        return items

    def secret_env_summary(self):
        names = [name for name, _ in self.configured_secret_env_items()]
        return {"secret_env_count": len(names), "secret_env_names": names}

    def detected_secret_env_summary(self):
        names = [name for name, _ in self.detected_secret_env_items()]
        return {"secret_env_count": len(names), "secret_env_names": names}

    @property
    def secret_vault(self):
        return self.__dict__.get("_secret_vault") or DEFAULT_VAULT

    @secret_vault.setter
    def secret_vault(self, vault):
        self.__dict__["_secret_vault"] = vault

    def known_secret_items(self):
        """Values we know are secret: secret-looking env vars plus every configured provider key."""
        items = list(self.detected_secret_env_items())
        client = getattr(self, "model_client", None)
        clients = [client, getattr(self, "critic_client", None)]
        clients += list(getattr(client, "fallback_clients", None) or []) if client is not None else []
        for item in clients:
            key = getattr(item, "api_key", None) if item is not None else None
            if isinstance(key, str) and key.strip():
                items.append(("api_key", key.strip()))
        return items

    def redact_text(self, text, known=None):
        return self.secret_vault.mask(text, self.known_secret_items() if known is None else known)

    def redact_artifact(self, value, key=None, known=None):
        known = self.known_secret_items() if known is None else known
        if key and self.is_secret_env_name(key):
            return REDACTED_VALUE
        if isinstance(value, dict):
            return {str(k): self.redact_artifact(v, key=k, known=known) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_artifact(item, key=key, known=known) for item in value]
        if isinstance(value, str):
            return self.redact_text(value, known)
        return value

    def redact_session(self, session):
        """Masked copy of a session dict for disk; remembers which nonces this session's placeholders use."""
        vault = self.secret_vault
        masked = vault.mask_structure(session, self.known_secret_items())
        if vault.issued:
            nonces = [str(item) for item in masked.get("secret_nonces", []) or []]
            if vault.nonce not in nonces:
                masked["secret_nonces"] = nonces + [vault.nonce]
        return masked

    def shell_env(self):
        env = {name: os.environ[name] for name in self.shell_env_allowlist if name in os.environ}
        env["PWD"] = str(self.root)
        if "PATH" not in env and os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        return env
