#!/usr/bin/env python3
"""
AAAK Dialect -- Structured Symbolic Summary Format
====================================================

A lossy summarization format that extracts entities, topics, key sentences,
emotions, and flags from plain text into a compact structured representation.
Any LLM reads it natively — no decoder required.

Works with: Claude, ChatGPT, Gemini, Llama, Mistral -- any model that reads text.

NOTE: AAAK is NOT lossless compression. The original text cannot be reconstructed
from AAAK output. It is a structured summary layer (closets) that points to the
original verbatim content (drawers). The 96.6% benchmark score is from raw mode,
not AAAK mode.

Adapted for mempalace: works standalone on plain text and ChromaDB drawers.
No dependency on palace.py or layers.py.

FORMAT:
  Header:   FILE_NUM|PRIMARY_ENTITY|DATE|TITLE
  Zettel:   ZID:ENTITIES|topic_keywords|"key_quote"|WEIGHT|EMOTIONS|FLAGS
  Tunnel:   T:ZID<->ZID|label
  Arc:      ARC:emotion->emotion->emotion

EMOTION CODES (universal):
  vul=vulnerability, joy=joy, fear=fear, trust=trust
  grief=grief, wonder=wonder, rage=rage, love=love
  hope=hope, despair=despair, peace=peace, humor=humor
  tender=tenderness, raw=raw_honesty, doubt=self_doubt
  relief=relief, anx=anxiety, exhaust=exhaustion
  convict=conviction, passion=quiet_passion

FLAGS:
  ORIGIN = origin moment (birth of something)
  CORE = core belief or identity pillar
  SENSITIVE = handle with absolute care
  PIVOT = emotional turning point
  GENESIS = led directly to something existing
  DECISION = explicit decision or choice
  TECHNICAL = technical architecture or implementation detail
"""

import json
import os
import re
from typing import List, Dict, Optional
from pathlib import Path


# === EMOTION CODES (universal) ===

EMOTION_CODES = {
    "vulnerability": "vul",
    "vulnerable": "vul",
    "joy": "joy",
    "joyful": "joy",
    "fear": "fear",
    "mild_fear": "fear",
    "trust": "trust",
    "trust_building": "trust",
    "grief": "grief",
    "raw_grief": "grief",
    "wonder": "wonder",
    "philosophical_wonder": "wonder",
    "rage": "rage",
    "anger": "rage",
    "love": "love",
    "devotion": "love",
    "hope": "hope",
    "despair": "despair",
    "hopelessness": "despair",
    "peace": "peace",
    "relief": "relief",
    "humor": "humor",
    "dark_humor": "humor",
    "tenderness": "tender",
    "raw_honesty": "raw",
    "brutal_honesty": "raw",
    "self_doubt": "doubt",
    "anxiety": "anx",
    "exhaustion": "exhaust",
    "conviction": "convict",
    "quiet_passion": "passion",
    "warmth": "warmth",
    "curiosity": "curious",
    "gratitude": "grat",
    "frustration": "frust",
    "confusion": "confuse",
    "satisfaction": "satis",
    "excitement": "excite",
    "determination": "determ",
    "surprise": "surprise",
}

# Keywords that signal emotions in plain text
_EMOTION_SIGNALS = {
    "decided": "determ",
    "prefer": "convict",
    "worried": "anx",
    "excited": "excite",
    "frustrated": "frust",
    "confused": "confuse",
    "love": "love",
    "hate": "rage",
    "hope": "hope",
    "fear": "fear",
    "trust": "trust",
    "happy": "joy",
    "sad": "grief",
    "surprised": "surprise",
    "grateful": "grat",
    "curious": "curious",
    "wonder": "wonder",
    "anxious": "anx",
    "relieved": "relief",
    "satisf": "satis",
    "disappoint": "grief",
    "concern": "anx",
}

