import { z } from "zod";
import "dotenv/config";

const ConfigSchema = z.object({
  PORT: z.coerce.number().default(8091),
  DATABASE_URL: z.string().url().default("postgresql://localhost:5432/open_brain"),
  VOYAGE_API_KEY: z.string().min(1),
  MCP_SERVER_URL: z.string().url(),
  AUTH_USER: z.string().min(1),
  AUTH_PASSWORD: z.string().min(8),
  JWT_SECRET: z.string().min(32),
  CLIENTS_FILE: z.string().default("/opt/mcp-server/clients.json"),
  ANTHROPIC_API_KEY: z.string().optional(),
});

export const config = ConfigSchema.parse(process.env);
