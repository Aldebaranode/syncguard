package logger

import (
	"fmt"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/aldebaranode/syncguard/internal/config"
	log "github.com/sirupsen/logrus"
)

// Logger is a structured logger with module and caller context.
type Logger struct {
	entry *log.Entry
	cfg   *config.Config
}

// NewLogger creates a new logger instance with the provided configuration.
func NewLogger(cfg *config.Config) *Logger {
	logger := log.WithFields(log.Fields{
		"node": cfg.Node.ID,
	})
	return &Logger{entry: logger, cfg: cfg}
}

// WithModule adds a module field to the logger.
func (l *Logger) WithModule(module string) {
	l.entry = l.entry.WithFields(log.Fields{
		"module": module,
	})
}

// WithCaller adds a caller field to the logger.
func (l *Logger) WithCaller(caller string) {
	l.entry = l.entry.WithFields(log.Fields{
		"v-caller": caller,
	})
}

// Info logs an info-level message with caller context.
func (l *Logger) Info(message string, format ...interface{}) {
	if l.cfg.Logging.Verbose {
		l.WithCaller(getCallerInfo(2))
	}
	if len(format) > 0 {
		l.entry.Infof(message, format...)
	} else {
		l.entry.Info(message)
	}
}

// Warn logs a warning-level message with caller context.
func (l *Logger) Warn(message string, format ...interface{}) {
	if l.cfg.Logging.Verbose {
		l.WithCaller(getCallerInfo(2))
	}
	if len(format) > 0 {
		l.entry.Warnf(message, format...)
	} else {
		l.entry.Warn(message)
	}
}

// Error logs an error-level message with caller context.
func (l *Logger) Error(message string, format ...interface{}) {
	if l.cfg.Logging.Verbose {
		l.WithCaller(getCallerInfo(2))
	}
	if len(format) > 0 {
		l.entry.Errorf(message, format...)
	} else {
		l.entry.Error(message)
	}
}

// getCallerInfo retrieves the file, line, and function of the caller.
func getCallerInfo(depth int) string {
	pc, file, line, ok := runtime.Caller(depth)
	if !ok {
		return "unknown:0 [unknown]"
	}
	fn := runtime.FuncForPC(pc).Name()
	filename := filepath.Base(file)
	module := strings.Split(fn, "/")
	return fmt.Sprintf("%s:%d [%s]", filename, line, module[len(module)-1])
}
