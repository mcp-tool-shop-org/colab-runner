// The site's npm audit gate (scripts/check-audit-allowlist.mjs, ported from brand) fails closed.
//
// Run: node --test scripts/check-audit-allowlist.test.mjs (CI runs it in the "Site build" step).
// Synthetic `npm audit --json` (v2) reports are judged by the real script. Allowlists here are copies of the
// committed one with the expiry moved, so the tests do not rot when the real exception expires.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SCRIPT = path.join(ROOT, 'scripts', 'check-audit-allowlist.mjs');
const ALLOWLIST = path.join(ROOT, '.github', 'audit-allowlist.json');
const GHSA = 'GHSA-fc5q-39xc-vjpf';
const SOURCE = 1247710;
const LOCK = {
  packages: {
    'node_modules/@astrojs/starlight': { version: '0.42.7' },
    'node_modules/picomatch': { version: '4.0.7' },
  },
};
const DOWNGRADE = { name: '@astrojs/starlight', version: '0.39.3', isSemVerMajor: true };

function advisory({ source = SOURCE, name = 'picomatch', ghsa = GHSA, range = '<=4.0.7', severity = 'high' } = {}) {
  return {
    source,
    name,
    dependency: name,
    title: 'synthetic advisory',
    url: `https://github.com/advisories/${ghsa}`,
    severity,
    cwe: ['CWE-1333'],
    range,
  };
}

function vuln(name, via, { severity = 'high', fix = false, range = '<=4.0.7' } = {}) {
  return { name, severity, isDirect: false, via, effects: [], range, nodes: [`node_modules/${name}`], fixAvailable: fix };
}

const report = (vulns) => ({ auditReportVersion: 2, vulnerabilities: vulns, metadata: {} });

function allowedReport(picomatch = {}) {
  return report({
    picomatch: vuln('picomatch', [advisory()], picomatch),
    tinyglobby: vuln('tinyglobby', ['picomatch'], { range: '*' }),
  });
}

function allowlist({ expires = '2999-12-31', ...changes } = {}) {
  const doc = JSON.parse(readFileSync(ALLOWLIST, 'utf-8'));
  const entry = doc.exceptions[0];
  entry.expiresOn = expires;
  for (const [key, value] of Object.entries(changes)) {
    if (value === null) delete entry[key];
    else entry[key] = value;
  }
  return doc;
}

function run(rep, exitCode, { allow = allowlist(), lock = LOCK } = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), 'audit-gate-'));
  const reportPath = path.join(dir, 'audit-report.json');
  writeFileSync(reportPath, typeof rep === 'string' ? rep : JSON.stringify(rep));
  const allowPath = path.join(dir, 'audit-allowlist.json');
  writeFileSync(allowPath, JSON.stringify(allow));
  const args = [SCRIPT, reportPath, allowPath, String(exitCode)];
  if (lock !== null) {
    const lockPath = path.join(dir, 'package-lock.json');
    writeFileSync(lockPath, typeof lock === 'string' ? lock : JSON.stringify(lock));
    args.push(lockPath);
  }
  const res = spawnSync(process.execPath, args, { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 60_000 });
  return { code: res.status, stdout: res.stdout, stderr: res.stderr };
}

test('a clean audit passes', () => {
  const res = run(report({}), 0);
  assert.equal(res.code, 0, res.stderr);
  assert.match(res.stdout, /no high\/critical advisories/);
});

test('an allowed-only audit passes and names the advisory and its dates', () => {
  const res = run(allowedReport(), 1);
  assert.equal(res.code, 0, res.stderr);
  assert.ok(res.stdout.includes(`advisory ${SOURCE} (${GHSA}) in picomatch <=4.0.7`), res.stdout);
  assert.ok(res.stdout.includes('added 2026-11-17, expires 2999-12-31'), res.stdout);
});

for (const severity of ['high', 'critical']) {
  test(`a different ${severity} advisory fails`, () => {
    const rep = allowedReport();
    rep.vulnerabilities.lodash = vuln(
      'lodash',
      [advisory({ source: 1000001, name: 'lodash', ghsa: 'GHSA-29mw-wpgm-hmr9', severity })],
      { severity }
    );
    const res = run(rep, 1);
    assert.notEqual(res.code, 0);
    assert.ok(res.stderr.includes('no valid exception: 1000001'), res.stderr);
  });
}

test('a picomatch advisory with a different id fails', () => {
  const res = run(report({ picomatch: vuln('picomatch', [advisory({ source: 1000002, ghsa: 'GHSA-952p-6rrq-rcjv' })]) }), 1);
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('no valid exception: 1000002'), res.stderr);
});

for (const severity of ['moderate', 'high']) {
  test(`the allowed advisory plus any other (${severity}) on the package fails`, () => {
    const other = advisory({ source: 1000003, ghsa: 'GHSA-952p-6rrq-rcjv', severity, range: '<2.3.1' });
    const res = run(report({ picomatch: vuln('picomatch', [advisory(), other]) }), 1);
    assert.notEqual(res.code, 0);
    assert.ok(res.stderr.includes('no valid exception: 1000003'), res.stderr);
  });
}

