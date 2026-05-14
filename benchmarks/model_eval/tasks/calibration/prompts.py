"""Calibration task: 5-class sentence-type classification.

Used as a sanity check for the harness. Any decent ≤4B instruct model
should ace this; if accuracy is poor, the harness is broken before the
real tasks even run.
"""
from __future__ import annotations


SYSTEM = """You are a sentence classifier. Respond with exactly one word from the provided class list. No explanation, no punctuation, no quotes."""


def build_user_prompt(text: str, classes: list[str]) -> str:
    return f"""Classes: {", ".join(classes)}

Sentence: {text}

Class:"""
