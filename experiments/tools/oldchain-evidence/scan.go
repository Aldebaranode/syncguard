package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/cometbft/cometbft/types"
)

// BlockSource is the part of the CometBFT block store the scan reads.
type BlockSource interface {
	Base() int64
	Height() int64
	LoadBlock(height int64) *types.Block
}

// EvidenceAt names one stored height whose block carries evidence.
type EvidenceAt struct {
	Height     int64    `json:"height"`
	Time       string   `json:"time"`
	Types      []string `json:"types"`
	Validators []string `json:"validators"`
}

// Report is the result of one pass over every stored height.
type Report struct {
	Base           int64        `json:"base"`
	Top            int64        `json:"top"`
	Loaded         int64        `json:"loaded"`
	Unreadable     []int64      `json:"unreadable"`
	FirstTime      string       `json:"first_time"`
	LastTime       string       `json:"last_time"`
	EvidenceBlocks []EvidenceAt `json:"evidence_blocks"`
}

// HeightSigners is the commit for one height as the next block stores it.
type HeightSigners struct {
	Height       int64    `json:"height"`
	Time         string   `json:"time"`
	CommitStored bool     `json:"commit_stored"`
	Committed    []string `json:"committed"`
	Nil          []string `json:"nil"`
	Absent       int      `json:"absent"`
}

// Scan reads every height from the store's base to its top and records the blocks that carry evidence.
func Scan(src BlockSource) Report {
	report := Report{Base: src.Base(), Top: src.Height(), Unreadable: []int64{}, EvidenceBlocks: []EvidenceAt{}}
	for height := report.Base; height <= report.Top; height++ {
		block := src.LoadBlock(height)
		if block == nil {
			report.Unreadable = append(report.Unreadable, height)
			continue
		}
		report.Loaded++
		blockTime := formatTime(block.Header.Time)
		if report.FirstTime == "" {
			report.FirstTime = blockTime
		}
		report.LastTime = blockTime
		if len(block.Evidence.Evidence) > 0 {
			report.EvidenceBlocks = append(report.EvidenceBlocks, evidenceAt(height, blockTime, block.Evidence.Evidence))
		}
	}
	return report
}

// Signers lists, for each height from first to last, the validators whose commit signature the next block stores.
func Signers(src BlockSource, first, last int64) []HeightSigners {
	rows := []HeightSigners{}
	for height := first; height <= last; height++ {
		row := HeightSigners{Height: height, Committed: []string{}, Nil: []string{}}
		if block := src.LoadBlock(height); block != nil {
			row.Time = formatTime(block.Header.Time)
		}
		next := src.LoadBlock(height + 1)
		if next == nil || next.LastCommit == nil {
			rows = append(rows, row)
			continue
		}
		row.CommitStored = true
		for _, sig := range next.LastCommit.Signatures {
			switch sig.BlockIDFlag {
			case types.BlockIDFlagCommit:
				row.Committed = append(row.Committed, shortAddress(sig.ValidatorAddress))
			case types.BlockIDFlagNil:
				row.Nil = append(row.Nil, shortAddress(sig.ValidatorAddress))
			default:
				row.Absent++
			}
		}
		rows = append(rows, row)
	}
	return rows
}

// evidenceAt describes the evidence items of one block by type and accused validator.
func evidenceAt(height int64, blockTime string, items types.EvidenceList) EvidenceAt {
	entry := EvidenceAt{Height: height, Time: blockTime, Types: []string{}, Validators: []string{}}
	for _, item := range items {
		switch ev := item.(type) {
		case *types.DuplicateVoteEvidence:
			entry.Types = append(entry.Types, "DuplicateVoteEvidence")
			entry.Validators = append(entry.Validators, strings.ToUpper(ev.VoteA.ValidatorAddress.String()))
		case *types.LightClientAttackEvidence:
			entry.Types = append(entry.Types, "LightClientAttackEvidence")
		default:
			entry.Types = append(entry.Types, fmt.Sprintf("%T", item))
		}
	}
	return entry
}

// shortAddress gives the first four bytes of a validator address in upper-case hex, the form the thesis uses.
func shortAddress(address []byte) string {
	if len(address) > 4 {
		address = address[:4]
	}
	return strings.ToUpper(fmt.Sprintf("%x", address))
}

// formatTime renders a block time in UTC with second precision.
func formatTime(t time.Time) string {
	return t.UTC().Format(time.RFC3339)
}
