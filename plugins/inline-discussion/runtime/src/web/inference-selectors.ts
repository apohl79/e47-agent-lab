import { settingsForModel, visibleInferenceModels } from '../inference-settings.ts';
import type { InferenceCatalog, InferenceModelOption, InferenceSettings } from '../types.ts';

export interface InferenceSelectorsOptions {
  catalog: InferenceCatalog;
  settings: InferenceSettings;
  label: string;
  onChange(settings: InferenceSettings): Promise<void>;
  onError(error: unknown): void;
}

export function setInferenceSelectorsDisabled(root: HTMLElement, disabled: boolean): void {
  if (disabled) root.dataset.disabled = 'true';
  else delete root.dataset.disabled;
  for (const select of root.querySelectorAll<HTMLSelectElement>('select')) {
    select.disabled = disabled;
  }
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
    const modelSelect = document.createElement('select');
    modelSelect.className = 'inference-select inference-model';
    modelSelect.title = 'Provider and model';
    modelSelect.setAttribute('aria-label', `${options.label} provider and model`);
    for (const model of models) {
      appendOption(modelSelect, modelKey(model), `${model.displayName} · ${model.provider}`);
    }
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

    modelSelect.addEventListener('change', () => {
      const nextModel = models.find((model) => modelKey(model) === modelSelect.value);
      if (nextModel) void commit(settingsForModel(nextModel, current.reasoningEffort));
    });
    effortSelect.addEventListener('change', () => {
      void commit(Object.freeze({ ...current, reasoningEffort: effortSelect.value }));
    });
    root.append(modelSelect, effortSelect);
    setInferenceSelectorsDisabled(root, root.dataset.disabled === 'true');
  };

  render();
  return root;
}
