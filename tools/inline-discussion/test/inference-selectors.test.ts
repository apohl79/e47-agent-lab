import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { createInferenceSelectors } from '../src/web/inference-selectors.ts';
import type { InferenceCatalog, InferenceSettings } from '../src/types.ts';

const catalog: InferenceCatalog = {
  defaultSettings: { provider: 'openai', model: 'model-a', reasoningEffort: 'medium' },
  models: [
    {
      provider: 'openai', model: 'model-a', displayName: 'Model A', description: '', hidden: false, isDefault: true,
      defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [
        { reasoningEffort: 'medium', description: '' },
        { reasoningEffort: 'high', description: '' },
      ],
    },
    {
      provider: 'other', model: 'model-b', displayName: 'Model B', description: '', hidden: false, isDefault: true,
      defaultReasoningEffort: 'high',
      supportedReasoningEfforts: [{ reasoningEffort: 'high', description: '' }],
    },
    {
      provider: 'openai', model: 'model-hidden', displayName: 'Hidden', description: '', hidden: true, isDefault: false,
      defaultReasoningEffort: 'medium',
      supportedReasoningEfforts: [{ reasoningEffort: 'medium', description: '' }],
    },
  ],
};

function installDom(): JSDOM {
  const dom = new JSDOM('<!doctype html><body></body>');
  (globalThis as unknown as { document: Document }).document = dom.window.document;
  return dom;
}

test('inference selectors expose one provider/model list and supported reasoning effort', async () => {
  const dom = installDom();
  const changes: InferenceSettings[] = [];
  const selectors = createInferenceSelectors({
    catalog,
    settings: catalog.defaultSettings,
    label: 'New thread inference',
    onChange: async (settings) => { changes.push(settings); },
    onError: assert.fail,
  });
  const select = selectors.querySelectorAll<HTMLSelectElement>('select');
  assert.equal(select.length, 2);
  assert.deepEqual([...select[0]!.options].map((option) => option.text), [
    'Model A · openai',
    'Model B · other',
  ]);
  assert.deepEqual([...select[1]!.options].map((option) => option.value), ['medium', 'high']);

  select[0]!.value = JSON.stringify(['other', 'model-b']);
  select[0]!.dispatchEvent(new dom.window.Event('change'));
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(changes, [{ provider: 'other', model: 'model-b', reasoningEffort: 'high' }]);
  dom.window.close();
});

test('a selected hidden model remains available', () => {
  const dom = installDom();
  const selectors = createInferenceSelectors({
    catalog,
    settings: { provider: 'openai', model: 'model-hidden', reasoningEffort: 'medium' },
    label: 'Thread inference',
    onChange: async () => undefined,
    onError: assert.fail,
  });
  const model = selectors.querySelector<HTMLSelectElement>('.inference-model')!;
  assert.deepEqual([...model.options].map((option) => option.text), [
    'Model A · openai',
    'Model B · other',
    'Hidden · openai',
  ]);
  assert.equal(model.selectedOptions[0]?.text, 'Hidden · openai');
  dom.window.close();
});
