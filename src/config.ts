import { z } from "zod";
import "dotenv/config";

const ConfigSchema = z.object({
  PORT: z.coerce.number().default(8091),
  DATABASE_URL: z
    .string()
    .default("postgresql://open_brain:password@localhost:5432/open_brain"),
  MCP_SERVER_URL: z.string().url(),
  AUTH_USER: z.string().min(1),
  AUTH_PASSWORD: z.string().min(8),
  JWT_SECRET: z.string().min(32),
  CLIENTS_FILE: z.string().default("/opt/open-brain/clients.json"),
  VOYAGE_API_KEY: z.string().min(1),
  VOYAGE_MODEL: z.string().default("voyage-4"),

  // LLM for metadata extraction / refinement
  LLM_PROVIDER: z
    .enum(["anthropic", "openrouter"])
    .default("anthropic"),
  LLM_MODEL: z.string().default("claude-haiku-4-5-20251001"),
  ANTHROPIC_API_KEY: z.string().optional(),
  OPENROUTER_API_KEY: z.string().optional(),
});

export const config = ConfigSchema.parse(process.env);
