<template>
  <section v-pre id="mechanics">
    <div class="section-mark"><span class="roman">iv</span> <span>how it works</span></div>

    <div class="mechanics-head">
      <span class="eyebrow">mechanism · architecture</span>
      <h2 class="display">
        Four pieces. <em>No cloud.</em> No keys.
      </h2>
    </div>

    <div class="mechanics">
      <article class="mech">
        <div class="icon" aria-hidden="true">
          <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.3">
            <rect x="8" y="10" width="32" height="22" rx="1"/>
            <path d="M8 16h32"/>
            <g class="mech-bars">
              <path d="M14 24h20"/>
              <path d="M14 28h12"/>
            </g>
            <path d="M16 38h16M20 32v6M28 32v6"/>
            <circle class="mech-led" cx="36" cy="13.5" r="1.1" fill="currentColor"/>
          </svg>
        </div>
        <span class="eyebrow no-rule"><span class="n">— 01</span></span>
        <h3>Local-<em>first</em></h3>
        <p>ChromaDB on disk. SQLite for the knowledge graph. Nothing is uploaded. Nothing is synced. Your palace lives under a single directory on your machine.</p>
        <div class="metric">path · <b>~/.mempalace</b></div>
      </article>

      <article class="mech">
        <div class="icon" aria-hidden="true">
          <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.3">
            <circle class="mech-ring" cx="24" cy="24" r="14"/>
            <path class="mech-plus" d="M16 24h16M24 16v16"/>
            <path class="mech-slash" d="M10 10l28 28" stroke-width="1.5"/>
          </svg>
        </div>
        <span class="eyebrow no-rule"><span class="n">— 02</span></span>
        <h3>Zero <em>API</em></h3>
        <p>Extraction, chunking, and embedding all run locally. No OpenAI key, no Anthropic key, no sentence-transformers endpoint. The memory works even offline, on a plane.</p>
        <div class="metric">keys required · <b>none</b></div>
      </article>

      <article class="mech">
        <div class="icon" aria-hidden="true">
          <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.3">
            <path d="M8 36V18l8-8h16l8 8v18"/>
            <path d="M8 36h32"/>
            <g class="mech-gear">
              <circle cx="24" cy="26" r="4"/>
              <path d="M24 22v-4M24 30v4M20 26h-4M28 26h4"/>
            </g>
          </svg>
        </div>
        <span class="eyebrow no-rule"><span class="n">— 03</span></span>
        <h3>Background <em>hooks</em></h3>
        <p>Filing and indexing happen silently through Claude Code hooks. On session end, on pre-compaction. You write. The palace fills itself behind the curtain.</p>
        <div class="metric">hook budget · <b>&lt;500 ms</b></div>
      </article>

      <article class="mech">
        <div class="icon" aria-hidden="true">
          <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.3">
            <path class="mech-edges" d="M12 14l10 10M36 12L26 24M14 36l8-8M36 34l-10-6" opacity="0.6"/>
            <g class="mech-nodes">
              <circle cx="10" cy="12" r="3"/>
              <circle cx="38" cy="10" r="3"/>
              <circle cx="24" cy="26" r="3"/>
              <circle cx="12" cy="38" r="3"/>
              <circle cx="38" cy="36" r="3"/>
            </g>
          </svg>
        </div>
        <span class="eyebrow no-rule"><span class="n">— 04</span></span>
        <h3>Temporal <em>graph</em></h3>
        <p>Relationships across entities with valid-from and valid-to dates. Who worked on what. When did this change. Facts that were true then, and may not be now.</p>
        <div class="metric">store · <b>sqlite</b></div>
      </article>
    </div>
  </section>
</template>
