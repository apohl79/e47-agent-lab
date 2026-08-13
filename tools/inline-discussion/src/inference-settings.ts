import type {
  InferenceCatalog,
  InferenceModelOption,
  InferenceSettings,
} from './types.ts';

export function inferenceModel(
  catalog: InferenceCatalog,
  settings: InferenceSettings,
): InferenceModelOption | undefined {
  return catalog.models.find((candidate) =>
    candidate.provider === settings.provider && candidate.model === settings.model,
  );
}

export function validInferenceSettings(
  catalog: InferenceCatalog,
  settings: InferenceSettings,
): boolean {
  const model = inferenceModel(catalog, settings);
  return model !== undefined && model.supportedReasoningEfforts.some((option) =>
    option.reasoningEffort === settings.reasoningEffort,
  );
}

export function resolvedInferenceSettings(
  catalog: InferenceCatalog,
  requested: InferenceSettings | undefined,
): InferenceSettings {
  return requested !== undefined && validInferenceSettings(catalog, requested)
    ? Object.freeze({ ...requested })
    : Object.freeze({ ...catalog.defaultSettings });
}

export function visibleInferenceModels(
  catalog: InferenceCatalog,
  selected: InferenceSettings,
): readonly InferenceModelOption[] {
  return catalog.models.filter((model) =>
    !model.hidden || (model.provider === selected.provider && model.model === selected.model),
  );
}

export function settingsForModel(
  model: InferenceModelOption,
  preferredEffort?: string,
): InferenceSettings {
  const supported = preferredEffort !== undefined && model.supportedReasoningEfforts.some((option) =>
    option.reasoningEffort === preferredEffort,
  );
  return Object.freeze({
    provider: model.provider,
    model: model.model,
    reasoningEffort: supported ? preferredEffort : model.defaultReasoningEffort,
  });
}
