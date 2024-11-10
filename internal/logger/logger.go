package logger

import (
	"fmt"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/aldebaranode/syncguard/internal/config"
	log "github.com/sirupsen/logrus"
)

// getModuleInfo retrieves the module (file and function) information.
func getModuleInfo() string {
	pc, file, line, ok := runtime.Caller(3) // Adjust depth as needed
	if !ok {
		return "unknown:0 [unknown]"
	}
	fn := runtime.FuncForPC(pc).Name()
	filename := filepath.Base(file)
	module := strings.Split(fn, "/")

	return fmt.Sprintf("%s:%d [%s]", filename, line, module[len(module)-1])
}

func WithConfig(cfg *config.Config, module string) *log.Entry {
	logger := log.WithFields(log.Fields{
		"node": cfg.Server.ID,
	})
	return withModule(logger, module)
}

func withModule(logger *log.Entry, module string) *log.Entry {
	return logger.WithFields(log.Fields{
		"module": module,
	})
}
