import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

// Install Chromium with `npx playwright-core install chromium`, or set SAC26_BROWSER_CHANNEL=chrome.
const origin = 'https://sac26.test';
const dist = new URL('../dist/', import.meta.url);
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jPXQAAAAASUVORK5CYII=', 'base64');
const base = { status: 'ready', message: '', frames: [], preview: null, comparison: null, partial: false };
const embedParent = 'https://catalog.test';

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

async function routeCoordinateViewer(context, lookupStatus) {
  const requests = [];
  const match = sequence('coordinate-sequence', [0, 1]);
  const alternative = sequence('other-sequence', [2, 3]);
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === embedParent) {
      const width = url.searchParams.get('width') ?? '800';
      return route.fulfill({ contentType: 'text/html', body: `<iframe title="SPHEREx" width="${width}" height="560"
        src="${origin}/embed?ra=127.69444&amp;dec=-39.1776&amp;fieldSize=6"></iframe>` });
    }
    if (url.origin !== origin) return route.abort();
    if (url.pathname === '/api/playback') {
      requests.push(request.postDataJSON());
      return route.fulfill({ json: { id: String(requests.length) } });
    }
    if (url.pathname.startsWith('/api/playback/')) {
      if (request.method() === 'DELETE') return route.fulfill({ status: 204 });
      if (url.pathname.endsWith('.png')) return route.fulfill({ body: png, contentType: 'image/png' });
      if (url.pathname.endsWith('/coordinate-sequence')) return route.fulfill({ json: match.result });
      if (url.pathname.endsWith('/other-sequence')) return route.fulfill({ json: alternative.result });
      const id = Number(url.pathname.split('/').at(-1));
      const ready = { ...base, sequences: [match.summary, alternative.summary] };
      const json = await lookupStatus(requests[id - 1], id, ready);
      return route.fulfill({ json });
    }
    if (url.pathname === '/' || url.pathname === '/embed') {
      const headers = { 'Content-Security-Policy': url.pathname === '/embed'
        ? "frame-ancestors 'self' https:" : "frame-ancestors 'none'" };
      return route.fulfill({ body: await readFile(new URL('index.html', dist)), contentType: 'text/html', headers });
    }
    return route.fulfill({ path: fileURLToPath(new URL(url.pathname.slice(1), dist)) });
  });
  return requests;
}

test('opens and shares coordinate searches and restores them across browser history', { timeout: 25_000 }, async () => {
  const browser = await chromium.launch({ channel: process.env.SAC26_BROWSER_CHANNEL, chromiumSandbox: true });
  const staleLookup = Promise.withResolvers();
  const stalePoll = Promise.withResolvers();
  try {
    const context = await browser.newContext();
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
    const page = await context.newPage();
    page.setDefaultTimeout(6_000);
    const currentParams = () => page.evaluate(() => Object.fromEntries(new URLSearchParams(location.search)));
    async function checkCopyRows(surface) {
      for (const name of ['Patch', 'Embed']) {
        const button = surface.getByRole('button', { name: `Copy ${name.toLowerCase()} link` });
        assert.equal(await button.textContent(), 'Copy');
        const inputBox = await surface.getByLabel(`${name} URL`, { exact: true }).boundingBox();
        const buttonBox = await button.boundingBox();
        assert.ok(inputBox && buttonBox && buttonBox.x >= inputBox.x + inputBox.width);
        assert.ok(buttonBox.y < inputBox.y + inputBox.height && inputBox.y < buttonBox.y + buttonBox.height);
      }
    }
    await context.addInitScript(() => sessionStorage.setItem('sac26.target-preset', 'WISE 0855'));
    const requests = await routeCoordinateViewer(context, async (patch, id, ready) => {
      if (id === 3) {
        stalePoll.resolve();
        await staleLookup.promise;
        return { ...base, status: 'error', message: 'Stale lookup error' };
      }
      return patch.ra === 0 ? { ...base, status: 'no-match', message: 'No comparable visits.' } : ready;
    });
    await page.goto(`${origin}/?ra=0&dec=-90&campaign=demo`);
    await page.getByText('No comparable visits.', { exact: true }).waitFor();
    assert.equal(requests.length, 1);
    assert.equal(await page.locator('#ra').inputValue(), '0');
    assert.equal(await page.evaluate(() => sessionStorage.getItem('sac26.target-preset')), 'WISE 0855');
    assert.deepEqual(requests[0], {
      ra: 0, dec: -90, fieldSize: 6, release: null, startDate: null, endDate: null,
      minWavelength: null, maxWavelength: null, minGapDays: 0, maxGapDays: null,
    });

    await page.locator('#preset').selectOption('R Draconis');
    assert.ok(Number((await currentParams()).ra) > 0);
    await page.locator('#ra').fill('08h00m00s');
    await page.locator('#dec').fill('');
    assert.equal((await currentParams()).dec, undefined);
    await page.locator('#dec').fill('+90d00m00s');
    await page.locator('#field-size').press('ArrowRight');
    await page.getByText('Filters', { exact: true }).click();
    await page.locator('#patch-release').selectOption('qr3');
    await page.locator('#startDate').fill('2024-02-29');
    const wavelength = page.locator('#minWavelength');
    await wavelength.fill('-1');
    assert.equal((await currentParams()).minWavelength, '-1');
    const picker = page.locator('#patch-drawer');
    assert.equal(await picker.getByText('Share patch', { exact: true }).count(), 0);
    await wavelength.fill('2');
    await wavelength.press('End');
    await wavelength.press('e');
    assert.equal(await wavelength.evaluate((input) => input.validity.badInput), true);
    assert.equal((await currentParams()).minWavelength, '2');
    assert.equal(await picker.getByRole('button', { name: 'Share patch' }).count(), 0);
    await page.getByRole('button', { name: 'Compare visits' }).click();
    await picker.getByText('Complete this filter or clear it.').waitFor();
    await wavelength.fill('1e-7');
    await page.locator('#maxGapDays').fill('40');
    assert.deepEqual(await currentParams(), {
      campaign: 'demo', ra: '120', dec: '90', fieldSize: '12', release: 'qr3',
      startDate: '2024-02-29', minWavelength: '0.0000001', maxGapDays: '40',
    });
    assert.equal(requests.length, 1);
    const pickerShare = picker.getByRole('button', { name: 'Share patch' });
    await pickerShare.click();
    const dialog = page.getByRole('dialog', { name: 'Share patch' });
    await checkCopyRows(dialog);
    const draftEmbed = await dialog.getByLabel('Embed URL').inputValue();
    await dialog.getByRole('button', { name: 'Copy embed link' }).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), draftEmbed);
    assert.equal(new URL(draftEmbed).pathname, '/embed');
    assert.equal(new URL(draftEmbed).searchParams.get('fieldSize'), '12');
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await page.waitForFunction(() => document.activeElement?.textContent === 'Share patch');
    await page.getByRole('button', { name: 'Compare visits' }).click();
    await page.getByRole('button', { name: 'Play', exact: true }).waitFor();
    await page.setViewportSize({ width: 320, height: 720 });
    await page.getByRole('group', { name: 'Workspace tools' }).getByRole('button', { name: 'Share', exact: true }).click();
    const sharePanel = page.getByRole('region', { name: 'Share', exact: true });
    await checkCopyRows(sharePanel);
    const shared = await sharePanel.getByLabel('Patch URL').inputValue();
    const markup = await sharePanel.getByLabel('Iframe HTML').inputValue();
    assert.match(markup, /\/embed\?ra=120&amp;dec=90&amp;fieldSize=12/);
    assert.equal(await sharePanel.getByLabel('Embed URL').inputValue(), draftEmbed);
    assert.equal(new URL(shared).searchParams.get('minWavelength'), '0.0000001');
    assert.deepEqual(requests[1], {
      ...requests[0], ra: 120, dec: 90, fieldSize: 12, release: 'qr3', startDate: '2024-02-29', minWavelength: 1e-7, maxGapDays: 40,
    });

    await page.goBack();
    await stalePoll.promise;
    assert.equal(await page.locator('#ra').inputValue(), '0');
    await page.goForward();
    await page.getByRole('button', { name: 'Play', exact: true }).waitFor();
    staleLookup.resolve();
    assert.equal(await page.getByText('Stale lookup error').count(), 0);
    assert.deepEqual(requests[3], requests[1]);

    const receiver = await context.newPage();
    await receiver.goto(shared);
    await receiver.getByRole('button', { name: 'Play', exact: true }).waitFor();
    assert.deepEqual(requests[4], requests[1]);
    await receiver.getByRole('button', { name: 'Back to sky' }).click();
    assert.equal(new URL(receiver.url()).search, new URL(shared).search);
    assert.equal(await receiver.locator('#ra').inputValue(), '120');
    await receiver.reload();
    await receiver.getByRole('button', { name: 'Compare visits' }).waitFor();
    assert.equal(await receiver.locator('#ra').inputValue(), '120');
    for (const [query, diagnostic] of [
      ['ra=20', 'dec'], ['ra=&dec=0', 'ra'], ['ra=1&ra=2&dec=0', 'ra'],
      ['ra=Infinity&dec=0', 'ra'], ['ra=360&dec=0', 'ra'], ['ra=1&dec=91', 'dec'],
      ['ra=1&dec=0&fieldSize=4', 'fieldSize'], ['ra=1&dec=0&startDate=2026-02-29', 'startDate'],
      ['ra=1&dec=0&minGapDays=2&maxGapDays=1', 'maxGapDays'],
    ]) {
      await receiver.goto(`${origin}/?${query}`);
      assert.match(await receiver.getByRole('alert').textContent(), new RegExp(diagnostic));
    }
    assert.equal(requests.length, 5);
    await receiver.goto(`${origin}/?ra=359.99999999999994&dec=%2B0`);
    await receiver.getByRole('button', { name: 'Play', exact: true }).waitFor();
    assert.equal(requests[5].ra, 359.99999999999994);
  } finally {
    staleLookup.resolve();
    await browser.close();
  }
});

