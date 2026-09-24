"""Splitting a typed command line into arguments, quotes respected.

Recovery Console commands like COPY and Terminal commands like open take
paths, and a path often contains spaces. Splitting on whitespace alone
breaks ``open My File.txt`` into three arguments, so every command that
takes a path splits through :func:`split_quoted_args` instead.

This module is deliberately tiny and free of imports so that both
:mod:`desktop` and :mod:`kernel` can use it: desktop imports kernel, so
kernel cannot import anything from desktop in return.
"""


def split_quoted_args(text):
    r"""Split a command line on whitespace, respecting double quotes.

    Lets commands accept paths containing spaces:
    COPY "C:\my folder\a.txt" out.txt
    """
    args = []
    current = []
    in_quotes = False
    for char in text:
        if char == '"':
            in_quotes = not in_quotes
        elif char == " " and not in_quotes:
            if current:
                args.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        args.append("".join(current))
    return args
