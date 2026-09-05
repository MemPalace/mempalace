package main

import (
	"database/sql"
	"fmt"
	"syscall"
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
	fmt.Printf("Initial RSS: %.2f MB\n", getRssMb())
	db, err := sql.Open("sqlite", "file:C:/Users/igorl/.mempalace/palace/sqlite_exact.sqlite3?mode=ro")
	if err != nil {
		panic(err)
	}
	defer db.Close()

	var count int
	err = db.QueryRow("SELECT count(*) FROM documents").Scan(&count)
	if err != nil {
		panic(err)
	}
	fmt.Printf("Total documents from Go: %d\n", count)
	fmt.Printf("RSS after query: %.2f MB\n", getRssMb())
}
