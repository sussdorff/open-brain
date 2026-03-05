import { z } from "zod";
import type { DataLayer } from "../data-layer/index.js";

export const searchByConceptSchema = {
  query: z.string().describe("Semantic concept to search for"),
  limit: z.number().optional().describe("Max results to return"),
  project: z.string().optional().describe("Filter by project name"),
};

export function createSearchByConceptTool(dl: DataLayer) {
  return async (params: Record<string, unknown>) =>
    dl.searchByConcept(
      params.query as string,
      params.limit as number | undefined,
      params.project as string | undefined
    );
}
