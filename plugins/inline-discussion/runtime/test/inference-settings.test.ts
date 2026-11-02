import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  inferenceModel,
  resolvedInferenceSettings,
  settingsForModel,
  validInferenceSettings,
  visibleInferenceModels,
} from '../src/inference-settings.ts';
import type { InferenceCatalog } from '../src/types.ts';

const catalog: InferenceCatalog = {
  defaultSettings: { provider: 'openai', model: 'visible', reasoningEffort: 'medium' },
  models: [
    {
      provider: 'openai', model: 'visible', displayName: 'Visible', description: '', hidden: false, isDefault: true,
      defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [
        { reasoningEffort: 'medium', description: '' },
        { reasoningEffort: 'high', description: '' },
      ],
    },
    {
      provider: 'openai', model: 'hidden', displayName: 'Hidden', description: '', hidden: true, isDefault: false,
      defaultReasoningEffort: 'high',
      supportedReasoningEfforts: [{ reasoningEffort: 'high', description: '' }],
    },
  ],
};

test('inference settings validation requires an exact provider, model, and effort match', () => {
  const selected = { provider: 'openai', model: 'visible', reasoningEffort: 'high' } as const;
  assert.equal(inferenceModel(catalog, selected)?.model, 'visible');
  assert.equal(validInferenceSettings(catalog, selected), true);
  assert.equal(validInferenceSettings(catalog, { ...selected, provider: 'other' }), false);
  assert.equal(validInferenceSettings(catalog, { ...selected, model: 'missing' }), false);
  assert.equal(validInferenceSettings(catalog, { ...selected, reasoningEffort: 'low' }), false);
});

test('requested settings are preserved only when supported', () => {
  const selected = { provider: 'openai', model: 'visible', reasoningEffort: 'high' } as const;
  assert.deepEqual(resolvedInferenceSettings(catalog, selected), selected);
  assert.deepEqual(resolvedInferenceSettings(catalog, undefined), catalog.defaultSettings);
  assert.deepEqual(
    resolvedInferenceSettings(catalog, { ...selected, reasoningEffort: 'unsupported' }),
    catalog.defaultSettings,
  );
});

test('hidden models are exposed only while selected', () => {
  assert.deepEqual(visibleInferenceModels(catalog, catalog.defaultSettings).map((model) => model.model), ['visible']);
  assert.deepEqual(visibleInferenceModels(catalog, {
    provider: 'openai', model: 'hidden', reasoningEffort: 'high',
  }).map((model) => model.model), ['visible', 'hidden']);
});

test('model changes retain supported effort and otherwise use the model default', () => {
  const visible = catalog.models[0]!;
  const hidden = catalog.models[1]!;
  assert.deepEqual(settingsForModel(visible, 'high'), {
    provider: 'openai', model: 'visible', reasoningEffort: 'high',
  });
  assert.deepEqual(settingsForModel(hidden, 'medium'), {
    provider: 'openai', model: 'hidden', reasoningEffort: 'high',
  });
  assert.deepEqual(settingsForModel(visible), {
    provider: 'openai', model: 'visible', reasoningEffort: 'medium',
  });
});
