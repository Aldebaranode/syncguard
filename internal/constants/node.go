package constants

import "fmt"

type NodeStatus string
type NodeManagerType string

const (
	NodeStatusActive  NodeStatus = "active"
	NodeStatusPassive NodeStatus = "passive"

	NodeManagerTypeBinary        NodeManagerType = "binary"
	NodeManagerTypeDocker        NodeManagerType = "docker"
	NodeManagerTypeDockerCompose NodeManagerType = "docker-compose"
)

func (n *NodeStatus) String() string {
	return string(*n)
}

func (n *NodeStatus) Set(value string) error {
	switch value {
	case "active", "passive", "":
		*n = NodeStatus(value)
		return nil
	default:
		return fmt.Errorf("must be 'active' or 'passive', got '%s'", value)
	}
}
func (n *NodeStatus) Type() string {
	return "NodeStatus"
}
