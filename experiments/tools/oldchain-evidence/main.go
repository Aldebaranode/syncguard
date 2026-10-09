// Command oldchain-evidence reads an archived CometBFT blockstore offline, read-only, and prints
// the evidence each stored block carries plus the commit signers of a chosen height range.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	dbm "github.com/cometbft/cometbft-db"
	"github.com/cometbft/cometbft/store"
	"github.com/syndtr/goleveldb/leveldb/opt"
)

// output is the JSON document the command prints.
type output struct {
	DataDir string          `json:"data_dir"`
	Scan    Report          `json:"scan"`
	Signers []HeightSigners `json:"signers"`
}

// main opens the blockstore named by -dir and prints the scan and the requested signer rows as JSON.
func main() {
	dataDir := flag.String("dir", "", "directory that contains blockstore.db")
	signersFrom := flag.Int64("signers-from", 0, "first height whose commit signers are listed (0 lists none)")
	signersTo := flag.Int64("signers-to", 0, "last height whose commit signers are listed")
	flag.Parse()
	if *dataDir == "" {
		fmt.Fprintln(os.Stderr, "usage: oldchain-evidence -dir <data dir> [-signers-from h -signers-to h]")
		os.Exit(2)
	}

	db, err := dbm.NewGoLevelDBWithOpts("blockstore", *dataDir, &opt.Options{ReadOnly: true})
	if err != nil {
		fmt.Fprintln(os.Stderr, "open blockstore:", err)
		os.Exit(1)
	}
	defer db.Close()
	blocks := store.NewBlockStore(db)

	result := output{DataDir: *dataDir, Scan: Scan(blocks), Signers: []HeightSigners{}}
	if *signersFrom > 0 {
		result.Signers = Signers(blocks, *signersFrom, *signersTo)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, "write json:", err)
		os.Exit(1)
	}
}
