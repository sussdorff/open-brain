import type { Memory, RefineAction } from "./index.js";
import { config } from "../config.js";
import { pool } from "../db/pool.js";

export async function analyzeWithHaiku(
  memories: Memory[]
): Promise<RefineAction[]> {
  if (!config.ANTHROPIC_API_KEY) {
    return findObviousDuplicates(memories);
  }

  const memorySummary = memories
    .map(
      (m) =>
        `[${m.id}] (${m.type}, priority=${m.priority}, stability=${m.stability}) ${m.title || ""}: ${m.content.slice(0, 200)}`
    )
    .join("\n");

  const response = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": config.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: "claude-haiku-4-5-20251001",
      max_tokens: 1024,
      messages: [
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
      ],
    }),
  });

  if (!response.ok) {
    console.error(`Haiku API error: ${response.status}`);
    return findObviousDuplicates(memories);
  }

  const data = (await response.json()) as {
    content: Array<{ text: string }>;
  };
  const text = data.content[0]?.text || "[]";

  try {
    const jsonMatch = text.match(/\[[\s\S]*\]/);
    if (!jsonMatch) return [];
    return JSON.parse(jsonMatch[0]) as RefineAction[];
  } catch {
    console.error("Failed to parse Haiku response:", text);
    return [];
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
