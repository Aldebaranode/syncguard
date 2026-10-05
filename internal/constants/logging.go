package constants

// LogTimestampFormat renders log timestamps with millisecond resolution so
// failover timings (detection, takeover, failback) can be measured below one
// second.
const LogTimestampFormat = "2006-01-02 15:04:05.000"
