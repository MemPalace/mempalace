<template>
  <footer v-pre class="catalog">
    <form class="waitlist waitlist-footer" data-source="footer" novalidate>
      <div class="waitlist-head">
        <span class="waitlist-pulse" aria-hidden="true"></span>
        <span class="waitlist-eyebrow">Last call &middot; subscribe for updates</span>
      </div>
      <div class="waitlist-row">
        <input type="email" class="waitlist-input" name="email" placeholder="you@example.com" autocomplete="email" aria-label="Email address" required />
        <button type="submit" class="waitlist-submit">
          <span class="waitlist-label-default">Join the list</span>
          <span class="waitlist-label-pending" aria-hidden="true">Joining…</span>
          <svg class="waitlist-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          <svg class="waitlist-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M5 12l5 5 9-11"/></svg>
        </button>
      </div>
      <p class="waitlist-msg" aria-live="polite"></p>
    </form>

    <div class="catalog-card">
      <div>
        <p class="catalog-title">MemPalace <em>&mdash;</em> a memory palace for AI.</p>
        <p class="catalog-desc">Verbatim storage, local-first, zero telemetry. Built for people who believe their words are theirs.</p>
      </div>
      <div>
        <h4>Documentation</h4>
        <ul>
          <li><a href="/guide/getting-started">Getting started</a></li>
          <li><a href="/concepts/the-palace">The palace</a></li>
          <li><a href="/reference/cli">CLI reference</a></li>
          <li><a href="/reference/benchmarks">Benchmarks</a></li>
        </ul>
      </div>
      <div>
        <h4>The project</h4>
        <ul>
          <li><a href="https://github.com/MemPalace/mempalace">GitHub</a></li>
          <li><a href="https://github.com/MemPalace/mempalace/blob/main/README.md">Readme</a></li>
          <li><a href="https://github.com/MemPalace/mempalace/blob/main/ROADMAP.md">Roadmap</a></li>
          <li><a href="https://github.com/MemPalace/mempalace/blob/main/CHANGELOG.md">Changelog</a></li>
        </ul>
      </div>
    </div>
  </footer>
</template>
