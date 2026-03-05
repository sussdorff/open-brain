import { z } from "zod";
import { workerGet } from "../worker-client.js";

export const getContextSchema = {
  limit: z.number().optional().describe("Max recent sessions to return"),
  project: z.string().optional().describe("Filter by project name"),
};

export async function getContextTool(params: Record<string, unknown>) {
  return workerGet("/api/context/recent", params as Record<string, string | number | undefined>);
}
