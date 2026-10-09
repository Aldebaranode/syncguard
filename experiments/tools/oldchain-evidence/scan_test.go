package main

import (
	"reflect"
	"testing"
	"time"

	"github.com/cometbft/cometbft/types"
)

// fakeStore serves blocks from a map so the scan can be tested without a database.
type fakeStore struct {
	base, top int64
	blocks    map[int64]*types.Block
}

// Base returns the lowest stored height.
func (s fakeStore) Base() int64 { return s.base }

// Height returns the highest stored height.
func (s fakeStore) Height() int64 { return s.top }

// LoadBlock returns the stored block or nil when the height has none.
func (s fakeStore) LoadBlock(height int64) *types.Block { return s.blocks[height] }

var (
	baseTime = time.Date(2026, 6, 18, 12, 0, 0, 0, time.UTC)
	addrA    = []byte{0x48, 0xDC, 0x21, 0x83}
	addrB    = []byte{0x7E, 0xFA, 0xEC, 0xCD}
)

// block builds a block at height with a header time one second per height and the given last commit and evidence.
func block(t *testing.T, height int64, lastCommit *types.Commit, evidence []types.Evidence) *types.Block {
	t.Helper()
	b := types.MakeBlock(height, nil, lastCommit, evidence)
	b.Header.Time = baseTime.Add(time.Duration(height) * time.Second)
	return b
}

// plainStore holds blocks 1 to top with no evidence and empty last commits.
func plainStore(t *testing.T, top int64) fakeStore {
	t.Helper()
	blocks := map[int64]*types.Block{}
	for h := int64(1); h <= top; h++ {
		blocks[h] = block(t, h, &types.Commit{Height: h - 1}, nil)
	}
	return fakeStore{base: 1, top: top, blocks: blocks}
}

func TestScanReportsTheRangeAndTheBlocksWithEvidence(t *testing.T) {
	store := plainStore(t, 5)
	ev, err := types.NewMockDuplicateVoteEvidence(3, baseTime, "story-localnet")
	if err != nil {
		t.Fatal(err)
	}
	store.blocks[3] = block(t, 3, &types.Commit{Height: 2}, []types.Evidence{ev})

	report := Scan(store)

	if report.Base != 1 || report.Top != 5 || report.Loaded != 5 {
		t.Fatalf("range: got base %d top %d loaded %d", report.Base, report.Top, report.Loaded)
	}
	if report.FirstTime != "2026-06-18T12:00:01Z" || report.LastTime != "2026-06-18T12:00:05Z" {
		t.Fatalf("times: got %s to %s", report.FirstTime, report.LastTime)
	}
	if len(report.EvidenceBlocks) != 1 {
		t.Fatalf("evidence blocks: got %+v", report.EvidenceBlocks)
	}
	got := report.EvidenceBlocks[0]
	if got.Height != 3 || !reflect.DeepEqual(got.Types, []string{"DuplicateVoteEvidence"}) || len(got.Validators) != 1 {
		t.Fatalf("evidence block: got %+v", got)
	}
}

func TestScanListsHeightsWhoseBlockCannotBeLoaded(t *testing.T) {
	store := plainStore(t, 5)
	delete(store.blocks, 4)

	report := Scan(store)

	if report.Loaded != 4 || !reflect.DeepEqual(report.Unreadable, []int64{4}) {
		t.Fatalf("got loaded %d unreadable %v", report.Loaded, report.Unreadable)
	}
	if len(report.EvidenceBlocks) != 0 {
		t.Fatalf("expected no evidence, got %+v", report.EvidenceBlocks)
	}
}

func TestSignersReadsTheCommitStoredInTheNextBlock(t *testing.T) {
	store := plainStore(t, 4)
	store.blocks[3] = block(t, 3, &types.Commit{Height: 2, Signatures: []types.CommitSig{
		{BlockIDFlag: types.BlockIDFlagCommit, ValidatorAddress: addrA, Timestamp: baseTime, Signature: []byte{1}},
		{BlockIDFlag: types.BlockIDFlagNil, ValidatorAddress: addrB, Timestamp: baseTime, Signature: []byte{1}},
		{BlockIDFlag: types.BlockIDFlagAbsent},
	}}, nil)

	got := Signers(store, 2, 2)

	want := []HeightSigners{{
		Height:       2,
		Time:         "2026-06-18T12:00:02Z",
		CommitStored: true,
		Committed:    []string{"48DC2183"},
		Nil:          []string{"7EFAECCD"},
		Absent:       1,
	}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v want %+v", got, want)
	}
}

func TestSignersMarksTheTopHeightAsHavingNoStoredCommit(t *testing.T) {
	store := plainStore(t, 4)

	got := Signers(store, 4, 4)

	if len(got) != 1 || got[0].CommitStored || got[0].Height != 4 {
		t.Fatalf("got %+v", got)
	}
}
