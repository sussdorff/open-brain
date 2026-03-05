import { z } from "zod";
import { workerGet } from "../worker-client.js";

export const searchByConceptSchema = {
  query: z.string().describe("Semantic concept to search for"),
  limit: z.number().optional().describe("Max results to return"),
  project: z.string().optional().describe("Filter by project name"),
};

export async function searchByConceptTool(params: Record<string, unknown>) {
  return workerGet("/api/search/by-concept", params as Record<string, string | number | undefined>);
}
