import { z } from "zod";
import { workerGet } from "../worker-client.js";

export const searchSchema = {
  query: z.string().optional().describe("Search query text"),
  limit: z.number().optional().describe("Max results to return"),
  project: z.string().optional().describe("Filter by project name"),
  type: z.string().optional().describe("Filter by observation type (discovery, decision, etc.)"),
  obs_type: z.string().optional().describe("Alias for type filter"),
  dateStart: z.string().optional().describe("Start date (ISO format)"),
  dateEnd: z.string().optional().describe("End date (ISO format)"),
  offset: z.number().optional().describe("Pagination offset"),
  orderBy: z.string().optional().describe("Sort order"),
  filePath: z.string().optional().describe("Filter by file path (routes to by-file endpoint)"),
};

export async function searchTool(params: Record<string, unknown>) {
  const { filePath, type, obs_type, query, ...rest } = params;

  let endpoint: string;
  const queryParams: Record<string, string | number | undefined> = {};

  if (filePath) {
    // Route to by-file endpoint
    endpoint = "/api/search/by-file";
    queryParams.filePath = filePath as string;
    if (query) queryParams.query = query as string;
  } else if ((type || obs_type) && !query) {
    // Route to by-type endpoint
    endpoint = "/api/search/by-type";
    queryParams.type = (type ?? obs_type) as string;
  } else {
    // Default search endpoint
    endpoint = "/api/search";
    if (query) queryParams.query = query as string;
    if (type) queryParams.type = type as string;
    if (obs_type) queryParams.obs_type = obs_type as string;
  }

  // Add remaining params
  for (const [k, v] of Object.entries(rest)) {
    if (v !== undefined) queryParams[k] = v as string | number;
  }

  return workerGet(endpoint, queryParams);
}
