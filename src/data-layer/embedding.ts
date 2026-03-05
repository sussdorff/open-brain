import { config } from "../config.js";

const VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings";
const VOYAGE_MODEL = config.VOYAGE_MODEL;
const EMBEDDING_DIM = 1024;

export interface EmbeddingResult {
  embedding: number[];
  usage: { total_tokens: number };
}

/**
 * Embed a single text using Voyage API.
 */
export async function embed(text: string): Promise<number[]> {
  const res = await fetch(VOYAGE_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${config.VOYAGE_API_KEY}`,
    },
    body: JSON.stringify({
      model: VOYAGE_MODEL,
      input: [text],
      input_type: "document",
      output_dimension: EMBEDDING_DIM,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Voyage API error ${res.status}: ${body}`);
  }

  const data = (await res.json()) as {
    data: Array<{ embedding: number[] }>;
  };
  return data.data[0].embedding;
}

/**
 * Embed a query text (uses "query" input_type for asymmetric retrieval).
 */
export async function embedQuery(text: string): Promise<number[]> {
  const res = await fetch(VOYAGE_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${config.VOYAGE_API_KEY}`,
    },
    body: JSON.stringify({
      model: VOYAGE_MODEL,
      input: [text],
      input_type: "query",
      output_dimension: EMBEDDING_DIM,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Voyage API error ${res.status}: ${body}`);
  }

  const data = (await res.json()) as {
    data: Array<{ embedding: number[] }>;
  };
  return data.data[0].embedding;
}

/**
 * Batch embed multiple texts. Max ~128 texts per batch for Voyage API.
 */
export async function embedBatch(texts: string[]): Promise<number[][]> {
  const BATCH_SIZE = 64;
  const results: number[][] = [];

  for (let i = 0; i < texts.length; i += BATCH_SIZE) {
    const batch = texts.slice(i, i + BATCH_SIZE);
    const res = await fetch(VOYAGE_API_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.VOYAGE_API_KEY}`,
      },
      body: JSON.stringify({
        model: VOYAGE_MODEL,
        input: batch,
        input_type: "document",
        output_dimension: EMBEDDING_DIM,
      }),
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`Voyage batch embed error ${res.status}: ${body}`);
    }

    const data = (await res.json()) as {
      data: Array<{ embedding: number[] }>;
    };
    results.push(...data.data.map((d) => d.embedding));

    // Rate limit: small delay between batches
    if (i + BATCH_SIZE < texts.length) {
      await new Promise((r) => setTimeout(r, 200));
    }
  }

  return results;
}

/**
 * Format embedding as pgvector literal string.
 */
export function toPgVector(embedding: number[]): string {
  return `[${embedding.join(",")}]`;
}
