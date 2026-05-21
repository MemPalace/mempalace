# C-β v0.1, LongMemEval Granularity × Hybrid-Weight Sweep

## Soru
Discussion #1384'te wash-vs-compound tartışmasının zayıf halkası: `hybrid_v4`
keyword boost'unun gerçek bir bağımsız sinyal mi yoksa session-level lexical
overlap'in yan ürünü mü olduğunu tek bir bench koşusundan ayırt edemiyorduk.

## Hipotez
H0 (wash): `hybrid_v4` lift sadece `granularity=session`'da görünür, çünkü
session metni uzun → keyword overlap kolay. `granularity=turn` (her user turn
ayrı doc) altında lift yok olur veya tersine döner.

H1 (compound): Lift her iki granularity'de de pozitif kalır. Boyut farklı
olabilir ama yön aynı.

## Set-up
- **Data**: `longmemeval_s_cleaned.json` (500 q, 264 MB)
- **Split**: `benchmarks/lme_split_50_450.json`, `--dev-only` (50 q)
- **Embedding**: default `all-MiniLM-L6-v2` (ChromaDB built-in)
- **LLM rerank**: OFF (eksen izolasyonu; #1384 LLM rerank ekseni ayrı)
- **Metrics**: SESSION-LEVEL Recall@{1,5,10,30}, NDCG@{10,30}

## Eksenler (8 koşu)
| mode       | granularity | hybrid_weight |
|------------|-------------|----------------|
| raw        | session     | NA             |
| raw        | turn        | NA             |
| hybrid_v4  | session     | 0.0            |
| hybrid_v4  | session     | 0.30 (default) |
| hybrid_v4  | session     | 0.60           |
| hybrid_v4  | turn        | 0.0            |
| hybrid_v4  | turn        | 0.30           |
| hybrid_v4  | turn        | 0.60           |

`hybrid_weight=0.0` koşuları sanity check: hybrid_v4 pipeline çalışıyor ama
keyword boost devre dışı → `raw` ile near-eşit beklenir.

## Karar kriteri
Δ_session = R@10(hybrid_v4 hw=0.30, session) − R@10(raw, session)
Δ_turn    = R@10(hybrid_v4 hw=0.30, turn)    − R@10(raw, turn)

- |Δ_turn| < 0.02 ve Δ_session ≥ 0.04 → **H0 (wash) destekleniyor**, lift
  session-level lexical overlap'tan geliyor.
- Δ_session ve Δ_turn ikisi de ≥ 0.02 ve aynı işaret → **H1 (compound)**.
- Karışık sonuç → eksen daha derin sweep ister (hw=0.10/0.20/0.45 ekle, 450q'a
  taşı).

## Bütçe
- LLM call yok → $0.
- Wall: ~75 s × 8 ≈ 10 dk Mac mini'de.
- Dev-only 50q dışına çıkmıyoruz (held-out kontamine etmemek için, repo CLAUDE
  konvansiyonu).

## Çıktılar
- `benchmarks/c_beta/*.jsonl`, per-run raw results
- `benchmarks/c_beta/*.stdout`, full bench output (PER-TYPE breakdown dahil)
- `benchmarks/c_beta/sweep_results.csv`, özet tablo
- `benchmarks/c_beta/sweep.log`, koşu özetleri (OK/FAIL satırları)

## Premortem (3 risk)
1. **dev-50 küçüklüğü** → tek soru farkı %2 ndcg. Mitigasyon: en az
   2 hw değeri ile çapraz doğrula; tek noktada karar verme. Tespit: hw=0.0
   ile raw arasında >%4 fark çıkarsa pipeline'da gizli bias var.
2. **Embedding non-determinism** → MiniLM ChromaDB içinde fixed seed mi?
   Mitigasyon: çıkmaz, ama smoke'ta aynı koşuyu 2x koşup R@10 sapmasını ölç.
3. **Turn granularity büyük corpus** → her session 5-50 turn → 50q × ~500
   turn ≈ 25K doc per query, ChromaDB yüklenme süresi şişebilir. Tespit:
   `*_turn_*.stdout` wall süresi 5x'i geçerse erken sinyal.

## C-β v0.2 yol haritası (sonuca göre)
- H0 destekleniyorsa: hybrid_v4 spec'ini #1384'te güncelle, "session-level
  bias" olarak işaretle.
- H1 destekleniyorsa: 450 held-out üzerinde tek-doğrulama koşusu (Atakan onay
  verirse), sonra MemPalace'a PR.
- Karışıksa: extra eksen (`embed-model=bge-base`) ekleyerek embedding'in
  rolünü izole et.
