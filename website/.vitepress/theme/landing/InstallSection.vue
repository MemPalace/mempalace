<template>
  <section v-pre id="install" class="install">
    <div class="section-mark" style="left:50%; transform:translateX(-50%);"><span class="roman">v</span> <span>begin</span></div>
    <span class="eyebrow" style="justify-content:center;">open a drawer</span>
    <h2 class="display">
      Build your <em>palace.</em>
    </h2>
    <p class="lede" style="margin-left:auto;margin-right:auto;text-align:center;">
      One command to install. One to initialize. Your words — yours, permanent,
      instantly recallable — from that moment on.
    </p>

    <div class="terminal" role="figure" aria-label="Installation commands">
      <div class="terminal-head">
        <span class="lights"><i></i><i></i><i></i></span>
        <span>~/mempalace &middot; bash</span>
      </div>
<pre><span class="prompt">$</span> pip install -e <span class="dim">".[dev]"</span>
<span class="c">Successfully installed mempalace</span>
<span class="prompt">$</span> mempalace init
<span class="ok">  ✓</span> palace created at <span class="dim">~/.mempalace</span>
<span class="ok">  ✓</span> hooks registered <span class="dim">(stop, precompact)</span>
<span class="ok">  ✓</span> knowledge graph initialized
<span class="prompt">$</span> mempalace mine <span class="dim">./notes</span>
<span class="ok">  ✓</span> filed · <span class="c">W-001/R-01/D-001</span></pre>
    </div>

    <div class="install-cta">
      <a href="/guide/getting-started" class="btn btn-primary">
        Read the docs
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
      </a>
      <a href="https://github.com/MemPalace/mempalace" class="btn">
        Visit the repository
      </a>
    </div>
  </section>
</template>
