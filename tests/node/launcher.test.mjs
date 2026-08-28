import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const launcher = path.join(root, 'bin', 'cli.mjs');
const autoResumeBegin = '<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->';
const autoResumeEnd = '<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->';
const solLunaBlock = [
  '<!-- BEGIN SOL-LUNA-HANDOFF MANAGED BLOCK -->',
  '## Sol–Luna project workflow',
  '保留 Sol–Luna 的独立规则块。',
  '<!-- END SOL-LUNA-HANDOFF MANAGED BLOCK -->',
].join('\r\n');

function withTemporaryCodexHome(prefix, callback) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  try {
    return callback(home);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
}

function runLauncher(home, args = []) {
  return spawnSync(process.execPath, [launcher, ...args], {
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
}

function assertSucceeded(result) {
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
}

function snapshotTree(directory) {
  const snapshot = [];
  function visit(current) {
    for (const entry of fs.readdirSync(current, { withFileTypes: true })
      .sort((left, right) => left.name.localeCompare(right.name))) {
      const absolute = path.join(current, entry.name);
      if (entry.isDirectory()) {
        visit(absolute);
      } else {
        snapshot.push([
          path.relative(directory, absolute).split(path.sep).join('/'),
          fs.readFileSync(absolute),
        ]);
      }
    }
  }
  visit(directory);
  return snapshot;
}

test('launcher is a thin Python facade', () => {
  const source = fs.readFileSync(launcher, 'utf8');
  assert.match(source, /spawnSync/);
  assert.match(source, /installer\.py/);
  assert.doesNotMatch(source, /rmSync|AGENTS\.md|installSkill/);
  assert.ok(source.split(/\r?\n/).length < 100);
});

test('no-argument launcher performs the default simulated install', () => {
  withTemporaryCodexHome('codex-auto-resume-node-', (home) => {
    const result = runLauncher(home);
    assertSucceeded(result);
    assert.equal(fs.readFileSync(path.join(home, 'skills', 'codex-auto-resume', 'VERSION'), 'utf8').trim(), '1.2.1');
  });
});

test('npx_install_preserves_sol_luna_block', () => {
  withTemporaryCodexHome('codex-auto-resume-sol-luna-', (home) => {
    const agentsPath = path.join(home, 'AGENTS.md');
    const original = `# User rules\r\n\r\n${solLunaBlock}\r\n\r\n用户自定义尾部。\r\n`;
    fs.writeFileSync(agentsPath, original, 'utf8');
    const solLunaBytes = Buffer.from(solLunaBlock, 'utf8');

    const result = runLauncher(home);

    assertSucceeded(result);
    const installed = fs.readFileSync(agentsPath);
    assert.notEqual(installed.indexOf(solLunaBytes), -1, 'Sol–Luna block bytes changed during install');
    const text = installed.toString('utf8');
    assert.ok(text.includes(solLunaBlock), 'Sol–Luna block text changed during install');
    assert.ok(text.includes('# User rules'));
    assert.ok(text.includes('用户自定义尾部。'));
    assert.equal(text.match(/BEGIN SOL-LUNA-HANDOFF MANAGED BLOCK/g)?.length, 1);
    assert.equal(text.match(/BEGIN CODEX-AUTO-RESUME MANAGED BLOCK/g)?.length, 1);
  });
});

test('npx_reinstall_is_byte_idempotent', () => {
  withTemporaryCodexHome('codex-auto-resume-idempotent-', (home) => {
    const agentsPath = path.join(home, 'AGENTS.md');
    const manifestPath = path.join(home, 'auto-resume', 'install-manifest.json');
    const backupPath = `${agentsPath}.codex-auto-resume.backup`;
    fs.writeFileSync(agentsPath, `ordinary\n\n${solLunaBlock}\n`, 'utf8');

    const first = runLauncher(home);
    assertSucceeded(first);
    const before = {
      agents: fs.readFileSync(agentsPath),
      manifest: fs.readFileSync(manifestPath),
      backup: fs.readFileSync(backupPath),
    };

    const second = runLauncher(home);

    assertSucceeded(second);
    assert.equal(JSON.parse(second.stdout).idempotent, true, second.stdout);
    assert.deepEqual(fs.readFileSync(agentsPath), before.agents);
    assert.deepEqual(fs.readFileSync(manifestPath), before.manifest);
    assert.deepEqual(fs.readFileSync(backupPath), before.backup);
  });
});

test('npx_corrupt_markers_fail_closed_without_changes', async (t) => {
  const corruptions = {
    missing_end_marker(text) {
      return text.replace(autoResumeEnd, '<!-- BROKEN CODEX-AUTO-RESUME END -->');
    },
    duplicate_begin_marker(text) {
      return `${autoResumeBegin}\n${text}`;
    },
  };

  for (const [scenario, corrupt] of Object.entries(corruptions)) {
    await t.test(scenario, () => {
      withTemporaryCodexHome(`codex-auto-resume-corrupt-${scenario}-`, (home) => {
        const agentsPath = path.join(home, 'AGENTS.md');
        const skillPath = path.join(home, 'skills', 'codex-auto-resume');
        const manifestPath = path.join(home, 'auto-resume', 'install-manifest.json');
        fs.writeFileSync(agentsPath, `ordinary\n\n${solLunaBlock}\n`, 'utf8');
        assertSucceeded(runLauncher(home));
        fs.writeFileSync(agentsPath, corrupt(fs.readFileSync(agentsPath, 'utf8')), 'utf8');
        const before = {
          agents: fs.readFileSync(agentsPath),
          skill: snapshotTree(skillPath),
          manifest: fs.readFileSync(manifestPath),
        };

        const result = runLauncher(home);

        assert.notEqual(result.status, 0, `${result.stdout}\n${result.stderr}`);
        assert.match(result.stderr, /malformed|duplicate managed markers/i);
        assert.deepEqual(fs.readFileSync(agentsPath), before.agents);
        assert.deepEqual(snapshotTree(skillPath), before.skill);
        assert.deepEqual(fs.readFileSync(manifestPath), before.manifest);
      });
    });
  }
});

test('npx_uninstall_removes_only_auto_resume_block', () => {
  withTemporaryCodexHome('codex-auto-resume-uninstall-', (home) => {
    const agentsPath = path.join(home, 'AGENTS.md');
    const original = `# Ordinary user content\n\n${solLunaBlock}\n\n保留这行普通内容。\n`;
    fs.writeFileSync(agentsPath, original, 'utf8');
    assertSucceeded(runLauncher(home));
    const installed = fs.readFileSync(agentsPath, 'utf8');
    assert.ok(installed.includes(autoResumeBegin));
    assert.ok(installed.includes(autoResumeEnd));

    const result = runLauncher(home, ['uninstall']);

    assertSucceeded(result);
    const uninstalledBytes = fs.readFileSync(agentsPath);
    assert.deepEqual(
      uninstalledBytes,
      Buffer.from(original, 'utf8'),
      'uninstall did not restore the exact pre-install AGENTS.md content',
    );
    const uninstalled = uninstalledBytes.toString('utf8');
    assert.ok(uninstalled.includes(solLunaBlock), 'Sol–Luna block was not preserved');
    assert.ok(uninstalled.includes('# Ordinary user content'));
    assert.ok(uninstalled.includes('保留这行普通内容。'));
    assert.equal(uninstalled.includes(autoResumeBegin), false);
    assert.equal(uninstalled.includes(autoResumeEnd), false);
    assert.equal(uninstalled.match(/BEGIN SOL-LUNA-HANDOFF MANAGED BLOCK/g)?.length, 1);
  });
});
