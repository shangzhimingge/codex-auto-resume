import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const launcher = path.join(root, 'bin', 'cli.mjs');

test('launcher is a thin Python facade', () => {
  const source = fs.readFileSync(launcher, 'utf8');
  assert.match(source, /spawnSync/);
  assert.match(source, /installer\.py/);
  assert.doesNotMatch(source, /rmSync|AGENTS\.md|installSkill/);
  assert.ok(source.split(/\r?\n/).length < 100);
});

test('no-argument launcher performs the default simulated install', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-auto-resume-node-'));
  const result = spawnSync(process.execPath, [launcher], {
    cwd: root,
    encoding: 'utf8',
    env: {
      ...process.env,
      CODEX_HOME: home,
      CODEX_AUTO_RESUME_PYTHON: process.env.CODEX_AUTO_RESUME_PYTHON || 'python',
      CODEX_AUTO_RESUME_SIMULATE: '1',
      CODEX_AUTO_RESUME_SKIP_PREREQUISITES: '1',
    },
    windowsHide: true,
  });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.equal(fs.readFileSync(path.join(home, 'skills', 'codex-auto-resume', 'VERSION'), 'utf8').trim(), '1.2.0');
});
