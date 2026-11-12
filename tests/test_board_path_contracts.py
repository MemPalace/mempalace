"""Glob overlap rules of card path contracts (commonplace.boards.contracts)."""

import json

import pytest

from commonplace.boards import contracts
from commonplace.extensions import ToolError


@pytest.mark.parametrize(
    "a,b",
    [
        ("src/api/**", "src/api/users.py"),
        ("src/api/users.py", "src/api/users.py"),
        ("src", "src/api/users.py"),  # a directory owns what is below it
        ("src/*.py", "src/a.py"),
        ("**", "anything/at/all.rs"),
        ("src/**/test_*.py", "src/a/b/c.py"),
        ("src/a*", "src/*b"),  # both wild: may share a name
        ("*.md", "*.md"),
    ],
)
def test_overlapping_globs(a, b):
    assert contracts.globs_overlap(a, b) and contracts.globs_overlap(b, a)


@pytest.mark.parametrize(
    "a,b",
    [
        ("src/api/**", "src/web/**"),
        ("src/api/users.py", "src/api/teams.py"),
        ("docs/*.md", "src/*.md"),
        ("src/*.py", "src/a/b.py"),
        ("src/a.py", "src/a"),
    ],
)
def test_disjoint_globs(a, b):
    assert not contracts.globs_overlap(a, b) and not contracts.globs_overlap(b, a)


def test_first_overlap_and_empty_contracts():
    assert contracts.first_overlap(["a/**", "b/x"], ["c/y", "b/**"]) == ("b/x", "b/**")
    assert contracts.first_overlap(["a/**"], ["b/**"]) is None
    assert contracts.first_overlap([], ["b/**"]) is None
    assert contracts.first_overlap(["a/**"], None) is None


def test_parse_is_forgiving():
    assert contracts.parse(None) == [] and contracts.parse("[]") == []
    assert contracts.parse('["a", "b"]') == ["a", "b"]


@pytest.mark.parametrize("raw", ["", "not json", '{"a": 1}', '["a", 3, "b"]', "7", '"src"', "null"])
def test_parse_marks_an_unreadable_value_instead_of_dropping_it(raw):
    assert contracts.parse(raw) is None


@pytest.mark.parametrize(
    "a,b",
    [
        ("src/app/*.{ts,tsx}", "src/app/page.tsx"),
        ("src/{api,web}/**", "src/api/users.py"),
        ("src/{api,web/{a,b}}/x.py", "src/web/b/x.py"),
        # Braces that cannot be expanded stand for "**", whatever follows them.
        ("src/{a/x.py", "docs/readme.md"),
        ("src/{a}/x.py", "docs/readme.md"),
        ("a}b/x", "zzz/y"),
        ("{p/q,r}/zzz/w{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}", "r/zzz/wacegikm"),
        (".", "any/file.py"),
        ("./", "any/file.py"),
        ("src//api/**", "src/api/users.py"),
        ("src/./api/**", "src/api/users.py"),
        ("src/web/../api/**", "src/api/users.py"),
        ("Docs/**", "docs/a.md"),
        ("Src/api/users.py", "src/API/Users.py"),
    ],
)
def test_overlap_after_canonicalising_expanding_and_casefolding(a, b):
    assert contracts.globs_overlap(a, b) and contracts.globs_overlap(b, a)


@pytest.mark.parametrize(
    "a,b",
    [
        ("src/app/*.{ts,tsx}", "src/app/page.py"),
        ("src/{api,web}/**", "src/db/users.py"),
        ("src/web/../api/**", "src/web/users.py"),
        ("{a,b}{c,d}", "ae"),
    ],
)
def test_disjoint_after_expanding(a, b):
    assert not contracts.globs_overlap(a, b) and not contracts.globs_overlap(b, a)


def test_too_many_alternatives_stay_conservative():
    many = "{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}"  # 128 expansions
    assert contracts.globs_overlap(many, "zzz")


