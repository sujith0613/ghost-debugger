package router

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	agentpb "github.com/sujithm/ghost-debugger/proto/agent"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type AgentRouter struct {
	target string
	conn   *grpc.ClientConn
	client agentpb.AgentServiceClient
}

func NewAgentRouter(target string) *AgentRouter {
	return &AgentRouter{target: target}
}

func (r *AgentRouter) Connect() error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := grpc.DialContext(ctx, r.target,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		return fmt.Errorf("agent router: failed to connect to %s: %w", r.target, err)
	}

	r.conn = conn
	r.client = agentpb.NewAgentServiceClient(conn)
	slog.Info("connected to agent service", "target", r.target)
	return nil
}

func (r *AgentRouter) Analyze(ctx context.Context, req *agentpb.AnalysisRequest) (*agentpb.AnalysisResponse, error) {
	if r.client == nil {
		return nil, fmt.Errorf("agent router: not connected")
	}

	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()

	resp, err := r.client.AnalyzeIncident(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("agent router: analysis failed: %w", err)
	}

	return resp, nil
}

func (r *AgentRouter) Close() error {
	if r.conn != nil {
		return r.conn.Close()
	}
	return nil
}
