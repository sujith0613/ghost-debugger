package incident

import (
	"sync"
	"sync/atomic"
	"time"
)

type Severity int

const (
	SeverityUnknown Severity = 0
	SeverityLow     Severity = 1
	SeverityMedium  Severity = 2
	SeverityHigh    Severity = 3
	SeverityCritical Severity = 4
)

type Incident struct {
	ID          string
	DetectedAt  time.Time
	Services    []string
	TriggerType string
	Description string
	Severity    Severity
}

type Detector struct {
	mu             sync.RWMutex
	errCounts      map[string]*slidingWindowCounter
	window         time.Duration
	threshold      int
	incidentID     atomic.Int64
}

type slidingWindowCounter struct {
	buckets  [10]int
	interval time.Duration
	lastTick int64
}

func newSlidingWindowCounter(window time.Duration) *slidingWindowCounter {
	return &slidingWindowCounter{
		interval: window / 10,
		lastTick: time.Now().UnixNano(),
	}
}

func (sw *slidingWindowCounter) increment() {
	now := time.Now().UnixNano()
	tick := now / sw.interval.Nanoseconds()
	if tick > sw.lastTick {
		diff := int(tick - sw.lastTick)
		if diff >= len(sw.buckets) {
			for i := range sw.buckets {
				sw.buckets[i] = 0
			}
		} else {
			for i := 1; i <= diff; i++ {
				idx := (int(sw.lastTick) + i) % len(sw.buckets)
				sw.buckets[idx] = 0
			}
		}
		sw.lastTick = tick
	}
	idx := tick % int64(len(sw.buckets))
	sw.buckets[idx]++
}

func (sw *slidingWindowCounter) total() int {
	now := time.Now().UnixNano()
	tick := now / sw.interval.Nanoseconds()
	if tick > sw.lastTick {
		return 0
	}
	sum := 0
	for _, v := range sw.buckets {
		sum += v
	}
	return sum
}

func NewDetector(window time.Duration, threshold int) *Detector {
	return &Detector{
		errCounts: make(map[string]*slidingWindowCounter),
		window:    window,
		threshold: threshold,
	}
}

func (d *Detector) RecordError(service string) {
	d.mu.Lock()
	c, ok := d.errCounts[service]
	if !ok {
		c = newSlidingWindowCounter(d.window)
		d.errCounts[service] = c
	}
	c.increment()
	d.mu.Unlock()
}

func (d *Detector) Check(service string) (*Incident, bool) {
	d.mu.RLock()
	c, ok := d.errCounts[service]
	d.mu.RUnlock()
	if !ok {
		return nil, false
	}

	errCount := c.total()
	if errCount < d.threshold {
		return nil, false
	}

	id := d.incidentID.Add(1)
	return &Incident{
		ID:          service + "-" + time.Now().Format("20060102-150405") + "-" + itoa(int(id)),
		DetectedAt:  time.Now(),
		Services:    []string{service},
		TriggerType: "error_rate",
		Description: itoa(errCount) + " errors in " + d.window.String() + " for " + service,
		Severity:    classifySeverity(errCount),
	}, true
}

func classifySeverity(count int) Severity {
	switch {
	case count >= 100:
		return SeverityCritical
	case count >= 50:
		return SeverityHigh
	case count >= 20:
		return SeverityMedium
	default:
		return SeverityLow
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
