import assert from 'node:assert/strict';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import { bindTaskCheckboxes, type TaskCheckboxChange } from '../src/web/task-checkboxes.ts';

test('bindTaskCheckboxes enables rendered task inputs and reports their block-local index', () => {
  const dom = new JSDOM('<main><ul data-block-id="tasks"><li><input type="checkbox" disabled data-task-checkbox-index="0"> First</li><li><input type="checkbox" disabled checked data-task-checkbox-index="1"> Second</li></ul></main>');
  const changes: TaskCheckboxChange[] = [];
  bindTaskCheckboxes(dom.window.document, (change) => changes.push(change));
  const inputs = dom.window.document.querySelectorAll<HTMLInputElement>('input');

  assert.equal(inputs[0]!.disabled, false);
  assert.equal(inputs[1]!.disabled, false);
  inputs[0]!.click();
  assert.deepEqual(changes, [{ blockId: 'tasks', checkboxIndex: 0, checked: true }]);
  dom.window.close();
});
