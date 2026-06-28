//go:build ignore

// pingpong/go_client.go
//
// Go gRPC client that calls the Python Ping-Pong server.
// Verifies: Go ↔ Python protobuf compat, gRPC transport, deadlines, error codes.

package main

import (
	"context"
	"fmt"
	"log"
	"time"

	agentpb "github.com/sujithm/ghost-debugger/proto/agent"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

func main() {
	serverAddr := "localhost:50099"

	fmt.Println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
	fmt.Println("  Ghost Debugger — Cross-Language gRPC Test")
	fmt.Println("  Go Client → Python Server")
	fmt.Println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
	fmt.Printf("\nConnecting to Python server at %s...\n", serverAddr)

	conn, err := grpc.NewClient(
		serverAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		log.Fatalf("Failed to connect: %v\nStart with: python pingpong/python_server.py", err)
	}
	defer conn.Close()

	client := agentpb.NewAgentServiceClient(conn)
	fmt.Println("Connected.\n")

	// ── TEST 1: Basic Ping ────────────────────────────────────────
	fmt.Println("TEST 1: Basic Ping")
	fmt.Println("──────────────────")

	ctx1, cancel1 := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel1()

	sentAt := time.Now().UnixMilli()
	req1 := &agentpb.PingRequest{
		Message: "Hello from Go — cross-language gRPC test",
		SentAt:  sentAt,
	}

	resp1, err := client.Ping(ctx1, req1)
	if err != nil {
		log.Fatalf("Ping failed: %v", err)
	}

	receivedAt := time.Now().UnixMilli()
	fmt.Printf("  Sent:            '%s'\n", req1.Message)
	fmt.Printf("  Echoed back:     '%s'\n", resp1.OriginalMessage)
	fmt.Printf("  Server language: %s\n", resp1.ServerLanguage)
	fmt.Printf("  Round-trip (server): %dms\n", resp1.RoundTripMs)
	fmt.Printf("  Round-trip (client): %dms\n", receivedAt-sentAt)

	if resp1.OriginalMessage != req1.Message {
		log.Fatalf("FAIL: echo mismatch")
	}
	if resp1.ServerLanguage != "Python" {
		log.Fatalf("FAIL: expected ServerLanguage='Python'")
	}
	fmt.Println("  ✓ Echo correct, server language: Python\n")

	// ── TEST 2: Multiple Pings ─────────────────────────────────────
	fmt.Println("TEST 2: 5 rapid pings (HTTP/2 connection reuse)")
	fmt.Println("──────────────────────────────────────────────────")

	var totalRTT int64
	for i := 1; i <= 5; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		start := time.Now().UnixMilli()
		resp, err := client.Ping(ctx, &agentpb.PingRequest{
			Message: fmt.Sprintf("Ping #%d", i),
			SentAt:  start,
		})
		cancel()
		if err != nil {
			log.Fatalf("Ping #%d failed: %v", i, err)
		}
		rtt := time.Now().UnixMilli() - start
		totalRTT += rtt
		fmt.Printf("  Ping #%d: rtt=%dms server=%s\n", i, rtt, resp.ServerLanguage)
	}
	fmt.Printf("  Average RTT: %dms\n", totalRTT/5)
	fmt.Println("  ✓ All 5 pings successful\n")

	// ── TEST 3: Deadline enforcement ──────────────────────────────
	fmt.Println("TEST 3: Deadline enforcement (1ns timeout)")
	fmt.Println("──────────────────────────────────────────────────")

	ctxTimeout, cancelTimeout := context.WithTimeout(context.Background(), 1*time.Nanosecond)
	defer cancelTimeout()
	time.Sleep(1 * time.Millisecond)

	_, err = client.Ping(ctxTimeout, &agentpb.PingRequest{
		Message: "should timeout",
		SentAt:  time.Now().UnixMilli(),
	})
	if err == nil {
		log.Fatalf("FAIL: expected deadline exceeded error")
	}
	st, _ := status.FromError(err)
	if st.Code() != codes.DeadlineExceeded {
		log.Fatalf("FAIL: expected DeadlineExceeded, got: %s", st.Code())
	}
	fmt.Printf("  Received error: code=%s\n", st.Code())
	fmt.Println("  ✓ Deadline enforcement works\n")

	// ── TEST 4: Unimplemented RPC ────────────────────────────────
	fmt.Println("TEST 4: Unimplemented RPC returns correct gRPC status")
	fmt.Println("────────────────────────────────────────────────────────")

	ctx4, cancel4 := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel4()
	_, err = client.AnalyzeIncident(ctx4, &agentpb.AnalysisRequest{
		IncidentId:         "test",
		TriggerType:        "error_rate",
		TriggerDescription: "test",
		Services:           []string{"service_a"},
		DetectedAtNs:      time.Now().UnixNano(),
		AnalysisWindowNs:  600_000_000_000,
	})
	if err == nil {
		log.Fatalf("FAIL: expected Unimplemented error")
	}
	st4, _ := status.FromError(err)
	if st4.Code() != codes.Unimplemented {
		log.Fatalf("FAIL: expected Unimplemented, got: %s", st4.Code())
	}
	fmt.Printf("  Received error: code=%s\n", st4.Code())
	fmt.Println("  ✓ Unimplemented RPCs handled correctly\n")

	// ── SUMMARY ─────────────────────────────────────────────────
	fmt.Println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
	fmt.Println("  ALL TESTS PASSED")
	fmt.Println()
	fmt.Println("  ✓ Go protobuf stubs compile and work")
	fmt.Println("  ✓ Python protobuf stubs compile and work")
	fmt.Println("  ✓ Go → Python cross-language gRPC succeeds")
	fmt.Println("  ✓ Protobuf binary serialization compatible")
	fmt.Println("  ✓ gRPC deadline enforcement works")
	fmt.Println("  ✓ gRPC error codes across language boundary")
	fmt.Println("  ✓ HTTP/2 connection reuse")
	fmt.Println()
	fmt.Println("  TODO-1.1 complete. Ready for TODO-1.2.")
	fmt.Println("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
}
