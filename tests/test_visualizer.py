"""
test_visualizer.py — Tests for web visualization.

Imports D3.js and generates interactive HTML from the knowledge graph.
"""

import os

from mempalace.visualizer import render_kg_web, _fetch_all_triples, _build_graph_data


class TestFetchTriples:
    def test_fetch_empty(self, kg):
        triples = _fetch_all_triples(kg.db_path)
        assert triples == []

    def test_fetch_with_data(self, seeded_kg):
        triples = _fetch_all_triples(seeded_kg.db_path)
        assert len(triples) >= 3

    def test_fetch_with_limit(self, seeded_kg):
        triples = _fetch_all_triples(seeded_kg.db_path, limit=2)
        assert len(triples) == 2


class TestBuildGraphData:
    def test_build_empty(self):
        nodes, links = _build_graph_data([])
        assert nodes == []
        assert links == []

    def test_build_with_triples(self, seeded_kg):
        triples = _fetch_all_triples(seeded_kg.db_path)
        nodes, links = _build_graph_data(triples)
        assert len(nodes) >= 4  # Alice, Max, swimming, chess
        assert len(links) >= 3


class TestRenderWeb:
    def test_render_empty(self, kg, tmp_path):
        output = os.path.join(tmp_path, "empty.html")
        result = render_kg_web(kg_path=kg.db_path, output_html=output)
        assert os.path.exists(result)
        with open(result) as f:
            content = f.read()
        assert "MemPalace" in content

    def test_render_with_data(self, seeded_kg, tmp_path):
        output = os.path.join(tmp_path, "graph.html")
        result = render_kg_web(kg_path=seeded_kg.db_path, output_html=output)
        assert os.path.exists(result)
        with open(result) as f:
            content = f.read()
        assert "d3.v7.min.js" in content
        assert "nodes" in content
        assert "links" in content

    def test_render_with_limit(self, seeded_kg, tmp_path):
        output = os.path.join(tmp_path, "limited.html")
        result = render_kg_web(kg_path=seeded_kg.db_path, output_html=output, limit=2)
        with open(result) as f:
            content = f.read()
        assert '"id"' in content  # nodes array


class TestCliIntegration:
    def test_visualize_command(self, tmp_path):
        from mempalace.cli import main
        import sys

        kg_path = os.path.join(tmp_path, "kg.sqlite3")
        from mempalace.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(db_path=kg_path)
        kg.add_entity("Bob", entity_type="person")
        kg.add_entity("Alice", entity_type="person")
        kg.add_triple("Bob", "knows", "Alice")
        kg.close()

        output = os.path.join(tmp_path, "test.html")
        old_argv = sys.argv
        sys.argv = ["mempalace", "visualize", "kg", "--static", "--output", output, "--kg-path", kg_path]
        try:
            main()
        finally:
            sys.argv = old_argv

        assert os.path.exists(output)
