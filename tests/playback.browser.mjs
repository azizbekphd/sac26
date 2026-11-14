import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

// Install Chromium with `npx playwright-core install chromium`, or set SAC26_BROWSER_CHANNEL=chrome.
const origin = 'https://sac26.test';
const dist = new URL('../dist/', import.meta.url);
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jPXQAAAAASUVORK5CYII=', 'base64');
const base = { status: 'ready', message: '', frames: [], preview: null, comparison: null, partial: false };

function exposure(index) {
  return {
    key: `e${index}`, observationId: `obs-${index}`, release: 'DR1', detector: 2,
    visit: index === 4 ? 4 : index + 1,
    observedAt: index === 4 ? '2025-01-04T00:01:00Z' : `2025-01-${String(index + 1).padStart(2, '0')}T00:00:00Z`,
    wavelength: 4.2, bandwidth: .05, coverage: 1, sourceUrl: `https://archive.test/e${index}.fits`,
  };
}

function sequence(id, indices) {
  const frames = indices.map((index, i) => ({ ...exposure(index), imageUrl: `/api/playback/${id}/images/frame-${i}.png` }));
  if (id === 'lookup.1') {
    // Later metadata can merge this exposure with obs-2 without merging any
    // of the visits in lookup.2, which uses a different earlier exposure.
    Object.assign(frames[0], { visit: 1, observedAt: '2025-01-02T23:46:00Z' });
    frames[1].visit = 2;
  }
  const exploratory = id === 'manual';
  const result = { ...base, frames, mode: exploratory ? 'exploratory' : 'matched', motionAvailable: true,
    message: exploratory ? 'Some exposures are from the same visit.' : '',
    comparison: { days: 3, overlap: 1, coverage: .95, bandpass: [4.18, 4.22],
      ...(exploratory ? { coverageIncludesBandpass: true } : {}) } };
  const summary = { id, count: frames.length, exposureIds: frames.map((f) => `${f.observationId}:D${f.detector}`),
    start: frames[0].observedAt, end: frames.at(-1).observedAt, bandpass: result.comparison.bandpass };
  return { result, summary };
}