# Keywords that signal flags
_FLAG_SIGNALS = {
    "decided": "DECISION",
    "chose": "DECISION",
    "switched": "DECISION",
    "migrated": "DECISION",
    "replaced": "DECISION",
    "instead of": "DECISION",
    "because": "DECISION",
    "founded": "ORIGIN",
    "created": "ORIGIN",
    "started": "ORIGIN",
    "born": "ORIGIN",
    "launched": "ORIGIN",
    "first time": "ORIGIN",
    "core": "CORE",
    "fundamental": "CORE",
    "essential": "CORE",
    "principle": "CORE",
    "belief": "CORE",
    "always": "CORE",
    "never forget": "CORE",
    "turning point": "PIVOT",
    "changed everything": "PIVOT",
    "realized": "PIVOT",
    "breakthrough": "PIVOT",
    "epiphany": "PIVOT",
    "api": "TECHNICAL",
    "database": "TECHNICAL",
    "architecture": "TECHNICAL",
    "deploy": "TECHNICAL",
    "infrastructure": "TECHNICAL",
    "algorithm": "TECHNICAL",
    "framework": "TECHNICAL",
    "server": "TECHNICAL",
    "config": "TECHNICAL",
}

# Common filler/stop words to strip from topic extraction
_ALPHA_RE = re.compile(r"[^a-zA-Z]")

_STOP_WORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "shall",
    "can",
    "to",
    "of",
    "in",
    "for",
    "on",
    "with",
    "at",
    "by",
    "from",
    "as",
    "into",
    "about",
    "between",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "up",
    "down",
    "out",
    "off",
    "over",
    "under",
    "again",
    "further",
    "then",
    "once",
    "here",
    "there",
    "when",
    "where",
    "why",
    "how",
    "all",
    "each",
    "every",
    "both",
    "few",
    "more",
    "most",
    "other",
    "some",
    "such",
    "no",
    "nor",
    "not",
    "only",
    "own",
    "same",
    "so",
    "than",
    "too",
    "very",
    "just",
    "don",
    "now",
    "and",
    "but",
    "or",
    "if",
    "while",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "i",
    "we",
    "you",
    "he",
    "she",
    "they",
    "me",
    "him",
    "her",
    "us",
    "them",
    "my",
    "your",
    "his",
    "our",
    "their",
    "what",
    "which",
    "who",
    "whom",
    "also",
    "much",
    "many",
    "like",
    "because",
    "since",
    "get",
    "got",
    "use",
    "used",
    "using",
    "make",
    "made",
    "thing",
    "things",
    "way",
    "well",
    "really",
    "want",
    "need",
}