def test_normalize_canonicalises_and_refuses_escapes():
    assert contracts.normalize(["./a//b/", ".", "a/b/../c", "x/{p,q}/*.py", "a//b"]) == [
        "a/b",
        "**",
        "a/c",
        "x/p/*.py",
        "x/q/*.py",
    ]
    assert contracts.normalize(["{./a,b}", "{x/../y,z}/*"]) == ["a", "b", "y/*", "z/*"]
    for bad in (["../x"], ["a/../.."], ["*/../x"], ["/a"], ["a\\b"]):
        with pytest.raises(ToolError):
            contracts.normalize(bad)


@pytest.mark.parametrize(
    "bad",
    [
        ["{../x,y}"],
        ["x/{a,../../b}"],
        ["{/a,b}"],
        ["{x,/etc}"],
        ["x/{/a,b}"],
        ["src/{/etc,api}"],
        ["x{/a,b}"],
        ["x/{,/a}"],
        ["{a,{/b,c}}"],
        ["{src,}/etc"],
        ["{,}/etc"],
        ["{,}/a"],
        ["src/{a"],
        ["src/{a}/x"],
        ["src/a}"],
        ["{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}"],  # 128 alternatives
        [f"d{i}/{{a,b,c}}" for i in range(22)],  # 66 stored globs
    ],
)
def test_normalize_refuses_unusable_braces_and_oversized_contracts(bad):
    with pytest.raises(ToolError):
        contracts.normalize(bad)


def test_comparison_budget_counts_segment_steps():
    mine = [f"a{i}/*.py" for i in range(10)]  # 2 segments deep
    theirs = [f"b{i}/*.py" for i in range(10)]
    # One matcher to build (`*.py`), then 100 pairs x 2 segments x the dearest segment
    # comparison: `*.py` (weight 1 + 4) against a 2-character name, 1 + 4 * 5 // 6 = 4 units.
    cost = contracts._compile_units("*.py") + 100 * 2 * 4
    assert contracts.first_overlap(mine, theirs, contracts.Budget(cost)) is None
    contracts._MATCHERS.clear()
    with pytest.raises(contracts.TooLarge):
        contracts.first_overlap(mine, theirs, contracts.Budget(cost - 1))
    deep = "/".join(["?"] * 20)
    with pytest.raises(contracts.TooLarge):  # the same 100 pairs, 21 segments deep
        contracts.first_overlap(
            [f"{deep}/m{i}" for i in range(10)],
            [f"{deep}/t{i}" for i in range(10)],
            contracts.Budget(200),
        )


def test_permitted_deep_wildcard_contracts_exhaust_the_budget_in_a_few_cards():
    """What ``normalize`` accepts at most, 64 globs of 89 segments, costs 4 096 x 89 work
    units per running card (a one-character `?` is one pass, one unit), so the budget is
    gone after a fixed number of them. (The time that allows was measured separately; see
    the docs.)"""
    deep = "/".join(["?"] * 88)
    mine = contracts.normalize([f"{deep}/{{m{i},n{i}}}" for i in range(32)])
    theirs = contracts.normalize([f"{deep}/{{t{i},u{i}}}" for i in range(32)])
    assert len(mine) == len(theirs) == 64
    budget, cards = contracts.Budget(), 0
    with pytest.raises(contracts.TooLarge):
        while True:
            assert contracts.first_overlap(mine, theirs, budget) is None
            cards += 1
    assert cards == contracts.MAX_STEPS // (64 * 64 * 89) == 6


def test_long_single_segment_contracts_are_charged_by_length():
    """A segment's cost grows with its length: 64 globs of one 172-character `?` pattern
    are matched against names of up to 173 characters in one pass (no `*`), which costs
    1 + 174 // 6 = 30 units per pair, not 1, so far fewer running cards fit the budget than
    the segment count alone would allow."""
    mine = contracts.normalize(["?" * 170 + f"{{m{i},n{i}}}" for i in range(32)])
    theirs = contracts.normalize(["x" * 170 + f"{{t{i},u{i}}}" for i in range(32)])
    assert len(mine) == len(theirs) == 64
    weight = 30
    budget, cards = contracts.Budget(), 0
    with pytest.raises(contracts.TooLarge):
        while True:
            assert contracts.first_overlap(mine, theirs, budget) is None
            cards += 1
    compiles = sum(contracts._compile_units(text) for text in {g.split("/")[0] for g in mine})
    assert cards == (contracts.MAX_STEPS - compiles) // (64 * 64 * weight)
    assert cards < contracts.MAX_STEPS // (64 * 64) // 4  # charging segments only: 537


