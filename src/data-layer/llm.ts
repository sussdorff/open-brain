import { config } from "../config.js";

interface LlmMessage {
  role: "user" | "assistant";
  content: string;
}

interface LlmOptions {
  model?: string;
  maxTokens?: number;
}

/**
 * Send a message to the configured LLM provider (Anthropic or OpenRouter).
 * Returns the text response.
 */
export async function llmComplete(
  messages: LlmMessage[],
  options: LlmOptions = {}
): Promise<string> {
  const provider = config.LLM_PROVIDER;
  const model = options.model ?? config.LLM_MODEL;
  const maxTokens = options.maxTokens ?? 1024;

  if (provider === "openrouter") {
    return callOpenRouter(messages, model, maxTokens);
  }
  return callAnthropic(messages, model, maxTokens);
}

async function callAnthropic(
  messages: LlmMessage[],
  model: string,
  maxTokens: number
): Promise<string> {
  if (!config.ANTHROPIC_API_KEY) {
    throw new Error("ANTHROPIC_API_KEY required when LLM_PROVIDER=anthropic");
  }

  const response = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": config.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({ model, max_tokens: maxTokens, messages }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Anthropic API error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as {
    content: Array<{ text: string }>;
  };
  return data.content[0]?.text || "";
}

async function callOpenRouter(
  messages: LlmMessage[],
  model: string,
  maxTokens: number
): Promise<string> {
  if (!config.OPENROUTER_API_KEY) {
    throw new Error(
      "OPENROUTER_API_KEY required when LLM_PROVIDER=openrouter"
    );
  }

  const response = await fetch(
    "https://openrouter.ai/api/v1/chat/completions",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.OPENROUTER_API_KEY}`,
      },
      body: JSON.stringify({
        model,
        max_tokens: maxTokens,
        messages,
      }),
    }
  );

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`OpenRouter API error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as {
    choices: Array<{ message: { content: string } }>;
  };
  return data.choices[0]?.message?.content || "";
}
