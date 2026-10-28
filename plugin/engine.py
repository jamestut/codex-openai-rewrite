"""Load configured plugins and apply a request's snapshot in order."""

import importlib
import json
import threading
from pathlib import Path


class ReplacementEngine:
    """Share stateless plugins while keeping parsed bodies local to requests."""

    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self._lock = threading.Lock()
        self._plugins = ()
        self.reload()

    def reload(self):
        """Replace the active chain only after every configured plugin loads."""
        with self.config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
        if not isinstance(config, dict) or not isinstance(config.get("plugins"), list):
            raise ValueError("config must contain a plugins array")

        plugins = []
        for name in config["plugins"]:
            if not isinstance(name, str) or not name.isidentifier():
                raise ValueError("plugin names must be simple module names")
            # Normal imports intentionally keep Python source cached across reloads.
            module = importlib.import_module(f".{name}", package=__package__)
            plugin = module.Plugin()
            if not callable(getattr(plugin, "matches", None)) or not callable(
                getattr(plugin, "apply", None)
            ):
                raise ValueError(f"plugin {name} must provide matches() and apply()")
            plugins.append(plugin)

        chain = tuple(plugins)
        # Imports and construction never hold the lock used by request threads.
        with self._lock:
            self._plugins = chain

    def snapshot(self) -> tuple:
        """Keep this tuple for the entire request, even if configuration reloads."""
        with self._lock:
            return self._plugins

    def rewrite(self, request_body: bytes, chain: tuple) -> bytes:
        if not chain:
            return request_body
        try:
            body = json.loads(request_body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return request_body
        if not isinstance(body, dict):
            return request_body

        matched = False
        for plugin in chain:
            # matches() must not mutate the body; apply() owns edits and notices.
            is_match, private_data = plugin.matches(body)
            if is_match:
                plugin.apply(body, private_data)
                matched = True

        if not matched:
            return request_body
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
