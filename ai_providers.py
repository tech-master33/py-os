"""AI provider back ends for the PyOS AI Assistant.

This module deliberately avoids importing wx so it can be imported and tested
without a GUI. It owns both the request/response logic for every provider and
the small ``ai_config.json`` file that remembers the chosen provider, that
provider's model, and any API keys the user has entered.

Keys are stored in plain text inside the PyOS data directory (the same place
``config.json`` lives) with owner-only permissions where the platform supports
them, so a user can inspect or delete them with any text editor.
"""

import json
import os
import tempfile
from urllib.parse import quote

import requests

from app_paths import get_data_dir

OLLAMA_API = "http://localhost:11434"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_API = "https://openrouter.ai/api/v1"

AI_CONFIG_FILENAME = "ai_config.json"

# Ollama is a local server, so the original timeouts are kept as-is.
OLLAMA_LIST_TIMEOUT = 5
OLLAMA_ASK_TIMEOUT = 30
# Cloud providers need a little more room for a connect/handshake.
CLOUD_LIST_TIMEOUT = (5, 20)
CLOUD_ASK_TIMEOUT = (5, 60)

DEFAULT_PROVIDER = "ollama"

# Sensible starting points. Every provider can refresh this list from its own
# API, so these only matter on the very first launch or when offline.
DEFAULT_MODELS = {
    "ollama": "llama3",
    "gemini": "gemini-2.0-flash",
    "openrouter": "openai/gpt-4o-mini",
}

PROVIDER_ORDER = ("ollama", "gemini", "openrouter")

# How many characters of standing instructions a provider will be sent. Local
# models usually have a smaller context window than the hosted ones.
OLLAMA_SYSTEM_LIMIT = 10000
CLOUD_SYSTEM_LIMIT = 40000


