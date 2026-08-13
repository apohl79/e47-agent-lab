import { settingsForModel, visibleInferenceModels } from '../inference-settings.ts';
import type { InferenceCatalog, InferenceModelOption, InferenceSettings } from '../types.ts';

export interface InferenceSelectorsOptions {
  catalog: InferenceCatalog;
  settings: InferenceSettings;
  label: string;
  onChange(settings: InferenceSettings): Promise<void>;
  onError(error: unknown): void;
}

function appendOption(select: HTMLSelectElement, value: string, label: string): void {
  const option = document.createElement('option');
  option.value = value;
  option.textContent = label;
  select.appendChild(option);
}

function modelKey(model: Pick<InferenceModelOption, 'provider' | 'model'>): string {
  return JSON.stringify([model.provider, model.model]);
}

export function createInferenceSelectors(options: InferenceSelectorsOptions): HTMLElement {
  const root = document.createElement('div');
  root.className = 'inference-selectors';
  root.setAttribute('aria-label', options.label);
  let current = options.settings;

  const render = (): void => {
    root.replaceChildren();
    const models = visibleInferenceModels(options.catalog, current);
    const providerSelect = document.createElement('select');
    providerSelect.className = 'inference-select inference-provider';
    providerSelect.title = 'Provider';
    providerSelect.setAttribute('aria-label', `${options.label} provider`);
    const providers = [...new Set(models.map((model) => model.provider))];
    for (const provider of providers) appendOption(providerSelect, provider, provider);
    providerSelect.value = current.provider;

    const modelSelect = document.createElement('select');
    modelSelect.className = 'inference-select inference-model';
    modelSelect.title = 'Model';
    modelSelect.setAttribute('aria-label', `${options.label} model`);
    const providerModels = models.filter((model) => model.provider === current.provider);
    for (const model of providerModels) appendOption(modelSelect, modelKey(model), model.displayName);
    modelSelect.value = JSON.stringify([current.provider, current.model]);

    const selectedModel = options.catalog.models.find((model) =>
      model.provider === current.provider && model.model === current.model,
    );
    const effortSelect = document.createElement('select');
    effortSelect.className = 'inference-select inference-effort';
    effortSelect.title = 'Reasoning effort';
    effortSelect.setAttribute('aria-label', `${options.label} reasoning effort`);
    for (const effort of selectedModel?.supportedReasoningEfforts ?? []) {
      appendOption(effortSelect, effort.reasoningEffort, effort.reasoningEffort);
    }
    effortSelect.value = current.reasoningEffort;

    const commit = async (next: InferenceSettings): Promise<void> => {
      for (const select of root.querySelectorAll<HTMLSelectElement>('select')) select.disabled = true;
      try {
        await options.onChange(next);
        current = next;
      } catch (error) {
        options.onError(error);
      }
      render();
    };

    providerSelect.addEventListener('change', () => {
      const nextModel = models.find((model) =>
        model.provider === providerSelect.value && model.isDefault,
      ) ?? models.find((model) => model.provider === providerSelect.value);
      if (nextModel) void commit(settingsForModel(nextModel, current.reasoningEffort));
    });
    modelSelect.addEventListener('change', () => {
      const nextModel = models.find((model) => modelKey(model) === modelSelect.value);
      if (nextModel) void commit(settingsForModel(nextModel, current.reasoningEffort));
    });
    effortSelect.addEventListener('change', () => {
      void commit(Object.freeze({ ...current, reasoningEffort: effortSelect.value }));
    });
    root.append(providerSelect, modelSelect, effortSelect);
  };

  render();
  return root;
}