test('clears the filter badge and submitted date after deleting every date segment', { timeout: 20_000 }, async () => {
  const browser = await chromium.launch({ channel: process.env.SAC26_BROWSER_CHANNEL, chromiumSandbox: true });
  try {
    const context = await browser.newContext();
    const requests = await routeCoordinateViewer(context, async (_patch, _id, ready) => ready);
    const page = await context.newPage();
    page.setDefaultTimeout(5_000);
    await page.goto(origin);
    await page.locator('#ra').fill('10');
    await page.locator('#dec').fill('20');
    const badge = page.locator('.patch-filters summary');
    await badge.click();
    const date = page.locator('#startDate');
    await date.fill('2026-01-08');
    await date.press('ArrowLeft');
    await date.press('ArrowLeft');
    await date.press('Backspace');
    assert.equal(await date.evaluate((input) => input.validity.badInput), true);
    assert.equal(new URL(page.url()).searchParams.get('startDate'), '2026-01-08');

    await date.press('ArrowRight');
    await date.press('Backspace');
    await date.press('ArrowRight');
    await date.press('Backspace');
    assert.equal(await date.evaluate((input) => input.validity.badInput), false);
    assert.equal(await date.inputValue(), '');
    assert.equal(await badge.textContent(), 'Filters');
    assert.equal(new URL(page.url()).searchParams.has('startDate'), false);

    await page.getByRole('button', { name: 'Compare visits' }).click();
    await page.getByRole('button', { name: 'Play', exact: true }).waitFor();
    assert.equal(requests[0].startDate, null);
  } finally {
    await browser.close();
  }
});

test('plays a fixed sky patch inside a different origin', { timeout: 25_000 }, async () => {
  const browser = await chromium.launch({ channel: process.env.SAC26_BROWSER_CHANNEL, chromiumSandbox: true });
  try {
    const context = await browser.newContext();
    const requests = await routeCoordinateViewer(context, (_patch, id, ready) => id === 1
      ? { ...base, status: 'no-match', partial: true, message: 'Search incomplete.' } : ready);
    const page = await context.newPage();
    page.setDefaultTimeout(6_000);
    const atlasRequests = [];
    context.on('request', (request) => {
      if (request.url().includes('aladin') || request.url().includes('Allsky.jpg')) atlasRequests.push(request.url());
    });
    for (const width of [320, 800]) {
      await page.goto(`${embedParent}/?width=${width}`);
      const player = page.frameLocator('iframe');
      if (width === 320) {
        await player.getByText('Search incomplete.').waitFor();
        await player.getByRole('button', { name: 'Retry', exact: true }).click();
      }
      await player.getByText('1 / 2 visits', { exact: true }).waitFor();
      assert.deepEqual(await player.getByRole('main').evaluate((element) => ({
        horizontalOverflow: element.scrollWidth - element.clientWidth,
        verticalOverflow: element.scrollHeight - element.clientHeight,
      })), { horizontalOverflow: 0, verticalOverflow: 0 });
      assert.equal(await player.getByRole('combobox', { name: 'Sequence' }).inputValue(), 'coordinate-sequence');
      const play = player.getByRole('button', { name: 'Play', exact: true });
      await play.click();
      await player.getByRole('button', { name: 'Pause', exact: true }).click();
      await player.getByRole('button', { name: 'Next' }).click();
      await player.getByText('2 / 2 visits', { exact: true }).waitFor();
      await player.getByText('2025-01-02 00:00:00 UTC', { exact: true }).waitFor();
      const link = player.getByRole('link', { name: 'Open full viewer' });
      assert.equal(await link.getAttribute('href'), '/?ra=127.69444&dec=-39.1776&fieldSize=6');
      assert.equal(await link.getAttribute('target'), '_blank');
    }
    assert.equal(atlasRequests.length, 0);
    assert.equal(requests.length, 3);
    await page.goto(`${origin}/embed?ra=2&dec=-91`);
    await page.getByRole('alert').filter({ hasText: 'Set dec' }).waitFor();
    const link = page.getByRole('link', { name: 'Open full viewer' });
    assert.equal(await link.getAttribute('href'), '/?ra=2&dec=-91');
    assert.equal(requests.length, 3);
  } finally {
    await browser.close();
  }
});