test('a transitive entry naming a package the report does not list fails', () => {
  const rep = allowedReport();
  rep.vulnerabilities.tinyglobby.via = ['picomatch', 'ghost-package'];
  const res = run(rep, 1);
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('tinyglobby -> ghost-package'), res.stderr);
});

test('a transitive entry is judged by the advisories it resolves to', () => {
  // tinyglobby carries no advisory of its own; it resolves through micromatch to an advisory nobody excepted.
  const rep = allowedReport();
  rep.vulnerabilities.tinyglobby.via = ['micromatch'];
  rep.vulnerabilities.micromatch = vuln(
    'micromatch',
    [advisory({ source: 1000004, name: 'micromatch', ghsa: 'GHSA-952p-6rrq-rcjv', severity: 'moderate' })],
    { severity: 'moderate' }
  );
  const res = run(rep, 1);
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('no valid exception: 1000004'), res.stderr);
});

test('a transitive entry that resolves to no advisory fails', () => {
  const res = run(report({ a: vuln('a', ['b']), b: vuln('b', ['a']) }), 1);
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('resolves to no advisory'), res.stderr);
});

test('the exception stops matching once the reported range changes', () => {
  const rep = allowedReport();
  rep.vulnerabilities.picomatch.via[0].range = '<=4.0.8';
  const res = run(rep, 1);
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('reported range is "<=4.0.8"'), res.stderr);
});

test('whitespace in the reported range is ignored', () => {
  const rep = allowedReport();
  rep.vulnerabilities.picomatch.via[0].range = '<= 4.0.7';
  const res = run(rep, 1);
  assert.equal(res.code, 0, res.stderr);
});

const FIXES = {
  true: true,
  'picomatch-minor': { name: 'picomatch', version: '4.0.8', isSemVerMajor: false },
  'parent-minor': { name: 'astro', version: '7.5.0', isSemVerMajor: false },
  'picomatch-major': { name: 'picomatch', version: '5.0.0', isSemVerMajor: true },
  'parent-major-upgrade': { name: '@astrojs/starlight', version: '1.0.0', isSemVerMajor: true },
  prerelease: { name: '@astrojs/starlight', version: '0.39.3-beta.1', isSemVerMajor: true },
  'no-version': { name: '@astrojs/starlight', isSemVerMajor: true },
  'unlocked-parent': { name: 'not-in-the-lockfile', version: '0.0.1', isSemVerMajor: true },
  string: 'yes',
};
for (const [label, fix] of Object.entries(FIXES)) {
  test(`the exception stops matching once npm reports a fix (${label})`, () => {
    const res = run(allowedReport({ fix }), 1);
    assert.notEqual(res.code, 0);
    assert.ok(res.stderr.includes('npm reports a fix for picomatch'), res.stderr);
  });
}

test('a semver-major downgrade of another package is not a fix', () => {
  // The real 2026-11-17 report: npm's only "fix" for picomatch is forcing @astrojs/starlight from 0.42.7 down to 0.39.3.
  const res = run(allowedReport({ fix: DOWNGRADE }), 1);
  assert.equal(res.code, 0, res.stderr);
  assert.ok(
    res.stdout.includes('semver-major downgrade of @astrojs/starlight 0.42.7 -> 0.39.3, not a fix for picomatch'),
    res.stdout
  );
});

test('a semver-major suggestion without a lockfile counts as a fix', () => {
  const res = run(allowedReport({ fix: DOWNGRADE }), 1, { lock: null });
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('npm reports a fix for picomatch'), res.stderr);
});

for (const [label, lock] of Object.entries({ 'not-json': '{not json', 'no-packages': JSON.stringify({ lockfileVersion: 3 }) })) {
  test(`an unreadable lockfile fails (${label})`, () => {
    const res = run(allowedReport({ fix: DOWNGRADE }), 1, { lock });
    assert.notEqual(res.code, 0);
    assert.ok(res.stderr.includes('Failing closed'), res.stderr);
  });
}

function realShapeReport() {
  // The shape of the real site report on 2026-11-17: picomatch reached through the astro toolchain.
  const via = {
    '@astrojs/internal-helpers': ['picomatch'],
    '@astrojs/markdown-satteri': ['@astrojs/internal-helpers'],
    '@astrojs/mdx': ['@astrojs/internal-helpers', '@astrojs/markdown-satteri'],
    '@astrojs/starlight': ['@astrojs/markdown-satteri', '@astrojs/mdx'],
    anymatch: ['picomatch'],
    astro: ['@astrojs/internal-helpers', '@astrojs/markdown-satteri', 'picomatch', 'tinyglobby', 'unstorage', 'vite'],
    fdir: ['picomatch'],
    tinyglobby: ['fdir', 'picomatch'],
    unstorage: ['anymatch'],
    vite: ['picomatch', 'tinyglobby'],
  };
  const vulns = {};
  for (const [name, v] of Object.entries(via)) {
    vulns[name] = vuln(name, v, { fix: name.startsWith('@astrojs/') ? DOWNGRADE : true, range: '*' });
  }
  vulns.picomatch = vuln('picomatch', [advisory()], { fix: DOWNGRADE });
  return report(vulns);
}

