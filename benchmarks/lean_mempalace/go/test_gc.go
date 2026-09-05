package main

import (
	"database/sql"
	"fmt"
	"math"
	"runtime"
	"syscall"
	"time"
	"unsafe"

	_ "modernc.org/sqlite"
)

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

func main() {
	db, err := sql.Open("sqlite", "file:C:/Users/igorl/.mempalace/palace/sqlite_exact.sqlite3?mode=ro")
	if err != nil {
		panic(err)
	}
	defer db.Close()

	t0 := time.Now()
	rows, err := db.Query("SELECT id, embedding, COALESCE(wing, '') FROM documents WHERE collection_id = 1 ORDER BY rowid")
	if err != nil {
		panic(err)
	}
	defer rows.Close()

	ids := make([]string, 0, 170000)
	wings := make([]string, 0, 170000)
	flatEmbeddings := make([]float32, 0, 170000*384)
	norms := make([]float32, 0, 170000)

	var (
		id   string
		blob []byte
		wing string
	)

	for rows.Next() {
		if err := rows.Scan(&id, &blob, &wing); err != nil {
			panic(err)
		}
		if len(blob) != 384*4 {
			continue
		}
		f32Slice := unsafe.Slice((*float32)(unsafe.Pointer(&blob[0])), 384)
		var sum float32
		for _, x := range f32Slice {
			sum += x * x
		}
		norms = append(norms, float32(math.Sqrt(float64(sum))))
		flatEmbeddings = append(flatEmbeddings, f32Slice...)
		ids = append(ids, id)
		wings = append(wings, wing)
	}
	t1 := time.Now()
	fmt.Printf("Go load time: %.2f ms\n", float64(t1.Sub(t0).Microseconds())/1000.0)
	fmt.Printf("RSS before GC: %.2f MB\n", getRssMb())
	runtime.GC()
	fmt.Printf("RSS after GC: %.2f MB\n", getRssMb())
}