def _stored(globs):
    return json.dumps(contracts.normalize(globs))


def test_long_literal_segments_are_charged_by_length():
    """Reviewer shape E: equal literals compare by length, 1 unit plus 1 per 32
    characters: 64 x 64 pairs of one 173-character literal segment cost 6 units each."""
    mine = contracts.normalize(["x" * 170 + f"{{m{i},n{i}}}" for i in range(32)])
    theirs = contracts.normalize(["x" * 170 + f"{{t{i},u{i}}}" for i in range(32)])
    cost = 64 * 64 * (1 + 173 // 32)
    assert contracts.first_overlap(mine, theirs, contracts.Budget(cost)) is None
    with pytest.raises(contracts.TooLarge):
        contracts.first_overlap(mine, theirs, contracts.Budget(cost - 1))


def test_many_large_running_cards_are_charged_before_they_are_parsed():
    """Reviewer shape L: a cheap new card against running cards of 64 stored globs of 89
    segments. Each running card costs its stored size, so the budget runs out after a few
    hundred of them; charging comparisons alone allowed about nineteen thousand."""
    running = [
        (k, json.dumps([("x/" * 88) + f"y{k:04d}{h:02d}" for h in range(64)])) for k in range(1000)
    ]
    budget = contracts.Budget()
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(["src/a.py"], running, budget)
    assert 300 < info.value.key < 500
    assert budget.left < 0
    assert contracts.size_units(running[0][1]) > 6000  # the stored size, billed before parsing


def test_unreadable_and_empty_running_contracts_are_billed_and_read_as_expected():
    budget = contracts.Budget()
    assert (
        contracts.first_overlap_running(["src/a.py"], [(1, "[]"), (2, '["docs/**"]')], budget)
        is None
    )
    assert 0 < contracts.MAX_STEPS - budget.left < 100
    found = contracts.first_overlap_running(["src/a.py"], [(1, "not json")], contracts.Budget())
    assert found == (1, ["**"], ("src/a.py", "**"))


def test_tiny_globs_are_billed_per_glob():
    """Reviewer shape T1: 64 short literal globs per running card. Each glob costs real work
    to read, expand and canonicalise, so about 3 000 such cards use up the budget; billing
    one unit per glob let the loop go on for about 16 000."""
    running = [(k, json.dumps([f"c{k}_{j}x" for j in range(64)])) for k in range(6000)]
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(["src/a.py"], running, contracts.Budget())
    assert 2500 < info.value.key < 3500


def test_long_distinct_wildcard_globs_are_billed_by_length():
    """Reviewer shape T2: 64 distinct 185-character wildcard globs per running card (the
    validator accepts them). Building each one's matcher is billed by its length, so the
    budget runs out after under two hundred such cards; a flat per-pattern charge let it go
    on for about 240."""
    running = [
        (
            k,
            _stored(
                [("?" * 179) + "{" + f"{k:03d}{j:02d}a,{k:03d}{j:02d}b" + "}" for j in range(32)]
            ),
        )
        for k in range(240)
    ]
    contracts._MATCHERS.clear()
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(["src/a.py"], running, contracts.Budget())
    assert info.value.key < 200


def test_compile_charge_follows_length_and_classes():
    plain = contracts._compile_units("?" * 20)
    assert contracts._compile_units("?" * 185) > 3 * plain
    assert contracts._compile_units("[a-z]" * 35) > 2 * contracts._compile_units("?" * 175)


def test_many_one_glob_running_cards_are_billed_per_card():
    """Reviewer shape S1: one literal glob per running card. Handling a card (parse, prepare,
    loop) has a cost of its own, so about 78 000 of them use up the budget; billing only the
    stored size let a start go on for about 220 000."""
    running = [(k, json.dumps([f"d{k}/x.py"])) for k in range(120_000)]
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(["src/a.py"], running, contracts.Budget())
    assert 60_000 < info.value.key < 100_000


def test_every_compile_is_billed_even_after_the_shared_cache_is_cleared(monkeypatch):
    """With a tiny shared cache the start's 64 wildcard segments overflow it. The start keeps
    the matchers it built, so it never recompiles one it has billed for or clears the cache
    in the middle of the comparisons: compiles run == compiles billed."""
    monkeypatch.setattr(contracts, "MAX_MATCHERS", 16)
    contracts._MATCHERS.clear()
    compiled, billed = [], []
    real_tokens, real_units = contracts._tokens, contracts._compile_units
    monkeypatch.setattr(
        contracts, "_tokens", lambda pattern: (compiled.append(pattern), real_tokens(pattern))[1]
    )
    monkeypatch.setattr(
        contracts, "_compile_units", lambda text: (billed.append(text), real_units(text))[1]
    )
    mine = [f"*a{i}z" for i in range(64)]
    running = [(k, json.dumps([f"b{k}"])) for k in range(50)]
    assert contracts.first_overlap_running(mine, running, contracts.Budget(10**9)) is None
    assert len(compiled) == len(billed) == 64


def test_the_new_cards_own_stored_size_is_billed_first():
    mine = [f"src/{i}/*.py" for i in range(8)]
    cost = contracts.size_units(mine)
    contracts.first_overlap_running(mine, [], contracts.Budget(cost + 100))
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(mine, [], contracts.Budget(cost - 1))
    assert not hasattr(info.value, "key")  # it ran out before any running card


def test_distinct_wildcard_patterns_are_charged_for_compiling():
    """Reviewer shape Q2: 64 globs of 8 segments, each segment a distinct wildcard pattern,
    against a literal glob. Every pattern is compiled once and billed by its length, so the
    budget is gone after a few dozen running cards and only a bounded number was compiled."""

    def digits(n):
        out = []
        for _ in range(13):
            n, r = divmod(n, 3)
            out.append("x?*"[r])
        return "".join(reversed(out)) + "x" * 8

    def card(k):
        inputs = []
        for i in range(32):
            base = k * 32 + i
            prefix = [digits(base * 7 + j) for j in range(7)]
            group = "{" + digits(10**6 + base * 2) + "," + digits(10**6 + base * 2 + 1) + "}"
            inputs.append("/".join(prefix) + "/" + group)
        return _stored(inputs)

    contracts._MATCHERS.clear()
    running = [(k, card(k)) for k in range(100)]
    mine = ["/".join(["x" * 20] * 7 + ["y" * 20])]
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(mine, running, contracts.Budget())
    assert info.value.key < 100
    # What was compiled, priced the way it is billed, fits the budget (a start never
    # compiles more than it paid for).
    assert sum(contracts._compile_units(p) for p in contracts._MATCHERS) <= contracts.MAX_STEPS
    # A pattern that is already compiled is not billed again.
    first = contracts.Budget()
    contracts.first_overlap(["*.py"], ["a.py"], first)
    again = contracts.Budget()
    contracts.first_overlap(["*.py"], ["a.py"], again)
    assert contracts._compile_units("*.py") == contracts.COMPILE_UNITS + 4
    assert again.left - first.left == contracts._compile_units("*.py")


def _random_pattern(rng, *, class_chars="abcmxz-!^][\\&~|0\u00e9\u0100AZ"):
    out = []
    for _ in range(rng.randint(0, 6)):
        if rng.random() < 0.3:
            body = "".join(rng.choice(class_chars) for _ in range(rng.randint(0, 7)))
            out.append("[" + body + rng.choice("]]]x"))  # sometimes left open
        else:
            out.append(rng.choice("ab-!^]*?[\\&~|zZ.\n\u00e9\u0100{}"))
    return "".join(out)


def test_the_matcher_agrees_with_fnmatch():
    """``_match`` replaces the regex ``fnmatch`` builds; on random patterns (classes with
    ranges, negation, stray brackets and hyphens included) and names it decides the same."""
    import fnmatch
    import random
    import re
    import warnings

    rng = random.Random(389)
    names = "abcmxz-!^]\\&~|zZ.\n\u00e9\u0100[*?{}0AZ\u2000"
    edges = ["[a-]", "[!a-]", "[a-c]", "[!a-c]", "[a-cb-e]", "[a-eb-c]", "[]-a]", "[z-a]", "[!z-a]"]
    edges += ["[a-c-e]", "[--/]", "[!]a]", "[^a]", "[\\-a]", "[a-\u0100]", "[&&~|]", "[[]", "[a"]
    edges += ["[b-a!]", "[a--!]"]  # a backwards range dropped can leave `!`: any character
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)  # the regex parser warns on `[[`
        for pattern in edges:
            regex = re.compile(fnmatch.translate(pattern))
            tokens = contracts._tokens(pattern)
            for char in names + "dey/\u00ff\uffff":
                assert contracts._match(tokens, char) == (regex.match(char) is not None), (
                    pattern,
                    char,
                )
        for _ in range(20_000):
            pattern = _random_pattern(rng)
            regex = re.compile(fnmatch.translate(pattern))
            tokens = contracts._tokens(pattern)
            for _ in range(3):
                name = "".join(rng.choice(names) for _ in range(rng.randint(0, 8)))
                assert contracts._match(tokens, name) == (regex.match(name) is not None), (
                    pattern,
                    name,
                )


