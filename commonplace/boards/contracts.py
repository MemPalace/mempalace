"""Path contracts: the repo paths a card declares it will edit.

A card's contract is a short list of glob patterns (``src/api/**``, ``docs/*.md``,
``src/*.{ts,tsx}``). Two cards whose contracts can match the same file must not run at
the same time; the start guards (``commonplace.orch.runner_bridge.contract_refusal``)
use ``first_overlap`` for that. A card without a contract claims nothing and is never
refused by this check.

Matching is deliberately conservative: when two patterns *might* match one path they
overlap. A false overlap only serialises two cards; a false "disjoint" is the collision
this check exists to prevent. So:

- globs are stored canonical: empty and ``.`` segments are dropped, ``name/..``
  collapses, the repo root (``.``) is ``**``, and ``{a,b}`` alternatives are expanded
  into separate globs (``MAX_STORED`` in all). A ``..`` that would leave the repo, an
  unbalanced brace group and an over-large expansion are refused at store time;
- a stored value that is not canonical anyway (written outside ``normalize``) is read
  defensively: an unreadable value, an unbalanced group, an over-cap expansion and a
  ``..`` that cannot collapse all count as ``**``, so they overlap everything;
- names compare case-insensitively, because a checkout on a case-insensitive file system
  maps ``Docs/a.md`` and ``docs/a.md`` to one file;
- a wildcard segment is matched by ``_match``, which means what ``fnmatch.fnmatchcase``
  means (classes included) but does not backtrack the way a regex does, so its work has a
  bound that can be computed before it runs (``_match_steps``: at most one pass over the
  pattern per character of the name, plus one);
- one start spends a budget of ``MAX_STEPS`` work units, set from timings on one host
  (see docs/orchestrator.md for the measured figures; a slower host takes proportionally
  longer). It bills every running card ``CARD_UNITS`` for its handling plus its stored size
  (``size_units``) before that card's contract is parsed or expanded, each wildcard segment
  whose matcher it has to build by what building costs (``_compile_units``: a base, plus
  per character and per ``[``), and each comparison as pairs times the shallower side's
  deepest glob times the dearest segment comparison (``_segment_units``: equal literals by
  length, a pattern against a name by the matcher's bound). Beyond the budget it fails
  closed (``TooLarge``) instead of holding the runner lock.
"""

from __future__ import annotations

import bisect
import functools
import json

from commonplace.extensions import ToolError

MAX_GLOBS = 32  # globs in one update
MAX_EXPANSIONS = 64  # alternatives one glob may expand to
MAX_STORED = 64  # globs stored per card, after expansion
MAX_GLOB_LENGTH = 200
MAX_STEPS = 2_200_000  # work units one start may spend (the measured time is in the docs)
SEGMENT_CHARS = 32  # characters that add one work unit to a segment
# Building a wildcard segment's matcher is billed by what it costs: a base, plus per
# character and per `[` (measured on one host, at most about 0.2 us per unit).
COMPILE_UNITS = 10
COMPILE_CHAR_UNITS = 1
COMPILE_CLASS_UNITS = 8
# Matching is billed by the matcher's bound (``_match_steps``): MATCH_STEPS steps make one
# unit (measured worst case about 0.3 us per unit), and testing a character against a
# ``[...]`` class counts CLASS_STEPS steps more than a plain character.
MATCH_STEPS = 6
CLASS_STEPS = 8
GLOB_UNITS = 10  # work units to read, expand and canonicalise one stored glob
CARD_UNITS = 16  # work units billed for handling one running card (parse, prepare, loop)
MAX_MATCHERS = 8192  # wildcard segment matchers kept
_WILDCARDS = frozenset("*?[{}")
ANY = ("**",)


class TooLarge(Exception):
    """A start would compare more work units than ``MAX_STEPS``."""


class Budget:
    """Work units one start may still compare."""

    def __init__(self, steps: int = MAX_STEPS):
        self.left = steps

    def take(self, steps: int) -> None:
        self.left -= steps
        if self.left < 0:
            raise TooLarge


