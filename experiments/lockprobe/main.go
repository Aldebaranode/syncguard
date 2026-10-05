// Command lockprobe makes one extra state-lock acquisition against a live
// priv_validator_state.json while syncguard holds the lock, and prints the
// outcome as JSON. It is the second acquisition of the lock negative test.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
)

// main parses -state, runs the probe, and prints its result; the exit code is
// 0 only when the acquisition was refused.
func main() {
	statePath := flag.String("state", "", "path to priv_validator_state.json")
	flag.Parse()
	if *statePath == "" {
		fmt.Fprintln(os.Stderr, "usage: lockprobe -state <path to priv_validator_state.json>")
		os.Exit(2)
	}
	result := probe(*statePath)
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		os.Exit(1)
	}
	if result.Outcome != outcomeRefused {
		os.Exit(1)
	}
}
