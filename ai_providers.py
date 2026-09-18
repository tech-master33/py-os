"""AI provider back ends for the PyOS AI Assistant.

This module deliberately avoids importing wx so it can be imported and tested
without a GUI. It owns the request logic for every provider, the vocabulary of
things a provider can report while it works, and the small ``ai_config.json``
file that remembers the chosen provider, that provider's model, whether web
research is switched on, and any API keys the user has entered.

Every provider can answer a question two ways:

* :meth:`AIProvider.ask` sends one request and returns the finished reply.
* :meth:`AIProvider.ask_stream` sends the same request with streaming turned on
  and reports what the model is doing as it happens, through ``on_event``.

The second one exists because the assistant must never *guess* what a model is
doing. A provider announces :data:`STATUS_THINKING` only after reasoning has
actually arrived, :data:`STATUS_GENERATING` only after answer text has arrived,
and the research statuses only when the provider really reports a search or a
page visit. A provider that cannot observe a phase never announces it, which is
why Ollama never claims to be researching the web.

Keys are stored in plain text inside the PyOS data directory (the same place
``config.json`` lives) with owner-only permissions where the platform supports
them, so a user can inspect or delete them with any text editor.
"""

import codecs
import json
import os
import tempfile
from urllib.parse import quote

import requests

from app_paths import get_data_dir

OLLAMA_API = "http://localhost:11434"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_API = "https://openrouter.ai/api/v1"
GROQ_API = "https://api.groq.com/openai/v1"

AI_CONFIG_FILENAME = "ai_config.json"

# Ollama is a local server, so the original timeouts are kept as-is.
OLLAMA_LIST_TIMEOUT = 5
OLLAMA_ASK_TIMEOUT = 30
# A streamed request stays open between chunks, so the read timeout is how long
# the model may stay silent, not how long the whole answer may take.
OLLAMA_STREAM_TIMEOUT = (5, 180)
# Cloud providers need a little more room for a connect/handshake.
CLOUD_LIST_TIMEOUT = (5, 20)
CLOUD_ASK_TIMEOUT = (5, 60)
CLOUD_STREAM_TIMEOUT = (10, 180)

DEFAULT_PROVIDER = "ollama"

# Sensible starting points. Every provider can refresh this list from its own
# API, so these only matter on the very first launch or when offline.
DEFAULT_MODELS = {
    "ollama": "llama3",
    "gemini": "gemini-2.0-flash",
    "openrouter": "openai/gpt-4o-mini",
    "groq": "openai/gpt-oss-120b",
}

PROVIDER_ORDER = ("ollama", "gemini", "openrouter", "groq")

# How many characters of standing instructions a provider will be sent. Local
# models usually have a smaller context window than the hosted ones. The PyOS
# reference runs to about 9,400 characters on a small install and about 10,400
# on a full one, so the local budget leaves room for the app list to grow. The
# per-request capability block in :mod:`ai_capability` is sent alongside it, so
# the pack is built to leave ``ai_capability.RESERVE_CHARS`` of room.
OLLAMA_SYSTEM_LIMIT = 12000
CLOUD_SYSTEM_LIMIT = 40000

# How much of an earlier conversation a provider is sent with each question.
# This is the whole remembered conversation, oldest turns dropped first, so it
# is deliberately smaller than the system budget: a local model has to hold the
# reference, the conversation and the answer in one context window, and clearing
# the conversation is what makes room when a local model slows down.
OLLAMA_HISTORY_LIMIT = 3000
CLOUD_HISTORY_LIMIT = 20000

# An SSE payload that never parses is set aside once it grows past this, so a
# broken stream cannot fill memory with garbage.
MAX_UNPARSED_PAYLOAD = 65536


# ---------------------------------------------------------------------------
# What a provider can report while it works
# ---------------------------------------------------------------------------

STATUS_WAITING = "waiting"
STATUS_THINKING = "thinking"
STATUS_GENERATING = "generating"
STATUS_RESEARCHING = "researching"
STATUS_VISITING = "visiting"

# The exact words the assistant speaks. These are deliberately short: they are
# heard mid-answer, while the user is waiting.
STATUS_SPEECH = {
    STATUS_WAITING: "Waiting for {provider}...",
    STATUS_THINKING: "Thinking...",
    STATUS_GENERATING: "Generating response...",
    STATUS_RESEARCHING: "Researching...",
    STATUS_VISITING: "Visiting website...",
}


def status_speech(status, provider_label=""):
    """Return the spoken form of a status, or an empty string if unknown.

    Only :data:`STATUS_WAITING` needs the provider's name, and the assistant
    says that one itself; every other status comes from the model and carries no
    provider.
    """
    text = STATUS_SPEECH.get(status, "")
    if provider_label:
        return text.format(provider=provider_label)
    return text


EVENT_STATUS = "status"
EVENT_REASONING = "reasoning"
EVENT_CONTENT = "content"
EVENT_SOURCE = "source"


class AIEvent:
    """One thing a provider observed while working on a question.

    ``kind`` is one of ``status``, ``reasoning``, ``content`` or ``source``. For
    a status, ``text`` is one of the ``STATUS_*`` words and ``detail`` carries
    whatever the provider said about it, such as a search query or a URL. For a
    source, ``text`` is the page title and ``detail`` is its address.
    """

    __slots__ = ("kind", "text", "detail")

    def __init__(self, kind, text="", detail=""):
        self.kind = kind
        self.text = text
        self.detail = detail

    def __eq__(self, other):
        if not isinstance(other, AIEvent):
            return NotImplemented
        return (self.kind, self.text, self.detail) == (other.kind, other.text, other.detail)

    def __hash__(self):
        return hash((self.kind, self.text, self.detail))

    def __repr__(self):
        if self.detail:
            return f"<AIEvent {self.kind} {self.text!r} {self.detail!r}>"
        return f"<AIEvent {self.kind} {self.text!r}>"


