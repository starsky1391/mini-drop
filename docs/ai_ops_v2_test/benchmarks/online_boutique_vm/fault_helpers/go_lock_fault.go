// Bounded Go mutex-contention helper used only by the VM benchmark.
package main

import (
	"os"
	"runtime"
	"strconv"
	"sync"
	"time"
)

func main() {
	seconds, workers := 180, 16
	if len(os.Args) > 1 {
		if value, err := strconv.Atoi(os.Args[1]); err == nil && value > 0 && value <= 600 {
			seconds = value
		}
	}
	if len(os.Args) > 2 {
		if value, err := strconv.Atoi(os.Args[2]); err == nil && value >= 2 && value <= 64 {
			workers = value
		}
	}
	var lock sync.Mutex
	lock.Lock()
	for i := 0; i < workers; i++ {
		go func() {
			runtime.LockOSThread()
			lock.Lock()
		}()
	}
	time.Sleep(time.Duration(seconds) * time.Second)
}
