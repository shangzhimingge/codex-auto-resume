#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const installer = path.join(root, 'installer.py');
const requested = process.env.CODEX_AUTO_RESUME_PYTHON;
const candidates = requested
  ? [[requested, []]]
  : process.platform === 'win32'
    ? [['py', ['-3']], ['python', []]]
    : [['python3', []], ['python', []]];

let selected = null;
for (const [command, prefix] of candidates) {
  const probe = spawnSync(command, [...prefix, '--version'], {
    encoding: 'utf8',
    shell: false,
    windowsHide: true,
  });
  if (!probe.error && probe.status === 0) {
    selected = [command, prefix];
    break;
  }
}

if (!selected) {
  console.error('codex-auto-resume: Python 3.9+ was not found.');
  process.exit(1);
}

const result = spawnSync(selected[0], [...selected[1], installer, ...process.argv.slice(2)], {
  stdio: 'inherit',
  shell: false,
  windowsHide: false,
});
if (result.error) {
  console.error(`codex-auto-resume: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
