.PHONY: build run test watch clean

build:
	@mkdir -p bin
	go build -o bin/syncguard ./cli

run: build
	./bin/syncguard --config config.yaml

test:
	go test -v ./internal/...

watch:
	~/go/bin/air

clean:
	rm -rf bin/ coverage.out