def status_event(status, detail=""):
    return AIEvent(EVENT_STATUS, status, detail)


def reasoning_event(text):
    return AIEvent(EVENT_REASONING, text)


def content_event(text):
    return AIEvent(EVENT_CONTENT, text)


def source_event(title, url):
    return AIEvent(EVENT_SOURCE, title, url)


# ---------------------------------------------------------------------------
# Local configuration (chosen provider, models, keys, web research)
# ---------------------------------------------------------------------------

def get_config_path():
    """Return the path of the AI configuration file."""
    return os.path.join(get_data_dir(), AI_CONFIG_FILENAME)


def default_config():
    return {
        "provider": DEFAULT_PROVIDER,
        "keys": {},
        "models": dict(DEFAULT_MODELS),
        "web": {},
        "speak_reasoning": True,
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

    web = data.get("web")
    if isinstance(web, dict):
        for name, value in web.items():
            if name in PROVIDER_ORDER:
                config["web"][str(name)] = bool(value)

    speak_reasoning = data.get("speak_reasoning")
    if isinstance(speak_reasoning, bool):
        config["speak_reasoning"] = speak_reasoning

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


def get_web(provider_key, path=None):
    """Whether web research was left switched on for this provider."""
    return bool(load_config(path)["web"].get(provider_key, False))


def set_web(provider_key, enabled, path=None):
    config = load_config(path)
    config["web"][provider_key] = bool(enabled)
    return save_config(config, path)


def get_speak_reasoning(path=None):
    """Whether the assistant narrates a model's reasoning as it arrives."""
    return bool(load_config(path).get("speak_reasoning", True))


def set_speak_reasoning(enabled, path=None):
    config = load_config(path)
    config["speak_reasoning"] = bool(enabled)
    return save_config(config, path)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """A provider problem that can be explained to the user out loud.

    ``kind`` is one of ``no_key``, ``bad_key``, ``offline``, ``timeout`` or
    ``http`` so the UI can react without inspecting HTTP status codes itself.
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

def _emit(on_event, event):
    """Hand one event to the caller, if anybody is listening."""
    if on_event is not None:
        on_event(event)


def _cancelled(cancel):
    """True when the user asked to stop the request that is running."""
    return cancel is not None and cancel.is_set()


def _iter_raw_lines(response):
    """Yield the lines of a streaming response, decoded as UTF-8 by us.

    ``requests`` picks its decoder from the response's Content-Type charset, and
    a ``text/event-stream`` header almost never carries one, so it falls back to
    ISO-8859-1 and reads UTF-8 bytes as Latin-1: accents, dashes, curly quotes
    and every emoji arrive mangled. Decoding the raw bytes ourselves with an
    incremental UTF-8 decoder fixes that and keeps a multi-byte character whole
    when it straddles two network chunks.

    Lines are split on the newline only, never with :meth:`str.splitlines`.
    Latin-1 decoding of UTF-8 can put a character such as U+0085 inside a JSON
    line, and ``splitlines`` treats that as a line break, which used to cut a
    payload in half and lose it.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    try:
        for chunk in response.iter_content(chunk_size=4096, decode_unicode=False):
            if not chunk:
                continue
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line[:-1] if line.endswith("\r") else line
        buffer += decoder.decode(b"", final=True)
    except ProviderError:
        raise
    except Exception:
        raise ProviderError(
            "offline", "The connection dropped while the model was replying."
        )
    if buffer:
        yield buffer[:-1] if buffer.endswith("\r") else buffer


def _unreadable_event(payload):
    """A payload that could not be parsed, kept so it is not lost silently."""
    return {"_unreadable": payload[:MAX_UNPARSED_PAYLOAD]}


def _iter_sse(response):
    """Yield the JSON objects in a ``text/event-stream`` body.

    Lines that are not data (comments, ``event:`` names, the blank separators
    between events) are skipped, as is the ``[DONE]`` marker that OpenAI-style
    streams end with.

    A payload that does not parse is held and retried against the next data
    line, because a provider can split one JSON object across several ``data:``
    lines, either mid-token or with the newline the event-stream format says to
    join them with. Only a payload that still will not parse is set aside, and
    then it is handed on as ``_unreadable`` rather than dropped, so a broken
    stream shows up in the logs and in tests instead of quietly losing text.
    """
    pending = ""
    for line in _iter_raw_lines(response):
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[len("data:"):]
        if payload.startswith(" "):
            payload = payload[1:]
        if not payload.strip() or payload.strip() == "[DONE]":
            # The event stream says the payloads are done, so anything still held
            # is reported rather than quietly thrown away with the marker.
            if pending.strip():
                yield _unreadable_event(pending)
            pending = ""
            continue

        attempts = [payload]
        if pending:
            attempts = [pending + payload, pending + "\n" + payload, payload]

        remaining = pending + payload
        for attempt in attempts:
            try:
                yield json.loads(attempt)
                pending = ""
                break
            except ValueError:
                continue
        else:
            if len(remaining) > MAX_UNPARSED_PAYLOAD:
                yield _unreadable_event(remaining)
                pending = ""
            else:
                pending = remaining

    if pending.strip():
        yield _unreadable_event(pending)


def _iter_ndjson(response):
    """Yield the JSON objects in Ollama's one-object-per-line stream."""
    for line in _iter_raw_lines(response):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            # A line that is not JSON at all is surfaced rather than skipped, so
            # it cannot disappear without a trace.
            yield _unreadable_event(line)


def _timeout_error(provider_name, error, offline_message):
    """Explain a timeout without guessing at the cause.

    A read timeout means the connection was fine and nothing came back, which is
    not the same as being offline, and saying so would send the user off to
    restart a server that is running perfectly well. A connect timeout really is
    a connection problem, so that case keeps the offline wording.
    """
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return ProviderError("offline", offline_message)
    return ProviderError(
        "timeout",
        f"{provider_name} did not reply in time. A large model, a slow computer or "
        "a long question can do that, so try a smaller model or a shorter question.",
    )


def _server_message(response):
    """Read a provider's own error sentence out of a failed response."""
    try:
        data = response.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")
    if isinstance(error, str):
        return error
    for field in ("detail", "message"):
        if isinstance(data.get(field), str):
            return data[field]
    return ""


def _with_detail(message, detail):
    """Add the provider's own wording to ours, when it says something new."""
    detail = (detail or "").strip()
    if not detail or detail in message:
        return message
    return f"{message} ({detail})"


def _argument_value(arguments, key):
    """Pull one value out of a tool call's arguments, tolerating plain text."""
    data = arguments
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return ""
    if isinstance(data, dict) and data.get(key):
        return str(data[key])
    return ""


def _looks_like_thinking_model(model):
    """Best guess at whether a local model reasons before answering.

    Only used to decide whether Ollama is asked for reasoning explicitly; the
    streaming reader narrates thinking whenever it turns up either way.
    """
    name = (model or "").lower()
    return any(
        hint in name
        for hint in ("r1", "qwen3", "gpt-oss", "deepseek-r", "reasoner", "thinking", "magistral")
    )


def _partial_tag_suffix(buffer, tag):
    """How many characters of ``buffer`` could be the start of ``tag``.

    A tag can be split across two chunks, so a tail that might still grow into
    ``<think`` or ``</think>`` is held back rather than read out as text.
    """
    for size in range(min(len(buffer), len(tag) - 1), 0, -1):
        if buffer.endswith(tag[:size]):
            return size
    return 0


# ---------------------------------------------------------------------------
# Conversation memory
# ---------------------------------------------------------------------------

_USER_ROLES = {"user", "you", "me", "human"}
_ASSISTANT_ROLES = {"assistant", "model", "ai", "provider", "bot"}


def normalize_history(history):
    """Return ``(role, text)`` pairs from whatever a caller passed in.

    The assistant keeps its conversation as ``{"role": ..., "text": ...}``
    dictionaries; pairs are accepted too, and roles are translated, so a
    provider never has to guess what it was handed. Unknown roles and empty
    turns are dropped rather than sent to a provider that would reject them.
    """
    entries = []
    for item in history or ():
        if isinstance(item, dict):
            role = item.get("role") or item.get("sender") or ""
            text = item.get("text")
            if text is None:
                text = item.get("content") or ""
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            role, text = item
        else:
            continue

        role = str(role).strip().lower()
        if role in _USER_ROLES:
            role = "user"
        elif role in _ASSISTANT_ROLES:
            role = "assistant"
        else:
            continue

        text = str(text or "").strip()
        if text:
            entries.append((role, text))
    return entries


def trim_history(history, max_chars):
    """Keep the newest turns that fit ``max_chars``.

    Returns ``(kept, dropped)``, where ``kept`` is a list of ``role``/``text``
    dictionaries newest-last, ready to be sent. Turns are dropped oldest first,
    and a leading answer whose question has been dropped goes with it, so the
    model never starts a conversation with a reply to nothing.
    """
    entries = normalize_history(history)
    if not max_chars or max_chars <= 0:
        return [], len(entries)

    kept = []
    used = 0
    for role, text in reversed(entries):
        cost = len(text) + 12
        if kept and used + cost > max_chars:
            break
        kept.append({"role": role, "text": text})
        used += cost
    kept.reverse()

    while kept and kept[0]["role"] != "user":
        kept.pop(0)

    return kept, len(entries) - len(kept)


def _is_compound_model(model):
    return (model or "").lower().startswith("groq/compound")


def _is_gpt_oss_model(model):
    return "gpt-oss" in (model or "").lower()


class ThinkingTagSplitter:
    """Separate ``<think>...</think>`` text from the answer body.

    Providers normally return reasoning in its own field, but some force it back
    into the answer text when a tool or JSON mode is in play. Feeding content
    through here keeps that reasoning narrated as reasoning rather than read out
    as part of the answer.
    """

    OPEN = "<think"
    CLOSE = "</think>"

    def __init__(self):
        self.buffer = ""
        self.in_think = False

    def feed(self, text):
        """Return a list of ``(is_reasoning, text)`` pieces."""
        self.buffer += text
        pieces = []
        while self.buffer:
            if self.in_think:
                end = self.buffer.find(self.CLOSE)
                if end != -1:
                    pieces.append((True, self.buffer[:end]))
                    self.buffer = self.buffer[end + len(self.CLOSE):]
                    self.in_think = False
                    continue
                tag = self.CLOSE
            else:
                start = self.buffer.find(self.OPEN)
                if start != -1:
                    if start > 0:
                        pieces.append((False, self.buffer[:start]))
                    rest = self.buffer[start + len(self.OPEN):]
                    close = rest.find(">")
                    if close == -1:
                        # Wait for the rest of the opening tag.
                        self.buffer = self.buffer[start:]
                        break
                    self.buffer = rest[close + 1:]
                    self.in_think = True
                    continue
                tag = self.OPEN

            # Plain text, minus any tail that might grow into a tag.
            held = _partial_tag_suffix(self.buffer, tag)
            body = self.buffer[: len(self.buffer) - held] if held else self.buffer
            self.buffer = self.buffer[len(self.buffer) - held:] if held else ""
            if body:
                pieces.append((self.in_think, body))
            if held:
                break
        return [(flag, piece) for flag, piece in pieces if piece]

    def flush(self):
        """Return whatever is still buffered, as ``(is_reasoning, text)``."""
        leftover, self.buffer = self.buffer, ""
        if not leftover:
            return (self.in_think, "")
        return (self.in_think, leftover)


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
    stream_timeout = CLOUD_STREAM_TIMEOUT
    system_char_limit = CLOUD_SYSTEM_LIMIT
    # How much remembered conversation this provider will be sent per question.
    history_char_limit = CLOUD_HISTORY_LIMIT
    # Whether this provider can research the web at all, and the sentence the
    # assistant says when it cannot.
    supports_web = False
    web_note = "This provider cannot research the web."

    def __init__(self, api_key=None):
        self.api_key = (api_key or "").strip()
        # Set when a provider had to answer without the remembered conversation,
        # so the assistant can say so instead of quietly forgetting.
        self.memory_note = ""

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

    def ask(self, prompt, model, system=None, web=False, history=None):
        """Send one prompt and return the reply as plain text.

        ``system`` is optional standing instruction for the model, such as the
        PyOS reference built by :mod:`pyos_knowledge`. Each provider keeps that
        in a different place, which is the only reason this argument is not
        already worded as a message list. ``web`` asks a provider that can
        research the web to use its own search and page-reading tools.

        ``history`` is the earlier part of the conversation, oldest first, as
        ``{"role": "user"|"assistant", "text": ...}`` dictionaries. Each
        provider maps it into its own request shape, so a follow-up question
        such as "and the second one?" still makes sense to the model.
        """
        raise NotImplementedError

    def ask_stream(
        self, prompt, model, system=None, on_event=None, cancel=None, web=False, history=None
    ):
        """Send one prompt, reporting what happens as it happens.

        ``on_event`` receives :class:`AIEvent` objects: a status the moment a
        phase really begins, reasoning and answer text as it arrives, and a
        source for every page the model actually used. Providers must only
        announce a phase they can observe.

        ``cancel`` is an optional :class:`threading.Event`; when it is set the
        stream is closed and whatever text arrived is returned. The final reply
        is returned either way, so callers use one code path for both.

        The default implementation cannot stream, so it asks once and reports
        the finished answer as a single content event. Providers that can stream
        override this.
        """
        reply = self.ask(prompt, model, system=system, web=web, history=history)
        if _cancelled(cancel):
            return ""
        if reply:
            _emit(on_event, content_event(reply))
        return reply

    def can_research(self, model):
        """Whether this model can search the web or read pages on the web."""
        return bool(self.supports_web)

    def web_unavailable(self, model):
        """Why research is not available, for the assistant to explain."""
        return self.web_note

    def web_tool_summary(self, model):
        """One phrase naming the web tools this model would be given.

        Used in the per-request capability block from :mod:`ai_capability`, so
        the model is told what it can actually do rather than having to guess.
        """
        return ""

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
    stream_timeout = OLLAMA_STREAM_TIMEOUT
    system_char_limit = OLLAMA_SYSTEM_LIMIT
    history_char_limit = OLLAMA_HISTORY_LIMIT
    supports_web = False
    web_note = "Models running on this computer cannot search the web."

    # Whether this Ollama is too old for /api/chat, which is what carries the
    # conversation. Learned once, on the first 404, and remembered for the rest
    # of the session.
    legacy_endpoint = False

    def __init__(self, api_key=None, base_url=OLLAMA_API):
        super().__init__(api_key)
        self.base_url = base_url

    def _messages(self, prompt, system, history):
        """Build Ollama's message list: instructions, then conversation, then
        the new question."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        for role, text in normalize_history(history):
            messages.append({"role": role, "content": text})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _request(self, prompt, model, system, history, stream):
        """Return the endpoint and payload for one question.

        ``/api/chat`` is used because it is the only Ollama endpoint that takes
        a conversation; when the installed Ollama is too old to have it, the
        question is asked through ``/api/generate`` instead, without the memory,
        and :attr:`memory_note` explains why.
        """
        payload = {"model": model, "stream": stream}
        if self.legacy_endpoint:
            payload["prompt"] = prompt
            if system:
                payload["system"] = system
            return f"{self.base_url}/api/generate", payload
        payload["messages"] = self._messages(prompt, system, history)
        return f"{self.base_url}/api/chat", payload

    def _fall_back_to_legacy(self):
        """Stop asking for a conversation this Ollama cannot provide."""
        type(self).legacy_endpoint = True
        self.memory_note = (
            "This Ollama is too old to remember a conversation, so each question "
            "was answered on its own. Updating Ollama brings memory back."
        )

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

    def ask(self, prompt, model, system=None, web=False, history=None):
        endpoint, payload = self._request(prompt, model, system, history, stream=False)
        try:
            response = requests.post(endpoint, json=payload, timeout=self.ask_timeout)
        except requests.exceptions.Timeout as err:
            raise _timeout_error(
                "Ollama", err, "Connection error. Make sure Ollama is running."
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Connection error. Make sure Ollama is running."
            )

        if response.status_code == 404 and not self.legacy_endpoint:
            self._fall_back_to_legacy()
            return self.ask(prompt, model, system=system, web=web, history=history)
        if response.status_code != 200:
            raise ProviderError("http", f"Ollama returned error {response.status_code}.")

        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", "Ollama sent a response that could not be read.")

        return self._reply_from(data)

    def ask_stream(
        self, prompt, model, system=None, on_event=None, cancel=None, web=False, history=None
    ):
        endpoint, payload = self._request(prompt, model, system, history, stream=True)
        if _looks_like_thinking_model(model):
            # Only thinking models understand this, and older ones reject it.
            payload["think"] = True
        try:
            response = requests.post(
                endpoint,
                json=payload,
                timeout=self.stream_timeout,
                stream=True,
            )
        except requests.exceptions.Timeout as err:
            raise _timeout_error(
                "Ollama", err, "Connection error. Make sure Ollama is running."
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Connection error. Make sure Ollama is running."
            )

        with response:
            if response.status_code == 404 and not self.legacy_endpoint:
                self._fall_back_to_legacy()
                return self.ask_stream(
                    prompt,
                    model,
                    system=system,
                    on_event=on_event,
                    cancel=cancel,
                    web=web,
                    history=history,
                )
            if response.status_code != 200:
                raise ProviderError(
                    "http",
                    _with_detail(
                        f"Ollama returned error {response.status_code}.",
                        _server_message(response),
                    ),
                )

            answer, reasoning = [], []
            seen_thinking = seen_generating = False
            for chunk in _iter_ndjson(response):
                if _cancelled(cancel):
                    break
                error = chunk.get("error")
                if error:
                    raise ProviderError("http", f"Ollama reported an error: {error}")
                message = chunk.get("message") or {}
                thinking = message.get("thinking") or chunk.get("thinking") or ""
                if thinking:
                    if not seen_thinking:
                        seen_thinking = True
                        _emit(on_event, status_event(STATUS_THINKING))
                    reasoning.append(thinking)
                    _emit(on_event, reasoning_event(thinking))
                text = message.get("content") or chunk.get("response") or ""
                if text:
                    if not seen_generating:
                        seen_generating = True
                        _emit(on_event, status_event(STATUS_GENERATING))
                    answer.append(text)
                    _emit(on_event, content_event(text))
                if chunk.get("done"):
                    break

        if not answer:
            return "" if _cancelled(cancel) else "I couldn't generate a response."
        return "".join(answer).strip()

    def _reply_from(self, data):
        """Read the answer out of either endpoint's reply."""
        message = data.get("message") or {}
        reply = message.get("content") or data.get("response") or ""
        if not reply and (message.get("thinking") or data.get("thinking")):
            # A thinking model that ran out of room before answering.
            return "I couldn't generate a response."
        return reply or "I couldn't generate a response."

    def web_tool_summary(self, model):
        return ""


class GeminiProvider(AIProvider):
    key = "gemini"
    label = "Google Gemini"
    description = "Google's Gemini models, reached over the internet. Requires a Gemini API key."
    requires_api_key = True
    default_model = "gemini-2.0-flash"
    list_timeout = CLOUD_LIST_TIMEOUT
    ask_timeout = CLOUD_ASK_TIMEOUT
    system_char_limit = CLOUD_SYSTEM_LIMIT
    supports_web = True
    web_note = "Gemini can search the web."

    def __init__(self, api_key=None, base_url=GEMINI_API):
        super().__init__(api_key)
        self.base_url = base_url

    def _require_key(self):
        if not self.api_key:
            raise ProviderError(
                "no_key", "Gemini needs an API key. Press the Provider button to enter one."
            )

    def _contents(self, prompt, history):
        """Build Gemini's ``contents`` list, mending the shape Gemini insists on.

        Gemini wants a ``user`` and a ``model`` turn to alternate and wants the
        list to start with the user, so a run of same-role turns is joined into
        one and a leading model turn is dropped. The new question is joined onto
        a trailing user turn rather than following it, which keeps the
        alternation intact when the last remembered turn was a question that
        the model never answered.
        """
        contents = []
        for role, text in normalize_history(history):
            gemini_role = "user" if role == "user" else "model"
            if contents and contents[-1]["role"] == gemini_role:
                contents[-1]["parts"][0]["text"] += "\n\n" + text
                continue
            if not contents and gemini_role != "user":
                continue
            contents.append({"role": gemini_role, "parts": [{"text": text}]})

        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"][0]["text"] += "\n\n" + prompt
        else:
            contents.append({"role": "user", "parts": [{"text": prompt}]})
        return contents

    def web_tool_summary(self, model):
        return "able to search Google and read the pages you find"

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

    def ask(self, prompt, model, system=None, web=False, history=None):
        self._require_key()
        model_name = _strip_model_prefix(model)
        payload = {"contents": self._contents(prompt, history)}
        if system:
            # Gemini takes instructions in their own field; a "system" role is
            # not allowed among the contents.
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if web:
            payload["tools"] = _GEMINI_WEB_TOOLS
        try:
            response = requests.post(
                f"{self.base_url}/models/{quote(model_name, safe='')}:generateContent",
                params={"key": self.api_key},
                json=payload,
                timeout=self.ask_timeout,
            )
        except requests.exceptions.Timeout as err:
            raise _timeout_error(
                "Gemini",
                err,
                "Connection error. Could not reach Gemini. Check your internet connection.",
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
            text = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
            if not text:
                text = "".join(part.get("text", "") for part in parts).strip()
        except (KeyError, IndexError, TypeError):
            text = ""

        if not text:
            return self._empty_message(_finish_reason(data))
        return text

    def ask_stream(
        self, prompt, model, system=None, on_event=None, cancel=None, web=False, history=None
    ):
        self._require_key()
        model_name = _strip_model_prefix(model)
        payload = {"contents": self._contents(prompt, history)}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if model_name not in THINKING_CONFIG_REFUSED:
            payload["generationConfig"] = {"thinkingConfig": {"includeThoughts": True}}
        if web:
            payload["tools"] = _GEMINI_WEB_TOOLS

        try:
            response = requests.post(
                f"{self.base_url}/models/{quote(model_name, safe='')}:streamGenerateContent",
                params={"key": self.api_key, "alt": "sse"},
                json=payload,
                timeout=self.stream_timeout,
                stream=True,
            )
        except requests.exceptions.Timeout as err:
            raise _timeout_error(
                "Gemini",
                err,
                "Connection error. Could not reach Gemini. Check your internet connection.",
            )
        except requests.exceptions.RequestException:
            raise ProviderError(
                "offline", "Connection error. Could not reach Gemini. Check your internet connection."
            )

        with response:
            if response.status_code == 400 and "generationConfig" in payload:
                detail = _server_message(response)
                if _mentions_thinking(detail):
                    # This model cannot return thought summaries. Ask again
                    # without that setting rather than failing the question.
                    THINKING_CONFIG_REFUSED.add(model_name)
                    return self.ask_stream(
                        prompt,
                        model,
                        system=system,
                        on_event=on_event,
                        cancel=cancel,
                        web=web,
                        history=history,
                    )
            self._check_stream_status(response)

            answer, reasoning = [], []
            seen_thinking = seen_generating = False
            reported_queries, reported_urls, reported_sources = set(), set(), set()
            finish = ""
            for data in _iter_sse(response):
                if _cancelled(cancel):
                    break
                error = data.get("error")
                if isinstance(error, dict) and error.get("message"):
                    raise ProviderError(
                        "http", f"Gemini reported an error: {error['message']}"
                    )
                for candidate in data.get("candidates") or []:
                    finish = candidate.get("finishReason") or finish
                    metadata = candidate.get("groundingMetadata") or {}
                    for query in metadata.get("webSearchQueries") or []:
                        if query and query not in reported_queries:
                            reported_queries.add(query)
                            _emit(on_event, status_event(STATUS_RESEARCHING, str(query)))
                    for grounding in metadata.get("groundingChunks") or []:
                        page = (grounding or {}).get("web") or {}
                        uri = page.get("uri") or ""
                        if uri and uri not in reported_sources:
                            reported_sources.add(uri)
                            _emit(on_event, source_event(page.get("title") or uri, uri))
                    for entry in (candidate.get("urlContextMetadata") or {}).get("urlMetadata") or []:
                        target = (entry or {}).get("retrievedUrl") or ""
                        if not target or target in reported_urls:
                            continue
                        reported_urls.add(target)
                        _emit(on_event, status_event(STATUS_VISITING, target))
                        if str(entry.get("urlRetrievalStatus", "")).endswith("SUCCESS"):
                            if target not in reported_sources:
                                reported_sources.add(target)
                                _emit(on_event, source_event(target, target))
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        text = (part or {}).get("text") or ""
                        if not text:
                            continue
                        if part.get("thought"):
                            if not seen_thinking:
                                seen_thinking = True
                                _emit(on_event, status_event(STATUS_THINKING))
                            reasoning.append(text)
                            _emit(on_event, reasoning_event(text))
                        else:
                            if not seen_generating:
                                seen_generating = True
                                _emit(on_event, status_event(STATUS_GENERATING))
                            answer.append(text)
                            _emit(on_event, content_event(text))

        if not answer:
            return "" if _cancelled(cancel) else self._empty_message(finish)
        return "".join(answer).strip()

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

    def _empty_message(self, finish_reason):
        if finish_reason == "SAFETY":
            return "Gemini refused to answer that prompt for safety reasons."
        return "Gemini returned an empty response."

    def _check_status(self, response):
        if response.status_code in (400, 401, 403):
            raise ProviderError(
                "bad_key", "Gemini rejected that API key. Check the key and try again."
            )
        if response.status_code == 404:
            raise ProviderError("http", "Gemini does not recognise that model name.")
        if response.status_code != 200:
            raise ProviderError("http", f"Gemini returned error {response.status_code}.")

    def _check_stream_status(self, response):
        if response.status_code in (400, 401, 403):
            raise ProviderError(
                "bad_key",
                _with_detail(
                    "Gemini rejected that API key. Check the key and try again.",
                    _server_message(response),
                ),
            )
        if response.status_code == 404:
            raise ProviderError("http", "Gemini does not recognise that model name.")
        if response.status_code != 200:
            raise ProviderError(
                "http",
                _with_detail(
                    f"Gemini returned error {response.status_code}.",
                    _server_message(response),
                ),
            )


class OpenAICompatibleProvider(AIProvider):
    """Base for the providers that speak OpenAI's chat-completions dialect.

    OpenRouter and Groq use the same request shape, the same server-sent-event
    stream and the same delta fields, so the reader lives here once and each
    provider supplies its own address, headers, wording and payload extras.
    """

    base_url = ""
    provider_name = "The provider"
    supports_web = False
    # True when the provider researches on every web request without saying so
    # in the stream, so the assistant can still announce it honestly.
    research_is_request_driven = False

    def __init__(self, api_key=None, base_url=None):
        super().__init__(api_key)
        if base_url:
            self.base_url = base_url

    # -- hooks a provider fills in ---------------------------------------
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _messages(self, prompt, system, history=None):
        """Build the message list: instructions, conversation, new question."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        for role, text in normalize_history(history):
            messages.append({"role": role, "content": text})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _payload(self, prompt, model, system, stream, web, history=None):
        return {
            "model": model,
            "messages": self._messages(prompt, system, history),
            "stream": stream,
        }

    def _consume_choice(self, choice, context, on_event):
        """Provider-specific extras for one streamed choice (unused by default)."""

    # -- shared behaviour -------------------------------------------------
    def _require_key(self):
        if not self.api_key:
            raise ProviderError(
                "no_key",
                f"{self.provider_name} needs an API key. Press the Provider button to enter one.",
            )

    def _offline_message(self):
        return (
            f"Connection error. Could not reach {self.provider_name}. "
            "Check your internet connection."
        )

    def _bad_key_message(self):
        return (
            f"{self.provider_name} rejected that API key. "
            "Check the key and try again."
        )

    def _unreadable_message(self):
        return f"{self.provider_name} sent a response that could not be read."

    def _empty_message(self):
        return f"{self.provider_name} returned an empty response."

    def _refusal_message(self):
        return f"{self.provider_name} refused to answer that prompt."

    def _check_status(self, response):
        if response.status_code in (401, 403):
            raise ProviderError("bad_key", self._bad_key_message())
        if response.status_code != 200:
            raise ProviderError(
                "http",
                _with_detail(
                    f"{self.provider_name} returned error {response.status_code}.",
                    _server_message(response),
                ),
            )

    def ask(self, prompt, model, system=None, web=False, history=None):
        self._require_key()
        payload = self._payload(prompt, model, system, stream=False, web=web, history=history)
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.ask_timeout,
            )
        except requests.exceptions.Timeout as err:
            raise _timeout_error(self.provider_name, err, self._offline_message())
        except requests.exceptions.RequestException:
            raise ProviderError("offline", self._offline_message())

        self._check_status(response)
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", self._unreadable_message())

        try:
            choice = data["choices"][0]
            message = choice["message"] or {}
            text = message.get("content") or ""
            if not text and choice.get("finish_reason") == "content_filter":
                return self._refusal_message()
        except (KeyError, IndexError, TypeError):
            text = ""

        if not text.strip():
            return self._empty_message()
        return text.strip()

    def ask_stream(
        self, prompt, model, system=None, on_event=None, cancel=None, web=False, history=None
    ):
        self._require_key()
        payload = self._payload(prompt, model, system, stream=True, web=web, history=history)
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.stream_timeout,
                stream=True,
            )
        except requests.exceptions.Timeout as err:
            raise _timeout_error(self.provider_name, err, self._offline_message())
        except requests.exceptions.RequestException:
            raise ProviderError("offline", self._offline_message())

        with response:
            self._check_status(response)

            context = {
                "web": bool(web),
                "seen_thinking": False,
                "seen_generating": False,
                "seen_researching": False,
                "sources": set(),
                "tools": set(),
            }
            splitter = ThinkingTagSplitter()
            answer, reasoning = [], []
            finish = ""
            for data in _iter_sse(response):
                if _cancelled(cancel):
                    break
                error = data.get("error")
                if error:
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise ProviderError(
                        "http", f"{self.provider_name} reported an error: {detail}"
                    )
                choices = data.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish = choice.get("finish_reason") or finish
                if (
                    context["web"]
                    and not context["seen_researching"]
                    and self.research_is_request_driven
                ):
                    # The provider searches before every web request but reports
                    # nothing while it runs, so this one status comes from the
                    # request rather than from the stream.
                    context["seen_researching"] = True
                    _emit(on_event, status_event(STATUS_RESEARCHING))
                delta = choice.get("delta") or {}
                thought = delta.get("reasoning") or delta.get("reasoning_content") or ""
                if thought:
                    if not context["seen_thinking"]:
                        context["seen_thinking"] = True
                        _emit(on_event, status_event(STATUS_THINKING))
                    reasoning.append(thought)
                    _emit(on_event, reasoning_event(thought))
                self._emit_annotations(delta.get("annotations"), context, on_event)
                self._consume_choice(choice, context, on_event)
                content = delta.get("content") or ""
                if content:
                    for is_reasoning, piece in splitter.feed(content):
                        if is_reasoning:
                            if not context["seen_thinking"]:
                                context["seen_thinking"] = True
                                _emit(on_event, status_event(STATUS_THINKING))
                            reasoning.append(piece)
                            _emit(on_event, reasoning_event(piece))
                        else:
                            if not context["seen_generating"]:
                                context["seen_generating"] = True
                                _emit(on_event, status_event(STATUS_GENERATING))
                            answer.append(piece)
                            _emit(on_event, content_event(piece))
                self._emit_annotations(
                    (choice.get("message") or {}).get("annotations"), context, on_event
                )

            is_reasoning, piece = splitter.flush()
            if piece:
                if is_reasoning:
                    reasoning.append(piece)
                    _emit(on_event, reasoning_event(piece))
                else:
                    answer.append(piece)
                    _emit(on_event, content_event(piece))

        if not answer:
            if _cancelled(cancel):
                return ""
            if finish == "content_filter":
                return self._refusal_message()
            return self._empty_message()
        return "".join(answer).strip()

    def _emit_annotations(self, annotations, context, on_event):
        """Turn OpenRouter's ``url_citation`` annotations into source events."""
        for annotation in annotations or []:
            if not isinstance(annotation, dict):
                continue
            citation = annotation.get("url_citation") or {}
            url = citation.get("url") or ""
            if not url or url in context["sources"]:
                continue
            context["sources"].add(url)
            _emit(on_event, source_event(citation.get("title") or url, url))


class OpenRouterProvider(OpenAICompatibleProvider):
    key = "openrouter"
    label = "OpenRouter"
    description = "Many models from many companies through OpenRouter. Requires an OpenRouter API key."
    requires_api_key = True
    default_model = "openai/gpt-4o-mini"
    list_timeout = CLOUD_LIST_TIMEOUT
    ask_timeout = CLOUD_ASK_TIMEOUT
    base_url = OPENROUTER_API
    provider_name = "OpenRouter"
    supports_web = True
    web_note = "OpenRouter can search the web for any model."
    research_is_request_driven = True

    def __init__(self, api_key=None, base_url=OPENROUTER_API):
        super().__init__(api_key, base_url)

    def _headers(self):
        headers = super()._headers()
        headers["HTTP-Referer"] = "https://github.com/tech-master33/py-os"
        headers["X-Title"] = "PyOS AI Assistant"
        return headers

    def web_tool_summary(self, model):
        return "able to search the web before answering, through OpenRouter's web plugin"

    def _payload(self, prompt, model, system, stream, web, history=None):
        payload = super()._payload(prompt, model, system, stream, web, history)
        if web:
            # The plugin form is used rather than the ":online" model suffix so
            # the model name the user picked is sent exactly as it was chosen.
            payload["plugins"] = [{"id": "web"}]
        return payload

    def list_models(self):
        # The model catalogue is public, but sending the key lets OpenRouter
        # return the models this particular key can actually use.
        headers = self._headers() if self.api_key else None
        try:
            response = requests.get(
                f"{self.base_url}/models", headers=headers, timeout=self.list_timeout
            )
        except requests.exceptions.RequestException:
            raise ProviderError("offline", self._offline_message())

        if response.status_code in (401, 403):
            raise ProviderError("bad_key", self._bad_key_message())
        if response.status_code != 200:
            raise ProviderError(
                "http", f"{self.provider_name} returned error {response.status_code}."
            )

        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", self._unreadable_message())

        names = [item["id"] for item in data.get("data", []) if item.get("id")]
        if not names:
            raise ProviderError(
                "http", f"{self.provider_name} did not report any models."
            )
        return sorted(set(names))

    def verify_key(self, api_key=None):
        key = (api_key or self.api_key or "").strip()
        if not key:
            raise ProviderError("no_key", "Type or paste your OpenRouter API key first.")
        try:
            response = requests.get(
                f"{self.base_url}/key",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=self.list_timeout,
            )
        except requests.exceptions.RequestException:
            raise ProviderError("offline", self._offline_message())
        if response.status_code in (401, 403):
            raise ProviderError("bad_key", self._bad_key_message())
        if response.status_code != 200:
            raise ProviderError(
                "http", f"{self.provider_name} returned error {response.status_code}."
            )
        return True


class GroqProvider(OpenAICompatibleProvider):
    """Groq's fast hosted models, including the research-capable Compound ones."""

    key = "groq"
    label = "Groq"
    description = "Fast open models from Groq, including web-searching Compound systems. Requires a Groq API key."
    requires_api_key = True
    default_model = "openai/gpt-oss-120b"
    list_timeout = CLOUD_LIST_TIMEOUT
    ask_timeout = CLOUD_ASK_TIMEOUT
    base_url = GROQ_API
    provider_name = "Groq"
    supports_web = True
    web_note = (
        "Web research needs a Compound system such as groq/compound, "
        "or a GPT OSS model."
    )

    def __init__(self, api_key=None, base_url=GROQ_API):
        super().__init__(api_key, base_url)

    def _headers(self):
        headers = super()._headers()
        # Groq asks for this when the Compound tool settings are customised.
        headers["Groq-Model-Version"] = "latest"
        return headers

    def can_research(self, model):
        return _is_compound_model(model) or _is_gpt_oss_model(model)

    def web_tool_summary(self, model):
        if not self.can_research(model):
            return ""
        if _is_compound_model(model):
            return (
                "able to search the web and read pages with the Compound "
                "web_search and visit_website tools"
            )
        return "able to search the web with Groq's browser search tool"

    def _payload(self, prompt, model, system, stream, web, history=None):
        payload = super()._payload(prompt, model, system, stream, web, history)
        # "parsed" keeps reasoning in its own field; Groq's default is "raw",
        # which buries it in <think> tags inside the answer text.
        payload["reasoning_format"] = "parsed"
        if web and self.can_research(model):
            if _is_compound_model(model):
                payload["compound_custom"] = {
                    "tools": {"enabled_tools": ["web_search", "visit_website"]}
                }
            else:
                payload["tools"] = [{"type": "browser_search"}]
        return payload

    def list_models(self):
        self._require_key()
        try:
            response = requests.get(
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=self.list_timeout,
            )
        except requests.exceptions.RequestException:
            raise ProviderError("offline", self._offline_message())

        if response.status_code in (401, 403):
            raise ProviderError("bad_key", self._bad_key_message())
        if response.status_code != 200:
            raise ProviderError(
                "http", f"{self.provider_name} returned error {response.status_code}."
            )

        try:
            data = response.json()
        except ValueError:
            raise ProviderError("http", self._unreadable_message())

        names = [item["id"] for item in data.get("data", []) if item.get("id")]
        if not names:
            raise ProviderError(
                "http", f"{self.provider_name} did not report any models."
            )
        return sorted(set(names))

    def verify_key(self, api_key=None):
        key = (api_key or self.api_key or "").strip()
        if not key:
            raise ProviderError("no_key", "Type or paste your Groq API key first.")
        previous = self.api_key
        self.api_key = key
        try:
            # Groq has no dedicated key-check end point, so the model catalogue
            # doubles as one.
            self.list_models()
        finally:
            self.api_key = previous
        return True

    def _consume_choice(self, choice, context, on_event):
        """Report the built-in tools Groq ran on its own servers.

        Compound systems search and visit pages without telling us first, so
        these entries are what makes "Researching..." and "Visiting website..."
        truthful for Groq. When the model uses no tool, none of this appears and
        the assistant says nothing about research.
        """
        tools = []
        for part in (choice.get("delta") or {}, choice.get("message") or {}):
            tools.extend(part.get("executed_tools") or [])
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            kind = str(tool.get("type", "")).lower()
            arguments = tool.get("arguments") or ""
            if "visit" in kind:
                detail = _argument_value(arguments, "url") or str(arguments)
                marker = ("visit", detail)
                if detail and marker not in context["tools"]:
                    context["tools"].add(marker)
                    _emit(on_event, status_event(STATUS_VISITING, detail))
            elif "search" in kind:
                detail = _argument_value(arguments, "query") or str(arguments)
                marker = ("search", detail)
                if detail and marker not in context["tools"]:
                    context["tools"].add(marker)
                    _emit(on_event, status_event(STATUS_RESEARCHING, detail))
            for result in tool.get("search_results") or []:
                if not isinstance(result, dict):
                    continue
                url = result.get("url") or ""
                if url and url not in context["sources"]:
                    context["sources"].add(url)
                    _emit(on_event, source_event(result.get("title") or url, url))


def _strip_model_prefix(name):
    """Turn ``models/gemini-2.0-flash`` into ``gemini-2.0-flash``."""
    name = (name or "").strip()
    if name.startswith("models/"):
        name = name[len("models/"):]
    return name


def _finish_reason(data):
    try:
        return data["candidates"][0].get("finishReason", "") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _mentions_thinking(detail):
    text = (detail or "").lower()
    return "thinking" in text or "thought" in text


# Gemini's hosted web tools, asked for only when the user turns research on.
_GEMINI_WEB_TOOLS = [{"google_search": {}}, {"url_context": {}}]

# Models that refused thought summaries this session, so the setting is not sent
# to them again. Google only offers thought summaries on some models.
THINKING_CONFIG_REFUSED = set()


PROVIDER_CLASSES = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
    "openrouter": OpenRouterProvider,
    "groq": GroqProvider,
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
