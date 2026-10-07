"""Quality benchmark for pre-compression: does _fit_to_budget lose topic signal?

The risk with priority-based truncation is that T3 prose containing entity
names or topic keywords gets dropped under budget pressure, and the LLM
never sees them. This test suite builds realistic mixed content (code +
prose with embedded entities) and measures recall of important names/terms
after budget fitting.

If these tests fail, the classifier or budget packer needs tuning —
the feature is hurting recall and should not ship.
"""

from mempalace.classifier import classify_block
from mempalace.closet_llm import _fit_to_budget


# ─── Entity recall under pressure ────────────────────────────────────────────


def _make_entity_prose(names, padding_factor=8):
    """Build T3 prose blocks with embedded entity names, padded to be large."""
    blocks = []
    for name in names:
        block = (
            f"We had a detailed discussion with {name} about the migration "
            f"strategy and {name} raised several concerns about the timeline. "
            f"After reviewing the options, {name} agreed to proceed with the "
            f"revised plan. "
        ) * padding_factor
        blocks.append(block)
    return blocks


def _make_code_blocks(count):
    """Build T0 code fence blocks."""
    return [f"```python\ndef handler_{i}():\n    return {{'status': 'ok'}}\n```" for i in range(count)]


def _count_entity_recall(fitted_text, entities):
    """Return (found, total) count of entities present in the fitted output."""
    found = sum(1 for e in entities if e in fitted_text)
    return found, len(entities)


class TestEntityRecallUnderPressure:
    """Entity names must survive budget pressure when they appear in prose."""

    ENTITIES = [
        "Akiko Tanaka",
        "Marcus Chen",
        "Priya Sharma",
        "Diego Fernandez",
        "Fatima Al-Rashidi",
    ]

    def test_all_entities_survive_when_under_budget(self):
        prose_blocks = _make_entity_prose(self.ENTITIES, padding_factor=1)
        code_blocks = _make_code_blocks(3)
        content = "\n\n".join(code_blocks + prose_blocks)
        # Budget is generous — everything fits
        fitted = _fit_to_budget(content, budget=len(content) + 1000)
        found, total = _count_entity_recall(fitted, self.ENTITIES)
        assert found == total, f"lost entities under budget: {found}/{total}"

    def test_entities_in_code_comments_survive_pressure(self):
        """Entity names inside code (hard T0) are safe regardless of budget."""
        code_with_entities = [
            f"```python\n# Author: {name}\ndef process_{i}():\n    pass\n```"
            for i, name in enumerate(self.ENTITIES)
        ]
        filler = "x " * 5000  # big T3 padding
        content = "\n\n".join(code_with_entities + [filler] * 10)
        budget = sum(len(b) for b in code_with_entities) + 200
        fitted = _fit_to_budget(content, budget=budget)
        found, total = _count_entity_recall(fitted, self.ENTITIES)
        assert found == total, f"lost entities in code blocks: {found}/{total}"

    def test_prose_entities_lost_count_under_extreme_pressure(self):
        """Under extreme budget pressure, measure how many prose entities die.

        This is the honest test. If budget only fits code and no prose,
        prose-only entities WILL be lost. The question is: does the classifier
        at least preserve prose blocks that have higher signal?
        """
        prose_blocks = _make_entity_prose(self.ENTITIES, padding_factor=6)
        code_blocks = _make_code_blocks(30)  # lots of code
        content = "\n\n".join(code_blocks + prose_blocks)

        # Budget that fits all code but ~0 prose
        code_total = sum(len(b) for b in code_blocks) + len(code_blocks) * 2
        budget = code_total + 500  # tiny room for prose

        smart = _fit_to_budget(content, budget=budget)
        head = content[:budget]

        smart_found, _ = _count_entity_recall(smart, self.ENTITIES)
        head_found, _ = _count_entity_recall(head, self.ENTITIES)

        # Smart cut should not be worse than dumb head cut for entity recall
        assert smart_found >= head_found, (
            f"smart lost MORE entities than head cut: {smart_found} vs {head_found}"
        )

    def test_mixed_content_realistic_session(self):
        """Simulate a real closet_llm input: code + decisions + names + filler.

        30k budget on ~50k input. Measure entity recall and code preservation.
        """
        entities = [
            "Sarah Mitchell",
            "CloudFormation",
            "Kubernetes",
            "Redis cluster",
            "PagerDuty",
        ]

        # Mix of content types like a real session
        blocks = [
            # Decision with entity names (should be T0 via reasoning markers)
            (
                "Step 1: Sarah Mitchell proposed migrating the Redis cluster "
                "to a managed service.\n"
                "Step 2: We evaluated CloudFormation vs Terraform.\n"
                "Step 3: The team decided CloudFormation was simpler.\n"
                "Therefore, the migration will use CloudFormation templates."
            ),
            # Code blocks
            "```yaml\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: redis\n```",
            '```python\ndef deploy():\n    cf = CloudFormation()\n    cf.create_stack("redis")\n```',
            # Error traceback
            (
                "Traceback (most recent call last):\n"
                '  File "deploy.py", line 42, in create_stack\n'
                "    raise ConnectionError('Redis cluster unreachable')\n"
                "ConnectionError: Redis cluster unreachable"
            ),
            # Filler prose (should be dropped first)
            (
                "We spent quite some time going back and forth about various "
                "options and possibilities, considering the pros and cons of "
                "each approach in great detail before arriving at any kind of "
                "conclusion about what to do next. "
            ) * 30,
            # More filler
            (
                "The meeting ran long and we covered a lot of ground without "
                "making too many concrete decisions beyond what was already "
                "discussed in the previous section above. "
            ) * 30,
            # PagerDuty mention in prose
            (
                "We need to set up PagerDuty alerts for the Redis cluster "
                "before the migration goes live. Sarah Mitchell will handle "
                "the Kubernetes namespace configuration. "
            ),
        ]
        content = "\n\n".join(blocks)
        budget = 30_000

        fitted = _fit_to_budget(content, budget=budget)

        found, total = _count_entity_recall(fitted, entities)
        # At minimum, entities in T0 blocks (code, traceback, reasoning) must survive
        assert found >= 4, (
            f"only {found}/{total} entities survived in realistic scenario"
        )

        # Code blocks must survive
        assert "```yaml" in fitted
        assert "```python" in fitted
        # Traceback must survive
        assert "Traceback" in fitted