def test_the_matcher_stays_within_its_billed_bound():
    """The matcher's loop runs at most (len(name) + 1) x (tokens + 1) times, the bound
    ``_match_steps`` bills, on the shapes that make a regex backtrack (a leading star, a
    long fixed tail, a mismatch at the end) and on random ones. Counted, not timed."""
    import inspect
    import random
    import sys

    source, first = inspect.getsourcelines(contracts._match)
    loop = first + next(i for i, line in enumerate(source) if "while s < n:" in line)
    code = contracts._match.__code__

    def iterations(pattern, name):
        count = 0

        def trace(frame, event, arg):
            nonlocal count
            if frame.f_code is not code:
                return None
            if event == "line" and frame.f_lineno == loop:
                count += 1
            return trace

        sys.settrace(trace)
        try:
            contracts._match(contracts._tokens(pattern), name)
        finally:
            sys.settrace(None)
        return count

    rng = random.Random(9)
    shapes = [("*" + "?" * k + "ab", "0" * 199) for k in (1, 5, 28, 98, 196)]
    shapes += [("*a" * 20 + "b", "a" * 199), ("*" + "[0-9]" * 30 + "z", "0" * 199)]
    shapes += [
        (_random_pattern(rng), "".join(rng.choice("ab-]z") for _ in range(40))) for _ in range(300)
    ]
    for pattern, name in shapes:
        tokens = contracts._tokens(pattern)
        assert iterations(pattern, name) <= (len(name) + 1) * (len(tokens) + 1)
        assert len(tokens) < contracts._pattern_weight(pattern)


