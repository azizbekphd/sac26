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
  const firstMotion = Promise.withResolvers();
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
          if (id === 'lookup.1') await firstMotion.promise;
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
    await visible('Searching for more sequences…');
    assert.match(await selector.locator('option:checked').textContent(), /5 exposures across 4 visits.*Manual/);
    assert.deepEqual(await choices(), ['manual', 'lookup.3']);
    assert.equal(requests.inventorySeed, 'lookup.2');
    assert.deepEqual(requests.manualKeys, ['e0', 'e1', 'e2', 'e3', 'e4']);
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
    await selector.selectOption('lookup.3');
    await visible('1 / 2 visits');
    await markers.waitFor({ state: 'hidden' });
    await checkFrame('lookup.3', 0, 5);
    await selector.selectOption('manual');
    await visible('1 / 5 frames');
    await button('Motion').click();
    await visible('Detected in 3 of 4 visits');
    await markers.locator('.marker-outline').waitFor();
    assert.equal(requests.lookups, 1);
    assert.deepEqual(requests.motion, ['lookup.1', 'lookup.2', 'manual', 'manual']);
    assert.deepEqual(requests.unexpected, []);
  } finally {
    firstMotion.resolve();
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
        if (request.method() === 'DELETE') {
          removed.push(path);
          return route.fulfill({ status: 204 });
        }
        if (interruptPoll) {
          interruptPoll = false;
          return route.abort('failed');
        }
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
    measurements = points.slice(3, 16);
    await panel.getByText(/14 \/ 60 exposures checked/).waitFor();
    assert.equal(await chart.locator('.spectrum-chart-target').count(), 13);
    assert.equal(await measurement.inputValue(), 's8');
    await button('Cancel spectrum').waitFor();
    await panel.getByText('Measurement details', { exact: true }).click();
    await panel.evaluate((node) => { node.scrollTop = 0; });
    finish = true;
    await panel.getByText('Some archive exposures could not be checked.', { exact: true }).waitFor();
    await panel.getByText('42 measurements', { exact: true }).waitFor();
    await button('Cancel spectrum').waitFor({ state: 'hidden' });
    async function checkPlayback() {
      const playback = page.getByRole('region', { name: 'Comparison playback' });
      const strip = await playback.boundingBox();
      const frame = await image.boundingBox();
      const tools = await page.getByRole('complementary', { name: 'Comparison tools' }).boundingBox();
      assert.ok(strip.y >= frame.y + frame.height - 1);
      assert.ok(strip.y + strip.height <= page.viewportSize().height + 1);
      if (page.viewportSize().width < 600) assert.ok(tools.y >= strip.y + strip.height - 1);
      else assert.ok(tools.x >= strip.x + strip.width - 1);
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
    assert.equal(await measurement.inputValue(), 's8');
    await checkLayout();
    await panel.getByText('Source and dates', { exact: true }).click();
    await button('Motion').click();
    await button('Spectrum').click();
    assert.equal(await measurement.inputValue(), 's8');
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
    measurements = points.slice(0, 1);
    await chart.waitFor();
    await button('Cancel spectrum').click();
    await chart.waitFor({ state: 'hidden' });
    await button('Show spectrum').waitFor();
    assert.deepEqual(removed.sort(), ['/api/spectrum/1', '/api/spectrum/2', '/api/spectrum/3']);
  } finally {
    await browser.close();
  }
});