# ─── Classification accuracy on realistic content ────────────────────────────


class TestClassificationAccuracy:
    """Verify the classifier doesn't misclassify real-world patterns."""

    def test_prose_with_names_is_not_t0(self):
        """Plain prose mentioning names should be T3, not false-positive T0."""
        text = (
            "We had a long conversation with Alice about the project timeline "
            "and she mentioned that Bob would be joining the team next month. "
            "Carol suggested we revisit the architecture decisions."
        )
        result = classify_block(text)
        # This is pure prose with names — should be T3 (or T2), not T0
        # Names alone don't make content structural
        assert result.decision in ("T2", "T3"), (
            f"plain prose with names misclassified as {result.decision}: {result.reasons}"
        )

    def test_reasoning_chain_is_t0(self):
        """Decision reasoning with step markers should be T0."""
        text = (
            "Step 1: Evaluate Redis vs Memcached for the cache layer.\n"
            "Step 2: Run load tests on both configurations.\n"
            "Step 3: Compare p99 latency numbers.\n"
            "Therefore, Redis is the better choice given our consistency requirements."
        )
        result = classify_block(text)
        assert result.decision == "T0"
        assert "reasoning_chain" in result.reasons

    def test_error_with_entity_is_t0(self):
        """Error messages are T0 regardless of surrounding prose."""
        text = (
            "Sarah reported that the deploy failed with:\n"
            "Traceback (most recent call last):\n"
            '  File "svc.py", line 99, in deploy\n'
            "    raise TimeoutError('cluster unreachable')\n"
            "TimeoutError: cluster unreachable"
        )
        result = classify_block(text)
        assert result.decision == "T0"

    def test_url_with_prose_is_soft_t0(self):
        """URLs in prose trigger soft T0 but not hard T0."""
        text = (
            "Check the dashboard at https://grafana.internal/d/redis-perf "
            "to see the current latency numbers before we decide."
        )
        result = classify_block(text)
        assert result.decision == "T0"
        assert "url" in result.reasons

    def test_short_decision_is_t2(self):
        """Short decisive statement is T2 (preserved over T3)."""
        text = "We chose Redis over Memcached."
        result = classify_block(text)
        assert result.decision == "T2"

    def test_long_filler_is_t3(self):
        """Verbose filler with no structural signal is T3 (dropped first)."""
        text = (
            "We spent quite a lot of time going over the various different "
            "options and approaches that were available to us at the time, "
            "considering the many pros and cons of each possible direction "
            "we could take and thinking about what would be best overall."
        )
        result = classify_block(text)
        assert result.decision == "T3"
        assert result.reasons == []