test('keeps playback, details and motion scoped while Auto and Manual overlap', { timeout: 35_000 }, async () => {
  const channel = process.env.SAC26_BROWSER_CHANNEL;
  const browser = await chromium.launch({ channel, chromiumSandbox: true });
  const firstMotion = Promise.withResolvers();
  const otherSequence = Promise.withResolvers();
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
        if (!action) {
          if (id === 'lookup.3') await otherSequence.promise;
          return send(sequences[id].result);
        }
        if (action.startsWith('images/')) {
          if (id === 'lookup.2' && failExtensionImage) return route.fulfill({ status: 404 });
          return route.fulfill({ body: png, contentType: 'image/png' });
        }
        if (action === 'motion') {
          const retry = requests.motion.includes(id);
          requests.motion.push(id);
          if (id === 'lookup.1') await firstMotion.promise;
          if (id === 'lookup.3') return send({ status: 'inspected', visits: [null, null], candidates: [] });
          const [status, displacement, frames, issues] = {
            'lookup.1': ['inspected', 2.1, [0, 1], [null, null]],
            'lookup.2': ['inspected', 3.7, [0, 2, 3], [null, null, null, null]],
            manual: [retry ? 'partial' : 'inspected', 4.2, [0, 2, 4], [null, null, null, 'registration_unconstrained', null]],
          }[id];
          const candidate = { displacement, uncertainty: .4, score: 40,
            measurements: frames.map((frameIndex) => ({ frameIndex, imagePosition: [.3 + .1 * frameIndex, .5] })),
          };
          return send({ status, visits: issues, candidates: id === 'lookup.1'
            ? [candidate, { ...candidate, displacement: 4.6, measurements: candidate.measurements.map((measurement) => ({
              ...measurement, imagePosition: [measurement.imagePosition[0], .6],
            })) }] : [candidate] });
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
      if (await button('Details').getAttribute('aria-expanded') === 'false') await button('Details').click();
      await details.getByRole('cell', { name: `obs-${exposureIndex}`, exact: true }).waitFor();
      assert.equal(await source.getAttribute('href'), exposure(exposureIndex).sourceUrl);
    }

    await page.goto(origin);
    await page.getByLabel('Right ascension (RA)').fill('10');
    await page.getByLabel('Declination (Dec)').fill('20');
    await button('Compare visits').click();
    await visible('1 / 2 visits');
    assert.equal(await button('Compare').getAttribute('aria-pressed'), 'true');
    await button('Next').click();
    await checkFrame('lookup.1', 1, 2);
    await button('Motion').click();
    assert.equal(await selector.inputValue(), 'lookup.1');
    await button('Check for motion').click();
    await visible('Checking the sequence for motion…');
    await button('Spectrum').click();
    await visible('2 / 2 visits');
    firstMotion.resolve();
    await page.locator('#comparison-tool-motion').getByText('Detected in 2 of 2 visits', { exact: true })
      .waitFor({ state: 'attached' });
    await button('Motion').click();
    await visible('Detected in 2 of 2 visits');
    await markers.locator('.marker-outline').waitFor();
    const candidate = page.getByRole('combobox', { name: /Motion candidate/ });
    await candidate.selectOption('1');
    assert.equal(await markers.locator('circle.current').getAttribute('cy'), '60');
    await button('Hide motion markers').click();
    await button('Compare').click();
    await markers.waitFor({ state: 'hidden' });
    await button('Motion').click();
    assert.equal(await candidate.inputValue(), '1');
    await button('Show motion markers').click();
    assert.equal(await markers.locator('circle.current').getAttribute('cy'), '60');
    await button('Compare').click();
    await markers.waitFor({ state: 'hidden' });

    extend = true;
    await visible('Reconnecting to the archive search…');
    await visible('Could not load the comparison images. Try again.');
    await checkFrame('lookup.1', 1, 2);
    assert.deepEqual(await choices(), ['lookup.2', 'lookup.3']);
    await button('Motion').click();
    await visible('Detected in 2 of 2 visits');
    assert.equal(await selector.inputValue(), 'lookup.1');
    assert.deepEqual(await choices(), ['lookup.2', 'lookup.3']);
    withdrawFirst = true;
    await visible('New archive metadata changed the visit grouping. Choose a current sequence to check for motion.');
    await markers.waitFor({ state: 'hidden' });
    failExtensionImage = false;
    await button('Compare').click();
    await button('Retry loading sequence').click();
    await visible('3 / 4 visits');
    await button('Compare').click();
    await checkFrame('lookup.2', 2, 2);
    await markers.waitFor({ state: 'hidden' });
    await button('Motion').click();
    await button('Check for motion').click();
    await visible('Detected in 3 of 4 visits');
    assert.equal(await markers.locator('circle.current').getAttribute('cx'), '50');

    await button('Compare').click();
    await button('Edit exposures').click();
    const manual = page.getByRole('dialog');
    await manual.getByRole('heading', { name: 'Selected exposures (4)' }).waitFor();
    await manual.getByRole('checkbox', { name: /obs-4/ }).check();
    await button('Apply selection').click();
    await button('Open Exploratory blink').click();
    await visible('1 / 5 frames');
    assert.equal(await button('Stack').isDisabled(), true);
    assert.equal(await button('Difference').isDisabled(), true);
    await visible('Searching for more sequences…');
    assert.match(await selector.locator('option:checked').textContent(), /5 exposures across 4 visits.*Manual/);
    assert.deepEqual(await choices(), ['manual', 'lookup.3']);
    assert.equal(requests.inventorySeed, 'lookup.2');
    assert.deepEqual(requests.manualKeys, ['e0', 'e1', 'e2', 'e3', 'e4']);
    await button('Share').click();
    assert.equal(await page.getByRole('region', { name: 'Share', exact: true }).getByLabel('Patch URL').inputValue(),
      `${origin}/?ra=10&dec=20&fieldSize=6`);
    await button('Compare').click();
    await checkFrame('manual', 0, 0);
    await markers.waitFor({ state: 'hidden' });
    await button('Motion').click();
    await button('Check for motion').click();
    await visible('Motion analysis was partial.');
    await visible('Could not evaluate exposure 4.');
    await visible('Detected in 3 of 4 visits');
    await button('Retry motion analysis').click();
    await visible('Motion analysis was partial.');
    await markers.locator('.marker-outline').waitFor();

    await button('Compare').click();
    later = true;
    await selector.locator('option[value="lookup.4"]').waitFor({ state: 'attached' });
    await checkFrame('manual', 0, 0);
    assert.equal(await selector.inputValue(), 'manual');
    await button('Stop search').click();
    await visible('Search stopped.');
    await button('Motion').click();
    await selector.selectOption('lookup.3');
    await visible('Loading sequence…');
    await visible('Detected in 3 of 4 visits');
    assert.equal(await selector.inputValue(), 'manual');
    assert.equal(await button('Retry motion analysis').isDisabled(), true);
    otherSequence.resolve();
    await visible('1 / 2 visits');
    await markers.waitFor({ state: 'hidden' });
    await button('Compare').click();
    await checkFrame('lookup.3', 0, 5);
    await button('Motion').click();
    assert.equal(await selector.inputValue(), 'lookup.3');
    await button('Check for motion').click();
    await visible('No motion candidates met the alert threshold in this sequence.');
    await selector.selectOption('manual');
    await visible('1 / 5 frames');
    await visible('Detected in 3 of 4 visits');
    await markers.locator('.marker-outline').waitFor();
    assert.equal(requests.lookups, 1);
    assert.deepEqual(requests.motion, ['lookup.1', 'lookup.2', 'manual', 'manual', 'lookup.3']);
    assert.deepEqual(requests.unexpected, []);
  } finally {
    firstMotion.resolve();
    otherSequence.resolve();
    await browser.close();
  }
});

