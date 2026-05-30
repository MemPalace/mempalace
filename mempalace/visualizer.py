"""
visualizer.py — Interactive web visualization for MemPalace.

Renders the knowledge graph as an interactive D3.js force-directed graph
that runs in a web browser. No external dependencies — the generated HTML
includes D3.js via CDN.

Usage:
    from mempalace.visualizer import render_kg_web

    render_kg_web(kg_path="~/.mempalace/knowledge_graph.sqlite3", output_html="kg.html")
"""

import json
import os
import sqlite3

from .knowledge_graph import DEFAULT_KG_PATH

ENTITY_TYPE_COLORS = {
    "person": "#4A90D9",
    "project": "#50C878",
    "topic": "#F5A623",
    "tool": "#D0021B",
    "location": "#9013FE",
    "unknown": "#9B9B9B",
}


def _fetch_all_triples(kg_path: str, limit: int = None, as_of: str = None) -> list[dict]:
    """Fetch all triples from the knowledge graph."""
    conn = sqlite3.connect(kg_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT
            s.name as subject,
            t.predicate,
            o.name as object,
            t.valid_from,
            t.valid_to,
            e.type as subject_type,
            eo.type as object_type
        FROM triples t
        JOIN entities s ON t.subject = s.id
        JOIN entities o ON t.object = o.id
        JOIN entities e ON t.subject = e.id
        JOIN entities eo ON t.object = eo.id
    """
    params = []

    if as_of:
        query += " WHERE (t.valid_from IS NULL OR t.valid_from <= ?) AND (t.valid_to IS NULL OR t.valid_to >= ?)"
        params.extend([as_of, as_of])

    query += " ORDER BY t.valid_from DESC"

    if limit:
        query += f" LIMIT {limit}"

    results = []
    for row in conn.execute(query, params).fetchall():
        results.append({
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "subject_type": row["subject_type"],
            "object_type": row["object_type"],
        })

    conn.close()
    return results


def _build_graph_data(triples: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build nodes and links from triples for D3.js."""
    nodes_map = {}
    links = []

    for t in triples:
        s = t["subject"]
        o = t["object"]
        p = t["predicate"]

        if s not in nodes_map:
            nodes_map[s] = {"id": s, "type": t.get("subject_type", "unknown"), "group": 1}
        if o not in nodes_map:
            nodes_map[o] = {"id": o, "type": t.get("object_type", "unknown"), "group": 1}

        links.append({"source": s, "target": o, "predicate": p, "valid_from": t.get("valid_from")})

    nodes = list(nodes_map.values())
    return nodes, links


def export_kg_json(kg_path: str = None, limit: int = None, as_of: str = None) -> str:
    """Export knowledge graph as JSON for API endpoint."""
    kg_path = kg_path or DEFAULT_KG_PATH
    kg_path = os.path.expanduser(kg_path)

    if not os.path.exists(kg_path):
        return json.dumps({"nodes": [], "links": [], "error": "KG not found"})

    triples = _fetch_all_triples(kg_path, limit=limit, as_of=as_of)
    nodes, links = _build_graph_data(triples)

    return json.dumps({"nodes": nodes, "links": links, "count": len(nodes)})


def _generate_html(
    nodes: list[dict],
    links: list[dict],
    title: str = "Knowledge Graph",
) -> str:
    """Generate interactive D3.js visualization HTML."""
    import json

    nodes_json = json.dumps(nodes, indent=2)
    links_json = json.dumps(links, indent=2)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MemPalace — {title}</title>
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            overflow: hidden;
        }}
        #graph {{
            width: 100vw;
            height: 100vh;
        }}
        .node {{
            cursor: pointer;
            transition: opacity 0.2s;
        }}
        .node:hover {{
            opacity: 0.8;
        }}
        .node circle {{
            stroke: #fff;
            stroke-width: 2px;
        }}
        .link {{
            stroke: #666;
            stroke-opacity: 0.6;
        }}
        .link:hover {{
            stroke-opacity: 1;
        }}
        .label {{
            fill: #eee;
            font-size: 12px;
            pointer-events: none;
            text-shadow: 0 1px 3px rgba(0,0,0,0.8);
        }}
        #controls {{
            position: fixed;
            top: 20px;
            left: 20px;
            background: rgba(30, 30, 50, 0.9);
            padding: 16px;
            border-radius: 8px;
            z-index: 100;
        }}
        #controls h1 {{
            font-size: 18px;
            margin-bottom: 12px;
            color: #4A90D9;
        }}
        #legend {{
            margin-top: 12px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            margin: 6px 0;
            font-size: 13px;
        }}
        .legend-dot {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 8px;
        }}
        #stats {{
            margin-top: 16px;
            font-size: 12px;
            color: #888;
        }}
    </style>
