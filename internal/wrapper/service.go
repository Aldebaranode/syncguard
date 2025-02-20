package wrapper

type Program struct {
	isRunning bool
}

func (p *Program) Start() {
	p.isRunning = true
}

func (p *Program) Stop() {
	p.isRunning = false
}