test('keeps Stack and Difference inputs synchronized with the active sequence', { timeout: 35_000 }, async () => {
  const browser = await chromium.launch({ channel: process.env.SAC26_BROWSER_CHANNEL, chromiumSandbox: true });
  const late = Promise.withResolvers();
  const startedLate = Promise.withResolvers();
  const finishedLate = Promise.withResolvers();
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    page.setDefaultTimeout(6_000);
    const pairs = {
      'lookup.a': sequence('lookup.a', [0, 2]),
      'lookup.b': sequence('lookup.b', [0, 1, 2, 3]),
      'lookup.c': sequence('lookup.c', [2, 3, 5]),
      'lookup.d': sequence('lookup.d', [6, 7, 8]),
    };
    let current = 'lookup.a';
    let renumbered = false;
    let failExplicitLoad = true;
    let failImageMath = true;
    const imageRequests = [];
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin) return route.abort();
      const path = url.pathname;
      if (path === '/api/playback') return route.fulfill({ json: { id: 'lookup' } });
      if (path === '/api/playback/lookup') {
        const active = pairs[current].summary;
        const sequences = [renumbered && current === 'lookup.a' ? { ...active, visits: [2, 4] } : active,
          pairs['lookup.d'].summary];
        return route.fulfill({ json: { ...base, status: 'searching', sequences,
          withdrawnSequenceIds: current === 'lookup.c' ? ['lookup.b'] : [] } });
      }
      const match = path.match(/^\/api\/playback\/([^/]+)(?:\/(.+))?$/);
      if (match && pairs[match[1]]) {
        const [, id, action] = match;
        if (!action) return route.fulfill({ json: pairs[id].result });
        if (action === 'image-math') {
          const body = request.postDataJSON();
          imageRequests.push({ id, ...body });
          if (id === 'lookup.a' && body.operation === 'difference' && failImageMath) {
            failImageMath = false;
            return route.fulfill({ status: 503, json: { detail: 'Comparison image unavailable.' } });
          }
          if (id === 'lookup.b' && body.operation === 'difference' && body.keys.includes('e3')) {
            startedLate.resolve();
            await late.promise;
            try {
              await route.fulfill({ body: png, contentType: 'image/png' });
            } catch (error) {
              if (!request.failure()) throw error;
            } finally {
              finishedLate.resolve();
            }
            return;
          }
        } else if (id === 'lookup.d' && failExplicitLoad) {
          return route.fulfill({ status: 404 });
        }
        return route.fulfill({ body: png, contentType: 'image/png' });
      }
      return route.fulfill({ path: fileURLToPath(new URL(path === '/' ? 'index.html' : path.slice(1), dist)) });
    });

    const button = (name) => page.getByRole('button', { name, exact: true });
    const earlier = page.getByLabel('Earlier visit', { exact: true });
    const later = page.getByLabel('Later visit', { exact: true });
    const sequencePicker = page.getByLabel('Sequence', { exact: true });
    const stack = page.getByRole('img', { name: /North-up average/ });
    const difference = page.getByRole('img', { name: /North-up SPHEREx difference/ });
    const checkedVisits = () => page.getByRole('group', { name: /Stack visits/ }).locator('input:checked')
      .evaluateAll((inputs) => inputs.map((input) => input.parentElement.textContent.match(/^Visit (\d+)/)[1]));

    await page.goto(origin);
    await page.getByLabel('Right ascension (RA)').fill('10');
    await page.getByLabel('Declination (Dec)').fill('20');
    await button('Compare visits').click();
    await page.getByText('1 / 2 visits', { exact: true }).waitFor();
    await button('Next').click();
    await button('Stack').click();
    await stack.waitFor();
    assert.deepEqual(await checkedVisits(), ['1', '3']);
    const initialStack = await stack.getAttribute('src');
    renumbered = true;
    await page.getByRole('checkbox', { name: /^Visit 4 ·/ }).waitFor();
    assert.deepEqual(await checkedVisits(), ['2', '4']);
    assert.equal(await stack.getAttribute('src'), initialStack);
    assert.equal(await button('Next').count(), 0);
    await button('Difference').click();
    await page.getByRole('alert').getByText('Comparison image unavailable.').waitFor();
    await button('Retry image').click();
    await difference.waitFor();
    assert.equal(await earlier.inputValue(), 'e0');
    assert.equal(await later.inputValue(), 'e2');
    await button('Blink').click();
    await page.getByText('2 / 2 visits', { exact: true }).waitFor();
    await button('Stack').click();
    await stack.waitFor();

    current = 'lookup.b';
    await page.getByRole('group', { name: /Stack visits/ }).getByRole('checkbox', { name: /2025-01-04 00:00:00 UTC/ }).waitFor();
    await stack.waitFor();
    assert.deepEqual(await checkedVisits(), ['1', '3']);
    assert.deepEqual(imageRequests.at(-1), { id: 'lookup.b', operation: 'stack', keys: ['e0', 'e2'] });
    await button('Difference').click();
    await difference.waitFor();
    assert.equal(await earlier.inputValue(), 'e0');
    assert.equal(await later.inputValue(), 'e2');
    await later.selectOption('e3');
    await startedLate.promise;
    await later.selectOption('e1');
    await difference.waitFor();
    const latestImage = await difference.getAttribute('src');
    late.resolve();
    await finishedLate.promise;
    assert.equal(await difference.getAttribute('src'), latestImage);
    assert.deepEqual(imageRequests.at(-1), { id: 'lookup.b', operation: 'difference', keys: ['e0', 'e1'] });

    current = 'lookup.c';
    await page.getByText('Choose two visits to subtract.', { exact: true }).waitFor();
    assert.equal(await earlier.inputValue(), '');
    assert.equal(await later.inputValue(), '');
    assert.equal(await difference.count(), 0);
    await earlier.selectOption('e2');
    await later.selectOption('e5');
    await difference.waitFor();
    await button('Stack').click();
    assert.deepEqual(await checkedVisits(), []);
    await page.getByRole('checkbox', { name: /^Visit [34] ·/ }).first().check();
    await page.getByRole('checkbox', { name: /^Visit 6 ·/ }).check();
    await stack.waitFor();

    await sequencePicker.selectOption('lookup.d');
    await page.getByText('Could not load the comparison images. Try again.', { exact: true }).waitFor();
    assert.deepEqual(await checkedVisits(), ['3', '6']);
    await stack.waitFor();
    failExplicitLoad = false;
    await button('Retry loading sequence').click();
    await page.getByRole('checkbox', { name: /^Visit 9 ·/ }).waitFor();
    await stack.waitFor();
    assert.deepEqual(await checkedVisits(), ['7', '8', '9']);
    await button('Difference').click();
    await difference.waitFor();
    assert.equal(await earlier.inputValue(), 'e6');
    assert.equal(await later.inputValue(), 'e8');
    for (const viewport of [{ width: 390, height: 844 }, { width: 1280, height: 800 }]) {
      await page.setViewportSize(viewport);
      const box = await difference.boundingBox();
      assert.ok(box && box.width > 100 && box.x >= 0 && box.x + box.width <= viewport.width);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
  } finally {
    late.resolve();
    await browser.close();
  }
});

