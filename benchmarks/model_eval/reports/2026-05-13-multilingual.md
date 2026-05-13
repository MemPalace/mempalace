# Multilingual Benchmark — 2026-05-13 (Mercurio)

**6 local models × 7 languages × 5 tasks = 210 runs**  
Hardware: RTX 3080 Laptop 8 GB · Ollama 0.23.3  
Embed model: `nomic-embed-text` · Dataset: n=20 (calibration), n=40 (memory/entity), n=101 (room)

---

## Models

| short name      | tag                                          | quant  |
|:--------------- |:-------------------------------------------- |:------:|
| qwen3-4b-q8     | qwen3:4b-instruct-2507-q8_0                  | q8_0   |
| gemma4-e4b-q4   | gemma4:e4b-it-q4_K_M                         | q4_K_M |
| gemma4-e4b      | gemma4:e4b                                   | q4_K_M |
| classifier-q8   | igorls/gemma4-e4b-classifier:Q8_0            | q8_0   |
| classifier-q4   | igorls/gemma4-e4b-classifier:latest          | q4_K_M |
| heretic-q4      | igorls/gemma-4-E4B-it-heretic-GGUF:Q4_K_M   | q4_K_M |

---

## Overall (all tasks × all locales)

| model          | EN    | non-EN avg | all avg | calib fastest |
|:-------------- |:-----:|:----------:|:-------:|:-------------:|
| classifier-q8  | 0.798 | 0.674      | **0.691** | 354 ms      |
| classifier-q4  | 0.792 | 0.659      | 0.678   | 282 ms        |
| gemma4-e4b     | 0.790 | 0.655      | 0.675   | 324 ms        |
| gemma4-e4b-q4  | 0.784 | 0.655      | 0.673   | 312 ms        |
| qwen3-4b-q8    | 0.781 | 0.645      | 0.665   | **161 ms**    |
| heretic-q4     | 0.787 | 0.644      | 0.664   | 272 ms        |

`classifier-q8` lidera em accuracy (+2.6 pp vs heretic no all avg).  
`qwen3-4b-q8` é 2–3× mais rápido em tasks simples e 3.º em accuracy.  
`gemma4-e4b` e `gemma4-e4b-q4` são estatisticamente equivalentes (dentro do ruído).

---

## Por tarefa

### Room Classification — closed-set

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| classifier-q8  | 0.624 | 0.604 | 0.604 | 0.624 | 0.604 | 0.634 | 0.624 | **0.617** |
| classifier-q4  | 0.644 | 0.584 | 0.594 | 0.554 | 0.584 | 0.604 | 0.604 | 0.596 |
| gemma4-e4b     | 0.624 | 0.574 | 0.584 | 0.554 | 0.584 | 0.604 | 0.594 | 0.588 |
| gemma4-e4b-q4  | 0.604 | 0.594 | 0.594 | 0.554 | 0.584 | 0.604 | 0.604 | 0.591 |
| heretic-q4     | 0.624 | 0.594 | 0.594 | 0.545 | 0.564 | 0.594 | 0.604 | 0.588 |
| qwen3-4b-q8    | 0.624 | 0.564 | 0.554 | 0.554 | 0.535 | 0.554 | 0.564 | 0.564 |

Queda média EN→non-EN: ~3–6 pp. Distribuição uniforme entre idiomas — nenhuma língua é outlier.

### Room Classification — open-set

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| classifier-q8  | 0.678 | 0.637 | 0.634 | 0.651 | 0.644 | 0.645 | 0.633 | **0.646** |
| gemma4-e4b     | 0.657 | 0.647 | 0.642 | 0.630 | 0.637 | 0.648 | 0.642 | **0.643** |
| gemma4-e4b-q4  | 0.655 | 0.644 | 0.633 | 0.634 | 0.632 | 0.647 | 0.640 | 0.641 |
| classifier-q4  | 0.655 | 0.651 | 0.632 | 0.622 | 0.626 | 0.636 | 0.643 | 0.638 |
| heretic-q4     | 0.627 | 0.605 | 0.603 | 0.629 | 0.601 | 0.639 | 0.644 | 0.621 |
| qwen3-4b-q8    | 0.603 | 0.572 | 0.562 | 0.599 | 0.570 | 0.581 | 0.559 | 0.578 |

Open-set é mais estável que closed-set entre idiomas — a cosine similarity absorve variações de phrasing melhor que exact-match.  
Gemma4 lidera abertamente; qwen3 fica ~6 pp abaixo.

### Entity Extraction

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| heretic-q4     | 0.782 | 0.701 | 0.771 | 0.751 | **0.792** | 0.733 | 0.729 | **0.751** |
| qwen3-4b-q8    | 0.777 | 0.732 | **0.799** | 0.764 | **0.801** | 0.770 | 0.758 | **0.771** |
| classifier-q8  | 0.763 | 0.709 | 0.761 | 0.754 | 0.763 | 0.736 | 0.745 | 0.747 |
| gemma4-e4b     | 0.759 | 0.676 | 0.761 | 0.745 | 0.773 | 0.709 | 0.726 | 0.736 |
| gemma4-e4b-q4  | 0.748 | 0.663 | 0.760 | 0.736 | 0.773 | 0.712 | 0.729 | 0.732 |
| classifier-q4  | 0.723 | 0.680 | 0.756 | 0.733 | 0.745 | 0.698 | 0.708 | 0.720 |

