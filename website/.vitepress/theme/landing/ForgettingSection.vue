<template>
  <section v-pre id="forgetting" class="forgetting">
    <div class="section-mark"><span class="roman">i</span> <span>the forgetting</span></div>

    <header class="forgetting-head">
      <div class="copy">
        <span class="eyebrow">before &middot; after</span>
        <h2 class="display">
          The same conversation, <em>twice.</em>
        </h2>
        <p class="lede" style="margin:0;">
          Scroll down and watch. On the left, a model without memory. On the right,
          the same model with MemPalace. The words are identical — until two weeks
          pass.
        </p>
      </div>
      <button class="replay" id="replay-demo" type="button" aria-label="Replay the demo">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 4v6h6"/><path d="M20 20v-6h-6"/><path d="M4 10a8 8 0 0114-5l2 3"/><path d="M20 14a8 8 0 01-14 5l-2-3"/></svg>
        replay
      </button>
    </header>

    <div class="forgetting-compare" id="forgetting-compare" aria-label="Comparison demo">
      <article class="demo-pane demo-forget">
        <header>
          <span class="pane-tag">without mempalace</span>
          <span class="pane-meta">session <em>resets</em> &middot; no recall</span>
        </header>
        <div class="chat" data-pane="forget" aria-live="polite"></div>
      </article>

      <div class="divider" aria-hidden="true"></div>

      <article class="demo-pane demo-remember">
        <header>
          <span class="pane-tag">with mempalace</span>
          <span class="pane-meta">verbatim &middot; retrieved <em>instantly</em></span>
        </header>
        <div class="chat" data-pane="remember" aria-live="polite"></div>
      </article>
    </div>
  </section>
</template>