test('overlays Gaia and WISE sources independently on the current sky patch', { timeout: 25_000 }, async () => {
  const browser = await chromium.launch({ channel: process.env.SAC26_BROWSER_CHANNEL, chromiumSandbox: true });
  const stack = Promise.withResolvers();
  const gaiaResponse = Promise.withResolvers();
  try {
    const context = await browser.newContext();
    await routeCoordinateViewer(context, (_patch, _id, ready) => ready);
    const page = await context.newPage();
    page.setDefaultTimeout(6_000);
    const requests = [];
    await page.route('**/api/catalogs/**', async (route) => {
      const url = new URL(route.request().url());
      const catalog = url.pathname.split('/').at(-1);
      requests.push([catalog, Object.fromEntries(url.searchParams)]);
      if (catalog === 'wise' && requests.filter(([name]) => name === 'wise').length === 1) {
        return route.fulfill({ status: 502 });
      }
      if (catalog === 'gaia') await gaiaResponse.promise;
      const sources = url.searchParams.get('ra') !== '10' ? [] : catalog === 'gaia'
        ? [{ x: .25, y: .35 }] : [{ x: .75, y: .65 }];
      return route.fulfill({ json: { sources, limited: false } });
    });
    await page.route('**/api/playback/**/image-math', async (route) => {
      await stack.promise;
      return route.fulfill({ body: png, contentType: 'image/png' });
    });

    await page.goto(`${origin}/?ra=10&dec=20&fieldSize=6`);
    const button = (name) => page.getByRole('button', { name, exact: true });
    const gaiaToggle = button('Gaia');
    const wiseToggle = button('WISE');
    const gaiaDetails = button('Gaia catalog details');
    const wiseDetails = button('WISE catalog details');
    const gaiaPopup = page.getByRole('region', { name: 'Gaia DR3 · G band', exact: true });
    const wisePopup = page.getByRole('region', { name: 'AllWISE · W2 (4.6 µm)', exact: true });
    const gaia = page.locator('.catalog-markers[data-catalog="gaia"]');
    const wise = page.locator('.catalog-markers[data-catalog="wise"]');
    const wiseControl = page.locator('.catalog-toggle[data-catalog="wise"]');
    const background = (locator) => locator.evaluate((element) => getComputedStyle(element).backgroundColor);
    await gaiaToggle.waitFor();
    assert.deepEqual(requests, []);
    assert.equal(await gaiaToggle.getAttribute('aria-pressed'), 'false');
    const idleBackground = await background(wiseControl);
    await wiseToggle.hover();
    assert.notEqual(await background(wiseControl), idleBackground);
    assert.deepEqual(await wiseToggle.boundingBox(), await wiseControl.boundingBox());
    await gaiaToggle.press('Enter');
    const loading = button('Loading Gaia catalog');
    await loading.locator('[data-state="loading"]').waitFor();
    assert.equal(await loading.getAttribute('aria-disabled'), 'true');
    const [labelBox, statusBox] = await Promise.all([gaiaToggle.boundingBox(), loading.boundingBox()]);
    assert.equal(labelBox.x + labelBox.width, statusBox.x);
    assert.equal(labelBox.y, statusBox.y);
    gaiaResponse.resolve();
    await gaia.waitFor();
    await gaiaDetails.locator('[data-state="ready"]').waitFor();
    await gaiaDetails.press('Enter');
    await gaiaPopup.getByText('1 source', { exact: true }).waitFor();
    await page.waitForFunction(() => document.activeElement?.matches('.catalog-details'));
    const [gaiaIcon, gaiaBox] = await Promise.all([gaiaDetails.boundingBox(), gaiaPopup.boundingBox()]);
    assert.ok(gaiaBox.x >= gaiaIcon.x + gaiaIcon.width && gaiaBox.x <= gaiaIcon.x + gaiaIcon.width + 8);
    assert.equal(gaiaBox.y, gaiaIcon.y);
    await page.keyboard.press('Escape');
    await gaiaPopup.waitFor({ state: 'hidden' });
    assert.equal(await gaiaDetails.evaluate((element) => element === document.activeElement), true);
    const { offsetX, offsetY } = await gaia.locator('circle').evaluate((marker) => {
      const bounds = marker.getBoundingClientRect();
      const image = marker.ownerSVGElement.ownerSVGElement.getBoundingClientRect();
      return { offsetX: (bounds.x + bounds.width / 2 - image.x) / image.width,
        offsetY: (bounds.y + bounds.height / 2 - image.y) / image.height };
    });
    assert.ok(Math.abs(offsetX - .25) < .002 && Math.abs(offsetY - .35) < .002);
    assert.equal(await button('Compare').getAttribute('aria-pressed'), 'true');

    await wiseToggle.click();
    const retryWise = button('Retry WISE');
    await retryWise.locator('[data-state="failed"]').waitFor();
    await retryWise.click();
    await wise.locator('path').waitFor();
    await wiseDetails.waitFor();
    assert.equal(await wiseToggle.getAttribute('aria-pressed'), 'true');
    await button('Compare').hover();
    const activeBackground = await background(wiseControl);
    await wiseToggle.hover();
    const hoverBackground = await background(wiseControl);
    assert.notEqual(hoverBackground, activeBackground);
    await wiseDetails.hover();
    assert.equal(await background(wiseControl), hoverBackground);
    assert.equal(await background(wiseDetails), 'rgba(0, 0, 0, 0)');
    await page.setViewportSize({ width: 320, height: 680 });
    const bounds = await Promise.all([gaiaDetails.boundingBox(), wiseDetails.boundingBox()]);
    assert.ok(bounds.every((box) => box && box.x >= 0 && box.x + box.width <= 320));
    await wiseDetails.click();
    await page.waitForFunction(() => document.activeElement?.matches('.catalog-details'));
    const wiseBox = await wisePopup.boundingBox();
    assert.ok(wiseBox.x >= 0 && wiseBox.x + wiseBox.width <= 320 && wiseBox.y >= 0);
    assert.ok(wiseBox.y >= bounds[1].y + bounds[1].height && wiseBox.y <= bounds[1].y + bounds[1].height + 16,
      'The popup sits just below WISE and leaves the tool rows clear');
    await button('Motion').click();
    await wisePopup.waitFor({ state: 'hidden' });
    assert.equal(await button('Motion').getAttribute('aria-pressed'), 'true');
    await button('Compare').click();
    await page.setViewportSize({ width: 390, height: 650 });
    const phoneBounds = await Promise.all([gaiaDetails.boundingBox(), wiseDetails.boundingBox(), button('Share').boundingBox()]);
    assert.ok(phoneBounds.every((box) => box && box.x >= 0 && box.x + box.width <= 390));
    await page.setViewportSize({ width: 1280, height: 720 });
    await gaia.waitFor();
    await button('Next').click();
    await page.getByRole('slider', { name: 'Playback position' }).getAttribute('aria-valuetext').then((text) => {
      assert.equal(text, 'Frame 2 of 2');
    });
    await gaia.waitFor();
    await wise.waitFor();
    assert.equal(requests.length, 3);
    assert.deepEqual(requests[0], ['gaia', { ra: '10', dec: '20', fieldSize: '6' }]);

    await button('Spectrum').click();
    await button('Select a source for spectrum').click();
    await button('Show spectrum').waitFor();
    await button('Compare').click();
    await button('Stack').click();
    await page.getByText('Calculating stack…').waitFor();
    await gaia.waitFor({ state: 'hidden' });
    stack.resolve();
    await page.getByRole('img', { name: /North-up average/ }).waitFor();
    await gaia.waitFor();
    await wise.waitFor();
    await wiseToggle.click();
    await wise.waitFor({ state: 'hidden' });
    await gaia.waitFor();

    await button('Back to sky').click();
    await page.getByLabel('Right ascension (RA)').fill('30');
    await button('Compare visits').click();
    await gaiaDetails.click();
    await gaiaPopup.getByText('No sources in this patch', { exact: true }).waitFor();
    await gaia.waitFor({ state: 'hidden' });
    assert.deepEqual(requests.at(-1), ['gaia', { ra: '30', dec: '20', fieldSize: '6' }]);
    assert.equal(requests.length, 4);
  } finally {
    gaiaResponse.resolve();
    stack.resolve();
    await browser.close();
  }
});