</head>
<body>
    <div id="controls">
        <h1>MemPalace</h1>
        <div style="font-size: 12px; color: #888;">Drag nodes to move. Scroll to zoom.</div>
        <div id="legend"></div>
        <div id="stats"></div>
    </div>
    <svg id="graph"></svg>
    <script>
        const nodes = {nodes_json};
        const links = {links_json};

        const colors = {json.dumps(ENTITY_TYPE_COLORS)};

        const width = window.innerWidth;
        const height = window.innerHeight;

        const svg = d3.select("#graph")
            .attr("width", width)
            .attr("height", height);

        const g = svg.append("g");

        svg.call(d3.zoom()
            .scaleExtent([0.1, 4])
            .on("zoom", (event) => {{
                g.attr("transform", event.transform);
            }}));

        const simulation = d3.forceSimulation(nodes)
            .force("link", d3.forceLink(links).id(d => d.id).distance(100))
            .force("charge", d3.forceManyBody().strength(-300))
            .force("center", d3.forceCenter(width / 2, height / 2))
            .force("collision", d3.forceCollide().radius(30));

        const link = g.append("g")
            .selectAll("line")
            .data(links)
            .join("line")
            .attr("class", "link")
            .attr("stroke-width", 2);

        const node = g.append("g")
            .selectAll("g")
            .data(nodes)
            .join("g")
            .attr("class", "node")
            .call(d3.drag()
                .on("start", dragstarted)
                .on("drag", dragged)
                .on("end", dragended));

        node.append("circle")
            .attr("r", 12)
            .attr("fill", d => colors[d.type] || colors.unknown);

        node.append("text")
            .attr("class", "label")
            .attr("dx", 16)
            .attr("dy", 4)
            .text(d => d.id);

        simulation.on("tick", () => {{
            link
                .attr("x1", d => d.source.x)
                .attr("y1", d => d.source.y)
                .attr("x2", d => d.target.x)
                .attr("y2", d => d.target.y);

            node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
        }});

        function dragstarted(event) {{
            if (!event.active) simulation.alphaTarget(0.3).restart();
            event.subject.fx = event.subject.x;
            event.subject.fy = event.subject.y;
        }}

        function dragged(event) {{
            event.subject.fx = event.x;
            event.subject.fy = event.y;
        }}

        function dragended(event) {{
            if (!event.active) simulation.alphaTarget(0);
            event.subject.fx = null;
            event.subject.fy = null;
        }}

        // Legend
        const types = [...new Set(nodes.map(n => n.type))];
        const legend = document.getElementById("legend");
        types.forEach(type => {{
            const item = document.createElement("div");
            item.className = "legend-item";
            item.innerHTML = `<div class="legend-dot" style="background:${{colors[type] || colors.unknown}}"></div>${{type}}`;
            legend.appendChild(item);
        }});

// Stats
        document.getElementById("stats").textContent = `${{nodes.length}} nodes · ${{links.length}} edges`;
    </script>
</body>
</html>
"""
    return html


def render_kg_web(
    kg_path: str = None,
    output_html: str = "kg_mindmap.html",
    limit: int = None,
    as_of: str = None,
) -> str:
    """
    Render the knowledge graph as an interactive web page.

    Args:
        kg_path: Path to the knowledge graph SQLite database.
                 Defaults to ~/.mempalace/knowledge_graph.sqlite3
        output_html: Where to write the HTML file.
        limit: Maximum number of triples to render.
        as_of: Filter to triples valid on this date (YYYY-MM-DD).

    Returns:
        Path to the generated HTML file.
    """
    kg_path = kg_path or DEFAULT_KG_PATH
    kg_path = os.path.expanduser(kg_path)

    if not os.path.exists(kg_path):
        raise FileNotFoundError(f"Knowledge graph not found: {kg_path}")

    triples = _fetch_all_triples(kg_path, limit=limit, as_of=as_of)

    if not triples:
        return _write_empty_html(output_html)

    nodes, links = _build_graph_data(triples)

    html = _generate_html(
        nodes,
        links,
        title=f"Knowledge Graph ({len(nodes)} nodes)",
    )

    output_path = os.path.abspath(output_html)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
        f.write("\n")

    return output_path


def _write_empty_html(output_html: str) -> str:
    """Write an empty state HTML."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MemPalace — Knowledge Graph</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #1a1a2e;
            color: #eee;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }
        .empty {
            text-align: center;
            color: #888;
        }
        h1 { color: #4A90D9; margin-bottom: 8px; }
    </style>
</head>
<body>
    <div class="empty">
        <h1>MemPalace</h1>
        <p>No knowledge graph data yet.</p>
        <p style="margin-top: 16px; font-size: 14px;">
            Add entities and relationships using the KG API or start a conversation to build your memory palace.
        </p>
    </div>
</body>
</html>
"""
    output_path = os.path.abspath(output_html)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
        f.write("\n")
    return output_path
