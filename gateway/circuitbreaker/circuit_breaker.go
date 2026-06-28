package circuitbreaker

import (
	"sync"
	"time"
)

type State int

const (
	StateClosed   State = 0
	StateOpen     State = 1
	StateHalfOpen State = 2
)

func (s State) String() string {
	switch s {
	case StateClosed:
		return "closed"
	case StateOpen:
		return "open"
	case StateHalfOpen:
		return "half-open"
	default:
		return "unknown"
	}
}

type CircuitBreaker struct {
	mu             sync.Mutex
	state          State
	failureCount   int
	failureLimit   int
	timeout        time.Duration
	lastFailureAt  time.Time
	halfOpenSentAt time.Time
	halfOpenSent   bool
}

func NewCircuitBreaker(failureLimit int, timeout time.Duration) *CircuitBreaker {
	return &CircuitBreaker{
		state:        StateClosed,
		failureLimit: failureLimit,
		timeout:      timeout,
	}
}

func (cb *CircuitBreaker) State() State {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	return cb.state
}

func (cb *CircuitBreaker) Allow() bool {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	switch cb.state {
	case StateClosed:
		return true
	case StateOpen:
		if time.Since(cb.lastFailureAt) >= cb.timeout {
			cb.state = StateHalfOpen
			cb.halfOpenSent = false
			return true
		}
		return false
	case StateHalfOpen:
		if !cb.halfOpenSent {
			cb.halfOpenSent = true
			cb.halfOpenSentAt = time.Now()
			return true
		}
		return false
	default:
		return false
	}
}

func (cb *CircuitBreaker) Success() {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	if cb.state == StateHalfOpen {
		cb.state = StateClosed
		cb.failureCount = 0
		cb.halfOpenSent = false
	}
}

func (cb *CircuitBreaker) Failure() {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	cb.failureCount++
	cb.lastFailureAt = time.Now()

	if cb.state == StateHalfOpen {
		cb.state = StateOpen
		cb.halfOpenSent = false
		return
	}

	if cb.failureCount >= cb.failureLimit {
		cb.state = StateOpen
	}
}

func (cb *CircuitBreaker) Reset() {
	cb.mu.Lock()
	defer cb.mu.Unlock()

	cb.state = StateClosed
	cb.failureCount = 0
	cb.halfOpenSent = false
}