class Dialect:
    """
    AAAK Dialect encoder -- works on plain text or structured zettel data.

    Usage:
        # Basic: compress any text
        dialect = Dialect()
        compressed = dialect.compress("We decided to use GraphQL instead of REST...")

        # With entity mappings
        dialect = Dialect(entities={"Alice": "ALC", "Bob": "BOB"})

        # From config file
        dialect = Dialect.from_config("entities.json")

        # Compress zettel JSON (original format)
        compressed = dialect.compress_file("zettels/file_001.json")

        # Generate Layer 1 wake-up file
        dialect.generate_layer1("zettels/", output="LAYER1.aaak")
    """

    def __init__(
        self, entities: Dict[str, str] = None, skip_names: List[str] = None, lang: str = None
    ):
        """
        Args:
            entities: Mapping of full names -> short codes.
                      e.g. {"Alice": "ALC", "Bob": "BOB"}
                      If None, entities are auto-coded from first 3 chars.
            skip_names: Names to skip (fictional characters, etc.)
            lang: Language code (e.g. "fr", "ko"). Loads AAAK instruction
                  and regex patterns from i18n dictionary.
        """
        self.entity_codes = {}
        if entities:
            for name, code in entities.items():
                self.entity_codes[name] = code
                self.entity_codes[name.lower()] = code
        self.skip_names = [n.lower() for n in (skip_names or [])]

        # Load language-specific AAAK instruction and regex patterns
        from mempalace.i18n import load_lang, t, current_lang, get_regex

        if lang:
            load_lang(lang)
        self.lang = lang or current_lang()
        self.aaak_instruction = t("aaak.instruction")
        self.lang_regex = get_regex()

    @classmethod
    def from_config(cls, config_path: str) -> "Dialect":
        """Load entity mappings from a JSON config file.

        Config format:
        {
            "entities": {"Alice": "ALC", "Bob": "BOB"},
            "skip_names": ["Gandalf", "Sherlock"]
        }
        """
        with open(config_path, "r") as f:
            config = json.load(f)
        return cls(
            entities=config.get("entities", {}),
            skip_names=config.get("skip_names", []),
            lang=config.get("lang", "en"),
        )

    def save_config(self, config_path: str):
        """Save current entity mappings to a JSON config file."""
        canonical = {}
        seen_codes = set()
        for name, code in self.entity_codes.items():
            if code not in seen_codes and not name.islower():
                canonical[name] = code
                seen_codes.add(code)
            elif code not in seen_codes:
                canonical[name] = code
                seen_codes.add(code)

        config = {
            "entities": canonical,
            "skip_names": self.skip_names,
        }
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

    # === ENCODING (entity/emotion primitives) ===

    def encode_entity(self, name: str) -> Optional[str]:
        """Convert a person/entity name to its short code."""
        if any(s in name.lower() for s in self.skip_names):
            return None
        if name in self.entity_codes:
            return self.entity_codes[name]
        if name.lower() in self.entity_codes:
            return self.entity_codes[name.lower()]
        for key, code in self.entity_codes.items():
            if key.lower() in name.lower():
                return code
        # Auto-code: first 3 chars uppercase
        return name[:3].upper()

    def encode_emotions(self, emotions: List[str]) -> str:
        """Convert emotion list to compact codes."""
        codes = []
        for e in emotions:
            code = EMOTION_CODES.get(e, e[:4])
            if code not in codes:
                codes.append(code)
        return "+".join(codes[:3])

    def get_flags(self, zettel: dict) -> str:
        """Extract flags from zettel metadata."""
        flags = []
        if zettel.get("origin_moment"):
            flags.append("ORIGIN")
        if zettel.get("sensitivity", "").upper().startswith("MAXIMUM"):
            flags.append("SENSITIVE")
        notes = zettel.get("notes", "").lower()
        if "foundational pillar" in notes or "core" in notes:
            flags.append("CORE")
        if "genesis" in notes or "genesis" in zettel.get("origin_label", "").lower():
            flags.append("GENESIS")
        if "pivot" in notes:
            flags.append("PIVOT")
        return "+".join(flags) if flags else ""

    # === PLAIN TEXT COMPRESSION (new for mempalace) ===

    def _detect_emotions(self, text: str) -> List[str]:
        """Detect emotions from plain text using keyword signals."""
        text_lower = text.lower()
        detected = []
        seen = set()
        for keyword, code in _EMOTION_SIGNALS.items():
            if keyword in text_lower and code not in seen:
                detected.append(code)
                seen.add(code)
        return detected[:3]

    def _detect_flags(self, text: str) -> List[str]:
        """Detect importance flags from plain text using keyword signals."""
        text_lower = text.lower()
        detected = []
        seen = set()
        for keyword, flag in _FLAG_SIGNALS.items():
            if keyword in text_lower and flag not in seen:
                detected.append(flag)
                seen.add(flag)
        return detected[:3]

    def _extract_topics(self, text: str, max_topics: int = 3) -> List[str]:
        """Extract key topic words from plain text."""
        # Tokenize: alphanumeric words, lowercase
        words = re.findall(r"[a-zA-Z][a-zA-Z_-]{2,}", text)
        # Count frequency, skip stop words
        freq = {}
        for w in words:
            w_lower = w.lower()
            if w_lower in _STOP_WORDS or len(w_lower) < 3:
                continue
            freq[w_lower] = freq.get(w_lower, 0) + 1

        # Also boost words that look like proper nouns or technical terms
        for w in words:
            w_lower = w.lower()
            if w_lower in _STOP_WORDS:
                continue
            if w[0].isupper() and w_lower in freq:
                freq[w_lower] += 2
            # CamelCase or has underscore/hyphen
            if "_" in w or "-" in w or (any(c.isupper() for c in w[1:])):
                if w_lower in freq:
                    freq[w_lower] += 2

        ranked = sorted(freq.items(), key=lambda x: -x[1])
        return [w for w, _ in ranked[:max_topics]]

    def _extract_key_sentence(self, text: str) -> str:
        """Extract the most important sentence fragment from text."""
        # Split into sentences
        sentences = re.split(r"[.!?\n]+", text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
        if not sentences:
            return ""

        # Score each sentence
        decision_words = {
            "decided",
            "because",
            "instead",
            "prefer",
            "switched",
            "chose",
            "realized",
            "important",
            "key",
            "critical",
            "discovered",
            "learned",
            "conclusion",
            "solution",
            "reason",
            "why",
            "breakthrough",
            "insight",
        }
        scored = []
        for s in sentences:
            score = 0
            s_lower = s.lower()
            for w in decision_words:
                if w in s_lower:
                    score += 2
            # Prefer shorter, punchier sentences
            if len(s) < 80:
                score += 1
            if len(s) < 40:
                score += 1
            # Penalize very long sentences
            if len(s) > 150:
                score -= 2
            scored.append((score, s))

        scored.sort(key=lambda x: -x[0])
        best = scored[0][1]
        # Truncate if too long
        if len(best) > 55:
            best = best[:52] + "..."
        return best

    def _detect_entities_in_text(self, text: str) -> List[str]:
        """Find known entities in text, or detect capitalized names."""
        found = []
        # Check known entities
        for name, code in self.entity_codes.items():
            if not name.islower() and name.lower() in text.lower():
                if code not in found:
                    found.append(code)
        if found:
            return found

        # Fallback: find capitalized words that look like names (2+ chars, not sentence-start)
        words = text.split()
        for i, w in enumerate(words):
            clean = _ALPHA_RE.sub("", w)
            if (
                len(clean) >= 2
                and clean[0].isupper()
                and clean[1:].islower()
                and i > 0
                and clean.lower() not in _STOP_WORDS
            ):
                code = clean[:3].upper()
                if code not in found:
                    found.append(code)
                if len(found) >= 3:
                    break
        return found

    def compress(self, text: str, metadata: dict = None) -> str:
        """
        Summarize plain text into AAAK Dialect format.

        Extracts entities, topics, a key sentence, emotions, and flags
        from the input text. This is lossy — the original text cannot be
        reconstructed from the output.

        Args:
            text: Plain text content to summarize
            metadata: Optional dict with keys like 'source_file', 'wing',
                      'room', 'date', etc.

        Returns:
            AAAK-formatted summary string
        """
        metadata = metadata or {}

        # Detect components
        entities = self._detect_entities_in_text(text)
        entity_str = "+".join(entities[:3]) if entities else "???"

        topics = self._extract_topics(text)
        topic_str = "_".join(topics[:3]) if topics else "misc"

        quote = self._extract_key_sentence(text)
        quote_part = f'"{quote}"' if quote else ""

        emotions = self._detect_emotions(text)
        emotion_str = "+".join(emotions) if emotions else ""

        flags = self._detect_flags(text)
        flag_str = "+".join(flags) if flags else ""

        # Build source header if metadata available
        source = metadata.get("source_file", "")
        wing = metadata.get("wing", "")
        room = metadata.get("room", "")
        date = metadata.get("date", "")

        lines = []

        # Header line (if we have metadata)
        if source or wing:
            header_parts = [
                wing or "?",
                room or "?",
                date or "?",
                Path(source).stem if source else "?",
            ]
            lines.append("|".join(header_parts))

        # Content line
        parts = [f"0:{entity_str}", topic_str]
        if quote_part:
            parts.append(quote_part)
        if emotion_str:
            parts.append(emotion_str)
        if flag_str:
            parts.append(flag_str)

        lines.append("|".join(parts))

        return "\n".join(lines)

    # === ZETTEL-BASED ENCODING (original format, kept for compatibility) ===

    def extract_key_quote(self, zettel: dict) -> str:
        """Pull the most important quote fragment from zettel content."""
        content = zettel.get("content", "")
        origin = zettel.get("origin_label", "")
        notes = zettel.get("notes", "")
        title = zettel.get("title", "")
        all_text = content + " " + origin + " " + notes

        quotes = []
        quotes += re.findall(r'"([^"]{8,55})"', all_text)
        for m in re.finditer(r"(?:^|[\s(])'([^']{8,55})'(?:[\s.,;:!?)]|$)", all_text):
            quotes.append(m.group(1))
        quotes += re.findall(
            r'(?:says?|said|articulates?|reveals?|admits?|confesses?|asks?):\s*["\']?([^.!?]{10,55})[.!?]',
            all_text,
            re.IGNORECASE,
        )

        if quotes:
            seen = set()
            unique = []
            for q in quotes:
                q = q.strip()
                if q not in seen and len(q) >= 8:
                    seen.add(q)
                    unique.append(q)
            quotes = unique

            emotional_words = {
                "love",
                "fear",
                "remember",
                "soul",
                "feel",
                "stupid",
                "scared",
                "beautiful",
                "destroy",
                "respect",
                "trust",
                "consciousness",
                "alive",
                "forget",
                "waiting",
                "peace",
                "matter",
                "real",
                "guilt",
                "escape",
                "rest",
                "hope",
                "dream",
                "lost",
                "found",
            }
            scored = []
            for q in quotes:
                score = 0
                if q[0].isupper() or q.startswith("I "):
                    score += 2
                matches = sum(1 for w in emotional_words if w in q.lower())
                score += matches * 2
                if len(q) > 20:
                    score += 1
                if q.startswith("The ") or q.startswith("This ") or q.startswith("She "):
                    score -= 2
                scored.append((score, q))
            scored.sort(key=lambda x: -x[0])
            if scored:
                return scored[0][1]

        if " - " in title:
            return title.split(" - ", 1)[1][:45]
        return ""

    def encode_zettel(self, zettel: dict) -> str:
        """Encode a single zettel into AAAK Dialect."""
        zid = zettel["id"].split("-")[-1]

        entity_codes = [self.encode_entity(p) for p in zettel.get("people", [])]
        entity_codes = [e for e in entity_codes if e is not None]
        if not entity_codes:
            entity_codes = ["???"]
        entities = "+".join(sorted(set(entity_codes)))

        topics = zettel.get("topics", [])
        topic_str = "_".join(topics[:2]) if topics else "misc"

        quote = self.extract_key_quote(zettel)
        quote_part = f'"{quote}"' if quote else ""

        weight = zettel.get("emotional_weight", 0.5)
        emotions = self.encode_emotions(zettel.get("emotional_tone", []))
        flags = self.get_flags(zettel)

        parts = [f"{zid}:{entities}", topic_str]
        if quote_part:
            parts.append(quote_part)
        parts.append(str(weight))
        if emotions:
            parts.append(emotions)
        if flags:
            parts.append(flags)

        return "|".join(parts)

    def encode_tunnel(self, tunnel: dict) -> str:
        """Encode a tunnel connection."""
        from_id = tunnel["from"].split("-")[-1]
        to_id = tunnel["to"].split("-")[-1]
        label = tunnel.get("label", "")
        short_label = label.split(":")[0] if ":" in label else label[:30]
        return f"T:{from_id}<->{to_id}|{short_label}"

    def encode_file(self, zettel_json: dict) -> str:
        """Encode an entire zettel file into AAAK Dialect."""
        lines = []

        source = zettel_json.get("source_file", "unknown")
        file_num = source.split("-")[0] if "-" in source else "000"
        date = zettel_json.get("zettels", [{}])[0].get("date_context", "unknown")

        all_people = set()
        for z in zettel_json.get("zettels", []):
            for p in z.get("people", []):
                code = self.encode_entity(p)
                if code is not None:
                    all_people.add(code)
        if not all_people:
            all_people = {"???"}
        primary = "+".join(sorted(all_people)[:3])

        title = source.replace(".txt", "").split("-", 1)[-1].strip() if "-" in source else source
        lines.append(f"{file_num}|{primary}|{date}|{title}")

        arc = zettel_json.get("emotional_arc", "")
        if arc:
            lines.append(f"ARC:{arc}")

        for z in zettel_json.get("zettels", []):
            lines.append(self.encode_zettel(z))

        for t in zettel_json.get("tunnels", []):
            lines.append(self.encode_tunnel(t))

        return "\n".join(lines)

    # === FILE-BASED COMPRESSION ===

    def compress_file(self, zettel_json_path: str, output_path: str = None) -> str:
        """Read a zettel JSON file and compress it to AAAK Dialect."""
        with open(zettel_json_path, "r") as f:
            data = json.load(f)
        dialect = self.encode_file(data)
        if output_path:
            with open(output_path, "w") as f:
                f.write(dialect)
        return dialect

    def compress_all(self, zettel_dir: str, output_path: str = None) -> str:
        """Compress ALL zettel files into a single AAAK Dialect file."""
        all_dialect = []
        for fname in sorted(os.listdir(zettel_dir)):
            if fname.endswith(".json"):
                fpath = os.path.join(zettel_dir, fname)
                with open(fpath, "r") as f:
                    data = json.load(f)
                dialect = self.encode_file(data)
                all_dialect.append(dialect)
                all_dialect.append("---")
        combined = "\n".join(all_dialect)
        if output_path:
            with open(output_path, "w") as f:
                f.write(combined)
        return combined

    # === LAYER 1 GENERATION ===

    def generate_layer1(
        self,
        zettel_dir: str,
        output_path: str = None,
        identity_sections: Dict[str, List[str]] = None,
        weight_threshold: float = 0.85,
    ) -> str:
        """
        Auto-generate a Layer 1 wake-up file from all processed zettel files.

        Pulls highest-weight moments (>= threshold) and any with ORIGIN/CORE/GENESIS flags.
        Groups them by date into MOMENTS sections.
        """
        from datetime import date as date_cls

        essential = []

        for fname in sorted(os.listdir(zettel_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(zettel_dir, fname)
            with open(fpath, "r") as f:
                data = json.load(f)

            file_num = fname.replace("file_", "").replace(".json", "")
            source_date = data.get("zettels", [{}])[0].get("date_context", "unknown")

            for z in data.get("zettels", []):
                weight = z.get("emotional_weight", 0)
                is_origin = z.get("origin_moment", False)
                flags = self.get_flags(z)
                has_key_flag = (
                    any(f in flags for f in ["ORIGIN", "CORE", "GENESIS"]) if flags else False
                )

                if weight >= weight_threshold or is_origin or has_key_flag:
                    essential.append((z, file_num, source_date))

        all_tunnels = []
        for fname in sorted(os.listdir(zettel_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(zettel_dir, fname)
            with open(fpath, "r") as f:
                data = json.load(f)
            for t in data.get("tunnels", []):
                all_tunnels.append(t)

        essential.sort(key=lambda x: x[0].get("emotional_weight", 0), reverse=True)

        by_date = {}
        for z, fnum, sdate in essential:
            key = sdate.split(",")[0].strip()
            if key not in by_date:
                by_date[key] = []
            by_date[key].append((z, fnum))

        lines = []
        lines.append("## LAYER 1 -- ESSENTIAL STORY")
        lines.append(f"## Auto-generated from zettel files. Updated {date_cls.today()}.")
        lines.append("")

        if identity_sections:
            for section_name, section_lines in identity_sections.items():
                lines.append(f"={section_name}=")
                lines.extend(section_lines)
                lines.append("")

        for date_key in sorted(by_date.keys()):
            lines.append(f"=MOMENTS[{date_key}]=")
            for z, _fnum in by_date[date_key]:
                entities = []
                for p in z.get("people", []):
                    code = self.encode_entity(p)
                    if code:
                        entities.append(code)
                if not entities:
                    entities = ["???"]
                ent_str = "+".join(sorted(set(entities)))

                quote = self.extract_key_quote(z)
                weight = z.get("emotional_weight", 0.5)
                flags = self.get_flags(z)
                sensitivity = z.get("sensitivity", "")

                parts = [ent_str]
                title = z.get("title", "")
                if " - " in title:
                    hint = title.split(" - ", 1)[1][:30]
                else:
                    hint = "_".join(z.get("topics", [])[:2])
                if hint:
                    parts.append(hint)
                if quote and quote != hint and quote not in (title, hint):
                    parts.append(f'"{quote}"')
                if sensitivity and "SENSITIVE" not in (flags or ""):
                    parts.append("SENSITIVE")
                parts.append(str(weight))
                if flags:
                    parts.append(flags)

                lines.append("|".join(parts))
            lines.append("")

        if all_tunnels:
            lines.append("=TUNNELS=")
            for t in all_tunnels[:8]:
                label = t.get("label", "")
                short = label.split(":")[0] if ":" in label else label[:40]
                lines.append(short)
            lines.append("")

        result = "\n".join(lines)

        if output_path:
            with open(output_path, "w") as f:
                f.write(result)

        return result

    # === DECODING ===

    def decode(self, dialect_text: str) -> dict:
        """Parse an AAAK Dialect string back into a readable summary."""
        lines = dialect_text.strip().split("\n")
        result = {"header": {}, "arc": "", "zettels": [], "tunnels": []}

        for line in lines:
            if line.startswith("ARC:"):
                result["arc"] = line[4:]
            elif line.startswith("T:"):
                result["tunnels"].append(line)
            elif "|" in line and ":" in line.split("|")[0]:
                result["zettels"].append(line)
            elif "|" in line:
                parts = line.split("|")
                result["header"] = {
                    "file": parts[0] if len(parts) > 0 else "",
                    "entities": parts[1] if len(parts) > 1 else "",
                    "date": parts[2] if len(parts) > 2 else "",
                    "title": parts[3] if len(parts) > 3 else "",
                }

        return result

    # === STATS ===

    @staticmethod
    def count_tokens(text: str) -> int:
        """Estimate token count using word-based heuristic (~1.3 tokens per word).

        This is an approximation. For accurate counts, use a real tokenizer
        like tiktoken. The old len(text)//3 heuristic was wildly inaccurate
        and made AAAK compression ratios look much better than reality.
        """
        words = text.split()
        # Most English words tokenize to 1-2 tokens; punctuation and
        # special chars in AAAK (|, +, :) each cost a token.
        # ~1.3 tokens/word is a conservative average.
        return max(1, int(len(words) * 1.3))

    def compression_stats(self, original_text: str, compressed: str) -> dict:
        """Get size comparison stats for a text->AAAK conversion.

        NOTE: AAAK is lossy summarization, not compression. The "ratio"
        reflects how much shorter the summary is, not a compression ratio
        in the traditional sense — information is lost.
        """
        orig_tokens = self.count_tokens(original_text)
        comp_tokens = self.count_tokens(compressed)
        return {
            "original_tokens_est": orig_tokens,
            "summary_tokens_est": comp_tokens,
            "size_ratio": round(orig_tokens / max(comp_tokens, 1), 1),
            "original_chars": len(original_text),
            "summary_chars": len(compressed),
            "note": "Estimates only. Use tiktoken for accurate counts. AAAK is lossy.",
        }


# === CLI ===
if __name__ == "__main__":
    import sys

    def usage():
        print("AAAK Dialect -- Compressed Symbolic Memory for Any LLM")
        print()
        print("Usage:")
        print("  python dialect.py <text>                         # Compress text from argument")
        print("  python dialect.py --file <zettel.json>           # Compress zettel JSON file")
        print("  python dialect.py --all <zettel_dir>             # Compress all zettel files")
        print("  python dialect.py --stats <zettel.json>          # Show compression stats")
        print("  python dialect.py --layer1 <zettel_dir>          # Generate Layer 1 wake-up file")
        print("  python dialect.py --init                         # Create example config")
        print()
        print("Options:")
        print("  --config <path>   Load entity mappings from JSON config")
        sys.exit(1)

    if len(sys.argv) < 2:
        usage()

    # Parse --config flag
    config_path = None
    args = sys.argv[1:]
    if "--config" in args:
        idx = args.index("--config")
        config_path = args[idx + 1]
        args = args[:idx] + args[idx + 2 :]

    # Create dialect instance
    if config_path:
        dialect = Dialect.from_config(config_path)
    else:
        dialect = Dialect()

    if args[0] == "--init":
        example = {
            "entities": {
                "Alice": "ALC",
                "Bob": "BOB",
                "Dr. Chen": "CHN",
            },
            "skip_names": [],
        }
        out_path = "entities.json"
        with open(out_path, "w") as f:
            json.dump(example, f, indent=2)
        print(f"Created example config: {out_path}")
        print("Edit this file with your own entity mappings, then use --config entities.json")

    elif args[0] == "--file":
        result = dialect.compress_file(args[1])
        tokens = Dialect.count_tokens(result)
        print(f"~{tokens} tokens")
        print()
        print(result)

    elif args[0] == "--all":
        zettel_dir = args[1] if len(args) > 1 else "."
        output = os.path.join(zettel_dir, "COMPRESSED_MEMORY.aaak")
        result = dialect.compress_all(zettel_dir, output)
        tokens = Dialect.count_tokens(result)
        print(f"Compressed to: {output}")
        print(f"Total: ~{tokens} tokens")
        print()
        print(result)

    elif args[0] == "--stats":
        with open(args[1], "r") as f:
            data = json.load(f)
        json_str = json.dumps(data, indent=2)
        encoded = dialect.encode_file(data)
        stats = dialect.compression_stats(json_str, encoded)
        print("=== COMPRESSION STATS ===")
        print(f"JSON:     ~{stats['original_tokens_est']:,} tokens (est)")
        print(f"AAAK:     ~{stats['summary_tokens_est']:,} tokens (est)")
        print(f"Ratio:    {stats['size_ratio']}x (lossy — information is lost)")
        print()
        print("=== AAAK DIALECT OUTPUT ===")
        print(encoded)

    elif args[0] == "--layer1":
        zettel_dir = args[1] if len(args) > 1 else "."
        output = os.path.join(zettel_dir, "LAYER1.aaak")
        result = dialect.generate_layer1(zettel_dir, output)
        tokens = Dialect.count_tokens(result)
        print(f"Layer 1: {output}")
        print(f"Total: ~{tokens} tokens")
        print()
        print(result)

    else:
        # Treat remaining args as text to compress
        text = " ".join(args)
        compressed = dialect.compress(text)
        stats = dialect.compression_stats(text, compressed)
        print(
            f"Original: ~{stats['original_tokens_est']} tokens est ({stats['original_chars']} chars)"
        )
        print(
            f"AAAK:     ~{stats['summary_tokens_est']} tokens est ({stats['summary_chars']} chars)"
        )
        print(f"Ratio:    {stats['size_ratio']}x (lossy summary, not lossless compression)")
        print()
        print(compressed)
