import { onMounted, onBeforeUnmount } from 'vue'

export function useLandingEffects() {
// Shared cleanup registry — IIFEs push disconnect/removeEventListener thunks
// here so onBeforeUnmount can tear everything down on SPA nav.
const cleanups = []

onMounted(() => {
  if (typeof document === 'undefined') return

  // Hide VitePress chrome while the landing component is live, restore on leave.
  document.body.classList.add('mempalace-active')

  /* ---------- Waitlist submission ---------- */
  ;(function initWaitlist(){
    const ENDPOINT = 'https://br.staging.mempalaceofficial.com/waitlist'
    const forms = document.querySelectorAll('.mempalace-landing .waitlist')
    const emailRe = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

    forms.forEach(form => {
      const input  = form.querySelector('.waitlist-input')
      const button = form.querySelector('.waitlist-submit')
      const msg    = form.querySelector('.waitlist-msg')
      const source = form.dataset.source || 'landing'

      function setState(state, text) {
        form.classList.remove('is-pending', 'is-success', 'is-error')
        if (state) form.classList.add('is-' + state)
        if (text != null) msg.textContent = text
      }

      const onSubmit = async (e) => {
        e.preventDefault()
        if (form.classList.contains('is-success') || form.classList.contains('is-pending')) return

        const email = (input.value || '').trim()
        if (!emailRe.test(email)) {
          setState('error', 'Please provide a valid email address.')
          input.focus()
          return
        }

        setState('pending', 'Sending…')
        button.disabled = true
        input.disabled = true

        try {
          const res = await fetch(ENDPOINT, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, source })
          })
          let data = null
          try { data = await res.json() } catch (_) { /* no body */ }

          if (res.ok) {
            setState('success', (data && data.message) || "Success! You're on the list for updates.")
            // keep inputs disabled so they can't resubmit accidentally
            input.value = email
            return
          }

          if (res.status === 429) {
            setState('error', 'Whoa — slow down a moment, then try again.')
          } else if (res.status === 400) {
            setState('error', (data && data.message) || 'Please provide a valid email address.')
          } else {
            setState('error', (data && data.message) || 'Something went wrong. Please try again later.')
          }
          button.disabled = false
          input.disabled = false
        } catch (_err) {
          setState('error', 'Network error — please try again.')
          button.disabled = false
          input.disabled = false
        }
      }

      const onInput = () => {
        if (form.classList.contains('is-error')) setState(null, '')
      }

      form.addEventListener('submit', onSubmit)
      input.addEventListener('input', onInput)
      cleanups.push(() => {
        form.removeEventListener('submit', onSubmit)
        input.removeEventListener('input', onInput)
      })
    })
  })()



  /* ---------- Reveal-on-scroll for cards ---------- */
  ;(function(){
    if (!('IntersectionObserver' in window)) return
    const items = document.querySelectorAll('.mempalace-landing .stratum, .mempalace-landing .mech, .mempalace-landing .slab')
    items.forEach(el => {
      el.style.opacity = '0'
      el.style.transform = 'translateY(20px)'
      el.style.transition = 'opacity 0.9s ease, transform 0.9s ease'
    })
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting){
          const idx = [...entry.target.parentElement.children].indexOf(entry.target)
          entry.target.style.transitionDelay = (idx * 80) + 'ms'
          entry.target.style.opacity = '1'
          entry.target.style.transform = 'translateY(0)'
          io.unobserve(entry.target)
        }
      })
    }, { rootMargin: '0px 0px -80px 0px' })
    items.forEach(el => io.observe(el))
    cleanups.push(() => io.disconnect())
  })()

  /* ---------- Forgetting demo ---------- */
  ;(function initForgettingDemo(){
    const compare = document.getElementById('forgetting-compare')
    if (!compare) return
    const leftChat  = compare.querySelector('[data-pane="forget"]')
    const rightChat = compare.querySelector('[data-pane="remember"]')
    const replayBtn = document.getElementById('replay-demo')
    const reduced   = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    const delay = ms => new Promise(r => setTimeout(r, reduced ? Math.min(ms, 60) : ms))

    function clear() {
      leftChat.innerHTML = ''
      rightChat.innerHTML = ''
      if (replayBtn) replayBtn.classList.remove('visible')
    }

    function addMsg(chat, who, opts = {}) {
      const row = document.createElement('div')
      row.className = 'msg ' + (who === 'You' ? 'you' : 'ai')
      if (opts.id) row.dataset.id = opts.id
      row.innerHTML = '<span class="who">' + who + '</span><span class="body"></span>'
      chat.appendChild(row)
      chat.scrollTop = chat.scrollHeight
      return row
    }

    async function typeInto(row, text, speed = 14) {
      const body = row.querySelector('.body')
      const parts = text.split(/(<[^>]+>)/)
      row.classList.add('typing')
      for (const part of parts) {
        if (!part) continue
        if (part.startsWith('<')) { body.insertAdjacentHTML('beforeend', part); continue }
        for (const ch of part) {
          body.insertAdjacentText('beforeend', ch)
          if (!reduced) await delay(speed + (Math.random() < 0.08 ? 40 : 0))
        }
      }
      row.classList.remove('typing')
    }

    function addDivider(chat, text) {
      const d = document.createElement('div')
      d.className = 'divider-time'
      d.textContent = '— ' + text + ' —'
      chat.appendChild(d)
      return d
    }

    function addRetrieval(chat, callNumber, ms) {
      const row = document.createElement('div')
      row.className = 'retrieval'
      row.innerHTML =
        '<span class="who">mem</span>' +
        '<span class="l">retrieved &middot; <span class="r">' + callNumber + '</span></span>' +
        '<span>' + ms + '&nbsp;ms</span>'
      chat.appendChild(row)
      return row
    }

    function addStamp(chat, text, callNumber) {
      const el = document.createElement('div')
      el.className = 'stamp'
      el.innerHTML = '<span>— ' + text + '</span>' +
        (callNumber ? '<span class="call">' + callNumber + '</span>' : '')
      chat.appendChild(el)
      return el
    }

    function disintegrate(target) {
      return new Promise(resolve => {
        const parent = target.closest('.chat')
        if (!parent) { resolve(); return }
        const parentRect = parent.getBoundingClientRect()
        const style = getComputedStyle(target)
        const font = style.font ||
          (style.fontStyle + ' ' + style.fontWeight + ' ' + style.fontSize + '/' + style.lineHeight + ' ' + style.fontFamily)
        const color = style.color

        let overlay = parent.querySelector('.dust-overlay')
        if (!overlay) {
          overlay = document.createElement('div')
          overlay.className = 'dust-overlay'
          parent.appendChild(overlay)
        }

        const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT)
        const range = document.createRange()
        const spans = []
        let node
        while ((node = walker.nextNode())) {
          const chars = node.textContent
          for (let i = 0; i < chars.length; i++) {
            if (chars[i] === ' ') continue
            range.setStart(node, i)
            range.setEnd(node, i + 1)
            const r = range.getBoundingClientRect()
            if (r.width === 0 || r.height === 0) continue
            const span = document.createElement('span')
            span.className = 'dust'
            span.textContent = chars[i]
            span.style.left = (r.left - parentRect.left) + 'px'
            span.style.top  = (r.top  - parentRect.top)  + 'px'
            span.style.width  = r.width  + 'px'
            span.style.height = r.height + 'px'
            span.style.font = font
            span.style.color = color
            span.style.opacity = '1'
            span.style.transform = 'translate(0,0)'
            span.style.transitionDuration = (1500 + Math.random() * 900) + 'ms'
            overlay.appendChild(span)
            spans.push(span)
          }
        }

        target.style.transition = 'color 0.35s ease, opacity 0.35s ease'
        target.style.color = 'transparent'

        void overlay.offsetHeight
        const cx = parentRect.width / 2
        spans.forEach((s) => {
          s.style.transitionDelay = (Math.random() * 500) + 'ms'
          const x0 = parseFloat(s.style.left)
          const dx = (x0 - cx) * 0.06 + (Math.random() - 0.5) * 36
          const dy = 30 + Math.random() * 80
          const rot = (Math.random() - 0.5) * 44
          s.style.transform = 'translate(' + dx + 'px,' + dy + 'px) rotate(' + rot + 'deg)'
          s.style.opacity = '0'
          s.style.filter = 'blur(2px)'
        })

        setTimeout(() => {
          spans.forEach(s => s.remove())
          resolve()
        }, reduced ? 200 : 2600)
      })
    }

    const NOAH_TEXT = "My son's name is Noah. He turns six on September 12th."

    async function runForget() {
      const you1 = addMsg(leftChat, 'You', { id: 'noah' })
      await delay(200)
      await typeInto(you1, NOAH_TEXT, 16)
      await delay(500)
      const ai1 = addMsg(leftChat, 'Model')
      await typeInto(ai1, "Noted. I'll remember that for next time we talk.", 14)
      await delay(900)
      addDivider(leftChat, 'two weeks later')
      await delay(700)
      const you2 = addMsg(leftChat, 'You')
      await typeInto(you2, "Help me plan Noah's birthday.", 18)
      await delay(700)
      const target = leftChat.querySelector('.msg[data-id="noah"] .body')
      if (target) await disintegrate(target)
      await delay(250)
      const ai2 = addMsg(leftChat, 'Model')
      await typeInto(ai2, "Of course. Who is Noah? How old is he turning?", 16)
      await delay(500)
      addStamp(leftChat, 'forgotten.')
    }

    async function runRemember() {
      const you1 = addMsg(rightChat, 'You', { id: 'noah' })
      await delay(200)
      await typeInto(you1, NOAH_TEXT, 16)
      await delay(500)
      const ai1 = addMsg(rightChat, 'Model')
      await typeInto(ai1, "Noted. Filed — <strong>W-042/R-01/D-003</strong>.", 14)
      await delay(900)
      addDivider(rightChat, 'two weeks later')
      await delay(700)
      const you2 = addMsg(rightChat, 'You')
      await typeInto(you2, "Help me plan Noah's birthday.", 18)
      await delay(600)
      addRetrieval(rightChat, 'W-042/R-01/D-003', 42)
      await delay(700)
      const ai2 = addMsg(rightChat, 'Model')
      await typeInto(ai2,
        "Of course — <strong>Noah</strong> turns <strong>six</strong> on <strong>September 12th</strong>. " +
        "You mentioned he loves the <strong>therizinosaurus</strong>, and a park on " +
        "<strong>Glebe Point Road</strong>. Shall we build from there?",
        11)
      await delay(500)
      addStamp(rightChat, 'remembered.', 'W-042/R-01/D-003')
    }

    let running = { forget: false, remember: false }
    let started = { forget: false, remember: false }

    async function runBoth() {
      if (running.forget || running.remember) return
      running.forget = running.remember = true
      started.forget = started.remember = true
      clear()
      await delay(200)
      await Promise.all([runForget(), runRemember()])
      running.forget = running.remember = false
      if (replayBtn) replayBtn.classList.add('visible')
    }

    async function runSide(side) {
      if (running[side] || started[side]) return
      running[side] = true
      started[side] = true
      const chat = side === 'forget' ? leftChat : rightChat
      chat.innerHTML = ''
      await delay(200)
      await (side === 'forget' ? runForget() : runRemember())
      running[side] = false
      if (started.forget && started.remember && !running.forget && !running.remember && replayBtn) {
        replayBtn.classList.add('visible')
      }
    }

    function resetAll() {
      started.forget = started.remember = false
      clear()
    }

    const stackedMQ = window.matchMedia('(max-width: 900px)')
    const isStacked = () => stackedMQ.matches

    function observeOnce(el, onReach) {
      if (!('IntersectionObserver' in window)) { onReach(); return null }
      let done = false
      const io = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          if (done || !entry.isIntersecting) return
          const rect = entry.boundingClientRect
          const elementCoverage  = entry.intersectionRatio
          const viewportCoverage = entry.intersectionRect.height / window.innerHeight
          const mostlyVisible  = elementCoverage >= 0.65
          const dominatesView  = viewportCoverage >= 0.60 && rect.top <= window.innerHeight * 0.15
          if (mostlyVisible || dominatesView) {
            done = true
            onReach()
            io.disconnect()
          }
        })
      }, {
        threshold: [0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 1.0],
        rootMargin: '-8% 0px -8% 0px'
      })
      io.observe(el)
      return io
    }

    let observers = []
    function disconnectObservers() {
      observers.forEach(io => io && io.disconnect())
      observers = []
    }

    function armObservers() {
      disconnectObservers()
      if (isStacked()) {
        observers.push(observeOnce(compare.querySelector('.demo-forget'),   () => runSide('forget')))
        observers.push(observeOnce(compare.querySelector('.demo-remember'), () => runSide('remember')))
      } else {
        observers.push(observeOnce(compare, runBoth))
      }
    }

    const onReplayClick = () => {
      resetAll()
      armObservers()
    }
    if (replayBtn) replayBtn.addEventListener('click', onReplayClick)

    armObservers()

    cleanups.push(() => {
      disconnectObservers()
      if (replayBtn) replayBtn.removeEventListener('click', onReplayClick)
    })
  })()
})

onBeforeUnmount(() => {
  if (typeof document === 'undefined') return
  document.body.classList.remove('mempalace-active')
  while (cleanups.length) {
    const fn = cleanups.pop()
    try { fn() } catch (_) { /* swallow — teardown best-effort */ }
  }
})
}
