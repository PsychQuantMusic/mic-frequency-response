import Ajv from 'ajv/dist/2020.js';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const ROOT = new URL('../../', import.meta.url);
const manifestSchemaPath = new URL('schemas/source-manifest-v1.schema.json', ROOT);
const redactionSchemaPath = new URL('schemas/source-url-redactions-v1.schema.json', ROOT);
const sourceHostsPath = new URL('config/source-hosts.json', ROOT);
const sourceRedactionsPath = new URL('config/source-url-redactions.json', ROOT);
const packageLockPath = new URL('digitizer/package-lock.json', ROOT);
const fixtureDirectory = join(import.meta.dirname, 'fixtures/source-manifests');

function loadFixture(name) {
  return JSON.parse(readFileSync(join(fixtureDirectory, name), 'utf8'));
}

function compile(schemaPath) {
  const schema = JSON.parse(readFileSync(schemaPath, 'utf8'));
  return new Ajv({ strict: true }).compile(schema);
}

test('source contract schemas exist', () => {
  const missing = [
    manifestSchemaPath,
    redactionSchemaPath,
    sourceHostsPath,
    sourceRedactionsPath,
    packageLockPath,
  ]
    .filter((path) => !existsSync(path))
    .map((path) => fileURLToPath(path));
  assert.deepEqual(missing, []);
});