test('toggles Pixel Flags alongside the active workspace tool', { timeout: 25_000 }, async () => {
  const browser = await chromium.launch({ channel: process.env.SAC26_BROWSER_CHANNEL, chromiumSandbox: true });
  const secondMap = Promise.withResolvers();
  try {
    const page = await browser.newPage({ hasTouch: true });
    page.setDefaultTimeout(6_000);
    const { result, summary } = sequence('lookup.1', [0, 1]);
    const requests = [];
    for (const [index, frame] of result.frames.entries()) {
      frame.flagsUrl = `/api/playback/lookup.1/flags/frame-${index}`;
    }
    const flagMap = (samples) => {
      const bytes = Buffer.alloc(5 * samples.length);
      for (const [index, [flags, status]] of samples.entries()) {
        bytes.writeUInt32LE(flags, index * 5);
        bytes.writeUInt8(status, index * 5 + 4);
      }
      return { width: 2, height: 2, samples: bytes.toString('base64'), definitions: {
        10: ['HOT', 'Noisy detector pixel'], 21: ['SOURCE', 'Known source'], 25: ['STREAK', 'Satellite streak'],
      } };
    };

    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      const path = url.pathname;
      if (path === '/api/playback') return route.fulfill({ json: { id: 'lookup' } });
      if (path === '/api/playback/lookup') return route.fulfill({ json: { ...base, sequences: [summary] } });
      if (path === '/api/playback/lookup.1') return route.fulfill({ json: result });
      if (path.includes('/images/') || path.endsWith('/image-math')) {
        return route.fulfill({ body: png, contentType: 'image/png' });
      }
      if (path.includes('/flags/')) {
        requests.push(path);
        if (requests.length === 1) return route.fulfill({ status: 503 });
        const first = path.endsWith('frame-0');
        if (!first) await secondMap.promise;
        return route.fulfill({ json: flagMap(first
          ? [[1 << 10, 1], [1 << 21, 2], [0, 0], [0, 2]]
          : [[1 << 25, 2], [0xffffffff, 2], [0, 0], [0, 3]]) });
      }
      return route.fulfill({ path: fileURLToPath(new URL(path === '/' ? 'index.html' : path.slice(1), dist)) });
    });

    const button = (name) => page.getByRole('button', { name, exact: true });
    const compare = page.getByRole('region', { name: 'Compare', exact: true });
    const rail = page.getByRole('group', { name: 'Workspace tools' });
    const readout = page.locator('.pixel-flag-readout');
    const overlay = page.locator('.pixel-flag-overlay');
    const target = button('Inspect pixel flags');
    await page.goto(origin);
    await page.getByLabel('Right ascension (RA)').fill('10');
    await page.getByLabel('Declination (Dec)').fill('20');
    await button('Compare visits').click();
    const flagsTool = rail.getByRole('button', { name: 'Pixel Flags', exact: true });
    await flagsTool.waitFor();
    assert.deepEqual(await rail.getByRole('button').allTextContents(),
      ['Compare', 'Motion', 'Spectrum', 'Pixel Flags', 'Gaia', 'WISE', 'Share']);
    assert.deepEqual(requests, []);
    await flagsTool.press('Enter');
    await button('Retry flags').click();
    await overlay.waitFor();
    assert.equal(await flagsTool.getAttribute('aria-pressed'), 'true');
    assert.equal(await button('Compare').getAttribute('aria-pressed'), 'true');
    const box = await target.boundingBox();
    const railBox = await rail.boundingBox();
    const panelBox = await compare.boundingBox();
    assert.ok(railBox.x + railBox.width <= panelBox.x, 'The tool rail sits to the left of the panel');
    assert.ok(panelBox.x + panelBox.width <= box.x, 'The sidebar sits to the left of the image');
    const shareTool = rail.getByRole('button', { name: 'Share', exact: true });
    assert.ok((await shareTool.boundingBox()).y > (await flagsTool.boundingBox()).y);
    await page.mouse.move(box.x + box.width / 4, box.y + box.height / 4);
    await readout.getByText('HOT', { exact: true }).waitFor();
    await readout.getByText('Masked in this exposure', { exact: true }).waitFor();
    assert.deepEqual(await overlay.evaluate((canvas) => {
      const pixels = canvas.getContext('2d').getImageData(0, 0, 2, 2).data;
      return [pixels[3], pixels[7], pixels[11], pixels[15]];
    }), [135, 0, 0, 0]);

    await target.press('ArrowRight');
    await readout.getByText('SOURCE', { exact: true }).waitFor();
    await target.press('ArrowDown');
    await readout.getByText('No flags set', { exact: true }).waitFor();
    await page.touchscreen.tap(box.x + box.width / 4, box.y + box.height * 3 / 4);
    await readout.getByText('Outside detector coverage', { exact: true }).waitFor();
    await page.touchscreen.tap(box.x + box.width / 4, box.y + box.height / 4);
    await readout.getByText('HOT', { exact: true }).waitFor();

    await button('Next').click();
    await readout.getByText('Loading pixel flags…', { exact: true }).waitFor();
    await readout.getByText('HOT', { exact: true }).waitFor({ state: 'hidden' });
    await page.touchscreen.tap(box.x + box.width * 3 / 4, box.y + box.height * 3 / 4);
    await page.mouse.move(box.x + box.width / 4, box.y + box.height / 4);
    secondMap.resolve();
    await readout.getByText('STREAK', { exact: true }).waitFor();
    await page.mouse.move(0, 0);
    await readout.getByText('Pixel 2, 2 · Pinned', { exact: true }).waitFor();
    await readout.getByText('Masked in this comparison', { exact: true }).waitFor();
    await button('Motion').click();
    await overlay.waitFor();
    assert.equal(await button('Motion').getAttribute('aria-pressed'), 'true');
    await button('Spectrum').click();
    const spectrumTarget = button('Select a source for spectrum');
    const spectrumBox = await spectrumTarget.boundingBox();
    await page.touchscreen.tap(spectrumBox.x + spectrumBox.width / 4, spectrumBox.y + spectrumBox.height / 4);
    await button('Show spectrum').waitFor();
    await readout.getByText('Pixel 1, 1 · Source', { exact: true }).waitFor();
    await readout.getByText('STREAK', { exact: true }).waitFor();
    await flagsTool.click();
    await overlay.waitFor({ state: 'hidden' });
    await button('Show spectrum').waitFor();
    assert.equal(await button('Spectrum').getAttribute('aria-pressed'), 'true');
    await button('Compare').click();
    await flagsTool.click();
    await overlay.waitFor();
    await button('Stack').click();
    await page.getByRole('img', { name: /North-up average/ }).waitFor();
    await overlay.waitFor({ state: 'hidden' });
    assert.equal(await flagsTool.isDisabled(), true);
    await button('Blink').click();
    await overlay.waitFor();
    assert.equal(await flagsTool.getAttribute('aria-pressed'), 'true');
    await page.setViewportSize({ width: 320, height: 680 });
    const toolBoxes = await Promise.all((await rail.getByRole('button').all()).map((tool) => tool.boundingBox()));
    assert.equal(toolBoxes.length, 7);
    assert.equal(new Set(toolBoxes.map(({ y }) => y)).size, 2, 'The tools wrap to two rows on a narrow screen');
    for (const tool of toolBoxes) {
      assert.ok(tool.x >= 0 && tool.x + tool.width <= 320);
    }
    const mobileImage = await target.boundingBox();
    await page.touchscreen.tap(mobileImage.x + mobileImage.width / 4, mobileImage.y + mobileImage.height / 4);
    await readout.getByText('STREAK', { exact: true }).waitFor();
    await page.setViewportSize({ width: 320, height: 360 });
    const compactImage = await target.boundingBox();
    await page.touchscreen.tap(compactImage.x + compactImage.width * 3 / 4, compactImage.y + compactImage.height / 4);
    await readout.getByText('Pixel 2, 1 · Pinned', { exact: true }).waitFor();
    assert.ok((await readout.boundingBox()).y >= compactImage.y + compactImage.height / 2,
      'The readout leaves the half of the image with the pinned pixel clear');
    await readout.hover();
    await page.mouse.wheel(0, 400);
    await page.waitForFunction(() => document.getElementById('pixel-flag-readout').scrollTop > 0);
    await readout.getByText('Pixel 2, 1 · Pinned', { exact: true }).waitFor();
    await page.touchscreen.tap(compactImage.x + compactImage.width / 4, compactImage.y + compactImage.height / 4);
    await readout.getByText('Pixel 1, 1 · Pinned', { exact: true }).waitFor();
  } finally {
    secondMap.resolve();
    await browser.close();
  }
});