class ProviderError(Exception):
    """A provider problem that can be explained to the user out loud.

    ``kind`` is one of ``no_key``, ``bad_key``, ``offline`` or ``http`` so the
    UI can react without inspecting HTTP status codes itself.
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------------------
# Local configuration (chosen provider, models, API keys)
# ---------------------------------------------------------------------------

def get_config_path():
    """Return the path of the AI configuration file."""
    return os.path.join(get_data_dir(), AI_CONFIG_FILENAME)


def default_config():
    return {
        "provider": DEFAULT_PROVIDER,
        "keys": {},
        "models": dict(DEFAULT_MODELS),
    }


def load_config(path=None):
    """Load the AI configuration, falling back to defaults if unreadable."""
    path = path or get_config_path()
    config = default_config()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return config

    if not isinstance(data, dict):
        return config

    provider = data.get("provider")
    if isinstance(provider, str) and provider in PROVIDER_ORDER:
        config["provider"] = provider

    keys = data.get("keys")
    if isinstance(keys, dict):
        for name, value in keys.items():
            if isinstance(value, str) and value.strip():
                config["keys"][str(name)] = value.strip()

    models = data.get("models")
    if isinstance(models, dict):
        for name, value in models.items():
            if isinstance(value, str) and value.strip():
                config["models"][str(name)] = value.strip()

    return config


def save_config(config, path=None):
    """Write the AI configuration atomically.

    The new contents are written to a temporary file in the same directory and
    then moved into place, so an interrupted save can never truncate keys that
    were already stored.
    """
    path = path or get_config_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    handle_fd, temp_path = tempfile.mkstemp(
        prefix=".ai_config-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
        try:
            # mkstemp already uses 0600; re-assert it for platforms that care.
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise

    return path


def get_provider_name(path=None):
    return load_config(path).get("provider", DEFAULT_PROVIDER)


def set_provider_name(provider_key, path=None):
    config = load_config(path)
    config["provider"] = provider_key
    return save_config(config, path)


def get_api_key(provider_key, path=None):
    return load_config(path)["keys"].get(provider_key, "")


def has_api_key(provider_key, path=None):
    return bool(get_api_key(provider_key, path))


def set_api_key(provider_key, api_key, path=None):
    config = load_config(path)
    api_key = (api_key or "").strip()
    if api_key:
        config["keys"][provider_key] = api_key
    else:
        config["keys"].pop(provider_key, None)
    return save_config(config, path)


def clear_api_key(provider_key, path=None):
    return set_api_key(provider_key, "", path)


def get_model(provider_key, path=None):
    return load_config(path)["models"].get(provider_key, "")


def set_model(provider_key, model, path=None):
    config = load_config(path)
    if model:
        config["models"][provider_key] = model
    return save_config(config, path)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class AIProvider:
    """Common interface for every AI provider."""

    key = ""
    label = ""
    description = ""
    requires_api_key = False
    default_model = ""
    list_timeout = CLOUD_LIST_TIMEOUT
    ask_timeout = CLOUD_ASK_TIMEOUT
    system_char_limit = CLOUD_SYSTEM_LIMIT

    def __init__(self, api_key=None):
        self.api_key = (api_key or "").strip()

    def default_models(self):
        return [self.default_model] if self.default_model else []

    def status_text(self):
        """Short status used in the provider picker."""
        if not self.requires_api_key:
            return "no API key needed"
        return "API key saved" if self.api_key else "API key required"

    def list_models(self):
        """Return the model names this provider currently offers."""
        raise NotImplementedError

    def ask(self, prompt, model, system=None):
        """Send one prompt and return the reply as plain text.

        ``system`` is optional standing instruction for the model, such as the
        PyOS reference built by :mod:`pyos_knowledge`. Each provider keeps that
        in a different place, which is the only reason this argument is not
        already worded as a message list.
        """
        raise NotImplementedError

    def verify_key(self, api_key=None):
        """Raise ProviderError if the key is missing or rejected."""
        return True


class OllamaProvider(AIProvider):
    key = "ollama"
    label = "Ollama"
    description = "Local models served by Ollama on this computer. No API key needed."
    requires_api_key = False
    default_model = "llama3"
    list_timeout = OLLAMA_LIST_TIMEOUT
    ask_timeout = OLLAMA_ASK_TIMEOUT
    system_char_limit = OLLAMA_SYSTEM_LIMIT

    def __init__(self, api_key=None, base_url=OLLAMA_API):
        super().__init__(api_key)
        self.base_url = base_url

    def list_models(self):
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=self.list_timeout)
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Could not reach Ollama. Make sure Ollama is running."
            )

        if response.status_code != 200:
            raise ProviderError("http", f"Ollama returned error {response.status_code}.")

        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", "Ollama sent a response that could not be read.")

        names = [item["name"] for item in data.get("models", []) if item.get("name")]
        if not names:
            raise ProviderError("http", "Ollama is running but has no models installed.")
        return names

    def ask(self, prompt, model, system=None):
        payload = {"model": model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        try:
            response = requests.post(
                f"{self.base_url}/api/generate", json=payload, timeout=self.ask_timeout
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Connection error. Make sure Ollama is running."
            )

        if response.status_code != 200:
            raise ProviderError("http", f"Ollama returned error {response.status_code}.")

        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", "Ollama sent a response that could not be read.")

        return data.get("response") or "I couldn't generate a response."


class GeminiProvider(AIProvider):
    key = "gemini"
    label = "Google Gemini"
    description = "Google's Gemini models, reached over the internet. Requires a Gemini API key."
    requires_api_key = True
    default_model = "gemini-2.0-flash"
    list_timeout = CLOUD_LIST_TIMEOUT
    ask_timeout = CLOUD_ASK_TIMEOUT

    def __init__(self, api_key=None, base_url=GEMINI_API):
        super().__init__(api_key)
        self.base_url = base_url

    def _require_key(self):
        if not self.api_key:
            raise ProviderError(
                "no_key", "Gemini needs an API key. Press the Provider button to enter one."
            )

    def list_models(self):
        self._require_key()
        try:
            response = requests.get(
                f"{self.base_url}/models",
                params={"key": self.api_key, "pageSize": 200},
                timeout=self.list_timeout,
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Could not reach Gemini. Check your internet connection."
            )

        self._check_status(response)
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", "Gemini sent a response that could not be read.")

        names = []
        for item in data.get("models", []):
            name = item.get("name", "")
            methods = item.get("supportedGenerationMethods")
            if not name or (methods and "generateContent" not in methods):
                continue
            names.append(_strip_model_prefix(name))

        if not names:
            raise ProviderError("http", "Gemini did not report any usable models for this key.")
        return sorted(set(names))

    def ask(self, prompt, model, system=None):
        self._require_key()
        model_name = _strip_model_prefix(model)
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            # Gemini takes instructions in their own field; a "system" role is
            # not allowed among the contents.
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        try:
            response = requests.post(
                f"{self.base_url}/models/{quote(model_name, safe='')}:generateContent",
                params={"key": self.api_key},
                json=payload,
                timeout=self.ask_timeout,
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Connection error. Could not reach Gemini. Check your internet connection."
            )

        self._check_status(response)
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", "Gemini sent a response that could not be read.")

        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts).strip()
        except (KeyError, IndexError, TypeError):
            text = ""

        if not text:
            reason = ""
            try:
                reason = data["candidates"][0].get("finishReason", "")
            except (KeyError, IndexError, TypeError):
                pass
            if reason == "SAFETY":
                return "Gemini refused to answer that prompt for safety reasons."
            return "Gemini returned an empty response."
        return text

    def verify_key(self, api_key=None):
        key = (api_key or self.api_key or "").strip()
        if not key:
            raise ProviderError("no_key", "Type or paste your Gemini API key first.")
        previous = self.api_key
        self.api_key = key
        try:
            self.list_models()
        finally:
            self.api_key = previous
        return True

    def _check_status(self, response):
        if response.status_code in (400, 401, 403):
            raise ProviderError(
                "bad_key", "Gemini rejected that API key. Check the key and try again."
            )
        if response.status_code == 404:
            raise ProviderError("http", "Gemini does not recognise that model name.")
        if response.status_code != 200:
            raise ProviderError("http", f"Gemini returned error {response.status_code}.")


class OpenRouterProvider(AIProvider):
    key = "openrouter"
    label = "OpenRouter"
    description = "Many models from many companies through OpenRouter. Requires an OpenRouter API key."
    requires_api_key = True
    default_model = "openai/gpt-4o-mini"
    list_timeout = CLOUD_LIST_TIMEOUT
    ask_timeout = CLOUD_ASK_TIMEOUT

    def __init__(self, api_key=None, base_url=OPENROUTER_API):
        super().__init__(api_key)
        self.base_url = base_url

    def _require_key(self):
        if not self.api_key:
            raise ProviderError(
                "no_key",
                "OpenRouter needs an API key. Press the Provider button to enter one.",
            )

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/tech-master33/py-os",
            "X-Title": "PyOS AI Assistant",
        }

    def list_models(self):
        # The model catalogue is public, but sending the key lets OpenRouter
        # return the models this particular key can actually use.
        headers = self._headers() if self.api_key else None
        try:
            response = requests.get(
                f"{self.base_url}/models", headers=headers, timeout=self.list_timeout
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Could not reach OpenRouter. Check your internet connection."
            )

        if response.status_code in (401, 403):
            raise ProviderError(
                "bad_key", "OpenRouter rejected that API key. Check the key and try again."
            )
        if response.status_code != 200:
            raise ProviderError("http", f"OpenRouter returned error {response.status_code}.")

        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", "OpenRouter sent a response that could not be read.")

        names = [item["id"] for item in data.get("data", []) if item.get("id")]
        if not names:
            raise ProviderError("http", "OpenRouter did not report any models.")
        return sorted(set(names))

    def ask(self, prompt, model, system=None):
        self._require_key()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages}
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.ask_timeout,
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline",
                "Connection error. Could not reach OpenRouter. Check your internet connection.",
            )

        if response.status_code in (401, 403):
            raise ProviderError(
                "bad_key", "OpenRouter rejected that API key. Check the key and try again."
            )
        if response.status_code != 200:
            raise ProviderError("http", f"OpenRouter returned error {response.status_code}.")

        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", "OpenRouter sent a response that could not be read.")

        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            if not text and choice.get("finish_reason") == "content_filter":
                return "OpenRouter refused to answer that prompt."
        except (KeyError, IndexError, TypeError):
            text = ""

        if not text.strip():
            return "OpenRouter returned an empty response."
        return text.strip()

    def verify_key(self, api_key=None):
        key = (api_key or self.api_key or "").strip()
        if not key:
            raise ProviderError("no_key", "Type or paste your OpenRouter API key first.")
        try:
            response = requests.get(
                f"{self.base_url}/key",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=self.list_timeout,
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Could not reach OpenRouter. Check your internet connection."
            )
        if response.status_code in (401, 403):
            raise ProviderError(
                "bad_key", "OpenRouter rejected that API key. Check the key and try again."
            )
        if response.status_code != 200:
            raise ProviderError("http", f"OpenRouter returned error {response.status_code}.")
        return True


def _strip_model_prefix(name):
    """Turn ``models/gemini-2.0-flash`` into ``gemini-2.0-flash``."""
    name = (name or "").strip()
    if name.startswith("models/"):
        name = name[len("models/"):]
    return name


PROVIDER_CLASSES = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
    "openrouter": OpenRouterProvider,
}


def list_providers(api_keys=None):
    """Return one instance of every provider, in picker order.

    When ``api_keys`` is omitted each provider is given the key stored for it,
    which is what the picker uses to show "API key saved" / "API key required".
    """
    if api_keys is None:
        api_keys = {name: get_api_key(name) for name in PROVIDER_ORDER}
    return [
        PROVIDER_CLASSES[name](api_keys.get(name, "")) for name in PROVIDER_ORDER
    ]


def get_provider(provider_key, api_key=None):
    """Return a provider instance, or None for an unknown key."""
    provider_class = PROVIDER_CLASSES.get(provider_key)
    if provider_class is None:
        return None
    if api_key is None:
        api_key = get_api_key(provider_key)
    return provider_class(api_key)
