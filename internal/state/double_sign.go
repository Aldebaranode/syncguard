package state

import (
	"fmt"
	"sync"
	"time"
)

// SignatureRecord tracks what we've signed to prevent double-signing
type SignatureRecord struct {
	Height    int64
	Round     int32
	Step      int8
	Timestamp time.Time
}

// DoubleSignProtector prevents double-signing by tracking signed blocks
type DoubleSignProtector struct {
	mu              sync.RWMutex
	signedRecords   map[string]*SignatureRecord
	lastSignedBlock int64
	maxRecords      int
	pruneInterval   time.Duration
	stopCh          chan struct{}
}

// NewDoubleSignProtector creates a new double-sign prevention mechanism
func NewDoubleSignProtector() *DoubleSignProtector {
	dsp := &DoubleSignProtector{
		signedRecords: make(map[string]*SignatureRecord),
		maxRecords:    10000,
		pruneInterval: 5 * time.Minute,
		stopCh:        make(chan struct{}),
	}

	go dsp.pruneOldRecords()

	return dsp
}

// CanSign checks if it's safe to sign at the given height/round/step
func (dsp *DoubleSignProtector) CanSign(height int64, round int32, step int8) (bool, error) {
	dsp.mu.RLock()
	defer dsp.mu.RUnlock()

	key := fmt.Sprintf("%d:%d:%d", height, round, step)
	if record, exists := dsp.signedRecords[key]; exists {
		return false, fmt.Errorf("already signed at height %d, round %d, step %d at %v",
			height, round, step, record.Timestamp)
	}

	if height < dsp.lastSignedBlock {
		return false, fmt.Errorf("attempting to sign height %d but already signed %d",
			height, dsp.lastSignedBlock)
	}

	for _, record := range dsp.signedRecords {
		if record.Height == height {
			if record.Round == round && record.Step != step {
				if !isValidStepProgression(record.Step, step) {
					return false, fmt.Errorf("invalid step progression at height %d, round %d: %d -> %d",
						height, round, record.Step, step)
				}
			}
		}
	}

	return true, nil
}

// RecordSignature records that we've signed at a given height/round/step
func (dsp *DoubleSignProtector) RecordSignature(height int64, round int32, step int8) error {
	dsp.mu.Lock()
	defer dsp.mu.Unlock()

	key := fmt.Sprintf("%d:%d:%d", height, round, step)
	if _, exists := dsp.signedRecords[key]; exists {
		return fmt.Errorf("signature already recorded for %s", key)
	}

	dsp.signedRecords[key] = &SignatureRecord{
		Height:    height,
		Round:     round,
		Step:      step,
		Timestamp: time.Now(),
	}

	if height > dsp.lastSignedBlock {
		dsp.lastSignedBlock = height
	}

	if len(dsp.signedRecords) > dsp.maxRecords {
		dsp.pruneOldRecordsLocked()
	}

	return nil
}

// isValidStepProgression checks if step transition is valid
func isValidStepProgression(oldStep, newStep int8) bool {
	return newStep > oldStep
}

// pruneOldRecords periodically removes old signature records
func (dsp *DoubleSignProtector) pruneOldRecords() {
	ticker := time.NewTicker(dsp.pruneInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			dsp.mu.Lock()
			dsp.pruneOldRecordsLocked()
			dsp.mu.Unlock()
		case <-dsp.stopCh:
			return
		}
	}
}

// pruneOldRecordsLocked removes records older than the retention window
func (dsp *DoubleSignProtector) pruneOldRecordsLocked() {
	if len(dsp.signedRecords) <= dsp.maxRecords/2 {
		return
	}

	minHeight := dsp.lastSignedBlock - 1000
	if minHeight < 0 {
		minHeight = 0
	}

	for key, record := range dsp.signedRecords {
		if record.Height < minHeight {
			delete(dsp.signedRecords, key)
		}
	}
}

// GetLastSignedHeight returns the last height we signed
func (dsp *DoubleSignProtector) GetLastSignedHeight() int64 {
	dsp.mu.RLock()
	defer dsp.mu.RUnlock()
	return dsp.lastSignedBlock
}

// Stop stops the double-sign protector
func (dsp *DoubleSignProtector) Stop() {
	close(dsp.stopCh)
}
