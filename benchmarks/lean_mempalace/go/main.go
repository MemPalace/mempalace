package main

import (
	"container/heap"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"runtime"
	"runtime/debug"
	"sort"
	"sync"
	"syscall"
	"time"
	"unsafe"

	_ "modernc.org/sqlite"
)

const DIM = 384

type ProcessMemoryCounters struct {
	CB                         uint32
	PageFaultCount             uint32
	PeakWorkingSetSize         uintptr
	WorkingSetSize             uintptr
	QuotaPeakPagedPoolUsage    uintptr
	QuotaPagedPoolUsage        uintptr
	QuotaPeakNonPagedPoolUsage uintptr
	QuotaNonPagedPoolUsage     uintptr
	PagefileUsage              uintptr
	PeakPagefileUsage          uintptr
}

var (
	modpsapi                 = syscall.NewLazyDLL("psapi.dll")
	procGetProcessMemoryInfo = modpsapi.NewProc("GetProcessMemoryInfo")
	kernel32                 = syscall.NewLazyDLL("kernel32.dll")
	procGetCurrentProcess    = kernel32.NewProc("GetCurrentProcess")
)

func getRssMb() float64 {
	h, _, _ := procGetCurrentProcess.Call()
	var pmc ProcessMemoryCounters
	pmc.CB = uint32(unsafe.Sizeof(pmc))
	r, _, _ := procGetProcessMemoryInfo.Call(h, uintptr(unsafe.Pointer(&pmc)), uintptr(pmc.CB))
	if r != 0 {
		return float64(pmc.WorkingSetSize) / (1024.0 * 1024.0)
	}
	return 0.0
}

type Candidate struct {
	idIdx    int
	distance float32
}

type CandidateHeap []Candidate

func (h CandidateHeap) Len() int           { return len(h) }
func (h CandidateHeap) Less(i, j int) bool { return h[i].distance > h[j].distance }
func (h CandidateHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }

func (h *CandidateHeap) Push(x interface{}) {
	*h = append(*h, x.(Candidate))
}

func (h *CandidateHeap) Pop() interface{} {
	old := *h
	n := len(old)
	x := old[n-1]
	*h = old[0 : n-1]
	return x
}

func pushBounded(h *CandidateHeap, k int, c Candidate) {
	if h.Len() < k {
		heap.Push(h, c)
	} else if c.distance < (*h)[0].distance {
		(*h)[0] = c
		heap.Fix(h, 0)
	}
}

func dotProduct(a, b []float32) float32 {
	var sum float32 = 0
	for i := 0; i < DIM; i += 8 {
		sum += a[i]*b[i] +
			a[i+1]*b[i+1] +
			a[i+2]*b[i+2] +
			a[i+3]*b[i+3] +
			a[i+4]*b[i+4] +
			a[i+5]*b[i+5] +
			a[i+6]*b[i+6] +
			a[i+7]*b[i+7]
	}
	return sum
}

func l2Norm(v []float32) float32 {
	var sum float32 = 0
	for i := 0; i < DIM; i++ {
		sum += v[i] * v[i]
	}
	return float32(math.Sqrt(float64(sum)))
}

type TopHitOut struct {
	ID       string  `json:"id"`
	Distance float64 `json:"distance"`
}

type BenchResult struct {
	Language          string      `json:"language"`
	Mode              string      `json:"mode"`
	RowsIndexed       int         `json:"rows_indexed"`
	EmbeddingDim      int         `json:"embedding_dim"`
	RssStartMb        float64     `json:"rss_start_mb"`
	RssAfterLoadMb    float64     `json:"rss_after_load_mb"`
	RssEndMb          float64     `json:"rss_end_mb"`
	LoadTimeMs        float64     `json:"load_time_ms"`
	ColdFirstQueryMs  float64     `json:"cold_first_query_ms"`
	TotalColdStartMs  float64     `json:"total_cold_start_ms"`
	WarmP50Ms         float64     `json:"warm_p50_ms"`
	WarmP95Ms         float64     `json:"warm_p95_ms"`
	WarmP99Ms         float64     `json:"warm_p99_ms"`
	WarmMinMs         float64     `json:"warm_min_ms"`
	WarmMaxMs         float64     `json:"warm_max_ms"`
	WarmParallelP50Ms float64     `json:"warm_parallel_p50_ms"`
	WarmParallelP95Ms float64     `json:"warm_parallel_p95_ms"`
	FilteredP50Ms     float64     `json:"filtered_p50_ms"`
	FilteredP95Ms     float64     `json:"filtered_p95_ms"`
	Top10             []TopHitOut `json:"top_10"`
}

func percentile(sorted []float64, p float64) float64 {
	idx := int(math.Floor((p / 100.0) * float64(len(sorted))))
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}

func round6(v float64) float64 {
	return math.Round(v*1000000) / 1000000
}

