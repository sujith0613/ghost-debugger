// Cross-language gRPC ping-pong test: Go client → Python server
package main

import (
	"context"
	"fmt"
	"log"
	"time"

	agentpb "github.com/sujithm/ghost-debugger/proto/agent"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	conn, err := grpc.DialContext(ctx, "localhost:9001",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		log.Fatalf("failed to connect: %v", err)
	}
	defer conn.Close()

	client := agentpb.NewAgentServiceClient(conn)

	resp, err := client.AnalyzeIncident(ctx, &agentpb.AnalysisRequest{
		IncidentId:         "ping-" + fmt.Sprint(time.Now().UnixNano()),
		DetectedAtNs:       time.Now().UnixNano(),
		Services:           []string{"service_a", "service_b", "service_c"},
		TriggerType:        "error_rate_spike",
		TriggerDescription: "error rate exceeded threshold in service_a",
		AnalysisWindowNs:   5 * time.Minute.Nanoseconds(),
	})
	if err != nil {
		log.Fatalf("analysis failed: %v", err)
	}

	fmt.Printf("PONG: incident=%s root_cause=%s confidence=%.2f duration=%dms\n",
		resp.IncidentId, resp.RootCause, resp.RootCauseConfidence, resp.AnalysisDurationMs)
	fmt.Printf("      findings: %d, similar incidents: %d\n",
		len(resp.Findings), len(resp.SimilarIncidents))
	fmt.Printf("      status: %s\n", resp.Status)
}
