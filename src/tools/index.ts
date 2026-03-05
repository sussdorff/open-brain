import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { searchSchema, searchTool } from "./search.js";
import { timelineSchema, timelineTool } from "./timeline.js";
import { getObservationsSchema, getObservationsTool } from "./get-observations.js";
import { saveMemorySchema, saveMemoryTool } from "./save-memory.js";
import { searchByConceptSchema, searchByConceptTool } from "./search-by-concept.js";
import { getContextSchema, getContextTool } from "./get-context.js";
import { statsSchema, statsTool } from "./stats.js";

function wrapTool(fn: (params: Record<string, unknown>) => Promise<unknown>) {
  return async (params: Record<string, unknown>) => {
    try {
      const result = await fn(params);
      // If the worker already returns MCP content format, use it directly
      if (
        result &&
        typeof result === "object" &&
        "content" in (result as Record<string, unknown>)
      ) {
        return result as { content: Array<{ type: "text"; text: string }> };
      }
      // Otherwise wrap in text content
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

export function registerTools(server: McpServer): void {
  // Tool 1: Workflow reminder
  server.tool(
    "____IMPORTANT",
    "3-LAYER WORKFLOW (ALWAYS FOLLOW):\n1. search(query) → Get index with IDs (~50-100 tokens/result)\n2. timeline(anchor=ID) → Get context around interesting results\n3. get_observations([IDs]) → Fetch full details ONLY for filtered IDs\nNEVER fetch full details without filtering first. 10x token savings.",
    {},
    async () => ({
      content: [
        {
          type: "text",
          text: "This is a workflow reminder tool. Use search → timeline → get_observations for efficient memory access.",
        },
      ],
    })
  );

  // Tool 2: Search
  server.tool(
    "search",
    "Step 1: Search memory. Returns index with IDs. Params: query, limit, project, type, obs_type, dateStart, dateEnd, offset, orderBy, filePath",
    searchSchema,
    wrapTool(searchTool)
  );

  // Tool 3: Timeline
  server.tool(
    "timeline",
    "Step 2: Get context around results. Params: anchor (observation ID) OR query (finds anchor automatically), depth_before, depth_after, project",
    timelineSchema,
    wrapTool(timelineTool)
  );

  // Tool 4: Get observations
  server.tool(
    "get_observations",
    "Step 3: Fetch full details for filtered IDs. Params: ids (array of observation IDs, required), orderBy, limit, project",
    getObservationsSchema,
    wrapTool(getObservationsTool)
  );

  // Tool 5: Save memory (NEW - write)
  server.tool(
    "save_memory",
    "Save a new observation to memory. Params: text (required), type, project, title",
    saveMemorySchema,
    wrapTool(saveMemoryTool)
  );

  // Tool 6: Search by concept (NEW)
  server.tool(
    "search_by_concept",
    "Semantic search across memories using ChromaDB embeddings. Params: query (required), limit, project",
    searchByConceptSchema,
    wrapTool(searchByConceptTool)
  );

  // Tool 7: Get context (NEW)
  server.tool(
    "get_context",
    "Get recent session context. Params: limit, project",
    getContextSchema,
    wrapTool(getContextTool)
  );

  // Tool 8: Stats (NEW)
  server.tool(
    "stats",
    "Get worker and database statistics (observation count, sessions, DB size, uptime)",
    statsSchema,
    wrapTool(statsTool)
  );
}