func round2(v float64) float64 {
	return math.Round(v*100) / 100
}

func main() {
	mode := "drawers"
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}

	rssStart := getRssMb()
	tStart := time.Now()

	dbPath := "file:C:/Users/igorl/.mempalace/palace/sqlite_exact.sqlite3?mode=ro"
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		panic(err)
	}
	defer db.Close()

	// Sample query vector
	var sampleBlob []byte
	err = db.QueryRow("SELECT embedding FROM documents WHERE id = 'drawer_44fb808c93188a039e5ce4ef712ebe0a'").Scan(&sampleBlob)
	if err != nil {
		panic(err)
	}
	queryVec := unsafe.Slice((*float32)(unsafe.Pointer(&sampleBlob[0])), DIM)
	qNorm := l2Norm(queryVec)

	// Load vectors
	tLoadStart := time.Now()
	querySql := "SELECT collection_id, id, embedding, COALESCE(wing, '') FROM documents WHERE collection_id = 1 ORDER BY rowid"
	capHint := 170000
	if mode == "all" {
		querySql = "SELECT collection_id, id, embedding, COALESCE(wing, '') FROM documents ORDER BY rowid"
		capHint = 350000
	}

	rows, err := db.Query(querySql)
	if err != nil {
		panic(err)
	}
	defer rows.Close()

	cids := make([]int64, 0, capHint)
	ids := make([]string, 0, capHint)
	wingIds := make([]uint16, 0, capHint)
	flatEmbeddings := make([]float32, 0, capHint*DIM)
	norms := make([]float32, 0, capHint)

	wingMap := make(map[string]uint16)
	wingMap[""] = 0

	var (
		cid  int64
		id   string
		blob []byte
		wing string
	)

	for rows.Next() {
		if err := rows.Scan(&cid, &id, &blob, &wing); err != nil {
			panic(err)
		}
		if len(blob) != DIM*4 {
			continue
		}

		wid := uint16(0)
		if wing != "" {
			var ok bool
			wid, ok = wingMap[wing]
			if !ok {
				wid = uint16(len(wingMap))
				wingMap[wing] = wid
			}
		}

		f32Slice := unsafe.Slice((*float32)(unsafe.Pointer(&blob[0])), DIM)
		norm := l2Norm(f32Slice)
		norms = append(norms, norm)
		flatEmbeddings = append(flatEmbeddings, f32Slice...)
		cids = append(cids, cid)
		ids = append(ids, id)
		wingIds = append(wingIds, wid)
	}

	loadTimeMs := float64(time.Since(tLoadStart).Microseconds()) / 1000.0
	runtime.GC()
	debug.FreeOSMemory()
	rssAfterLoad := getRssMb()
	nDocs := len(ids)

	targetWingID := wingMap["claude_conversations_windows"]

	searchSingle := func(k int, filterWid uint16) []Candidate {
		h := make(CandidateHeap, 0, k)
		heap.Init(&h)
		for i := 0; i < nDocs; i++ {
			if filterWid != 0 && wingIds[i] != filterWid {
				continue
			}
			offset := i * DIM
			vecSlice := flatEmbeddings[offset : offset+DIM]
			dot := dotProduct(vecSlice, queryVec)
			denom := norms[i] * qNorm
			cos := float32(0)
			if denom > 0 {
				cos = dot / denom
			}
			if cos > 1.0 {
				cos = 1.0
			}
			if cos < -1.0 {
				cos = -1.0
			}
			dist := 1.0 - cos
			pushBounded(&h, k, Candidate{idIdx: i, distance: dist})
		}
		res := make([]Candidate, h.Len())
		for idx := len(res) - 1; idx >= 0; idx-- {
			res[idx] = heap.Pop(&h).(Candidate)
		}
		return res
	}

	numWorkers := runtime.NumCPU()
	searchParallel := func(k int, filterWid uint16) []Candidate {
		chunkSize := (nDocs + numWorkers - 1) / numWorkers
		var wg sync.WaitGroup
		localHeaps := make([]CandidateHeap, numWorkers)

		for w := 0; w < numWorkers; w++ {
			start := w * chunkSize
			end := start + chunkSize
			if end > nDocs {
				end = nDocs
			}
			if start >= end {
				continue
			}
			wg.Add(1)
			go func(workerIdx, s, e int) {
				defer wg.Done()
				lh := make(CandidateHeap, 0, k)
				heap.Init(&lh)
				for i := s; i < e; i++ {
					if filterWid != 0 && wingIds[i] != filterWid {
						continue
					}
					offset := i * DIM
					vecSlice := flatEmbeddings[offset : offset+DIM]
					dot := dotProduct(vecSlice, queryVec)
					denom := norms[i] * qNorm
					cos := float32(0)
					if denom > 0 {
						cos = dot / denom
					}
					if cos > 1.0 {
						cos = 1.0
					}
					if cos < -1.0 {
						cos = -1.0
					}
					dist := 1.0 - cos
					pushBounded(&lh, k, Candidate{idIdx: i, distance: dist})
				}
				localHeaps[workerIdx] = lh
			}(w, start, end)
		}
		wg.Wait()

		finalH := make(CandidateHeap, 0, k)
		heap.Init(&finalH)
		for _, lh := range localHeaps {
			for _, item := range lh {
				pushBounded(&finalH, k, item)
			}
		}
		res := make([]Candidate, finalH.Len())
		for idx := len(res) - 1; idx >= 0; idx-- {
			res[idx] = heap.Pop(&finalH).(Candidate)
		}
		return res
	}

	hydrateStmt, err := db.Prepare("SELECT id, document, metadata_json FROM documents WHERE collection_id = ? AND id = ?")
	if err != nil {
		panic(err)
	}
	defer hydrateStmt.Close()

	hydrate := func(hits []Candidate) {
		var dID, doc, meta string
		for _, h := range hits {
			_ = hydrateStmt.QueryRow(cids[h.idIdx], ids[h.idIdx]).Scan(&dID, &doc, &meta)
		}
	}

	tColdQStart := time.Now()
	firstHits := searchSingle(10, 0)
	hydrate(firstHits)
	coldFirstQueryMs := float64(time.Since(tColdQStart).Microseconds()) / 1000.0
	totalColdMs := float64(time.Since(tStart).Microseconds()) / 1000.0

	// Warm-up iterations (3 runs not recorded)
	for w := 0; w < 3; w++ {
		hydrate(searchSingle(10, 0))
		hydrate(searchParallel(10, 0))
		hydrate(searchSingle(10, targetWingID))
	}

	warmLatencies := make([]float64, 25)
	var warmHits []Candidate
	for iter := 0; iter < 25; iter++ {
		t0 := time.Now()
		warmHits = searchSingle(10, 0)
		hydrate(warmHits)
		warmLatencies[iter] = float64(time.Since(t0).Microseconds()) / 1000.0
	}

	parallelLatencies := make([]float64, 25)
	for iter := 0; iter < 25; iter++ {
		t0 := time.Now()
		pHits := searchParallel(10, 0)
		hydrate(pHits)
		parallelLatencies[iter] = float64(time.Since(t0).Microseconds()) / 1000.0
	}

	filteredLatencies := make([]float64, 25)
	for iter := 0; iter < 25; iter++ {
		t0 := time.Now()
		fHits := searchSingle(10, targetWingID)
		hydrate(fHits)
		filteredLatencies[iter] = float64(time.Since(t0).Microseconds()) / 1000.0
	}

	sort.Float64s(warmLatencies)
	sort.Float64s(parallelLatencies)
	sort.Float64s(filteredLatencies)

	runtime.GC()
	debug.FreeOSMemory()
	rssEnd := getRssMb()
	runtime.KeepAlive(flatEmbeddings)
	runtime.KeepAlive(cids)
	runtime.KeepAlive(ids)
	runtime.KeepAlive(norms)
	runtime.KeepAlive(wingIds)

	top10 := make([]TopHitOut, len(warmHits))
	for i, c := range warmHits {
		top10[i] = TopHitOut{
			ID:       ids[c.idIdx],
			Distance: round6(float64(c.distance)),
		}
	}

	result := BenchResult{
		Language:          fmt.Sprintf("Go (%s + modernc.org/sqlite)", runtime.Version()),
		Mode:              mode,
		RowsIndexed:       nDocs,
		EmbeddingDim:      DIM,
		RssStartMb:        round2(rssStart),
		RssAfterLoadMb:    round2(rssAfterLoad),
		RssEndMb:          round2(rssEnd),
		LoadTimeMs:        round2(loadTimeMs),
		ColdFirstQueryMs:  round2(coldFirstQueryMs),
		TotalColdStartMs:  round2(totalColdMs),
		WarmP50Ms:         round2(percentile(warmLatencies, 50.0)),
		WarmP95Ms:         round2(percentile(warmLatencies, 95.0)),
		WarmP99Ms:         round2(percentile(warmLatencies, 99.0)),
		WarmMinMs:         round2(warmLatencies[0]),
		WarmMaxMs:         round2(warmLatencies[len(warmLatencies)-1]),
		WarmParallelP50Ms: round2(percentile(parallelLatencies, 50.0)),
		WarmParallelP95Ms: round2(percentile(parallelLatencies, 95.0)),
		FilteredP50Ms:     round2(percentile(filteredLatencies, 50.0)),
		FilteredP95Ms:     round2(percentile(filteredLatencies, 95.0)),
		Top10:             top10,
	}

	out, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		panic(err)
	}
	fmt.Println(string(out))
}
