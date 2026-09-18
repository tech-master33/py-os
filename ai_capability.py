"""What a model is told about itself, for the question it is being asked.

The PyOS reference in :mod:`pyos_knowledge` describes the assistant from the
outside: it lists the providers, what each one can do and where the settings
live. A model reading that has no idea that *it* is the provider being talked
to, so when someone says "research this for me" it answers with advice about
PyOS instead of searching, or says the user should check whether their provider
can research at all.

This module supplies the missing half: a short block, rebuilt for every
question, that says who the model is right now, whether web research is
switched on for this exact request, what that means for this provider, and how
to behave either way. It is deliberately small, because it is sent with every
question and it is paid for on the hosted providers.

Like :mod:`ai_providers`, :mod:`pyos_knowledge` and :mod:`text_integrity`, this
module avoids importing wx so it can be tested without a GUI, and it lives at the
repository root rather than in ``apps/`` so the plugin loader does not treat it
as an application.
"""

import re

# How much of a provider's system budget this block may use. The knowledge pack
# is built to leave this much room, so the two always fit together.
RESERVE_CHARS = 1600

_HEADING = "== YOU, RIGHT NOW =="

_IDENTITY = (
    "You are {label}, the provider the user chose in the PyOS AI Assistant, and "
    "you are being talked to directly. You are not a list of providers, you "
    "cannot see the PyOS settings, and you cannot switch anything on. Never "
    "answer a request by telling the user to look at which providers exist, "
    "which one they are using, or whether a provider can research: everything "
    "you need to know about yourself is in this block."
)

_MEMORY = (
    "Earlier turns of this conversation may be included with the question; use "
    "them to follow what the user means. If they refer to something you cannot "
    "see, say you have forgotten it instead of guessing."
)

_RESEARCH_ON = (
    "Web research for this request: ON. You are {tools}. When the user asks you "
    "to search, look something up, check the news or research anything, use "
    "those tools during this same reply, then answer from what you actually "
    "found and mention the pages you used. Never claim to have searched when "
    "you have not, and never hand the question back with advice about PyOS."
)

_RESEARCH_OFF = (
    "Web research for this request: OFF, so you cannot reach the web while "
    "answering. If the user asks you to search, look something up or check the "
    "news, say plainly that web research is switched off in PyOS and that the "
    "Web research box in the AI Assistant turns it on, then answer from what "
    "you already know and say that is what you are doing."
)

_RESEARCH_IMPOSSIBLE = (
    "Web research for this request: OFF, and nothing can turn it on for this "
    "model. {reason}"
)

# Phrases that suggest the user wants something looked up rather than
# remembered. Used only to warn honestly before a question is sent, and kept
# deliberately narrow so ordinary questions are not interrupted.
_RESEARCH_PHRASES = (
    "research",
    "search",
    "google",
    "look up",
    "look-up",
    "lookup",
    "find out",
    "find information",
    "latest",
    "newest",
    "most recent",
    "recent",
    "current",
    "news",
    "online",
    "website",
    "web site",
    "browse",
    "article",
    "weather forecast",
    "exchange rate",
    "price of",
    "stock price",
    "who won",
)

_RESEARCH_RE = re.compile(
    r"\b(" + "|".join(re.escape(phrase) for phrase in _RESEARCH_PHRASES) + r")\b"
)


def looks_like_research(prompt):
    """Whether a question is asking for something looked up on the web.

    This decides one thing only: whether the assistant mentions, before sending
    the question, that research is switched off. It is a hint for the user, not
    a gate, so a wrong answer costs a sentence and never blocks a question.
    """
    if not prompt:
        return False
    return bool(_RESEARCH_RE.search(prompt.lower()))


def capability_block(provider, model, web_on, remembered_turns=0):
    """Return the block describing this model's situation for one question.

    ``provider`` is any object with ``label``, ``can_research(model)``,
    ``web_unavailable(model)`` and ``web_tool_summary(model)``, which every
    provider in :mod:`ai_providers` supplies. ``web_on`` is whether the user has
    research switched on *and* this provider can do it for this model, so the
    model is never told it has tools it was not sent.
    """
    label = getattr(provider, "label", "the AI provider") or "the AI provider"
    lines = [_HEADING, _IDENTITY.format(label=label)]

    # What the caller asked for is not the same as what this model can do, and
    # this block is the one place that must never promise a tool that was not
    # sent. A model that cannot research is told research is off however the
    # caller called it.
    can_research = False
    if provider is not None:
        try:
            can_research = bool(provider.can_research(model))
        except Exception:
            can_research = False
    web_on = bool(web_on) and can_research

    if web_on:
        tools = ""
        summary = getattr(provider, "web_tool_summary", None)
        if callable(summary):
            try:
                tools = (summary(model) or "").strip()
            except Exception:
                tools = ""
        lines.append(_RESEARCH_ON.format(tools=tools or "able to search the web"))
    elif not can_research and provider is not None:
        reason = ""
        unavailable = getattr(provider, "web_unavailable", None)
        if callable(unavailable):
            try:
                reason = (unavailable(model) or "").strip()
            except Exception:
                reason = ""
        lines.append(
            _RESEARCH_IMPOSSIBLE.format(
                reason=reason or "This model has no web tools."
            )
        )
    else:
        lines.append(_RESEARCH_OFF)

    if remembered_turns:
        lines.append(_MEMORY)

    return "\n\n".join(lines)
