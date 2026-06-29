<template>
  <section v-pre id="dialect" class="dialect">
    <div class="section-mark"><span class="roman">iii</span> <span>the aaak dialect</span></div>

    <div class="dialect-head">
      <span class="eyebrow">index &larr; verbatim</span>
      <h2 class="display">
        A compressed symbolic language <em>for finding</em>, not remembering.
      </h2>
      <p class="lede">
        The content stays verbatim — always. The <em>index</em> above it is written
        in AAAK: a dense symbolic dialect an LLM can scan at a glance. Thousands
        of entries, one pass, exact drawer located.
      </p>
    </div>

    <div class="dialect-grid">
      <article class="slab">
        <header class="card-head">
          <span class="l">drawer · D-007</span>
          <span>verbatim · exact · permanent</span>
        </header>
        <p class="label">The drawer, as stored.</p>
        <p>
          "My son's name is <strong>Noah</strong>. He turns <strong>six</strong>
          on <strong>September 12th</strong>. He loves dinosaurs —
          especially the <strong>therizinosaurus</strong> because of the
          claws. We want to do a small party at <strong>the park on Glebe
          Point Road</strong>, maybe eight kids."
        </p>
        <p style="color:var(--ice-ghost); font-size: 13.5px; font-family: var(--f-mono); letter-spacing: 0.05em; margin-top:1.5rem;">
          &mdash; kept as spoken. never rewritten.
        </p>
      </article>

      <div class="dialect-arrow" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3">
          <path d="M12 3v18M7 8l5-5 5 5M7 16l5 5 5-5"/>
        </svg>
        <span>index · AAAK</span>
      </div>

      <article class="slab mono">
        <header class="card-head">
          <span class="l">index · AAAK</span>
          <span>indexes · compressed · addressable</span>
        </header>
        <p class="label">The pointer, as indexed.</p>
<pre><span class="c">§ W-042/R-11/D-007</span>
<span class="k">@p</span> <span class="t">noah</span>~<span class="v">son.age=6</span>~<span class="v">dob=09-12</span>
<span class="k">@l</span> <span class="t">glebe-pt-rd.park</span>
<span class="k">@e</span> <span class="t">birthday</span>~<span class="v">party(n≈8)</span>
<span class="k">@i</span> <span class="t">therizinosaurus</span>~<span class="v">claws</span>
<span class="k">@t</span> <span class="v">2026-04-14T09:41</span>
<span class="c">§ ptr → D-007 (verbatim)</span></pre>
      </article>
    </div>

    <p class="dialect-caption">
      Dense compression on the pointer layer. Full fidelity on the content
      layer. You get speed without ever losing a word.
    </p>
  </section>
</template>
