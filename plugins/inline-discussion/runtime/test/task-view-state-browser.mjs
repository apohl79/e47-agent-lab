import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from '../dist/server.js';

// Uses existing Playwright and browser installations against freshly built assets.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const runtime = dirname(dirname(fileURLToPath(import.meta.url)));
const root = mkdtempSync(join(tmpdir(), 'task-view-state-'));
const register = join(root, 'tasks.json');
const doc = join(root, 'today.md');
const cli = join(runtime, 'dist/task-control.js');
execFileSync(process.execPath, [cli, 'init', register]);
const items = [];
for (let index = 0; index < 30; index++) {
  const id = `TASK-${String(index).padStart(3, '0')}`;
  const title = `Review task ${index}`;
  const { revision } = JSON.parse(readFileSync(register, 'utf8'));
  execFileSync(process.execPath, [cli, 'update', register, '-'], {
    input: JSON.stringify({ action: 'create', id, expectedRevision: revision, task: { title, source: `https://example.test/${id}` } }),
  });
  items.push(`- [ ] ${title} <!-- task-id: ${id} -->`);
}
writeFileSync(doc, `# Today

<!-- task-register: tasks.json -->

## Planning

${items.join('\n')}

${'A paragraph of document content to preserve while reviewing tasks.\n\n'.repeat(100)}`);
const server = await createServer({
  docPath: doc, projectRoot: root, sessionDir: join(root, 'session'),
  prefsPath: join(root, 'prefs.json'), staticDir: join(runtime, 'dist'), shutdownOnFinish: false,
  agentFactory: () => { throw new Error('Acceptance must not start agents'); },
});
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM });
try {
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    await page.goto(`http://127.0.0.1:${server.port}/`);
    const toggle = page.locator('#task-sidebar-toggle');
    await toggle.waitFor({ state: 'visible' });
    const scrollTo = async (top) => {
      await page.evaluate((value) => window.scrollTo({ top: value, behavior: 'instant' }), top);
      await page.waitForFunction((value) => window.scrollY === value, top);
    };
    const expectScroll = async (top) => {
      await page.waitForFunction((value) => Math.abs(window.scrollY - value) < 2, top);
    };
    await scrollTo(1800);
    await toggle.click();
    await expectScroll(0);
    await page.getByLabel('Filter tasks by status').selectOption('all');
    await page.getByLabel('Filter tasks by category').selectOption('Planning');
    await page.locator('#task-toggle-TASK-000').click();
    await page.getByLabel('Answer or review evidence for TASK-000').fill('Retain this task draft');
    const table = page.locator('.task-table-scroll');
    await table.evaluate((element) => { element.scrollLeft = 180; });
    const horizontal = await table.evaluate((element) => element.scrollLeft);
    await scrollTo(600);
    for (let round = 0; round < 3; round++) {
      await toggle.click();
      await expectScroll(1800 + round * 100);
      await scrollTo(1900 + round * 100);
      await toggle.click();
      await expectScroll(600 + round * 100);
      assert.equal(await page.getByLabel('Filter tasks by status').inputValue(), 'all');
      assert.equal(await page.getByLabel('Filter tasks by category').inputValue(), 'Planning');
      assert.equal(await page.locator('#task-toggle-TASK-000').getAttribute('aria-expanded'), 'true');
      assert.equal(await page.getByLabel('Answer or review evidence for TASK-000').inputValue(), 'Retain this task draft');
      assert.equal(await table.evaluate((element) => element.scrollLeft), horizontal);
      await scrollTo(700 + round * 100);
    }
    // Explicit navigation still reveals its requested target rather than a saved offset.
    await page.locator('.task-sidebar-item > a').click();
    await page.waitForFunction(() => !document.body.classList.contains('task-mode'));
    await page.waitForFunction(() => {
      const target = document.querySelector('#task-TASK-000').getBoundingClientRect();
      return target.top >= 0 && target.bottom <= innerHeight;
    });
    await page.locator('#task-TASK-029 input').click();
    await page.waitForFunction(() => document.body.classList.contains('task-mode'));
    assert.equal(await page.locator('#task-toggle-TASK-029').getAttribute('aria-expanded'), 'true');
    await page.close();
  }
  console.log('PASS document/tasks independent scroll, filters, expansion, drafts, horizontal table scroll, and explicit navigation; desktop/mobile');
} finally {
  await browser.close();
  await server.close();
}
