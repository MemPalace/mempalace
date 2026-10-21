import pytest
import timeit
import re

from mempalace.dialect import Dialect

def test_detect_entities_benchmark():
    dialect = Dialect()
    text = "Alice went to the market and met Bob who is a nice guy. They both discussed about Dr. Chen and how he solved the big issue. Another sentence with Name and Name2 and SomeName"

    # Run the function multiple times to measure the performance
    number = 10000
    time = timeit.timeit(lambda: dialect._detect_entities_in_text(text), number=number)
    print(f"\nDialect._detect_entities_in_text benchmark: {time:.4f} seconds for {number} iterations")
