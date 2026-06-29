<template>
  <section v-pre id="anatomy" class="anatomy">
    <div class="section-mark"><span class="roman">ii</span> <span>anatomy of a palace</span></div>

    <div class="anatomy-head">
      <div>
        <span class="eyebrow">the method of loci, updated</span>
        <h2 class="display">
          Wings. Rooms. Closets. <em>Drawers.</em>
        </h2>
      </div>
      <p class="lede">
        An ancient memory technique, reworked for a machine. Broad categories
        nest time-based groupings; time-based groupings bundle topics; topics
        hold verbatim drawers. A symbolic index lets the model scan thousands
        of drawers in a single pass and open only the ones it needs.
      </p>
    </div>

    <div class="anatomy-diagram">
      <article class="stratum">
        <span class="n">W — wing</span>
        <h3>The <em>Wings</em></h3>
        <p class="sub">people · projects · topics</p>
        <p>A broad region of the palace, keyed to a real entity — a person by name, a project by codename, a domain of your life. Entity-first, always.</p>
        <div class="diagram">
          <svg viewBox="0 0 200 80" fill="none" stroke="currentColor" stroke-width="1" style="color:var(--prism);">
            <rect x="5" y="20" width="190" height="50" opacity="0.4"/>
            <rect x="15" y="28" width="50" height="34" />
            <rect x="75" y="28" width="50" height="34" />
            <rect x="135" y="28" width="50" height="34" />
            <line x1="5" y1="12" x2="195" y2="12" stroke-dasharray="2 3" opacity="0.5"/>
          </svg>
        </div>
      </article>

      <article class="stratum">
        <span class="n">R — room</span>
        <h3>The <em>Rooms</em></h3>
        <p class="sub">days · sessions · threads</p>
        <p>Inside a wing sit rooms — discrete units of time. One room per day, or one per session. Walk the corridor and the palace unfolds chronologically, room by room.</p>
        <div class="diagram">
          <svg viewBox="0 0 200 80" fill="none" stroke="currentColor" stroke-width="1" style="color:var(--prism);">
            <rect x="10" y="20" width="36" height="44" />
            <rect x="56" y="20" width="36" height="44" />
            <rect x="102" y="20" width="36" height="44" />
            <rect x="148" y="20" width="36" height="44" />
            <line x1="10" y1="70" x2="184" y2="70" stroke-dasharray="1 3" opacity="0.6"/>
          </svg>
        </div>
      </article>

      <article class="stratum">
        <span class="n">C — closet</span>
        <h3>The <em>Closets</em></h3>
        <p class="sub">topics · threads · bundles</p>
        <p>Inside a room, closets group related drawers by topic or thread. Open one closet and you see every drawer on that subject together — no need to walk the whole room.</p>
        <div class="diagram">
          <svg viewBox="0 0 200 80" fill="none" stroke="currentColor" stroke-width="1" style="color:var(--prism);">
            <g class="closet">
              <rect x="10" y="14" width="54" height="52" />
              <line x1="37" y1="14" x2="37" y2="66" opacity="0.5"/>
              <circle cx="33" cy="40" r="1.2" fill="currentColor"/>
              <circle cx="41" cy="40" r="1.2" fill="currentColor"/>
            </g>
            <g class="closet">
              <rect x="73" y="14" width="54" height="52" />
              <line x1="100" y1="14" x2="100" y2="66" opacity="0.5"/>
              <circle cx="96" cy="40" r="1.2" fill="currentColor"/>
              <circle cx="104" cy="40" r="1.2" fill="currentColor"/>
            </g>
            <g class="closet">
              <rect x="136" y="14" width="54" height="52" />
              <line x1="163" y1="14" x2="163" y2="66" opacity="0.5"/>
              <circle cx="159" cy="40" r="1.2" fill="currentColor"/>
              <circle cx="167" cy="40" r="1.2" fill="currentColor"/>
            </g>
          </svg>
        </div>
      </article>

      <article class="stratum">
        <span class="n">D — drawer</span>
        <h3>The <em>Drawers</em></h3>
        <p class="sub">verbatim · permanent · exact</p>
        <p>Each room holds drawers. A drawer is a single chunk of verbatim content — the exact words, untouched. The palace's promise is kept here.</p>
        <div class="diagram">
          <svg viewBox="0 0 200 80" fill="none" stroke="currentColor" stroke-width="1" style="color:var(--prism);">
            <rect x="40" y="14" width="120" height="16" />
            <rect x="40" y="34" width="120" height="16" />
            <rect x="40" y="54" width="120" height="16" />
            <circle cx="150" cy="22" r="1.5" fill="currentColor"/>
            <circle cx="150" cy="42" r="1.5" fill="currentColor"/>
            <circle cx="150" cy="62" r="1.5" fill="currentColor"/>
          </svg>
        </div>
      </article>
    </div>
  </section>
</template>
