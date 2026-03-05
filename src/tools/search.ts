import { z } from "zod";
import type { DataLayer, SearchParams } from "../data-layer/index.js";

export const searchSchema = {
  query: z.string().optional().describe("Search query text"),
  limit: z.number().optional().describe("Max results to return"),
  project: z.string().optional().describe("Filter by project name"),
  type: z
    .string()
    .optional()
    .describe("Filter by observation type (discovery, decision, etc.)"),
  obs_type: z.string().optional().describe("Alias for type filter"),
  dateStart: z.string().optional().describe("Start date (ISO format)"),
  dateEnd: z.string().optional().describe("End date (ISO format)"),
  offset: z.number().optional().describe("Pagination offset"),
  orderBy: z.string().optional().describe("Sort order"),
  filePath: z
    .string()
    .optional()
    .describe("Filter by file path in metadata"),
};

export function createSearchTool(dl: DataLayer) {
  return async (params: Record<string, unknown>) =>
    dl.search(params as SearchParams);
}
