import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { DataLayer } from "../data-layer/index.js";
import { searchSchema, createSearchTool } from "./search.js";
import { timelineSchema, createTimelineTool } from "./timeline.js";
import {
  getObservationsSchema,
  createGetObservationsTool,
} from "./get-observations.js";
import { saveMemorySchema, createSaveMemoryTool } from "./save-memory.js";
import {
  searchByConceptSchema,
  createSearchByConceptTool,
} from "./search-by-concept.js";
import { getContextSchema, createGetContextTool } from "./get-context.js";
import { statsSchema, createStatsTool } from "./stats.js";

function wrapTool(fn: (params: Record<string, unknown>) => Promise<unknown>) {
  return async (params: Record<string, unknown>) => {
    try {
      const result = await fn(params);
      if (
        result &&
        typeof result === "object" &&
        "content" in (result as Record<string, unknown>)
      ) {
        return result as { content: Array<{ type: "text"; text: string }> };
      }
      return {
        content: [
          { type: "text" as const, text: JSON.stringify(result, null, 2) },
        ],
      };
    } catch (err) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${err instanceof Error ? err.message : String(err)}`,
          },
        ],
        isError: true,
      };
    }
  };
}

export function registerTools(server: McpServer, dl: DataLayer): void {
  server.tool(
    "____IMPORTANT",
    "3-LAYER WORKFLOW (ALWAYS FOLLOW):\n1. search(query) -> Get index with IDs (~50-100 tokens/result)\n2. timeline(anchor=ID) -> Get context around interesting results\n3. get_observations([IDs]) -> Fetch full details ONLY for filtered IDs\nNEVER fetch full details without filtering first. 10x token savings.",
    {},
    async () => ({
      content: [
        {
          type: "text",
          text: "This is a workflow reminder tool. Use search -> timeline -> get_observations for efficient memory access.",
        },
      ],
    })
  );

  server.tool(
    "search",
    "Step 1: Search memory (hybrid: vector + FTS). Returns index with IDs. Params: query, limit, project, type, obs_type, dateStart, dateEnd, offset, orderBy, filePath",
    searchSchema,
    wrapTool(createSearchTool(dl))
  );

  server.tool(
    "timeline",
    "Step 2: Get context around results. Params: anchor (observation ID) OR query (finds anchor automatically), depth_before, depth_after, project",
    timelineSchema,
    wrapTool(createTimelineTool(dl))
  );

  server.tool(
    "get_observations",
    "Step 3: Fetch full details for filtered IDs. Params: ids (array of observation IDs, required)",
    getObservationsSchema,
    wrapTool(createGetObservationsTool(dl))
  );

  server.tool(
    "save_memory",
    "Save a new observation to memory (auto-embeds via Voyage). Params: text (required), type, project, title",
    saveMemorySchema,
    wrapTool(createSaveMemoryTool(dl))
  );

  server.tool(
    "search_by_concept",
    "Semantic search across memories using vector embeddings. Params: query (required), limit, project",
    searchByConceptSchema,
    wrapTool(createSearchByConceptTool(dl))
  );

  server.tool(
    "get_context",
    "Get recent session context. Params: limit, project",
    getContextSchema,
    wrapTool(createGetContextTool(dl))
  );

  server.tool(
    "stats",
    "Get database statistics (memory count, sessions, DB size)",
    statsSchema,
    wrapTool(createStatsTool(dl))
  );
}
