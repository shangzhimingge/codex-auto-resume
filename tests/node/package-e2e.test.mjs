import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const expectedFiles = [
  'LICENSE',
  'README.md',
  'README.zh-CN.md',
  'RELEASE_NOTES.md',
  'activation/AGENTS.block.md',
  'bin/cli.mjs',
  'installer.py',
  'installer/__init__.py',
  'installer/codex_auto_resume_installer/__init__.py',
  'installer/codex_auto_resume_installer/cli.py',
  'installer/codex_auto_resume_installer/core.py',
  'installer/codex_auto_resume_installer/services.py',
  'package.json',
  'skill/codex-auto-resume/SKILL.md',
  'skill/codex-auto-resume/VERSION',
  'skill/codex-auto-resume/agents/openai.yaml',
  'skill/codex-auto-resume/scripts/auto_resume.py',
  'skill/codex-auto-resume/scripts/auto_resume/__init__.py',
  'skill/codex-auto-resume/scripts/auto_resume/activation.py',
  'skill/codex-auto-resume/scripts/auto_resume/checkpoints.py',
  'skill/codex-auto-resume/scripts/auto_resume/cli.py',
  'skill/codex-auto-resume/scripts/auto_resume/daemon.py',
  'skill/codex-auto-resume/scripts/auto_resume/limits.py',
  'skill/codex-auto-resume/scripts/auto_resume/processes.py',
  'skill/codex-auto-resume/scripts/auto_resume/registering.py',
  'skill/codex-auto-resume/scripts/auto_resume/repo.py',
  'skill/codex-auto-resume/scripts/auto_resume/resume.py',
  'skill/codex-auto-resume/scripts/auto_resume/state.py',
  'skill/codex-auto-resume/scripts/auto_resume/watch.py',
  'skill/codex-auto-resume/scripts/auto_resume/watchdog_lease.py',
  'skill/codex-auto-resume/scripts/checkpoint.py',
  'skill/codex-auto-resume/scripts/daemon.py',
  'skill/codex-auto-resume/scripts/preflight.py',
  'skill/codex-auto-resume/scripts/register.py',
  'skill/codex-auto-resume/scripts/watchdog.py',
].sort();

function run(command, args, options = {}) {
  return spawnSync(command, args, {
    cwd: root,
    encoding: 'utf8',
    windowsHide: true,
    ...options,
  });
}

function assertSucceeded(result) {
  assert.equal(result.status, 0, `${result.error?.stack || ''}\n${result.stdout}\n${result.stderr}`);
}

function runNpm(args, options = {}) {
  const bundledCli = path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js');
  const npmCli = process.env.npm_execpath || (fs.existsSync(bundledCli) ? bundledCli : null);
  return npmCli ? run(process.execPath, [npmCli, ...args], options) : run('npm', args, options);
}

function runPublicBin(publicBin, args, env) {
  if (process.platform !== 'win32') {
    return run(publicBin, args, { env });
  }
  return run(publicBin, args, { env, shell: true });
}

test('packed tarball installs and its public bin performs an isolated simulated install', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-auto-resume-package-e2e-'));
  try {
    const packDirectory = path.join(temporary, 'pack');
    const prefix = path.join(temporary, 'prefix');
    const codexHome = path.join(temporary, 'codex-home');
    const npmCache = path.join(temporary, 'npm-cache');
    fs.mkdirSync(packDirectory, { recursive: true });
    const npmEnv = { ...process.env, npm_config_cache: npmCache };

    const packed = runNpm(['pack', '--json', '--pack-destination', packDirectory], { env: npmEnv });
    assertSucceeded(packed);
    const metadata = JSON.parse(packed.stdout)[0];
    const publishedFiles = metadata.files.map((item) => item.path).sort();
    assert.equal(publishedFiles.length, 35);
    assert.deepEqual(publishedFiles, expectedFiles);
    assert.equal(
      publishedFiles.some((file) => /(^|\/)(tests|docs\/superpowers|__pycache__)(\/|$)|\.py[co]$/.test(file)),
      false,
    );

    const tarball = path.join(packDirectory, metadata.filename);
    assert.ok(fs.statSync(tarball).isFile());
    const installed = runNpm([
      'install', '--prefix', prefix, '--offline', '--ignore-scripts', '--no-audit', '--no-fund', tarball,
    ], { env: npmEnv });
    assertSucceeded(installed);

    const publicBin = path.join(
      prefix, 'node_modules', '.bin',
      process.platform === 'win32' ? 'codex-auto-resume.cmd' : 'codex-auto-resume',
    );
    assert.ok(fs.statSync(publicBin).isFile(), publicBin);
    const result = runPublicBin(publicBin, ['--codex-home', codexHome, '--platform', 'win32'], {
      ...process.env,
      CODEX_AUTO_RESUME_PYTHON: process.env.CODEX_AUTO_RESUME_PYTHON || 'python',
      CODEX_AUTO_RESUME_SIMULATE: '1',
      CODEX_AUTO_RESUME_SKIP_PREREQUISITES: '1',
    });
    assertSucceeded(result);

    const installedSkill = path.join(codexHome, 'skills', 'codex-auto-resume');
    assert.equal(fs.readFileSync(path.join(installedSkill, 'VERSION'), 'utf8').trim(), '1.2.1');
    const manifest = JSON.parse(fs.readFileSync(
      path.join(codexHome, 'auto-resume', 'install-manifest.json'), 'utf8',
    ));
    assert.equal(manifest.version, '1.2.1');
    assert.equal(manifest.product, 'io.github.shangzhimingge.codex-auto-resume');
    assert.equal(manifest.service.simulated, true);
    const agents = fs.readFileSync(path.join(codexHome, 'AGENTS.md'), 'utf8');
    assert.equal(agents.match(/BEGIN CODEX-AUTO-RESUME MANAGED BLOCK/g)?.length, 1);
    assert.equal(agents.match(/END CODEX-AUTO-RESUME MANAGED BLOCK/g)?.length, 1);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
