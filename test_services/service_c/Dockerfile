FROM golang:1.25-alpine AS builder

WORKDIR /app

RUN apk add --no-cache git

COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -buildvcs=false -o /service_c ./test_services/service_c

FROM alpine:3.21

WORKDIR /app

RUN apk --no-cache add ca-certificates

COPY --from=builder /service_c /app/service_c

EXPOSE 8083

CMD ["/app/service_c"]
