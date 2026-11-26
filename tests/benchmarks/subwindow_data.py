"""Deterministic long-drawer corpus for the sub-window recall benchmark (#390).

Every drawer is filler prose on one or two everyday topics with exactly one
planted fact. The fact sits either in the **head** (inside the first ~60
tokens, which every embedder sees) or in the **tail** (past the default
MiniLM 256-token window, which today's drawer vector never sees).

Each fact has two queries:

* a **paraphrase** query that swaps the fact's content words for synonyms,
  so BM25 gets little help and the vector path has to carry the match;
* a **lexical** control query that reuses the fact's own words, where BM25
  is expected to find the drawer whatever the vector does.

The lexical triple identifies one fact. Individual terms and synonyms may
match competitors, including the ordinary filler prose.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

# (fact word, paraphrase word) pairs. The fact uses the left word, the
# paraphrase query the right one.
SUBJECTS = [
    ("physician", "doctor"),
    ("attorney", "lawyer"),
    ("grandmother", "nana"),
    ("landlord", "property owner"),
    ("neighbour", "person next door"),
    ("plumber", "pipe fitter"),
    ("accountant", "bookkeeper"),
    ("veterinarian", "animal doctor"),
    ("professor", "lecturer"),
    ("mechanic", "car technician"),
    ("dentist", "tooth doctor"),
    ("pharmacist", "chemist"),
]
ACTIONS = [
    ("repaired", "fixed"),
    ("purchased", "bought"),
    ("discarded", "threw away"),
    ("misplaced", "lost"),
    ("borrowed", "took on loan"),
    ("painted", "coloured"),
    ("sold", "got rid of for money"),
    ("donated", "gave away to charity"),
    ("inspected", "examined"),
    ("photographed", "took pictures of"),
]
OBJECTS = [
    ("automobile", "car"),
    ("greenhouse", "glasshouse"),
    ("bicycle", "bike"),
    ("refrigerator", "fridge"),
    ("sofa", "couch"),
    ("wristwatch", "timepiece"),
    ("spectacles", "glasses"),
    ("rowboat", "small boat"),
    ("piano", "keyboard instrument"),
    ("telescope", "stargazing scope"),
    ("motorcycle", "motorbike"),
    ("wardrobe", "closet"),
    ("chandelier", "ceiling light"),
    ("kayak", "canoe"),
]
WHENS = [
    ("on Tuesday", "earlier in the week"),
    ("last autumn", "in the fall"),
    ("after the storm", "following the bad weather"),
    ("before the wedding", "ahead of the marriage ceremony"),
    ("during the holidays", "over the vacation"),
]

# Topic filler is sampled independently of each planted fact.
FILLER_TOPICS = {
    "cooking": [
        "simmered the tomato sauce for an hour",
        "chopped onions and garlic on the wooden board",
        "the bread dough rose slowly near the oven",
        "seasoned the soup with pepper and thyme",
        "tried a new recipe for lemon cake",
        "the rice needed more water than expected",
    ],
    "weather": [
        "the forecast promised sunshine but clouds rolled in",
        "a cold wind blew across the empty field",
        "humidity made the afternoon feel heavy",
        "light drizzle fell for most of the morning",
        "the temperature dropped sharply after sunset",
        "fog covered the valley until noon",
    ],
    "sports": [
        "the team practised passing drills for an hour",
        "the striker missed an easy chance in the second half",
        "we watched the tennis final on a small screen",
        "the marathon route went past the old harbour",
        "the coach changed the formation at half time",
        "the swimmers trained in the outdoor pool",
    ],
    "travel": [
        "the train left the station twenty minutes late",
        "we found a quiet hostel near the market square",
        "the ferry crossing was calm and slow",
        "our flight connected through a busy airport",
        "the guidebook recommended a walk along the river",
        "we bought tickets for the evening bus",
    ],
    "gardening": [
        "the tomatoes ripened along the south fence",
        "weeds crept between the paving stones",
        "the roses needed pruning after flowering",
        "compost was turned with a garden fork",
        "the lawn turned brown during the dry spell",
        "seedlings were moved into larger pots",
    ],
    "music": [
        "the choir rehearsed a new hymn",
        "the guitarist tuned his strings before the set",
        "a jazz trio played in the corner of the cafe",
        "the concert hall was half empty",
        "the album was recorded in a single week",
        "the drummer kept a steady rhythm",
    ],
    "work": [
        "the meeting ran long and covered the budget",
        "emails piled up while the server was down",
        "the quarterly report needed another review",
        "the office printer jammed again",
        "the new intern shadowed the support team",
        "deadlines shifted after the client call",
    ],
    "reading": [
        "the novel opened with a long letter",
        "the library extended its evening hours",
        "the poem used a strict rhyme scheme",
        "the essay argued for slower cities",
        "the biography covered a quiet childhood",
        "the magazine printed a short story",
    ],
}

_CONNECTIVES = ["Then", "Later", "Meanwhile", "After that", "Also", "Somehow", "Anyway"]


@dataclass
class Drawer:
    drawer_id: str
    text: str
    position: str  # "head" | "tail"
    fact: str


@dataclass
class Query:
    query_id: str
    text: str
    target: str
    position: str  # "head" | "tail"
    style: str  # "paraphrase" | "lexical"


@dataclass
class Corpus:
    drawers: list = field(default_factory=list)
    queries: list = field(default_factory=list)


def _filler_sentences(rng: random.Random, topics: list, count: int) -> list:
    out = []
    for _ in range(count):
        topic = rng.choice(topics)
        sentence = rng.choice(FILLER_TOPICS[topic])
        out.append(f"{rng.choice(_CONNECTIVES)} {sentence}.")
    return out


def _fact_slots(rng: random.Random, n: int) -> list:
    # Keep the published small/medium seeds stable. The stress corpus
    # needs more distinct lexical triples than the original slots permit.
    actions = ACTIONS
    if n > len(SUBJECTS) * len(ACTIONS) * len(OBJECTS):
        actions = [*ACTIONS, ("inherited", "received as a bequest"), ("assembled", "put together")]
    combos = [(s, a, o, w) for s in SUBJECTS for a in actions for o in OBJECTS for w in WHENS]
    rng.shuffle(combos)
    # One fact per (subject, action, object) keeps every target distinguishable.
    seen, picked = set(), []
    for s, a, o, w in combos:
        key = (s[0], a[0], o[0])
        if key in seen:
            continue
        seen.add(key)
        picked.append((s, a, o, w))
        if len(picked) == n:
            break
    return picked


def build_corpus(
    n_tail: int = 60,
    n_head: int = 60,
    n_short: int = 60,
    seed: int = 390,
    count_tokens=None,
    tail_min_tokens: int = 300,
) -> Corpus:
    """Build the corpus.

    ``count_tokens`` (a ``str -> int`` callable, the active embedder's real
    tokenizer) places every tail fact past ``tail_min_tokens``; without it a
    sentence count stands in (~12 tokens per filler sentence).
    ``n_short`` short distractor drawers with no planted fact pad the palace.
    """
    rng = random.Random(seed)
    slots = _fact_slots(rng, n_tail + n_head)
    topics = list(FILLER_TOPICS)
    corpus = Corpus()

    def tokens(text: str) -> int:
        if count_tokens is not None:
            return count_tokens(text)
        return len(text.split()) * 4 // 3

    for index, ((s, a, o, w), position) in enumerate(
        zip(slots, ["tail"] * n_tail + ["head"] * n_head)
    ):
        drawer_topics = rng.sample(topics, 2)
        fact = f"My {s[0]} {a[0]} the old {o[0]} {w[0]}."
        if position == "head":
            body = [fact] + _filler_sentences(rng, drawer_topics, rng.randint(30, 45))
        else:
            body = _filler_sentences(rng, drawer_topics, 4)
            while tokens(" ".join(body)) < tail_min_tokens:
                body.extend(_filler_sentences(rng, drawer_topics, 2))
            body.append(fact)
            body.extend(_filler_sentences(rng, drawer_topics, rng.randint(1, 4)))
        drawer_id = f"drawer_{position}_{index:04d}"
        corpus.drawers.append(Drawer(drawer_id, " ".join(body), position, fact))
        corpus.queries.append(
            Query(
                f"q_{drawer_id}_para",
                f"when did my {s[1]} {a[1]} the {o[1]} {w[1]}",
                drawer_id,
                position,
                "paraphrase",
            )
        )
        corpus.queries.append(
            Query(
                f"q_{drawer_id}_lex",
                f"{s[0]} {a[0]} {o[0]}",
                drawer_id,
                position,
                "lexical",
            )
        )
    for index in range(n_short):
        drawer_topics = rng.sample(topics, 1)
        text = " ".join(_filler_sentences(rng, drawer_topics, rng.randint(3, 8)))
        corpus.drawers.append(Drawer(f"drawer_short_{index:04d}", text, "none", ""))
    return corpus


def head_control(mixed: Corpus) -> Corpus:
    """Remove only the planted tail sentences, keeping the head comparison paired."""
    return Corpus(
        drawers=[
            replace(d, text=d.text.replace(d.fact + " ", "", 1), fact="")
            if d.position == "tail"
            else d
            for d in mixed.drawers
        ],
        queries=[q for q in mixed.queries if q.position == "head"],
    )
