package constants

import "fmt"

type NodeStatus string

const (
	NodeStatusActive  NodeStatus = "active"
	NodeStatusPassive NodeStatus = "passive"
)

// String implements pflag.Value
func (n *NodeStatus) String() string {
	return string(*n)
}

// Set implements pflag.Value
func (n *NodeStatus) Set(value string) error {
	switch value {
	case "active", "passive", "":
		*n = NodeStatus(value)
		return nil
	default:
		return fmt.Errorf("must be 'active' or 'passive', got '%s'", value)
	}
}

// Type implements pflag.Value
func (n *NodeStatus) Type() string {
	return "NodeStatus"
}
