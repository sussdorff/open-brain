import type { Memory, RefineAction } from "./index.js";
import { config } from "../config.js";
import { pool } from "../db/pool.js";
import { llmComplete } from "./llm.js";

export async function analyzeWithLlm(
  memories: Memory[]
): Promise<RefineAction[]> {
  const hasKey =
    config.LLM_PROVIDER === "openrouter"
      ? !!config.OPENROUTER_API_KEY
      : !!config.ANTHROPIC_API_KEY;

  if (!hasKey) {
    return findObviousDuplicates(memories);
  }

  const memorySummary = memories
    .map(
      (m) =>
        `[${m.id}] (${m.type}, priority=${m.priority}, stability=${m.stability}) ${m.title || ""}: ${m.content.slice(0, 200)}`
    )
    .join("\n");

  try {
    const text = await llmComplete([
      {
        role: "user",
        content: `Analyze these memories and suggest consolidation actions. Return JSON array of actions.

Memories:
${memorySummary}

Rules:
- "merge": combine near-duplicate memories (keep the better one, delete others)
- "promote": change stability from tentative→stable or stable→canonical for high-quality, frequently-accessed memories
- "demote": lower priority for outdated or low-quality memories
- "delete": remove truly redundant or obsolete memories

Return ONLY a JSON array like:
[{"action":"merge","memory_ids":[1,2],"reason":"Near-duplicate observations about X"},{"action":"promote","memory_ids":[5],"reason":"High-quality canonical knowledge"}]

If no actions needed, return [].`,
      },
    ]);

    const jsonMatch = text.match(/\[[\s\S]*\]/);
    if (!jsonMatch) return [];
    return JSON.parse(jsonMatch[0]) as RefineAction[];
  } catch (err) {
    console.error(`LLM analysis error:`, err);
    return findObviousDuplicates(memories);
  }
}

function findObviousDuplicates(memories: Memory[]): RefineAction[] {
  const byTitle = new Map<string, Memory[]>();
  for (const m of memories) {
    const key = (m.title || m.content.slice(0, 50)).toLowerCase().trim();
    if (!byTitle.has(key)) byTitle.set(key, []);
    byTitle.get(key)!.push(m);
  }

  const actions: RefineAction[] = [];
  for (const [, group] of byTitle) {
    if (group.length > 1) {
      actions.push({
        action: "merge",
        memory_ids: group.map((m) => m.id),
        reason: `Duplicate title/content: "${group[0].title || group[0].content.slice(0, 50)}"`,
        executed: false,
      });
    }
  }
  return actions;
}

export async function executeRefineAction(action: RefineAction): Promise<void> {
  switch (action.action) {
    case "merge": {
      const [, ...remove] = action.memory_ids;
      if (remove.length > 0) {
        const placeholders = remove.map((_, i) => `$${i + 1}`).join(", ");
        await pool.query(
          `DELETE FROM memories WHERE id IN (${placeholders})`,
          remove
        );
        console.log(
          `Merged: kept ${action.memory_ids[0]}, deleted ${remove.join(", ")}`
        );
      }
      break;
    }
    case "promote": {
      for (const id of action.memory_ids) {
        await pool.query(
          `UPDATE memories SET
            stability = CASE stability WHEN 'tentative' THEN 'stable' WHEN 'stable' THEN 'canonical' ELSE stability END,
            updated_at = now()
          WHERE id = $1`,
          [id]
        );
      }
      break;
    }
    case "demote": {
      const placeholders = action.memory_ids
        .map((_, i) => `$${i + 1}`)
        .join(", ");
      await pool.query(
        `UPDATE memories SET priority = GREATEST(priority - 0.1, 0.01), updated_at = now() WHERE id IN (${placeholders})`,
        action.memory_ids
      );
      break;
    }
    case "delete": {
      const placeholders = action.memory_ids
        .map((_, i) => `$${i + 1}`)
        .join(", ");
      await pool.query(
        `DELETE FROM memories WHERE id IN (${placeholders})`,
        action.memory_ids
      );
      break;
    }
  }
}
