"""Checks for what a model is told about itself with every question.

The reference in :mod:`pyos_knowledge` describes the assistant from outside: it
lists the providers, the settings and where the keys live. A model reading only
that has no idea it *is* the provider being talked to, which is why asking one to
research used to come back as advice about checking providers. The block built
here closes that gap, and these tests pin down the three situations it has to
describe correctly: research on, research off, and a model that could never
research at all.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_capability
from ai_providers import GeminiProvider, GroqProvider, OllamaProvider, OpenRouterProvider


def setUpModule():
    os.environ.setdefault("PY_OS_DATA_DIR", os.path.join(os.path.dirname(__file__), "_unused"))


class IdentityTests(unittest.TestCase):
    def test_the_model_is_told_which_provider_it_is(self):
        block = ai_capability.capability_block(GroqProvider("k"), "groq/compound", True, 1)
        self.assertIn("You are Groq", block)

    def test_the_model_is_told_not_to_deflect_to_a_list_of_providers(self):
        for provider, model in (
            (OllamaProvider(), "llama3"),
            (GeminiProvider("k"), "gemini-2.5-flash"),
            (OpenRouterProvider("k"), "openai/gpt-4o-mini"),
            (GroqProvider("k"), "groq/compound"),
        ):
            block = ai_capability.capability_block(provider, model, True, 0)
            self.assertIn("not a list of providers", block)
            self.assertIn("Never answer a request", block)

    def test_memory_is_only_mentioned_when_there_is_some(self):
        provider = GeminiProvider("k")
        self.assertNotIn("Earlier turns", ai_capability.capability_block(provider, "m", True, 0))
        self.assertIn("Earlier turns", ai_capability.capability_block(provider, "m", True, 2))


class ResearchStateTests(unittest.TestCase):
    def test_research_on_names_the_tools_for_that_provider(self):
        cases = [
            (GeminiProvider("k"), "gemini-2.5-flash", "search Google"),
            (OpenRouterProvider("k"), "openai/gpt-4o-mini", "web plugin"),
            (GroqProvider("k"), "groq/compound", "web_search and visit_website"),
            (GroqProvider("k"), "openai/gpt-oss-120b", "browser search"),
        ]
        for provider, model, expected in cases:
            block = ai_capability.capability_block(provider, model, True, 0)
            self.assertIn("Web research for this request: ON", block)
            self.assertIn(expected, block)

    def test_the_model_is_told_to_use_the_tools_and_not_to_claim_otherwise(self):
        block = ai_capability.capability_block(GeminiProvider("k"), "gemini-2.5-flash", True, 0)
        self.assertIn("use those tools during this same reply", block)
        self.assertIn("Never claim to have searched when you have not", block)

    def test_research_off_says_so_and_points_at_the_box(self):
        block = ai_capability.capability_block(GeminiProvider("k"), "gemini-2.5-flash", False, 0)
        self.assertIn("Web research for this request: OFF", block)
        self.assertIn("Web research box", block)
        self.assertNotIn("ON.", block)

    def test_a_model_that_cannot_research_is_told_why(self):
        block = ai_capability.capability_block(OllamaProvider(), "llama3", False, 0)
        self.assertIn("nothing can turn it on for this model", block)
        self.assertIn("cannot search the web", block)

    def test_a_model_with_no_tools_is_never_told_it_has_some(self):
        # Ollama can never research, so asking for it must not produce the
        # sentence that promises web tools.
        block = ai_capability.capability_block(OllamaProvider(), "llama3", True, 0)
        self.assertNotIn("Web research for this request: ON", block)

    def test_the_block_is_small_enough_to_travel_with_every_question(self):
        for provider, model in (
            (OllamaProvider(), "llama3"),
            (GeminiProvider("k"), "gemini-2.5-flash"),
            (OpenRouterProvider("k"), "openai/gpt-4o-mini"),
            (GroqProvider("k"), "groq/compound"),
        ):
            block = ai_capability.capability_block(provider, model, True, 8)
            self.assertLess(len(block), ai_capability.RESERVE_CHARS)


class ResearchPhraseTests(unittest.TestCase):
    def test_a_look_up_question_is_recognised(self):
        for prompt in (
            "research the latest NVDA release",
            "search for the release notes",
            "look up the weather forecast",
            "Google that for me",
            "what is the current price of a Raspberry Pi",
            "check the news about screen readers",
        ):
            self.assertTrue(ai_capability.looks_like_research(prompt), prompt)

    def test_an_ordinary_question_is_not(self):
        for prompt in (
            "what commands does the Terminal have",
            "write me a haiku about keyboards",
            "summarise that in one sentence",
            "how do I write an app for PyOS",
            "",
        ):
            self.assertFalse(ai_capability.looks_like_research(prompt), prompt)


if __name__ == "__main__":
    unittest.main()
