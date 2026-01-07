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

watch-active:
	go run ./cli --config config.yaml --role active

watch-passive:
	go run ./cli --config config-passive.yaml --role passive

clean:
	rm -rf bin/ coverage.out