test('updates the spectrum chart while measurements arrive and keeps partial results', { timeout: 30_000 }, async () => {
  const browser = await chromium.launch({ channel: process.env.SAC26_BROWSER_CHANNEL, chromiumSandbox: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 650 }, isMobile: true, hasTouch: true });
    page.setDefaultTimeout(6_000);
    const { result, summary } = sequence('lookup.2', [0, 1, 2, 3]);
    result.frames[0].visitDates = ['2024-12-31', '2025-01-01'];
    const submitted = [];
    const removed = [];
    const stopped = new Set();
    let rejectStop = true;
    let measurements = [];
    let finish = false;
    let interruptPoll = false;
    const point = { ...exposure(0), wavelength: 1.5, bandwidth: .03, flux: 80, uncertainty: 6, flags: ['Masked pixels'] };
    const points = Array.from({ length: 42 }, (_, i) => ({ ...point, key: `s${i}`,
      wavelength: .82 + i * .09, flux: 1500 / (i + 1) + 25 * Math.sin(i), uncertainty: 8 + i,
      visit: Math.floor(i / 4) + 1,
    }));
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== origin) return route.abort();
      const path = url.pathname;
      const send = (json) => route.fulfill({ json });
      if (path === '/api/playback') return send({ id: 'lookup' });
      if (path === '/api/playback/lookup') return send({ ...base, sequences: [summary] });
      if (path === '/api/playback/lookup.2') return send(result);
      if (path.startsWith('/api/playback/lookup.2/images/')) return route.fulfill({ body: png, contentType: 'image/png' });
      if (path === '/api/spectrum') {
        submitted.push(request.postDataJSON());
        return send({ id: String(submitted.length) });
      }
      if (path.startsWith('/api/spectrum/')) {
        if (path.endsWith('/stop')) {
          if (rejectStop) {
            rejectStop = false;
            return route.fulfill({ status: 503 });
          }
          stopped.add(path.slice(0, -'/stop'.length));
          return route.fulfill({ status: 204 });
        }
        if (request.method() === 'DELETE') {
          removed.push(path);
          return route.fulfill({ status: 204 });
        }
        if (interruptPoll) {
          interruptPoll = false;
          return route.abort('failed');
        }
        if (stopped.has(path)) return send({ status: 'cancelled', points: measurements, message: 'Spectrum stopped.',
          processed: measurements.length, total: 60 });
        return send(finish
          ? { status: 'partial', points, message: 'Some archive exposures could not be checked.', processed: 60, total: 60 }
          : { status: 'searching', points: measurements, message: 'Measuring the spectrum…',
            processed: measurements.length + 1, total: 60 });
      }
      return route.fulfill({ path: fileURLToPath(new URL(path === '/' ? 'index.html' : path.slice(1), dist)) });
    });

    const button = (name) => page.getByRole('button', { name, exact: true });
    const target = button('Select a source for spectrum');
    await page.goto(origin);
    await page.getByLabel('Right ascension (RA)').fill('10');
    await page.getByLabel('Declination (Dec)').fill('20');
    await button('Compare visits').click();
    await page.getByText('1 / 4 visits', { exact: true }).waitFor();
    const image = page.getByRole('img', { name: /North-up SPHEREx detector/ });
    await image.click();
    assert.equal(await target.count(), 0);
    await button('Play').click();
    await button('Motion').click();
    await button('Pause').waitFor();
    await image.click();
    assert.equal(await target.count(), 0);
    await button('Spectrum').click();
    await button('Pause').waitFor();
    const box = await target.boundingBox();
    assert.ok(box.width > 100);
    await page.touchscreen.tap(box.x + .25 * box.width, box.y + .25 * box.height);
    await button('Play').waitFor();
    const panel = page.getByRole('region', { name: 'Spectrum', exact: true });
    assert.equal(await panel.getByLabel('From').inputValue(), '2024-12-31');
    assert.equal(await panel.getByLabel('Through').inputValue(), '2025-01-04');
    await panel.getByLabel('Through').fill('2025-02-01');
    await button('Show spectrum').click();
    await panel.getByText(/Measuring the spectrum/).waitFor();
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].endDate, '2025-02-01');
    assert.ok(Math.abs(submitted[0].ra - 10.02661) < .001, JSON.stringify(submitted[0]));
    assert.ok(Math.abs(submitted[0].dec - 20.025) < .001, JSON.stringify(submitted[0]));
    const marker = target.locator('.spectrum-marker');
    const style = await marker.getAttribute('style');
    await button('Next').click();
    assert.equal(await marker.getAttribute('style'), style);
    await panel.getByLabel('Through').fill('2025-03-01');
    await panel.getByText('Requested 2024-12-31 to 2025-02-01 UTC').waitFor();
    const pendingScroll = await panel.evaluate((node) => {
      node.scrollTop = node.scrollHeight;
      return node.scrollTop;
    });
    assert.ok(pendingScroll > 0, 'The pending spectrum form can be scrolled');
    await button('Compare').click();
    await panel.waitFor({ state: 'hidden' });
    await marker.waitFor({ state: 'hidden' });
    const hiddenPanel = page.getByRole('region', { name: 'Spectrum', exact: true, includeHidden: true });
    measurements = points.slice(6, 12);
    await hiddenPanel.getByText('6 measurements', { exact: true }).waitFor({ state: 'attached' });
    await button('Spectrum').click();
    await panel.getByText(/7 \/ 60 exposures checked/).waitFor();
    assert.equal(await panel.evaluate((node) => node.scrollTop), 0, 'The first results start at the top of the panel');
    await page.setViewportSize({ width: 390, height: 844 });
    const chart = panel.getByRole('img', { name: /Flux density by wavelength/i });
    const expand = panel.getByRole('button', { name: 'Expand spectrum' });
    const expanded = page.getByRole('dialog', { name: 'Expanded spectrum' });
    const measurement = panel.locator('details').getByLabel('Measurement', { exact: true });
    assert.equal(await chart.locator('.spectrum-chart-target').count(), 6);
    await chart.locator('.spectrum-chart-target').nth(2).tap();
    assert.equal(await measurement.inputValue(), 's8');
    assert.equal(await expanded.isVisible(), false);
    interruptPoll = true;
    await panel.getByText(/Reconnecting to the spectrum request/).waitFor();
    assert.equal(await chart.locator('.spectrum-chart-target').count(), 6);
    await measurement.focus();
    measurements = points.slice(3, 16);
    await panel.getByText(/14 \/ 60 exposures checked/).waitFor();
    assert.equal(await chart.locator('.spectrum-chart-target').count(), 13);
    assert.equal(await measurement.locator('option').count(), 6, 'The chart can grow without changing the active dropdown');
    await measurement.press('End');
    assert.equal(await measurement.inputValue(), 's15');
    await measurement.selectOption('s7');
    assert.equal(await measurement.inputValue(), 's7');
    assert.equal(await measurement.locator('option').count(), 13);

    await expand.click({ position: { x: 10, y: 20 } });
    const expandedMeasurement = expanded.getByRole('combobox', { name: 'Measurement' });
    assert.equal(await expandedMeasurement.inputValue(), 's7');
    await expandedMeasurement.focus();
    measurements = points.slice(0, 20);
    const expandedChart = expanded.getByRole('img', { name: /Flux density by wavelength/i });
    await expandedChart.locator('.spectrum-chart-target').nth(19).waitFor();
    assert.equal(await expandedMeasurement.locator('option').count(), 13);
    await expandedMeasurement.selectOption('s12');
    assert.match(await expandedChart.locator('.spectrum-chart-marker[opacity="1"] title').textContent(), /^Visit 4 · 1\.9000 µm/);
    assert.equal(await expandedMeasurement.locator('option').count(), 20);
    await expanded.getByRole('button', { name: 'Close' }).click();
    assert.equal(await measurement.inputValue(), 's12');
    await button('Stop spectrum').waitFor();
    await panel.getByText('Measurement details', { exact: true }).click();
    await panel.evaluate((node) => { node.scrollTop = 0; });
    finish = true;
    await panel.getByText('Some archive exposures could not be checked.', { exact: true }).waitFor();
    await panel.getByText('42 measurements', { exact: true }).waitFor();
    await button('Stop spectrum').waitFor({ state: 'hidden' });
    async function checkPlayback() {
      const playback = page.getByRole('region', { name: 'Comparison playback' });
      const strip = await playback.boundingBox();
      const frame = await image.boundingBox();
      const tools = await page.getByRole('complementary', { name: 'Comparison tools' }).boundingBox();
      assert.ok(strip.y >= frame.y + frame.height - 1);
      assert.ok(strip.y + strip.height <= page.viewportSize().height + 1);
      if (page.viewportSize().width < 600) assert.ok(tools.y >= strip.y + strip.height - 1);
      else assert.ok(tools.x + tools.width <= strip.x + 1);
      await playback.getByRole('slider', { name: 'Playback position' }).waitFor();
      assert.deepEqual(await playback.getByRole('button').evaluateAll((buttons) =>
        buttons.map((button) => button.getAttribute('aria-label'))), ['Previous', 'Play', 'Next']);
      await playback.getByRole('button', { name: 'Next' }).click();
    }
    async function checkTools() {
      for (const tool of ['Compare', 'Motion', 'Spectrum']) {
        await button(tool).click();
        assert.equal(await button(tool).getAttribute('aria-pressed'), 'true');
        await page.getByRole('complementary', { name: 'Comparison tools' })
          .getByRole('button', { name: 'Back to sky' }).waitFor();
        if (tool === 'Compare') {
          const compare = page.getByRole('region', { name: 'Compare', exact: true });
          await compare.getByRole('combobox', { name: 'Sequence' }).waitFor();
          await compare.getByText('4.200 µm', { exact: true }).waitFor();
        }
        await checkPlayback();
      }
    }
    async function checkLayout() {
      const chartBox = await chart.boundingBox();
      const next = await measurement.isVisible() ? await measurement.boundingBox() :
        await panel.getByText('Measurement details', { exact: true }).boundingBox();
      assert.ok(chartBox.height > 100, 'The chart has enough height to read its axes');
      assert.ok(chartBox.y + chartBox.height <= next.y, 'The chart stays above the controls');
      await checkPlayback();
    }
    async function checkChartFits() {
      const chartBox = await chart.boundingBox();
      const panelBox = await panel.boundingBox();
      assert.ok(chartBox.y + chartBox.height <= panelBox.y + panelBox.height, 'The chart fits in the visible sidebar');
    }
    await checkTools();
    await checkLayout();
    await checkChartFits();
    await page.setViewportSize({ width: 1150, height: 620 });
    await checkTools();
    await checkLayout();
    await checkChartFits();
    await page.setViewportSize({ width: 390, height: 844 });
    await panel.getByText('Measurement details', { exact: true }).click();
    assert.equal(await measurement.inputValue(), 's12');
    await checkLayout();
    await panel.getByText('Source and dates', { exact: true }).click();
    await button('Motion').click();
    await button('Spectrum').click();
    assert.equal(await measurement.inputValue(), 's12');
    assert.equal(await panel.getByLabel('Through').inputValue(), '2025-03-01');
    assert.equal(await marker.getAttribute('style'), style);
    assert.equal(submitted.length, 1);
    await expand.click({ position: { x: 10, y: 20 } });
    await expanded.getByText('Masked pixels', { exact: true }).waitFor();
    assert.equal(await expanded.getByRole('link', { name: 'Source FITS · obs-0' }).getAttribute('href'), point.sourceUrl);
    await expanded.getByRole('button', { name: 'Close' }).click();
    assert.equal(await expand.evaluate((node) => node === document.activeElement), true);
    for (const key of ['Enter', 'Space']) {
      await expand.press(key);
      await expanded.waitFor();
      await page.keyboard.press('Escape');
      await expanded.waitFor({ state: 'hidden' });
    }
    await expand.tap({ position: { x: 10, y: 20 } });
    await expanded.getByRole('button', { name: 'Close' }).click();
    await button('Clear source').click();
    await panel.waitFor({ state: 'hidden' });
    assert.deepEqual(removed, ['/api/spectrum/1']);
    await target.press('ArrowRight');
    await button('Show spectrum').waitFor();

    finish = false;
    measurements = [];
    await button('Show spectrum').click();
    await panel.getByText(/Measuring the spectrum/).waitFor();
    await target.press('ArrowDown');
    await panel.getByText(/Measuring the spectrum/).waitFor({ state: 'hidden' });
    await button('Show spectrum').click();
    measurements = points.slice(0, 5);
    await panel.getByText('5 measurements', { exact: true }).waitFor();
    await panel.getByText('Measurement details', { exact: true }).click();
    await measurement.selectOption('s4');
    const waitForRemoval = (id) => page.waitForResponse((response) =>
      new URL(response.url()).pathname === `/api/spectrum/${id}` && response.request().method() === 'DELETE');
    await button('Stop spectrum').click();
    await panel.getByRole('alert').getByText('Could not stop the spectrum. Try again.').waitFor();
    assert.equal(await panel.getByText('Spectrum stopped.', { exact: true }).isVisible(), false);
    await Promise.all([waitForRemoval(3), button('Stop spectrum').click()]);
    await panel.getByText('Spectrum stopped.', { exact: true }).waitFor();
    assert.equal(await chart.locator('.spectrum-chart-target').count(), 5);
    assert.equal(await measurement.inputValue(), 's4');
    await measurement.selectOption('s2');
    assert.equal(await measurement.inputValue(), 's2');

    await panel.getByText('Source and dates', { exact: true }).click();
    measurements = [];
    await button('Show spectrum').click();
    await panel.getByText(/Measuring the spectrum/).waitFor();
    await Promise.all([waitForRemoval(4), button('Stop spectrum').click()]);
    await panel.getByText('Spectrum stopped.', { exact: true }).waitFor();
    await chart.waitFor({ state: 'hidden' });
    await button('Show spectrum').waitFor();
    assert.deepEqual(removed.sort(), ['/api/spectrum/1', '/api/spectrum/2', '/api/spectrum/3', '/api/spectrum/4']);
  } finally {
    await browser.close();
  }
});