test('manifest schema rejects an unknown top-level field', () => {
  const validate = compile(manifestSchemaPath);
  const fixture = loadFixture('derived-chain.json');
  fixture.unknown = true;
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('manifest schema allows exactly five role and availability variants', () => {
  const validate = compile(manifestSchemaPath);
  const allowed = [
    ['available-pdf.json', 0, 'original/available'],
    ['pending-pdf.json', 0, 'original/pending'],
    ['unavailable-redacted.json', 0, 'original/unavailable'],
    ['derived-chain.json', 1, 'rendered-page/available'],
    ['derived-chain.json', 2, 'chart-crop/available'],
  ];

  for (const [name, artifactIndex, label] of allowed) {
    const fixture = loadFixture(name);
    fixture.artifacts = [fixture.artifacts[artifactIndex]];
    assert.equal(validate(fixture), true, `${label}: ${JSON.stringify(validate.errors)}`);
  }

  for (const [role, availability] of [
    ['rendered-page', 'pending'],
    ['rendered-page', 'unavailable'],
    ['chart-crop', 'pending'],
    ['chart-crop', 'unavailable'],
    ['reference', 'available'],
  ]) {
    const fixture = loadFixture('derived-chain.json');
    fixture.artifacts[1].role = role;
    fixture.artifacts[1].availability = availability;
    assert.equal(validate(fixture), false, `${role}/${availability} was accepted`);
  }
});

test('manifest schema rejects PNG as an original artifact', () => {
  const validate = compile(manifestSchemaPath);
  const fixture = loadFixture('available-pdf.json');
  fixture.artifacts[0].local_path = 'source/original.png';
  fixture.artifacts[0].media_type = 'image/png';
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('manifest schema rejects non-local redistribution', () => {
  const validate = compile(manifestSchemaPath);
  const fixture = loadFixture('available-pdf.json');
  fixture.artifacts[0].redistribution = 'redistributable';
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('manifest schema requires derived_from_sha256 on derived artifacts', () => {
  const validate = compile(manifestSchemaPath);
  for (const artifactIndex of [1, 2]) {
    const fixture = loadFixture('derived-chain.json');
    delete fixture.artifacts[artifactIndex].derived_from_sha256;
    assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
  }
});

test('manifest schema rejects non-UTC RFC 3339 timestamps', () => {
  const validate = compile(manifestSchemaPath);
  const cases = [
    ['available-pdf.json', 0, 'retrieved_at'],
    ['unavailable-redacted.json', 0, 'checked_at'],
    ['derived-chain.json', 1, 'generated_at'],
    ['derived-chain.json', 2, 'generated_at'],
  ];

  for (const [name, artifactIndex, field] of cases) {
    const fixture = loadFixture(name);
    fixture.artifacts[artifactIndex][field] = '2026-07-15T12:00:00+08:00';
    assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
  }
});

test('UTC timestamp rules reject nonexistent dates and allow leap days', () => {
  const manifestValidate = compile(manifestSchemaPath);
  for (const timestamp of ['2026-02-31T12:00:00Z', '2023-02-29T12:00:00Z']) {
    const fixture = loadFixture('available-pdf.json');
    fixture.artifacts[0].retrieved_at = timestamp;
    assert.equal(manifestValidate(fixture), false, `${timestamp}: ${JSON.stringify(manifestValidate.errors)}`);
  }

  const leapManifest = loadFixture('available-pdf.json');
  leapManifest.artifacts[0].retrieved_at = '2024-02-29T12:00:00Z';
  assert.equal(manifestValidate(leapManifest), true, JSON.stringify(manifestValidate.errors));

  const redactionValidate = compile(redactionSchemaPath);
  const invalidRedaction = loadFixture('redaction-replaced.json');
  invalidRedaction.entries[0].checked_at = '2026-02-31T12:00:00Z';
  assert.equal(redactionValidate(invalidRedaction), false, JSON.stringify(redactionValidate.errors));

  const leapRedaction = loadFixture('redaction-replaced.json');
  leapRedaction.entries[0].checked_at = '2024-02-29T12:00:00Z';
  assert.equal(redactionValidate(leapRedaction), true, JSON.stringify(redactionValidate.errors));
});

test('pending originals reject fields from other lifecycle states', () => {
  const validate = compile(manifestSchemaPath);
  const forbidden = {
    size_bytes: 123456,
    sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    retrieved_at: '2026-07-15T12:00:00Z',
    checked_at: '2026-07-15T12:00:00Z',
    reason: 'not available',
    redaction_ref: 'redacted-source',
    generated_at: '2026-07-15T12:00:00Z',
    derived_from: 'parent',
    derived_from_sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    derivation: {},
  };

  for (const [field, value] of Object.entries(forbidden)) {
    const fixture = loadFixture('pending-pdf.json');
    fixture.artifacts[0][field] = value;
    assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
  }
});

test('available originals reject unavailable and derived fields', () => {
  const validate = compile(manifestSchemaPath);
  const forbidden = {
    checked_at: '2026-07-15T12:00:00Z',
    reason: 'not available',
    redaction_ref: 'redacted-source',
    generated_at: '2026-07-15T12:00:00Z',
    derived_from: 'parent',
    derived_from_sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    derivation: {},
  };

  for (const [field, value] of Object.entries(forbidden)) {
    const fixture = loadFixture('available-pdf.json');
    fixture.artifacts[0][field] = value;
    assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
  }
});

test('unavailable originals reject local, available, and derived fields', () => {
  const validate = compile(manifestSchemaPath);
  const forbidden = {
    local_path: 'source/original.pdf',
    media_type: 'application/pdf',
    size_bytes: 123456,
    sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    retrieved_at: '2026-07-15T12:00:00Z',
    generated_at: '2026-07-15T12:00:00Z',
    derived_from: 'parent',
    derived_from_sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
    derivation: {},
  };

  for (const [field, value] of Object.entries(forbidden)) {
    const fixture = loadFixture('unavailable-redacted.json');
    fixture.artifacts[0][field] = value;
    assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
  }
});

test('unavailable originals require exactly one source locator', () => {
  const validate = compile(manifestSchemaPath);
  const redacted = loadFixture('unavailable-redacted.json');
  assert.equal(validate(redacted), true, JSON.stringify(validate.errors));

  const stableUrl = loadFixture('unavailable-redacted.json');
  delete stableUrl.artifacts[0].redaction_ref;
  stableUrl.artifacts[0].source_url = 'https://www.neumann.com/expired.svg';
  stableUrl.artifacts[0].allowed_hosts = ['www.neumann.com'];
  assert.equal(validate(stableUrl), true, JSON.stringify(validate.errors));

  const both = structuredClone(stableUrl);
  both.artifacts[0].redaction_ref = 'neumann-tlm102-source-asset';
  assert.equal(validate(both), false, JSON.stringify(validate.errors));

  const neither = loadFixture('unavailable-redacted.json');
  delete neither.artifacts[0].redaction_ref;
  assert.equal(validate(neither), false, JSON.stringify(validate.errors));
});

test('derived artifacts require the PNG media pair', () => {
  const validate = compile(manifestSchemaPath);
  for (const artifactIndex of [1, 2]) {
    const fixture = loadFixture('derived-chain.json');
    fixture.artifacts[artifactIndex].local_path = 'source/derived.svg';
    fixture.artifacts[artifactIndex].media_type = 'image/svg+xml';
    assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
  }
});

test('manifest SHA fields require 64 lowercase hexadecimal characters', () => {
  const validate = compile(manifestSchemaPath);
  const cases = [
    ['available-pdf.json', 0, 'sha256'],
    ['derived-chain.json', 1, 'derived_from_sha256'],
    ['derived-chain.json', 2, 'sha256'],
  ];

  for (const [name, artifactIndex, field] of cases) {
    const fixture = loadFixture(name);
    fixture.artifacts[artifactIndex][field] = 'ABCDEF';
    assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
  }
});

test('each manifest variant requires its complete field set', () => {
  const validate = compile(manifestSchemaPath);
  const cases = [
    ['pending-pdf.json', 0, ['id', 'source_url', 'local_path', 'media_type', 'curve_files', 'redistribution', 'allowed_hosts']],
    ['available-pdf.json', 0, ['id', 'source_url', 'local_path', 'media_type', 'curve_files', 'size_bytes', 'sha256', 'retrieved_at', 'redistribution', 'allowed_hosts']],
    ['unavailable-redacted.json', 0, ['id', 'redaction_ref', 'curve_files', 'checked_at', 'reason', 'redistribution']],
    ['derived-chain.json', 1, ['id', 'local_path', 'media_type', 'size_bytes', 'sha256', 'generated_at', 'redistribution', 'derived_from', 'derived_from_sha256', 'derivation']],
    ['derived-chain.json', 2, ['id', 'local_path', 'media_type', 'size_bytes', 'sha256', 'generated_at', 'redistribution', 'derived_from', 'derived_from_sha256', 'curve_files', 'derivation']],
  ];

  for (const [name, artifactIndex, fields] of cases) {
    for (const field of fields) {
      const fixture = loadFixture(name);
      delete fixture.artifacts[artifactIndex][field];
      assert.equal(validate(fixture), false, `${name} ${field}: ${JSON.stringify(validate.errors)}`);
    }
  }
});

test('derived artifact variants require their complete derivation fields', () => {
  const validate = compile(manifestSchemaPath);
  const cases = [
    [1, ['page', 'render_dpi', 'render_tool', 'render_tool_version']],
    [2, ['crop_box_px', 'crop_tool', 'crop_tool_version']],
  ];

  for (const [artifactIndex, fields] of cases) {
    for (const field of fields) {
      const fixture = loadFixture('derived-chain.json');
      delete fixture.artifacts[artifactIndex].derivation[field];
      assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
    }
  }

  const wrongDpi = loadFixture('derived-chain.json');
  wrongDpi.artifacts[1].derivation.render_dpi = 299;
  assert.equal(validate(wrongDpi), false, JSON.stringify(validate.errors));

  const wrongCropBox = loadFixture('derived-chain.json');
  wrongCropBox.artifacts[2].derivation.crop_box_px = [0, 0, 100];
  assert.equal(validate(wrongCropBox), false, JSON.stringify(validate.errors));
});

test('manifest top level requires the schema-v1 fields', () => {
  const validate = compile(manifestSchemaPath);
  for (const field of ['schema_version', 'mic_slug', 'references', 'artifacts']) {
    const fixture = loadFixture('available-pdf.json');
    delete fixture[field];
    assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
  }

  const wrongVersion = loadFixture('available-pdf.json');
  wrongVersion.schema_version = 2;
  assert.equal(validate(wrongVersion), false, JSON.stringify(validate.errors));
});

test('manifest references allow only closed HTTPS provenance variants', () => {
  const validate = compile(manifestSchemaPath);
  for (const role of ['product-page', 'landing-page', 'documentation-page']) {
    const fixture = loadFixture('available-pdf.json');
    fixture.references = [{ role, url: 'https://pubs.shure.com/product' }];
    assert.equal(validate(fixture), true, `${role}: ${JSON.stringify(validate.errors)}`);
  }

  const invalidRole = loadFixture('available-pdf.json');
  invalidRole.references = [{ role: 'download-page', url: 'https://pubs.shure.com/product' }];
  assert.equal(validate(invalidRole), false, JSON.stringify(validate.errors));

  const insecure = loadFixture('available-pdf.json');
  insecure.references = [{ role: 'product-page', url: 'http://pubs.shure.com/product' }];
  assert.equal(validate(insecure), false, JSON.stringify(validate.errors));

  const unknown = loadFixture('available-pdf.json');
  unknown.references = [{ role: 'product-page', url: 'https://pubs.shure.com/product', note: 'extra' }];
  assert.equal(validate(unknown), false, JSON.stringify(validate.errors));
});

test('schema URL fields require a hostname and forbid userinfo, controls, and fragments', () => {
  const invalidUrls = [
    'https://?token=secret',
    'https://user:pass@pubs.shure.com/manual.pdf',
    'https://pubs.shure.com/manual\n.pdf',
    'https://pubs.shure.com/manual\u0001.pdf',
    'https://pubs.shure.com/manual.pdf#frequency-response',
  ];
  const manifestValidate = compile(manifestSchemaPath);

  for (const url of invalidUrls) {
    const sourceFixture = loadFixture('available-pdf.json');
    sourceFixture.artifacts[0].source_url = url;
    assert.equal(manifestValidate(sourceFixture), false, `source_url ${JSON.stringify(url)}: ${JSON.stringify(manifestValidate.errors)}`);

    const referenceFixture = loadFixture('available-pdf.json');
    referenceFixture.references = [{ role: 'product-page', url }];
    assert.equal(manifestValidate(referenceFixture), false, `reference ${JSON.stringify(url)}: ${JSON.stringify(manifestValidate.errors)}`);
  }

  const opaquePath = loadFixture('available-pdf.json');
  opaquePath.artifacts[0].source_url = 'https://www.neumann.com/svg/ZzY3NTk2N2YtY2hhcnQ==';
  opaquePath.artifacts[0].allowed_hosts = ['www.neumann.com'];
  assert.equal(manifestValidate(opaquePath), true, JSON.stringify(manifestValidate.errors));

  const harmlessSourceQuery = loadFixture('available-pdf.json');
  harmlessSourceQuery.artifacts[0].source_url = 'https://products.electrovoice.com/re320?id=968996';
  harmlessSourceQuery.artifacts[0].allowed_hosts = ['products.electrovoice.com'];
  assert.equal(manifestValidate(harmlessSourceQuery), true, JSON.stringify(manifestValidate.errors));

  const harmlessReferenceQuery = loadFixture('available-pdf.json');
  harmlessReferenceQuery.references = [{ role: 'product-page', url: 'https://products.electrovoice.com/re320?id=968996' }];
  assert.equal(manifestValidate(harmlessReferenceQuery), true, JSON.stringify(manifestValidate.errors));

  const redactionValidate = compile(redactionSchemaPath);
  for (const url of invalidUrls) {
    const fixture = loadFixture('redaction-replaced.json');
    fixture.entries[0].canonical_url = url;
    assert.equal(redactionValidate(fixture), false, `canonical_url ${JSON.stringify(url)}: ${JSON.stringify(redactionValidate.errors)}`);
  }

  const canonicalOpaquePath = loadFixture('redaction-replaced.json');
  canonicalOpaquePath.entries[0].canonical_url = 'https://www.neumann.com/svg/ZzY3NTk2N2YtY2hhcnQ==';
  assert.equal(redactionValidate(canonicalOpaquePath), true, JSON.stringify(redactionValidate.errors));
});

test('manifest artifact and derivation objects are closed', () => {
  const validate = compile(manifestSchemaPath);
  const cases = [
    ['pending-pdf.json', 0],
    ['available-pdf.json', 0],
    ['unavailable-redacted.json', 0],
    ['derived-chain.json', 1],
    ['derived-chain.json', 2],
  ];

  for (const [name, artifactIndex] of cases) {
    const fixture = loadFixture(name);
    fixture.artifacts[artifactIndex].unknown = true;
    assert.equal(validate(fixture), false, `${name}: ${JSON.stringify(validate.errors)}`);
  }

  for (const artifactIndex of [1, 2]) {
    const fixture = loadFixture('derived-chain.json');
    fixture.artifacts[artifactIndex].derivation.unknown = true;
    assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
  }
});

test('redaction schema rejects an unknown top-level field', () => {
  const validate = compile(redactionSchemaPath);
  const fixture = loadFixture('redaction-replaced.json');
  fixture.unknown = true;
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('redaction schema allows exactly two disposition variants', () => {
  const validate = compile(redactionSchemaPath);
  for (const name of ['redaction-replaced.json', 'redaction-no-stable-endpoint.json']) {
    const fixture = loadFixture(name);
    assert.equal(validate(fixture), true, `${name}: ${JSON.stringify(validate.errors)}`);
  }

  const invalid = loadFixture('redaction-replaced.json');
  invalid.entries[0].disposition = 'ignored';
  assert.equal(validate(invalid), false, JSON.stringify(validate.errors));
});

test('redaction schema rejects exact duplicate entries', () => {
  const validate = compile(redactionSchemaPath);
  const fixture = loadFixture('redaction-replaced.json');
  fixture.entries.push(structuredClone(fixture.entries[0]));
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('redaction schema requires a lowercase SHA-256 digest', () => {
  const validate = compile(redactionSchemaPath);
  const fixture = loadFixture('redaction-replaced.json');
  fixture.entries[0].legacy_url_sha256 = 'ABCDEF';
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('redaction schema rejects non-UTC RFC 3339 timestamps', () => {
  const validate = compile(redactionSchemaPath);
  const fixture = loadFixture('redaction-replaced.json');
  fixture.entries[0].checked_at = '2026-07-15T12:00:00+08:00';
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('replaced redactions require a canonical HTTPS URL', () => {
  const validate = compile(redactionSchemaPath);
  const fixture = loadFixture('redaction-replaced.json');
  delete fixture.entries[0].canonical_url;
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('replaced redaction canonical URLs reject every query string', () => {
  const validate = compile(redactionSchemaPath);
  const queries = [
    '?id=968996',
    '?access_token=secret',
    '?%61ccess_token=secret',
    '?token=secret',
    '?X-Amz-Signature=secret',
  ];

  for (const query of queries) {
    const fixture = loadFixture('redaction-replaced.json');
    fixture.entries[0].canonical_url = `https://www.neumann.com/source-asset.svg${query}`;
    assert.equal(validate(fixture), false, `${query}: ${JSON.stringify(validate.errors)}`);
  }
});

test('no-stable-endpoint redactions forbid canonical URLs', () => {
  const validate = compile(redactionSchemaPath);
  const fixture = loadFixture('redaction-no-stable-endpoint.json');
  fixture.entries[0].canonical_url = 'https://www.neumann.com/source-asset.svg';
  assert.equal(validate(fixture), false, JSON.stringify(validate.errors));
});

test('redaction schema requires the schema-v1 entry fields', () => {
  const validate = compile(redactionSchemaPath);
  for (const name of ['redaction-replaced.json', 'redaction-no-stable-endpoint.json']) {
    for (const field of ['id', 'mic_slug', 'source_field', 'legacy_url_sha256', 'checked_at', 'disposition']) {
      const fixture = loadFixture(name);
      delete fixture.entries[0][field];
      assert.equal(validate(fixture), false, `${name} ${field}: ${JSON.stringify(validate.errors)}`);
    }
  }

  for (const field of ['schema_version', 'entries']) {
    const fixture = loadFixture('redaction-replaced.json');
    delete fixture[field];
    assert.equal(validate(fixture), false, `${field}: ${JSON.stringify(validate.errors)}`);
  }

  const wrongVersion = loadFixture('redaction-replaced.json');
  wrongVersion.schema_version = 2;
  assert.equal(validate(wrongVersion), false, JSON.stringify(validate.errors));
});

test('redaction entries reject unknown and raw credential URL fields', () => {
  const validate = compile(redactionSchemaPath);
  for (const name of ['redaction-replaced.json', 'redaction-no-stable-endpoint.json']) {
    for (const field of ['unknown', 'source_url', 'legacy_url', 'raw_url']) {
      const fixture = loadFixture(name);
      fixture.entries[0][field] = 'https://example.invalid/?token=secret';
      assert.equal(validate(fixture), false, `${name} ${field}: ${JSON.stringify(validate.errors)}`);
    }
  }
});

test('source host policy contains the exact 13 approved hosts', () => {
  const policy = JSON.parse(readFileSync(sourceHostsPath, 'utf8'));
  assert.deepEqual(policy, {
    schema_version: 1,
    hosts: [
      'assets.sennheiser.com',
      'audixusa.com',
      'content-files.shure.com',
      'docs.audio-technica.com',
      'docs.cloud.sennheiser.com',
      'edge.rode.com',
      'products.electrovoice.com',
      'pubs.shure.com',
      'seelectronics.com',
      'www.akg.com',
      'www.lewitt-audio.com',
      'www.neumann.com',
      'www.sennheiser.com',
    ],
  });
});

test('source URL redaction registry starts empty at schema version 1', () => {
  const registry = JSON.parse(readFileSync(sourceRedactionsPath, 'utf8'));
  assert.deepEqual(registry, {
    schema_version: 1,
    entries: [],
  });
});
