import express from "express";
import cors from "cors";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { mcpAuthRouter } from "@modelcontextprotocol/sdk/server/auth/router.js";
import { requireBearerAuth } from "@modelcontextprotocol/sdk/server/auth/middleware/bearerAuth.js";
import { config } from "./config.js";
import { oauthProvider, handleAuthorizeSubmit } from "./auth/provider.js";
import { registerTools } from "./tools/index.js";
import { createPostgresDataLayer } from "./data-layer/postgres.js";

const app = express();

// CORS for all origins (NetBird handles access control)
app.use(cors());

// Body parsing
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// DataLayer instance
const dataLayer = createPostgresDataLayer();

// Health endpoint (no auth)
app.get("/health", async (_req, res) => {
  try {
    const stats = await dataLayer.stats();
    res.json({ status: "ok", ...stats });
  } catch {
    res.status(503).json({ status: "degraded", error: "database unreachable" });
  }
});

// OAuth auth router (well-known, authorize, token, register)
const serverUrl = new URL(config.MCP_SERVER_URL);
app.use(
  mcpAuthRouter({
    provider: oauthProvider,
    issuerUrl: serverUrl,
    baseUrl: serverUrl,
    scopesSupported: ["read", "write"],
    resourceName: "open-brain MCP Server",
  })
);

// Handle login form submission
app.post("/authorize/submit", (req, res) => {
  handleAuthorizeSubmit(req.body, res);
});

// Bearer auth middleware for MCP endpoint
const bearerAuth = requireBearerAuth({
  verifier: oauthProvider,
});

// Session management: map session IDs to transports
const sessions = new Map<string, StreamableHTTPServerTransport>();

// MCP handler
const mcpHandler: express.RequestHandler = async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] as string | undefined;

  if (req.method === "GET" || req.method === "DELETE") {
    if (!sessionId || !sessions.has(sessionId)) {
      res.status(400).json({ error: "Invalid or missing session ID" });
      return;
    }
    const transport = sessions.get(sessionId)!;
    await transport.handleRequest(req, res);
    if (req.method === "DELETE") {
      sessions.delete(sessionId);
    }
    return;
  }

  // POST
  if (sessionId && sessions.has(sessionId)) {
    const transport = sessions.get(sessionId)!;
    await transport.handleRequest(req, res, req.body);
    return;
  }

  // New session - create server + transport
  const server = new McpServer({
    name: "open-brain",
    version: "1.0.0",
  });
  registerTools(server, dataLayer);

  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
  });

  transport.onclose = () => {
    const sid = transport.sessionId;
    if (sid) sessions.delete(sid);
  };

  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);

  // Store session after handleRequest so sessionId is set
  const sid = transport.sessionId;
  if (sid) {
    sessions.set(sid, transport);
  }
};

app.all("/mcp", bearerAuth, mcpHandler);
app.post("/", bearerAuth, mcpHandler);
app.get("/", bearerAuth, mcpHandler);
app.delete("/", bearerAuth, mcpHandler);

app.listen(config.PORT, "0.0.0.0", () => {
  console.log(`open-brain MCP Server listening on port ${config.PORT}`);
  console.log(`OAuth issuer: ${config.MCP_SERVER_URL}`);
});
