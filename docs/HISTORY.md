# MemPalace — History, Corrections, and Public Notices

This file is the canonical record of post-launch corrections, public notices,
and retractions that affect MemPalace's public claims. Newest first.

---

## 2026-04-14 — Benchmark table rewrite (issue [#875](https://github.com/MemPalace/mempalace/issues/875))

A community audit identified a category error in the public benchmark tables
on `README.md` and `mempalaceofficial.com`: MemPalace's retrieval recall
numbers (R@5, R@10) were listed in the same columns as competitors'
end-to-end QA accuracy numbers. They are different metrics and are not
comparable — a system can have 100% retrieval recall and 40% QA accuracy.

The audit also found that the retracted "+34% palace boost" claim (see the
April 7 note below) was still present in multiple surfaces despite that
retraction, and that two competitor numbers (`Mem0 ~85%`, `Zep ~85%`) had no
published source and did not match the metrics those projects actually
publish.

What changed in this PR:

- The headline number on all surfaces is now **96.6% R@5 on LongMemEval in
  raw mode**, independently reproduced on Linux x86_64 against the tagged
  v3.3.0 release on 2026-04-14. Result JSONLs are committed under
  `benchmarks/results_*.jsonl` (see PR description for the scorecard).
- The **"100% with Haiku rerank"** claim has been removed from all public
  comparison tables. It reproduces on our machines and with a different LLM
  family (minimax-m2.7 via Ollama Cloud: 99.2% R@5 / 100.0% R@10 on the full
  500-question LongMemEval set) — but the 99.4% → 100% step was developed
  by inspecting three specific wrong answers (`benchmarks/BENCHMARKS.md` has
  called this "teaching to the test" since February). It belongs in the
  methodology document, not in a headline.
- The **honest held-out number** for the hybrid pipeline — 98.4% R@5 on 450
  questions that `hybrid_v4` was never tuned on, deterministic seed — is now
  the comparable figure when an LLM rerank is involved.
- The **retracted "+34% palace boost"** has been removed from
  `README.md`, `website/concepts/the-palace.md`,
  `website/guide/searching.md`, and `website/reference/contributing.md`.
  Wing and room filters remain useful — they're standard metadata filters —
  but they are not presented as a novel retrieval improvement.
- **Competitor comparison tables** mixing retrieval recall with QA accuracy
  have been removed from `README.md` and `website/reference/benchmarks.md`.
  Where MemPalace can be fairly compared on the same metric, we link to the
  cited source. Otherwise we report our own numbers and let readers draw
  their own conclusions.
- **Reproduction instructions** in `benchmarks/BENCHMARKS.md` and
  `benchmarks/README.md` were pointing at a defunct branch
  (`aya-thekeeper/mempal`); they now point at `MemPalace/mempalace`.
- The **LoCoMo 100% R@10 with top-50 rerank** row has been removed from
  public comparison surfaces. With per-conversation session counts of 19–32
  and `top_k=50`, the retrieval stage returns every session in the
  conversation by construction, so the number measures an LLM's
  reading comprehension over the whole conversation, not retrieval.

Thanks to [@dial481](https://github.com/MemPalace/mempalace/issues/875) for
the detailed audit and to [@rohitg00](https://github.com/rohitg00) for the
parallel write-up in Discussion #747.

---

## 2026-04-11 — Impostor domains and malware

Several community members (issues #267, #326, #506) reported fake MemPalace
websites distributing malware. The only official surfaces for this project
are:

- This GitHub repository: [github.com/MemPalace/mempalace](https://github.com/MemPalace/mempalace)
- The PyPI package: [pypi.org/project/mempalace](https://pypi.org/project/mempalace/)
- The docs site: [mempalaceofficial.com](https://mempalaceofficial.com)

Any other domain — `mempalace.tech` being the one most commonly reported —
is not ours. Never run install scripts from unofficial sites.

Thanks to our community members for flagging the problem.

---

## 2026-04-07 — A Note from Milla & Ben

> The community caught real problems in this README within hours of launch
> and we want to address them directly.
>
> **What we got wrong:**
>
> - **The AAAK token example was incorrect.** We used a rough heuristic
>   (`len(text)//3`) for token counts instead of an actual tokenizer. Real
>   counts via OpenAI's tokenizer: the English example is 66 tokens, the
>   AAAK example is 73. AAAK does not save tokens at small scales — it's
>   designed for *repeated entities at scale*, and the README example was a
>   bad demonstration of that. We're rewriting it.
>
> - **"30x lossless compression" was overstated.** AAAK is a lossy
>   abbreviation system (entity codes, sentence truncation). Independent
>   benchmarks show AAAK mode scores **84.2% R@5 vs raw mode's 96.6%** on
>   LongMemEval — a 12.4 point regression. The honest framing is: AAAK is
>   an experimental compression layer that trades fidelity for token
>   density, and **the 96.6% headline number is from RAW mode, not AAAK**.
>
> - **"+34% palace boost" was misleading.** That number compares unfiltered
>   search to wing+room metadata filtering. Metadata filtering is a
>   standard feature of the underlying vector store, not a novel retrieval
>   mechanism. Real and useful, but not a moat.
>
> - **"Contradiction detection"** exists as a separate utility
>   (`fact_checker.py`) but is not currently wired into the knowledge graph
>   operations as the README implied.
>
> - **"100% with Haiku rerank"** is real (we have the result files) but
>   the rerank pipeline is not in the public benchmark scripts. We're
>   adding it.
>
> **What's still true and reproducible:**
>
> - **96.6% R@5 on LongMemEval in raw mode**, on 500 questions, zero API
>   calls — independently reproduced on M2 Ultra in under 5 minutes by
>   [@gizmax](https://github.com/MemPalace/mempalace/issues/39).
> - Local, free, no subscription, no cloud, no data leaving your machine.
> - The architecture (wings, rooms, closets, drawers) is real and useful,
>   even if it's not a magical retrieval boost.
>
> **What we're doing:**
>
> 1. Rewriting the AAAK example with real tokenizer counts and a scenario
>    where AAAK actually demonstrates compression
> 2. Adding `mode raw / aaak / rooms` clearly to the benchmark
>    documentation so the trade-offs are visible
> 3. Wiring `fact_checker.py` into the KG ops so the contradiction
>    detection claim becomes true
> 4. Pinning the vector store dependency to a tested range (issue #100),
>    fixing the shell injection in hooks (#110), and addressing the macOS
>    ARM64 segfault (#74)
>
> **Thank you to everyone who poked holes in this.** Brutal honest
> criticism is exactly what makes open source work, and it's what we asked
> for. Special thanks to
> [@panuhorsmalahti](https://github.com/MemPalace/mempalace/issues/43),
> [@lhl](https://github.com/MemPalace/mempalace/issues/27),
> [@gizmax](https://github.com/MemPalace/mempalace/issues/39), and everyone
> who filed an issue or a PR in the first 48 hours. We're listening, we're
> fixing, and we'd rather be right than impressive.
>
> — *Milla Jovovich & Ben Sigman*