def _canonical(glob: str, *, strict: bool) -> list[str]:
    """Segments of ``glob`` without empty and ``.`` parts, ``name/..`` collapsed. A
    ``..`` that has no literal segment to cancel (it would leave the repo, or follows a
    wildcard) is refused when ``strict``, and otherwise widened to ``**``."""
    out: list[str] = []
    for segment in glob.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if out and not _WILDCARDS & set(out[-1]):
                out.pop()
                continue
            if strict:
                raise ToolError(
                    "no: a path glob must stay inside the repo (no leading or wildcard ..)"
                )
            return ["**"]
        out.append(segment)
    return out


def _expand(glob: str, *, strict: bool) -> list[str]:
    """``{a,b}`` alternatives of ``glob`` (nested groups included). An unbalanced group,
    a group without a comma, a stray brace or more than ``MAX_EXPANSIONS`` alternatives is
    refused when ``strict``, and otherwise stands for ``**`` (it matches anything)."""
    pending, done = [glob], []
    while pending:
        current = pending.pop()
        start = current.find("{")
        if start < 0:
            done.append(current)
            continue
        depth, commas, end = 0, [], -1
        for i in range(start, len(current)):
            char = current[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            elif char == "," and depth == 1:
                commas.append(i)
        if end < 0 or not commas:
            return _unexpandable("unbalanced or comma-less brace group", strict)
        bounds = [start, *commas, end]
        head, tail = current[:start], current[end + 1 :]
        options = [current[a + 1 : b] for a, b in zip(bounds, bounds[1:])]
        if strict and any(option.startswith("/") for option in options):
            raise ToolError("no: a path glob is relative to the repo root (no / or \\)")
        pending.extend(reversed([head + option + tail for option in options]))
        if len(done) + len(pending) > MAX_EXPANSIONS:
            return _unexpandable(f"more than {MAX_EXPANSIONS} brace alternatives", strict)
    if any("}" in alternative for alternative in done):
        return _unexpandable("stray closing brace", strict)
    return done


def _unexpandable(reason: str, strict: bool) -> list[str]:
    if strict:
        raise ToolError(f"no: path glob braces are not usable ({reason})")
    return ["**"]


def normalize(value) -> list[str]:
    """Validated contract: a list of relative, canonical, brace-free, forward-slash globs,
    de-duplicated in order. An empty list clears the contract. Raises ``ToolError``
    otherwise."""
    if not isinstance(value, list) or len(value) > MAX_GLOBS:
        raise ToolError(f"no: paths must be a list of at most {MAX_GLOBS} globs")
    globs: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ToolError("no: paths must be a list of strings")
        glob = item.strip()
        if not glob or len(glob) > MAX_GLOB_LENGTH or any(ord(c) < 32 for c in glob):
            raise ToolError(f"no: a path glob must be 1-{MAX_GLOB_LENGTH} printable characters")
        if glob.startswith("/") or "\\" in glob:
            raise ToolError("no: a path glob is relative to the repo root (no / or \\)")
        for alternative in _expand(glob, strict=True):
            if alternative.startswith("/"):  # an empty group option can leave one in front
                raise ToolError("no: a path glob is relative to the repo root (no / or \\)")
            canonical = "/".join(_canonical(alternative, strict=True)) or "**"  # "." is the root
            if canonical not in globs:
                globs.append(canonical)
    if len(globs) > MAX_STORED:
        raise ToolError(f"no: paths expand to more than {MAX_STORED} globs")
    return globs


def column_update(args) -> dict:
    """``{"paths": <stored JSON>}`` when ``args`` carries a contract, else nothing."""
    return {"paths": json.dumps(normalize(args["paths"]))} if "paths" in args else {}


@functools.lru_cache(maxsize=8192)
def _alternatives(glob: str):
    """``(alternative, casefolded canonical segments)`` for each way ``glob`` can read, and
    the sizes that price comparing them: the longest segment, the longest literal segment (a
    name a pattern may be matched against), and the heaviest wildcard segment with a ``*``
    and without one (``_pattern_weight``)."""
    alternatives, longest, names, starred, plain = [], 0, 0, 0, 0
    for alternative in _expand(glob, strict=False):
        texts = [s.casefold() for s in _canonical(alternative, strict=False)]
        wild = [not _WILDCARDS.isdisjoint(text) for text in texts]
        alternatives.append((alternative, tuple(zip(texts, wild))))
        top = max(map(len, texts), default=0)
        longest = max(longest, top)
        if not any(wild):
            names = max(names, top)
            continue
        for text, is_wild in zip(texts, wild):
            if not is_wild:
                names = max(names, len(text))
            elif "*" not in text:
                plain = max(plain, _pattern_weight(text))
            elif text != "**":
                starred = max(starred, _pattern_weight(text))
    return tuple(alternatives), (longest, names, starred, plain)


_STAR = object()  # a run of `*` in a token list


def _tokens(pattern: str) -> tuple:
    """``pattern`` read the way ``fnmatch.translate`` reads it: ``_STAR`` for a run of ``*``,
    ``None`` for ``?``, a one-character string for a literal, and the one-character test of
    ``_class`` for a ``[...]`` class (an unclosed ``[`` is a literal)."""
    out: list = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        i += 1
        if char == "*":
            if not out or out[-1] is not _STAR:
                out.append(_STAR)
        elif char == "?":
            out.append(None)
        elif char == "[":
            j = i
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append("[")
            else:
                out.append(_class(pattern, i, j))
                i = j + 1
        else:
            out.append(char)
    return tuple(out)


def _class(pattern: str, i: int, j: int):
    """The one-character test of the class ``pattern[i:j]`` (between ``[`` and ``]``), with
    the meaning ``fnmatch`` gives it: ``translate``'s range clean-up, followed by how the
    regex parser reads the class it writes, without building a regex (compiling a class
    with a wide range walks every character in it, a cost the budget could not see)."""
    if "-" not in pattern[i:j]:
        chunks = [pattern[i:j]]
    else:  # translate's split at range hyphens; a range written backwards is dropped
        chunks = []
        k = i + 2 if pattern[i] == "!" else i + 1
        while (k := pattern.find("-", k, j)) >= 0:
            chunks.append(pattern[i:k])
            i, k = k + 1, k + 3
        if pattern[i:j]:
            chunks.append(pattern[i:j])
        else:
            chunks[-1] += "-"
        for k in range(len(chunks) - 1, 0, -1):
            if chunks[k - 1][-1] > chunks[k][0]:
                chunks[k - 1] = chunks[k - 1][:-1] + chunks[k][1:]
                del chunks[k]
    stuff = "-".join(chunks)
    if not stuff:
        return lambda char: False
    if stuff == "!":
        return lambda char: True
    negate = stuff[0] == "!"
    if negate:
        chunks[0] = chunks[0][1:]
    items: list = []  # characters, and _STAR for a hyphen that joins two chunks
    for k, chunk in enumerate(chunks):
        if k:
            items.append(_STAR)
        items.extend(chunk)
    singles, ranges, k = set(), [], 0
    while k < len(items):  # the regex parser: `x-y` is a range, any other `-` is literal
        low = "-" if items[k] is _STAR else items[k]
        k += 1
        if k + 1 < len(items) and items[k] is _STAR:
            high = "-" if items[k + 1] is _STAR else items[k + 1]
            ranges.append((low, high))
            k += 2
        elif k < len(items) and items[k] is _STAR:  # a hyphen at the end is literal
            singles.update((low, "-"))
            k += 1
        else:
            singles.add(low)
    singles = frozenset(singles)
    if not ranges:
        return lambda char: (char in singles) != negate
    lows, highs = [], []  # the ranges merged, in order, so one bisect finds the candidate
    for low, high in sorted(ranges):
        if highs and low <= highs[-1]:
            highs[-1] = max(highs[-1], high)
        else:
            lows.append(low)
            highs.append(high)

    def test(char: str) -> bool:
        k = bisect.bisect_right(lows, char)
        return (char in singles or (k > 0 and char <= highs[k - 1])) != negate

    return test


def _match(tokens: tuple, name: str) -> bool:
    """Whether all of ``name`` matches ``tokens``, as ``fnmatch.fnmatchcase`` decides. Every
    token but ``_STAR`` takes one character; on a mismatch the last star takes one character
    more and the tokens after it are tried again. So there are at most ``len(name) + 1``
    passes over the tokens and the work is bounded by ``_match_steps`` (no regex
    backtracking, whose cost the budget could not bound)."""
    m, n = len(tokens), len(name)
    p = s = mark = 0
    star = -1
    while s < n:
        if p < m:
            token = tokens[p]
            if token is _STAR:
                star, mark = p, s
                p += 1
                continue
            char = name[s]
            if token is None or token == char or (token.__class__ is not str and token(char)):
                p += 1
                s += 1
                continue
        if star < 0:
            return False
        mark += 1
        s, p = mark, star + 1
    while p < m and tokens[p] is _STAR:
        p += 1
    return p == m


def _pattern_weight(pattern: str) -> int:
    """An upper bound on what one pass of ``_match`` over the tokens of ``pattern`` costs:
    one step per character, ``CLASS_STEPS`` more per ``[`` (a class test is a call), and one
    for the pass itself."""
    return 1 + len(pattern) + CLASS_STEPS * pattern.count("[")


def _match_steps(weight: int, name_length: int, star: bool = True) -> int:
    """An upper bound on the steps of ``_match`` for a pattern of ``weight``
    (``_pattern_weight``) against a name of ``name_length`` characters: at most
    ``name_length + 1`` passes over the tokens and a last run over trailing stars, or one
    pass when the pattern has no ``*`` (nothing to go back to)."""
    return (name_length + 2) * weight if star else weight + 1


def _match_units(weight: int, name_length: int, star: bool = True) -> int:
    """Work units of that match (``_match_steps``), or nothing when there is no pattern."""
    return 1 + _match_steps(weight, name_length, star) // MATCH_STEPS if weight else 0


_MATCHERS: dict = {}  # wildcard segment -> matcher; bounded, see _matcher


def _matcher(pattern: str):
    """The matcher of a wildcard segment, ``name -> bool``. Building it (``_tokens``) is
    billed by ``_compile_charge`` before it happens."""
    found = _MATCHERS.get(pattern)
    if found is None:
        if len(_MATCHERS) >= MAX_MATCHERS:
            _MATCHERS.clear()
        found = _MATCHERS[pattern] = functools.partial(_match, _tokens(pattern))
    return found


def _overlap(left, right, matchers=None) -> bool:
    """``matchers`` is the call-local table of a budgeted start (every wildcard segment in
    it was billed when its matcher was built); without it the shared cache is used."""
    lookup = _matcher if matchers is None else matchers.__getitem__
    for (a, a_wild), (b, b_wild) in zip(left, right):
        if a == "**" or b == "**":
            return True
        if a == b:
            continue
        if a_wild and b_wild:
            continue  # two patterns may share a name; assume they do
        if a_wild:
            if lookup(a)(b):
                continue
        elif b_wild:
            if lookup(b)(a):
                continue
        return False
    return True


def globs_overlap(a: str, b: str) -> bool:
    """True when some path could match both globs. ``**`` matches any depth, a glob that
    ends where the other goes deeper owns that directory (``docs`` vs ``docs/a.md``), and
    the repo root overlaps everything."""
    return any(_overlap(x, y) for _, x in _alternatives(a)[0] for _, y in _alternatives(b)[0])


def size_units(stored) -> int:
    """Work units to read a stored contract (JSON text or a list of globs): one, plus
    ``GLOB_UNITS`` per glob, one per ``/`` and one per ``SEGMENT_CHARS`` characters. A start
    bills this for every running card before it parses or expands that card's contract."""
    text = stored if isinstance(stored, str) else json.dumps(stored)
    globs = text.count('"') // 2
    return 1 + GLOB_UNITS * globs + text.count("/") + len(text) // SEGMENT_CHARS


def _compile_units(text: str) -> int:
    """Work units to build the matcher of one wildcard segment: a base, plus per character,
    plus per ``[`` (each class is parsed into its one-character test)."""
    return COMPILE_UNITS + COMPILE_CHAR_UNITS * len(text) + COMPILE_CLASS_UNITS * text.count("[")


def _prepare(globs):
    """``(alternative, segments)`` pairs of ``globs``, the deepest glob, and the sizes that
    price a comparison (see ``_alternatives``), all computed once per contract."""
    pairs, sizes = [], (0, 0, 0, 0)
    for glob in globs or ():
        alternatives, more = _alternatives(glob)
        pairs.extend(alternatives)
        sizes = tuple(map(max, sizes, more))
    depth = max((len(x) for _, x in pairs), default=0)
    return pairs, depth, sizes


def _segment_units(left, right) -> int:
    """Work units of comparing one segment of ``left`` with one of ``right`` (the sizes
    from ``_prepare``) at worst: equal literals compare by length, and a pattern of one side
    is matched against a name of the other (``_match_units``)."""
    (a_long, a_names, a_starred, a_plain), (b_long, b_names, b_starred, b_plain) = left, right
    return max(
        1 + max(a_long, b_long) // SEGMENT_CHARS,
        _match_units(a_starred, b_names),
        _match_units(b_starred, a_names),
        _match_units(max(a_plain, b_plain), 0, star=False),
    )


def _compile_charge(pairs, budget: Budget, matchers: dict) -> None:
    """Bill, and build, the matcher of every wildcard segment this call has not got yet. A
    segment is billed whenever it has to be compiled, a recompile after the shared cache was
    cleared included; the call-local ``matchers`` table keeps what was built, so the
    comparisons never compile (and never clear the shared cache) unbilled."""
    for _, segments in pairs:
        for text, wild in segments:
            if wild and text != "**" and text not in matchers:
                if text not in _MATCHERS:
                    budget.take(_compile_units(text))
                matchers[text] = _matcher(text)


def _first(left, right, budget: Budget | None, matchers: dict | None = None):
    (a_pairs, a_depth, a_sizes), (b_pairs, b_depth, b_sizes) = left, right
    if not a_pairs or not b_pairs:
        return None
    if budget is not None:
        _compile_charge(b_pairs, budget, matchers)
        depth = max(min(a_depth, b_depth), 1)
        budget.take(len(a_pairs) * len(b_pairs) * depth * _segment_units(a_sizes, b_sizes))
    for a, x in a_pairs:
        for b, y in b_pairs:
            if _overlap(x, y, matchers):
                return a, b
    return None


def first_overlap(mine, theirs, budget: Budget | None = None) -> tuple[str, str] | None:
    """The first ``(my glob, their glob)`` pair that can match the same path, or None.
    Either side empty means no contract, so no overlap. With a ``budget`` the work is
    charged first and ``TooLarge`` is raised when it exceeds the budget: each wildcard
    segment whose matcher is not built yet (``_compile_units``), then the pairs times the
    shallower side's deepest glob times the dearest segment comparison
    (``_segment_units``)."""
    left = _prepare(mine)
    matchers = {} if budget is not None else None
    if budget is not None:
        _compile_charge(left[0], budget, matchers)
    return _first(left, _prepare(theirs), budget, matchers)


def first_overlap_running(mine, running, budget: Budget):
    """Check ``mine`` against the stored contracts of the running cards, ``(key, stored)``
    each. Every card is billed ``CARD_UNITS`` for its own handling and its stored size
    (``size_units``) before its contract is parsed or expanded, then its matchers and
    comparisons. Returns ``(key, globs, pair)`` for the first overlap (an unreadable stored
    contract reads as ``**``) or None; raises ``TooLarge`` with the ``key`` of the card the
    budget ran out on."""
    budget.take(size_units(mine))
    left = _prepare(mine)
    matchers: dict = {}
    _compile_charge(left[0], budget, matchers)
    for key, stored in running:
        try:
            budget.take(CARD_UNITS + size_units(stored))
            globs = parse(stored)
            if globs is None:
                globs = list(ANY)
            found = _first(left, _prepare(globs), budget, matchers)
        except TooLarge as exc:
            exc.key = key
            raise
        if found is not None:
            return key, globs, found
    return None


def parse(raw) -> list[str] | None:
    """Stored column value (JSON text or NULL) to a glob list. Only NULL and ``[]`` are no
    contract; anything that is not a JSON list of strings, an empty string included, is
    unreadable (``None``), and callers treat that as ``**`` rather than as no contract."""
    if raw is None:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(value, list) or not all(isinstance(g, str) for g in value):
        return None
    return value
