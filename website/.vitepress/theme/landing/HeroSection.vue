<template>
  <section v-pre class="hero" id="hero">
    <span class="corner-ticks" aria-hidden="true"><span></span></span>

    <div class="hero-inner">
      <div class="hero-copy">
        <h1 class="display">
          <span class="line">Memory <em class="is-accent">is</em></span>
          <span class="line line-2"><span class="identity-white">identity.</span></span>
        </h1>
        <p class="lede">
          Every conversation, every idea, every small decision&hellip; held somewhere safe.
          <br><br>Welcome to the future of memory: <span class="mp-blue">MemPalace</span>
        </p>
        <form class="waitlist waitlist-hero" data-source="hero" novalidate>
          <div class="waitlist-head">
            <span class="waitlist-pulse" aria-hidden="true"></span>
            <span class="waitlist-eyebrow">Subscribe for updates</span>
          </div>
          <div class="waitlist-row">
            <input
              type="email"
              class="waitlist-input"
              name="email"
              placeholder="you@example.com"
              autocomplete="email"
              aria-label="Email address"
              required
            />
            <button type="submit" class="waitlist-submit">
              <span class="waitlist-label-default">Join the list</span>
              <span class="waitlist-label-pending" aria-hidden="true">Joining…</span>
              <svg class="waitlist-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                <path d="M5 12h14M13 6l6 6-6 6"/>
              </svg>
              <svg class="waitlist-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
                <path d="M5 12l5 5 9-11"/>
              </svg>
            </button>
          </div>
          <p class="waitlist-msg" aria-live="polite"></p>
        </form>

        <div class="hero-secondary">
          <a href="/guide/getting-started">Read the docs</a>
          <span class="sep" aria-hidden="true">·</span>
          <a href="https://github.com/MemPalace/mempalace">GitHub ↗</a>
        </div>
      </div>

      <!-- Palace video visual -->
      <div class="palace-stage" aria-hidden="true">
        <div class="halo"></div>

        <div class="stars">
          <i style="top:12%; left:22%;  --t:5s;   --d:0.0s"></i>
          <i style="top:18%; left:74%;  --t:6s;   --d:1.2s"></i>
          <i style="top:34%; left:8%;   --t:4s;   --d:0.6s"></i>
          <i style="top:44%; left:88%;  --t:7s;   --d:0.3s"></i>
          <i style="top:62%; left:14%;  --t:5.5s; --d:1.8s"></i>
          <i style="top:72%; left:82%;  --t:4.5s; --d:0.9s"></i>
          <i style="top:82%; left:38%;  --t:6.2s; --d:2.4s"></i>
          <i style="top:28%; left:52%;  --t:5.2s; --d:3.0s"></i>
          <i style="top:88%; left:60%;  --t:4.8s; --d:1.5s"></i>
          <i style="top:6%;  left:48%;  --t:6.8s; --d:0.4s"></i>
        </div>

        <video
          class="palace-video"
          src="/hero_video.mp4"
          autoplay
          muted
          loop
          playsinline
          disablepictureinpicture
        ></video>
      </div>
    </div>
  </section>
</template>
