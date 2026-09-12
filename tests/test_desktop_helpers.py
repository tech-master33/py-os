import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop import split_quoted_args


class SplitQuotedArgsTests(unittest.TestCase):
    def test_plain_words(self):
        self.assertEqual(split_quoted_args("copy a.txt b.txt"), ["copy", "a.txt", "b.txt"])

    def test_quoted_path_with_spaces(self):
        result = split_quoted_args('copy "C:\\my folder\\a.txt" out.txt')
        self.assertEqual(result, ["copy", "C:\\my folder\\a.txt", "out.txt"])

    def test_multiple_quoted_segments(self):
        result = split_quoted_args('"first part" "second part" tail')
        self.assertEqual(result, ["first part", "second part", "tail"])

    def test_empty_and_whitespace(self):
        self.assertEqual(split_quoted_args(""), [])
        self.assertEqual(split_quoted_args("   "), [])

    def test_unclosed_quote_keeps_text(self):
        result = split_quoted_args('copy "C:\\my folder')
        self.assertEqual(result, ["copy", "C:\\my folder"])

    def test_collapses_runs_of_spaces(self):
        result = split_quoted_args("a    b")
        self.assertEqual(result, ["a", "b"])

    def test_quoted_spaces_preserved_exactly(self):
        result = split_quoted_args('"two  spaces"')
        self.assertEqual(result, ["two  spaces"])

    def test_windows_path_without_quotes(self):
        result = split_quoted_args(r"C:\Users\name\file.txt")
        self.assertEqual(result, [r"C:\Users\name\file.txt"])


if __name__ == "__main__":
    unittest.main()