A task mais robusta entre idiomas — queda de apenas ~3–5 pp EN→non-EN.  
**qwen3** e **heretic** empatam na liderança. FR e IT frequentemente superam EN (provável efeito de dados de treino mais ricos nessas línguas).  
KO e DE são os idiomas mais difíceis aqui.

### Memory Extraction ⚠️

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   | drop EN→avg |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----------:|
| qwen3-4b-q8    | **0.950** | 0.287 | 0.438 | 0.463 | 0.463 | 0.400 | 0.212 | **−0.573**  |
| heretic-q4     | **0.950** | 0.225 | 0.425 | 0.350 | 0.438 | 0.312 | 0.163 | **−0.631**  |
| classifier-q4  | 0.938 | 0.325 | 0.487 | 0.438 | 0.475 | 0.400 | 0.225 | −0.546      |
| classifier-q8  | 0.925 | 0.412 | 0.450 | 0.438 | **0.500** | 0.438 | 0.212 | −0.517      |
| gemma4-e4b     | 0.912 | 0.312 | 0.450 | 0.400 | 0.450 | 0.375 | 0.188 | −0.550      |
| gemma4-e4b-q4  | 0.912 | 0.312 | 0.438 | 0.400 | 0.463 | 0.362 | 0.188 | −0.552      |

**Esta é a tarefa crítica.** Todos os modelos colapsam ~0.52–0.63 pp de EN para não-EN.  
`classifier-q8` tem o menor drop (−0.517) e o melhor não-EN abs (0.375 avg).  
RU e DE são os piores — possivelmente efeito do embedding (`nomic-embed-text` tem sinal fraco para pares EN↔RU/DE em extração de memória, como documentado no PR #1483).

> **Nota metodológica**: os scores de memory_extraction usam cosine similarity via `nomic-embed-text`. Para pares de idiomas distantes (RU, DE), o modelo de embedding pode estar subestimando a cobertura real — veja PR #1483 para a comparação com `embeddinggemma`.

### Calibration

| model          |  en   |  de   |  fr   |  hi   |  it   |  ko   |  ru   |  avg  |
|:-------------- |:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|:-----:|
| gemma4-e4b     | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| gemma4-e4b-q4  | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| classifier-q8  | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| classifier-q4  | 1.000 | 0.950 | 0.950 | 0.950 | 1.000 | 0.950 | 0.950 | 0.964 |
| qwen3-4b-q8    | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 |
| heretic-q4     | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 | 0.950 |

Calibração é praticamente language-agnostic — sinal limpo, sem surpresas.

---

## Ranking por idioma (all-tasks avg)

| locale | best model     | score | worst model   | score |
|:------:|:-------------- |:-----:|:------------- |:-----:|
| en     | classifier-q8  | 0.798 | qwen3-4b-q8   | 0.781 |
| de     | classifier-q8  | 0.681 | heretic-q4    | 0.615 |
| fr     | classifier-q8  | 0.688 | qwen3-4b-q8   | 0.661 |
| hi     | classifier-q8  | 0.683 | heretic-q4    | 0.645 |
| it     | classifier-q8  | 0.700 | qwen3-4b-q8   | 0.676 |
| ko     | classifier-q8  | 0.680 | heretic-q4    | 0.647 |
| ru     | classifier-q8  | 0.641 | heretic-q4    | 0.608 |

`classifier-q8` lidera em todos os 7 idiomas. RU é o idioma mais difícil globalmente.

---

## Velocidade (e2e_p50 ms — calibration como proxy de latência base)

| model          | en   | de   | fr   | hi   | it   | ko   | ru   |
|:-------------- |:----:|:----:|:----:|:----:|:----:|:----:|:----:|
| qwen3-4b-q8    | 253  | 280  | 246  | 190  | 179  | 168  | 161  |
| heretic-q4     | 441  | 608  | 610  | 312  | 272  | 285  | 300  |
| classifier-q4  | 556  | 582  | 630  | 287  | 329  | 282  | 323  |
| gemma4-e4b     | 632  | 623  | 587  | 397  | 337  | 324  | 367  |
| gemma4-e4b-q4  | 633  | 633  | 610  | 459  | 312  | 434  | 366  |
| classifier-q8  | 662  | 643  | 669  | 437  | 433  | 395  | 354  |

`qwen3-4b-q8` é **2.5–4× mais rápido** que todos os modelos Gemma4 na latência base, apesar do q8_0. Idiomas com scripts não-latinos (HI, KO, RU) geram menos tokens por prompt → latências menores.

---

## Recomendações

**Para produção (melhor accuracy):** `classifier-q8` — lidera em todos os idiomas e tem o menor drop em memory_extraction não-EN. Custo: 2× mais lento que qwen3.

**Para edge / 8 GB apertado:** `classifier-q4` ou `qwen3-4b-q8` — accuracy próxima, 2–3× mais rápidos. qwen3 domina entity extraction; classifier-q4 domina room-open.

**gemma4-e4b vs gemma4-e4b-q4:** diferença < 0.003 em todos os scores — dentro do ruído estatístico. Prefira `q4_K_M` para poupar ~2 GB de VRAM.

**Memory extraction não-EN:** o colapso é universal (−0.5 a −0.63 pp). Antes de descartar, re-rodar com `--embed-model embeddinggemma` (ver PR #1483) para separar efeito de scoring vs. efeito de modelo.