def test_a_trailing_star_comparison_is_billed_by_pattern_and_name_length():
    """A depth-1 comparison was billed one unit whatever its length; it is billed by the
    matcher's bound, which grows with the pattern times the name."""
    short = contracts.Budget()
    contracts.first_overlap(["*" + "?" * 28 + "ab"], ["0" * 31], short)
    long = contracts.Budget()
    contracts.first_overlap(["*" + "?" * 196 + "ab"], ["0" * 199], long)
    spent_short = contracts.MAX_STEPS - short.left
    spent_long = contracts.MAX_STEPS - long.left
    # (31 + 2) x 32 and (199 + 2) x 200 matcher steps, 6 to a unit.
    assert spent_short >= 33 * 32 // 6
    assert spent_long >= 201 * 200 // 6 > 30 * spent_short


def _trailing_star(length, index):
    """One depth-1 pattern: a leading star, `?` for the rest, two distinct letters at the
    end (never matches the all-digit names of the running cards)."""
    return "*" + "?" * (length - 3) + chr(97 + index // 26) + chr(97 + index % 26)


def _digits(length, key):
    return "0" * (length - 6) + f"{key:06d}"


def test_short_trailing_star_globs_against_one_name_per_card_refuse_early():
    """Reviewer shape P31: 32 trailing-star globs of 31 characters against one 31-character
    name per running card. Billed one unit per comparison, a start went on to card 37 841
    (1.5 s on the measuring host); billed by the matcher's bound it stops before card 1000."""
    mine = contracts.normalize([_trailing_star(31, i) for i in range(32)])
    running = [(k, _stored([_digits(31, k)])) for k in range(40_000)]
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(mine, running, contracts.Budget())
    assert info.value.key < 1000


def test_long_trailing_star_globs_against_twenty_cards_refuse():
    """Reviewer shape P199x20: 64 trailing-star globs of 196 characters against 20 running
    cards of 64 literal 196-character globs. The start used 28% of the budget and 1.7 s;
    priced by the matcher's bound it is refused on the first card."""
    mine = contracts.normalize(["*" + "?" * 190 + f"{i:02d}" + "{a,b}" for i in range(32)])
    running = [
        (k, _stored(["0" * 190 + f"{k:03d}{j:02d}" + "{x,y}" for j in range(32)]))
        for k in range(20)
    ]
    assert len(mine) == 64 and len(json.loads(running[0][1])) == 64
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(mine, running, contracts.Budget())
    assert info.value.key < 2


def test_longest_trailing_star_globs_against_64_names_per_card_refuse_at_once():
    """Reviewer shape L199x64: 64 trailing-star globs of 199 characters against running
    cards of 64 literal 199-character names. Refused at card 73 after 6 s before; one card
    of it is already beyond the budget."""
    mine = [_trailing_star(199, i) for i in range(64)]
    running = [(k, json.dumps([_digits(199, k * 64 + j) for j in range(64)])) for k in range(200)]
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running(mine, running, contracts.Budget())
    assert info.value.key < 2


def test_trailing_star_globs_on_running_cards_are_billed_too():
    """The same shape the other way round: the running cards hold the trailing-star
    patterns and the new card holds a long name, so their patterns are matched against it."""
    running = [
        (k, _stored(["*" + "?" * 189 + f"{k:03d}{j:02d}" + "{a,b}" for j in range(32)]))
        for k in range(20)
    ]
    with pytest.raises(contracts.TooLarge) as info:
        contracts.first_overlap_running([_digits(199, 7)], running, contracts.Budget())
    assert info.value.key < 8
    # A class test is a call, billed above a plain character.
    assert contracts._pattern_weight("[a]") > contracts._pattern_weight("???") + 4


def test_wide_character_classes_build_no_regex(monkeypatch):
    """A regex for `[<low>-U+FFFF]` walks every character of the range when it is
    compiled (milliseconds per class, billed as microseconds). The matcher reads a class
    itself, so no regex is built, and the class still means what it means to fnmatch."""
    import re

    def no_regex(*args, **kwargs):
        raise AssertionError("a contract comparison compiled a regex")

    contracts._MATCHERS.clear()
    monkeypatch.setattr(re, "compile", no_regex)
    running = [
        (k, json.dumps([f"[{chr(0x100 + j)}-\uffff]{k:05d}" for j in range(64)])) for k in range(5)
    ]
    assert contracts.first_overlap_running(["src/a.py"], running, contracts.Budget()) is None
    assert contracts.globs_overlap("[\u0100-\uffff]00003", "\u2000" + "00003")
    assert not contracts.globs_overlap("[\u0100-\uffff]00003", "z00003")
    assert contracts.globs_overlap("[!a-c]x", "dx") and not contracts.globs_overlap("[!a-c]x", "bx")


def test_live_cards_needs_an_explicit_hold():
    """The cap keeps its hour and the contract check does not: no default may pick one."""
    import inspect

    from commonplace.orch import runner_bridge

    hold = inspect.signature(runner_bridge._live_cards).parameters["hold"]
    assert hold.default is inspect.Parameter.empty and hold.kind is hold.KEYWORD_ONLY
    # The worst glob lists a start could once have compared: 32 globs of 64 alternatives.
    alternatives = "{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}"
    wide = [f"s{i}/{alternatives}/x" for i in range(32)]
    with pytest.raises(contracts.TooLarge):
        contracts.first_overlap(wide, wide, contracts.Budget())
