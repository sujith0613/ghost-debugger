FROM golang:1.25-alpine AS builder

WORKDIR /app

RUN apk add --no-cache git

COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -buildvcs=false -o /failure_injector ./test_services/failure_injector

FROM alpine:3.21

WORKDIR /app

RUN apk --no-cache add ca-certificates

COPY --from=builder /failure_injector /app/failure_injector

EXPOSE 8099

CMD ["/app/failure_injector"]
