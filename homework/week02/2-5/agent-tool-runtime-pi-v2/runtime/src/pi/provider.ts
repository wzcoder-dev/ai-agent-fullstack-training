import {
  createModels,
  createProvider,
  envApiKeyAuth,
  type Model,
} from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import type { PlatformConfig } from "../config.js";

export function createGatewayProvider(config: PlatformConfig) {
  const model: Model<"openai-completions"> = {
    id: config.model.id,
    name: config.model.name,
    api: "openai-completions",
    provider: config.model.providerId,
    baseUrl: config.gateway.baseUrl,
    reasoning: false,
    input: ["text"],
    cost: {
      input: config.model.inputPerMillion,
      output: config.model.outputPerMillion,
      cacheRead: config.model.cacheReadPerMillion,
      cacheWrite: config.model.inputPerMillion,
    },
    contextWindow: config.model.contextWindow,
    maxTokens: config.model.maxTokens,
  };
  return createProvider({
    id: config.model.providerId,
    name: "Phase-One LiteLLM Gateway",
    baseUrl: config.gateway.baseUrl,
    auth: {
      apiKey: envApiKeyAuth("Phase-One Gateway API key", [config.gateway.apiKeyEnv]),
    },
    models: [model],
    api: openAICompletionsApi(),
  });
}

export function createGatewayModels(config: PlatformConfig) {
  const models = createModels();
  models.setProvider(createGatewayProvider(config));
  const model = models.getModel(config.model.providerId, config.model.id);
  if (!model) throw new Error(`Gateway model not found: ${config.model.id}`);
  return { models, model };
}