test('keeps playback, details and motion scoped while Auto and Manual overlap', { timeout: 35_000 }, async () => {
  const channel = process.env.SAC26_BROWSER_CHANNEL;
  const browser = await chromium.launch({ channel, chromiumSandbox: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(6_000);
    const sequences = {
      'lookup.1': sequence('lookup.1', [9, 2]),
      'lookup.2': sequence('lookup.2', [0, 1, 2, 3]),
      'lookup.3': sequence('lookup.3', [5, 6]),
      'lookup.4': sequence('lookup.4', [7, 8]),
      manual: sequence('manual', [0, 1, 2, 3, 4]),
    };
    let extend = false;
    let dropped = false;
    let failExtensionImage = true;
    let withdrawFirst = false;
    let later = false;
    let stopped = false;
    const requests = { lookups: 0, motion: [], unexpected: [] };
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin) return route.abort();
      const path = url.pathname;
      const send = (json) => route.fulfill({ json });
      if (path === '/api/playback') {
        requests.lookups++;
        return send({ id: 'lookup' });
      }
      if (path === '/api/playback/lookup') {
        if (request.method() === 'DELETE') {
          stopped = true;
          return route.fulfill({ status: 204 });
        }
        if (extend && !dropped) {
          dropped = true;
          return route.abort('failed');
        }
        const ids = extend ? ['lookup.2', 'lookup.3', ...(later ? ['lookup.4'] : [])] : ['lookup.1'];
        const summaries = ids.map((id) => sequences[id].summary);
        const alternatives = extend && !withdrawFirst ? [sequences['lookup.1'].summary] : [];
        return send({ ...base, status: stopped ? 'cancelled' : 'searching', message: stopped ? 'Search stopped.' : '',
          sequences: summaries, sequenceCandidates: [...summaries, ...alternatives],
          withdrawnSequenceIds: withdrawFirst ? ['lookup.1'] : [] });
      }
      if (path === '/api/manual/inventory') {
        requests.inventorySeed = request.postDataJSON().fromPlayback;
        return send({ id: 'inventory' });
      }
      if (path === '/api/playback/inventory') return send({ ...base, exposures: [4, 3, 2, 1, 0].map(exposure) });
      if (path === '/api/manual/inventory/calibration') return send({});
      if (path === '/api/manual/inventory/comparison') {
        requests.manualKeys = request.postDataJSON().keys;
        return send({ id: 'manual' });
      }
      const match = path.match(/^\/api\/playback\/([^/]+)(?:\/(.+))?$/);
      if (match && sequences[match[1]]) {
        const [, id, action] = match;
        if (!action) return send(sequences[id].result);
        if (action.startsWith('images/')) {
          if (id === 'lookup.2' && failExtensionImage) return route.fulfill({ status: 404 });
          return route.fulfill({ body: png, contentType: 'image/png' });
        }
        if (action === 'motion') {
          const retry = requests.motion.includes(id);
          requests.motion.push(id);
          const [status, displacement, frames, issues] = {
            'lookup.1': ['inspected', 2.1, [0, 1], [null, null]],
            'lookup.2': ['inspected', 3.7, [0, 2, 3], [null, null, null, null]],
            manual: [retry ? 'partial' : 'inspected', 4.2, [0, 2, 4], [null, null, null, 'registration_unconstrained', null]],
          }[id];
          return send({ status, visits: issues, candidates: [{ displacement, uncertainty: .4, score: 40,
            measurements: frames.map((frameIndex) => ({ frameIndex, imagePosition: [.3 + .1 * frameIndex, .5] })),
          }] });
        }
      }
      if (path.startsWith('/api/')) {
        requests.unexpected.push(path);
        return route.fulfill({ status: 404 });
      }
      return route.fulfill({ path: fileURLToPath(new URL(path === '/' ? 'index.html' : path.slice(1), dist)) });
    });

    const button = (name) => page.getByRole('button', { name, exact: true });
    const visible = (value) => page.getByText(value, { exact: true }).waitFor();
    const selector = page.getByRole('combobox', { name: 'Sequence', exact: true });
    const choices = () => selector.locator('option:not([disabled])').evaluateAll((options) => options.map((item) => item.value));
    const image = page.getByRole('img', { name: /North-up SPHEREx detector/ });
    const markers = page.getByRole('img', { name: 'Measured candidate positions' });
    const source = page.getByRole('link', { name: 'Source FITS' });
    const details = page.getByRole('region', { name: 'Comparison details' });
    async function checkFrame(id, frameIndex, exposureIndex) {
      assert.equal(await image.getAttribute('src'), `/api/playback/${id}/images/frame-${frameIndex}.png`);
      await details.getByRole('cell', { name: `obs-${exposureIndex}`, exact: true }).waitFor();
      assert.equal(await source.getAttribute('href'), exposure(exposureIndex).sourceUrl);
    }

    await page.goto(origin);
    await page.getByLabel('Right ascension (RA)').fill('10');
    await page.getByLabel('Declination (Dec)').fill('20');
    await button('Compare visits').click();
    await visible('1 / 2 visits');
    await button('Next').click();
    await button('Details').click();
    await checkFrame('lookup.1', 1, 2);
    await button('Check for motion').click();
    await visible('Detected in 2 of 2 visits');
    await markers.locator('.marker-outline').waitFor();

    extend = true;
    await visible('Reconnecting to the archive search…');
    await visible('Could not load the comparison images. Try again.');
    await checkFrame('lookup.1', 1, 2);
    assert.deepEqual(await choices(), ['lookup.2', 'lookup.3']);
    await visible('Detected in 2 of 2 visits');
    withdrawFirst = true;
    await visible('New archive metadata changed the visit grouping. Choose a current sequence to check for motion.');
    await markers.waitFor({ state: 'hidden' });
    failExtensionImage = false;
    await button('Retry loading sequence').click();
    await visible('3 / 4 visits');
    await checkFrame('lookup.2', 2, 2);
    await markers.waitFor({ state: 'hidden' });
    await button('Check for motion').click();
    await visible('Detected in 3 of 4 visits');
    assert.equal(await markers.locator('circle.current').getAttribute('cx'), '50');

    await button('Edit exposures').click();
    const manual = page.getByRole('dialog');
    await manual.getByRole('heading', { name: 'Selected exposures (4)' }).waitFor();
    await manual.getByRole('checkbox', { name: /obs-4/ }).check();
    await button('Apply selection').click();
    await button('Open Exploratory blink').click();
    await visible('1 / 5 frames');
    await visible('Searching for more sequences…');
    assert.match(await selector.locator('option:checked').textContent(), /5 exposures across 4 visits.*Manual/);
    assert.deepEqual(await choices(), ['manual', 'lookup.3']);
    assert.equal(requests.inventorySeed, 'lookup.2');
    assert.deepEqual(requests.manualKeys, ['e0', 'e1', 'e2', 'e3', 'e4']);
    await button('Details').click();
    await checkFrame('manual', 0, 0);
    await markers.waitFor({ state: 'hidden' });
    await button('Check for motion').click();
    await visible('Motion analysis was partial.');
    await visible('Could not evaluate exposure 4.');
    await visible('Detected in 3 of 4 visits');
    await button('Retry motion analysis').click();
    await visible('Motion analysis was partial.');
    await markers.locator('.marker-outline').waitFor();

    later = true;
    await selector.locator('option[value="lookup.4"]').waitFor({ state: 'attached' });
    await checkFrame('manual', 0, 0);
    assert.equal(await selector.inputValue(), 'manual');
    await button('Stop search').click();
    await visible('Search stopped.');
    await selector.selectOption('lookup.3');
    await visible('1 / 2 visits');
    await markers.waitFor({ state: 'hidden' });
    await button('Details').click();
    await checkFrame('lookup.3', 0, 5);
    await selector.selectOption('manual');
    await visible('1 / 5 frames');
    await visible('Detected in 3 of 4 visits');
    await markers.locator('.marker-outline').waitFor();
    assert.equal(requests.lookups, 1);
    assert.deepEqual(requests.motion, ['lookup.1', 'lookup.2', 'manual', 'manual']);
    assert.deepEqual(requests.unexpected, []);
  } finally {
    await browser.close();
  }
});
