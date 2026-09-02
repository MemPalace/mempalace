"""
USEL Encoder — NSM-grounded alternative to AAAK compression.

Encodes natural language memories into USEL symbolic notation using
the 63 NSM (Natural Semantic Metalanguage) semantic primes as foundation.

AAAK:  E1|John|colleague|MEETING|2024-03-15|discussed_project_alpha
USEL:  [SOMEONE:John]+[SAY]+[BEFORE+NOW]

Benefits over AAAK:
  - Semantic portability: NSM primes are universal (validated across 300+ languages)
  - Cross-session consistency: [SOMEONE:John] always means the same thing (no E1/E2 codes)
  - Human-readable: Users can decode USEL notation without a lookup table
  - AI-grounded: NSM primes map to stable directions in LLM embedding space

Note: Compression comparisons use the real Dialect.compress() from dialect.py
      for fair benchmarking.

References:
  - Wierzbicka, A. (1996). Semantics: Primes and Universals
  - Goddard, C. & Wierzbicka, A. (2014). Words and Meanings
  - USEL Project: https://github.com/kitfoxs/usel-lang
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# The 63 NSM Semantic Primes (Wierzbicka & Goddard)
NSM_PRIMES = {
    # Substantives
    "I", "YOU", "SOMEONE", "SOMETHING", "PEOPLE", "BODY",
    # Relational
    "KIND", "PART",
    # Determiners
    "THIS", "THE_SAME", "OTHER",
    # Quantifiers
    "ONE", "TWO", "SOME", "ALL", "MUCH",
    # Evaluators
    "GOOD", "BAD",
    # Descriptors
    "BIG", "SMALL",
    # Mental
    "THINK", "KNOW", "WANT", "DONT_WANT", "FEEL", "SEE", "HEAR",
    # Speech
    "SAY", "WORDS", "TRUE",
    # Actions
    "DO", "HAPPEN", "MOVE",
    # Existence
    "THERE_IS", "BE", "HAVE",
    # Life
    "LIVE", "DIE",
    # Time
    "WHEN", "NOW", "BEFORE", "AFTER", "A_LONG_TIME", "A_SHORT_TIME",
    "FOR_SOME_TIME", "MOMENT",
    # Space
    "WHERE", "HERE", "ABOVE", "BELOW", "FAR", "NEAR", "SIDE",
    "INSIDE", "TOUCH",
    # Logical
    "NOT", "MAYBE", "CAN", "BECAUSE", "IF",
    # Intensifier
    "VERY", "MORE",
    # Similarity
    "LIKE",
}

# Common English function words that should NOT be mapped to primes.
# These are too frequent and ambiguous to carry semantic signal.
_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "about", "between",
    "through", "during", "up", "down", "out", "off", "over", "under",
    "again", "further", "then", "once", "so", "it", "its", "itself",
    "that", "than", "or", "and", "but", "if", "nor", "yet",
}

# Capitalized words that are NOT named entities.
_NON_ENTITY_WORDS = {
    # Days of the week
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
    "Saturday", "Sunday",
    # Months
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
    # Common sentence-start words and determiners
    "The", "This", "That", "These", "Those", "Here", "There",
    "When", "Where", "What", "Which", "Who", "How", "Why",
    "But", "And", "Also", "However", "Although", "Because",
    "After", "Before", "During", "While", "Since", "Until",
    "Some", "Many", "Most", "Each", "Every", "Any", "All",
    "Not", "Never", "Always", "Often", "Sometimes",
    "Very", "Really", "Just", "Still", "Already", "Only",
}

# Keyword → NSM prime mapping for English.
# Only content words with clear semantic mapping are included.
# Function words / stopwords are excluded to avoid noise.
KEYWORD_MAP: dict[str, str] = {
    # People & entities
    "me": "I", "my": "I", "myself": "I",
    "you": "YOU", "your": "YOU", "yourself": "YOU",
    "someone": "SOMEONE", "somebody": "SOMEONE", "person": "SOMEONE",
    "people": "PEOPLE", "everyone": "PEOPLE", "team": "PEOPLE",
    "something": "SOMETHING", "thing": "SOMETHING",
    "body": "BODY", "physical": "BODY",
    # Evaluators
    "good": "GOOD", "great": "GOOD", "excellent": "GOOD", "nice": "GOOD",
    "positive": "GOOD", "happy": "GOOD", "love": "GOOD",
    "bad": "BAD", "terrible": "BAD", "awful": "BAD", "negative": "BAD",
    "wrong": "BAD", "hate": "BAD", "dislike": "BAD", "problem": "BAD",
    # Descriptors
    "big": "BIG", "large": "BIG", "huge": "BIG", "major": "BIG",
    "important": "BIG",
    "small": "SMALL", "little": "SMALL", "tiny": "SMALL", "minor": "SMALL",
    # Mental
    "think": "THINK", "thought": "THINK", "consider": "THINK",
    "believe": "THINK",
    "know": "KNOW", "knew": "KNOW", "understand": "KNOW", "aware": "KNOW",
    "want": "WANT", "need": "WANT", "desire": "WANT", "wish": "WANT",
    "feel": "FEEL", "felt": "FEEL", "emotion": "FEEL", "feeling": "FEEL",
    "see": "SEE", "saw": "SEE", "look": "SEE", "watch": "SEE",
    "observe": "SEE",
    "hear": "HEAR", "heard": "HEAR", "listen": "HEAR",
    # Speech
    "say": "SAY", "said": "SAY", "tell": "SAY", "told": "SAY", "talk": "SAY",
    "discuss": "SAY", "discussed": "SAY", "mention": "SAY", "speak": "SAY",
    "asked": "SAY",
    "words": "WORDS", "language": "WORDS", "message": "WORDS",
    "true": "TRUE", "truth": "TRUE", "correct": "TRUE",
    # Actions
    "make": "DO", "made": "DO",
    "create": "DO", "build": "DO", "work": "DO", "action": "DO",
    "happen": "HAPPEN", "happened": "HAPPEN", "occurred": "HAPPEN",
    "event": "HAPPEN",
    "move": "MOVE", "moved": "MOVE", "went": "MOVE", "come": "MOVE",
    # Existence
    "exist": "THERE_IS", "exists": "THERE_IS",
    "own": "HAVE",
    # Life
    "live": "LIVE", "alive": "LIVE", "life": "LIVE",
    "die": "DIE", "dead": "DIE", "death": "DIE",
    # Time
    "now": "NOW", "currently": "NOW", "today": "NOW",
    "before": "BEFORE", "previously": "BEFORE", "earlier": "BEFORE",
    "ago": "BEFORE", "yesterday": "BEFORE", "past": "BEFORE",
    "after": "AFTER", "later": "AFTER", "next": "AFTER",
    "tomorrow": "AFTER", "future": "AFTER", "soon": "AFTER",
    "time": "WHEN", "moment": "MOMENT",
    # Space
    "place": "WHERE", "location": "WHERE",
    "above": "ABOVE", "top": "ABOVE",
    "below": "BELOW", "bottom": "BELOW",
    "far": "FAR", "distant": "FAR", "away": "FAR", "remote": "FAR",
    "near": "NEAR", "nearby": "NEAR",
    "inside": "INSIDE", "within": "INSIDE",
    # Logical
    "not": "NOT", "never": "NOT", "without": "NOT",
    "maybe": "MAYBE", "perhaps": "MAYBE", "possibly": "MAYBE",
    "able": "CAN", "possible": "CAN",
    "because": "BECAUSE", "reason": "BECAUSE", "cause": "BECAUSE",
    "whether": "IF", "condition": "IF",
    # Intensifier
    "very": "VERY", "really": "VERY", "extremely": "VERY",
    "more": "MORE", "most": "MORE", "better": "MORE",
    "additional": "MORE",
    # Quantifiers
    "one": "ONE", "single": "ONE",
    "two": "TWO", "both": "TWO", "pair": "TWO",
    "some": "SOME", "few": "SOME", "several": "SOME",
    "all": "ALL", "every": "ALL", "everything": "ALL", "entire": "ALL",
    "many": "MUCH", "much": "MUCH",
}


@dataclass
class USELToken:
    """A single USEL token: a prime + optional entity qualifier."""
    prime: str
    qualifier: Optional[str] = None

    def __str__(self) -> str:
        if self.qualifier:
            return f"[{self.prime}:{self.qualifier}]"
        return f"[{self.prime}]"


@dataclass
class USELEncoding:
    """Complete USEL encoding of a memory."""
    tokens: list[USELToken] = field(default_factory=list)
    entities: dict[str, str] = field(default_factory=dict)
    original: str = ""

    def __str__(self) -> str:
        return "+".join(str(t) for t in self.tokens)

    @property
    def compression_ratio(self) -> float:
        """Ratio of original length to encoded length. Returns 0.0 for empty."""
        encoded = str(self)
        if not encoded:
            return 0.0
        return len(self.original) / len(encoded)


class USELEncoder:
    """
    Encodes natural language memories into USEL symbolic notation
    using the 65 NSM semantic primes as the foundation.

    Usage:
        encoder = USELEncoder()
        result = encoder.encode("John discussed project alpha in yesterday's meeting")
        print(result)  # [SOMEONE:John]+[SAY]+[BEFORE]

    Integration:
        This module is standalone — it can be used alongside AAAK as an
        alternative encoding backend. To integrate into the CLI, add a
        ``--mode usel`` flag to the compress command. See the README for
        the intended integration path.
    """

    def __init__(self) -> None:
        self._keyword_map = KEYWORD_MAP.copy()
        self._stop_words = _STOP_WORDS.copy()
        self._non_entity_words = _NON_ENTITY_WORDS.copy()

    def encode(self, memory: str) -> USELEncoding:
        """Encode a natural language memory into USEL notation.

        Tokens are emitted in document order — entities and keywords are
        interleaved based on their position in the source text, preserving
        agent/patient distinctions (e.g. "John told Alice" differs from
        "Alice told John").
        """
        encoding = USELEncoding(original=memory)

        # Split into sentences for first-word detection
        sentences = re.split(r'(?<=[.!?])\s+', memory)
        first_words_of_sentences: set[str] = set()
        for sentence in sentences:
            words_in_sentence = sentence.split()
            if words_in_sentence:
                # Extract just the alphabetic part
                match = re.match(r'[A-Za-z]+', words_in_sentence[0])
                if match:
                    first_words_of_sentences.add(match.group())

        # Tokenize into word positions from the original text
        word_positions = list(re.finditer(r"[a-zA-Z']+|[0-9]+", memory))

        seen_primes: set[str] = set()
        seen_entities: set[str] = set()

        for match in word_positions:
            raw_word = match.group()
            lower_word = raw_word.lower()

            # Skip stopwords entirely
            if lower_word in self._stop_words:
                continue

            # Check if this is a capitalized word (potential entity)
            is_capitalized = raw_word[0:1].isupper() and len(raw_word) > 1

            if is_capitalized:
                # Skip known non-entity words
                if raw_word in self._non_entity_words:
                    continue
                # Skip first words of sentences only if they're common
                # non-entity words (don't skip proper names like "John")
                if (raw_word in first_words_of_sentences
                        and raw_word in self._non_entity_words):
                    continue
                # Skip if it maps to a known keyword
                if lower_word in self._keyword_map:
                    prime = self._keyword_map[lower_word]
                    if prime not in seen_primes:
                        encoding.tokens.append(USELToken(prime))
                        seen_primes.add(prime)
                    continue
                # It's a named entity
                if raw_word not in seen_entities:
                    encoding.entities[raw_word] = "SOMEONE"
                    encoding.tokens.append(USELToken("SOMEONE", raw_word))
                    seen_entities.add(raw_word)
            elif lower_word in self._keyword_map:
                prime = self._keyword_map[lower_word]
                if prime not in seen_primes:
                    encoding.tokens.append(USELToken(prime))
                    seen_primes.add(prime)
            elif raw_word.isdigit():
                encoding.tokens.append(USELToken("SOMETHING", raw_word))

        return encoding

    def decode(self, usel_notation: str) -> str:
        """Decode USEL notation back to approximate natural language."""
        reverse_map: dict[str, str] = {}
        for word, prime in self._keyword_map.items():
            if prime not in reverse_map:
                reverse_map[prime] = word

        parts: list[str] = []
        tokens = re.findall(r"\[([^\]]+)\]", usel_notation)
        for token in tokens:
            if ":" in token:
                prime, qualifier = token.split(":", 1)
                parts.append(qualifier)
            elif "+" in token:
                sub_primes = token.split("+")
                for sp in sub_primes:
                    sp = sp.strip()
                    if sp in reverse_map:
                        parts.append(reverse_map[sp])
            elif token in reverse_map:
                parts.append(reverse_map[token])
            else:
                parts.append(token.lower())

        return " ".join(parts)

    def encode_aaak(self, memory: str) -> str:
        """Encode using real AAAK Dialect for fair comparison."""
        from mempalace.dialect import Dialect
        dialect = Dialect()
        return dialect.compress(memory)

    def compare_compression(self, memory: str) -> dict[str, int | float]:
        """Compare compression ratio: AAAK vs USEL."""
        usel = self.encode(memory)
        aaak = self.encode_aaak(memory)
        usel_str = str(usel)
        return {
            "original_chars": len(memory),
            "usel_chars": len(usel_str),
            "aaak_chars": len(aaak),
            "usel_ratio": len(memory) / max(len(usel_str), 1),
            "aaak_ratio": len(memory) / max(len(aaak), 1),
            "usel_compression_pct": (1 - len(usel_str) / len(memory)) * 100,
            "aaak_compression_pct": (1 - len(aaak) / len(memory)) * 100,
        }

    def validate(self, notation: str) -> tuple[bool, list[str]]:
        """Validate USEL notation format and structure."""
        errors: list[str] = []

        # Check structural format: [X]+[Y]+... with balanced brackets
        stripped = notation.replace(" ", "")
        if not re.fullmatch(r"(\[[^\[\]]+\])(\+\[[^\[\]]+\])*", stripped):
            errors.append(
                "Invalid USEL structure: expected [X]+[Y]+... format"
            )

        tokens = re.findall(r"\[([^\[\]]+)\]", notation)
        if not tokens:
            errors.append("No valid USEL tokens found")
            return False, errors

        for token in tokens:
            # Check for empty qualifiers like [SOMEONE:]
            if token.endswith(":"):
                errors.append(f"Empty qualifier in token: [{token}]")
                continue
            prime = token.split(":")[0].split("+")[0].strip()
            if not prime:
                errors.append(f"Empty prime in token: [{token}]")
            elif prime not in NSM_PRIMES and not prime[0:1].isdigit():
                errors.append(f"Unknown prime: {prime}")

        return len(errors) == 0, errors