test('the real report shape passes with only the picomatch advisory', () => {
  const res = run(realShapeReport(), 1);
  assert.equal(res.code, 0, res.stderr);
  assert.equal(res.stdout.split(`advisory ${SOURCE} `).length - 1, 1);
});

test("the real report shape fails once astro carries its own low and moderate advisories", () => {
  // astro is "high" only through picomatch; its own advisories still count, whatever their severity.
  const rep = realShapeReport();
  rep.vulnerabilities.astro.via.push(
    advisory({ source: 1246685, name: 'astro', ghsa: 'GHSA-mh32-jxcr-mh9f', range: '<=7.4.4', severity: 'low' }),
    advisory({ source: 1246686, name: 'astro', ghsa: 'GHSA-r3h5-4585-7x3w', range: '>=5.14.3 <=7.4.6', severity: 'moderate' })
  );
  const res = run(rep, 1);
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('no valid exception: 1246685, 1246686'), res.stderr);
});

test('an expired exception fails', () => {
  const res = run(allowedReport(), 1, { allow: allowlist({ expires: '2026-11-16' }) });
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('its exception expired on 2026-11-16'), res.stderr);
});

test('an exception naming another package fails', () => {
  const res = run(allowedReport(), 1, { allow: allowlist({ package: 'micromatch' }) });
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('the exception names micromatch'), res.stderr);
});

test('an exception without a range does not apply', () => {
  const res = run(allowedReport(), 1, { allow: allowlist({ range: null }) });
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('it does not apply'), res.stderr);
});

const MALFORMED = {
  'not-json': '{not json',
  empty: '',
  'npm-error': JSON.stringify({ error: { code: 'ENOTFOUND', summary: 'network' } }),
  'no-vulnerabilities': JSON.stringify({ auditReportVersion: 2 }),
  'via-not-a-list': JSON.stringify(report({ picomatch: { severity: 'high', via: 'picomatch' } })),
  'unknown-severity': JSON.stringify(report({ picomatch: { severity: 'severe', via: [advisory()] } })),
};
for (const [label, rep] of Object.entries(MALFORMED)) {
  for (const exitCode of [0, 1]) {
    test(`a malformed audit report fails (${label}, npm exit ${exitCode})`, () => {
      assert.notEqual(run(rep, exitCode).code, 0);
    });
  }
}

test('a non-zero npm exit with nothing flagged fails', () => {
  const res = run(report({}), 1);
  assert.notEqual(res.code, 0);
  assert.ok(res.stderr.includes('Failing closed'), res.stderr);
});

test('the committed allowlist holds exactly the one dated exception', () => {
  const doc = JSON.parse(readFileSync(ALLOWLIST, 'utf-8'));
  assert.ok(doc.description.startsWith('Reviewed, time-boxed npm-audit exceptions'));
  assert.equal(doc.exceptions.length, 1);
  const [entry] = doc.exceptions;
  assert.deepEqual(Object.keys(entry).sort(), ['addedOn', 'approvedBy', 'expiresOn', 'id', 'package', 'range', 'reason']);
  assert.equal(entry.id, String(SOURCE));
  assert.equal(entry.package, 'picomatch');
  assert.equal(entry.range, '<=4.0.7');
  assert.equal(entry.approvedBy, 'mcp-tool-shop');
  assert.equal(entry.addedOn, '2026-11-17');
  assert.equal(entry.expiresOn, '2026-12-17');
  assert.ok(entry.reason.startsWith(`${GHSA} `));
  assert.ok(entry.reason.includes('Remove this entry in the PR that bumps to a patched picomatch.'));
});

test('CI runs these tests and the gate in the Site build step, and triggers on the allowlist', () => {
  const ci = readFileSync(path.join(ROOT, '.github', 'workflows', 'ci.yml'), 'utf-8').replace(/\r\n/g, '\n');
  const step = ci.split('      - name: Site build\n')[1].split('\n      - name: ')[0];
  assert.ok(step.includes('node --test ../scripts/check-audit-allowlist.test.mjs\n'));
  assert.ok(step.includes('npm audit --audit-level=high --json > audit-report.json\n'));
  assert.ok(
    step.includes(
      'node ../scripts/check-audit-allowlist.mjs audit-report.json ../.github/audit-allowlist.json "$AUDIT_EXIT" package-lock.json\n'
    )
  );
  assert.ok(!/npm audit --audit-level=high\n/.test(step));
  assert.ok(ci.includes('      - "scripts/**"\n'));
  assert.ok(ci.includes('      - ".github/audit-allowlist.json"\n'));
});
