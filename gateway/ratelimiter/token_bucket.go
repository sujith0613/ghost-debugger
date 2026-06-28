package ratelimiter

import (
	"sync"
	"time"
)

type TokenBucket struct {
	mu         sync.Mutex
	capacity   int
	tokens     int
	refillRate int
	refillPer  time.Duration
	lastRefill time.Time
}

func NewTokenBucket(capacity, refillRate int, refillPer time.Duration) *TokenBucket {
	return &TokenBucket{
		capacity:   capacity,
		tokens:     capacity,
		refillRate: refillRate,
		refillPer:  refillPer,
		lastRefill: time.Now(),
	}
}

func (b *TokenBucket) refill() {
	elapsed := time.Since(b.lastRefill)
	refillCount := int(elapsed / b.refillPer) * b.refillRate
	if refillCount > 0 {
		b.tokens = min(b.capacity, b.tokens+refillCount)
		b.lastRefill = b.lastRefill.Add(time.Duration(refillCount/b.refillRate) * b.refillPer)
	}
}

func (b *TokenBucket) Allow() bool {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.refill()
	if b.tokens > 0 {
		b.tokens--
		return true
	}
	return false
}

func (b *TokenBucket) AllowN(n int) bool {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.refill()
	if b.tokens >= n {
		b.tokens -= n
		return true
	}
	return false
}

func (b *TokenBucket) Available() int {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.refill()
	return b.tokens
}

type PerServiceLimiter struct {
	mu       sync.RWMutex
	buckets  map[string]*TokenBucket
	capacity int
	rate     int
	per      time.Duration
}

func NewPerServiceLimiter(capacity, rate int, per time.Duration) *PerServiceLimiter {
	return &PerServiceLimiter{
		buckets:  make(map[string]*TokenBucket),
		capacity: capacity,
		rate:     rate,
		per:      per,
	}
}

func (l *PerServiceLimiter) getBucket(service string) *TokenBucket {
	l.mu.RLock()
	b, ok := l.buckets[service]
	l.mu.RUnlock()
	if ok {
		return b
	}

	l.mu.Lock()
	defer l.mu.Unlock()

	if b, ok = l.buckets[service]; ok {
		return b
	}

	b = NewTokenBucket(l.capacity, l.rate, l.per)
	l.buckets[service] = b
	return b
}

func (l *PerServiceLimiter) Allow(service string) bool {
	return l.getBucket(service).Allow()
}

func (l *PerServiceLimiter) Available(service string) int {
	return l.getBucket(service).Available()
}
