FROM golang:1.25-alpine AS builder

WORKDIR /app

RUN apk add --no-cache git

COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -buildvcs=false -o /service_b ./test_services/service_b

FROM alpine:3.21

WORKDIR /app

RUN apk --no-cache add ca-certificates

COPY --from=builder /service_b /app/service_b

EXPOSE 8082

CMD ["/app/service_b"]
