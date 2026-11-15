import { useEffect, useId, useRef, useState } from 'react';
import type { SubmittedPatch } from './patchQuery';

const catalogs = {
  gaia: { name: 'Gaia', band: 'Gaia DR3 · G band' },
  wise: { name: 'WISE', band: 'AllWISE · W2 (4.6 µm)' },
} as const;

type Catalog = keyof typeof catalogs;
type CatalogData = { sources: { x: number; y: number }[]; limited: boolean };

export function useCatalogOverlay(catalog: Catalog, patch?: SubmittedPatch) {
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState<{ url: string; data: CatalogData | null }>();
  const url = patch && `/api/catalogs/${catalog}?ra=${patch.position.ra}&dec=${patch.position.dec}&fieldSize=${patch.fieldSize}`;
  const current = url && loaded?.url === url ? loaded : undefined;
  const canLoad = enabled && Boolean(url) && !current;

  useEffect(() => {
    if (!url || !canLoad) return;
    const controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(18000)]);
    void fetch(url, { signal }).then(async (response) => {
      if (!response.ok) throw new Error('Catalog unavailable');
      const data = await response.json() as CatalogData;
      if (!controller.signal.aborted) setLoaded({ url, data });
    }).catch(() => {
      if (!controller.signal.aborted) setLoaded({ url, data: null });
    });
    return () => controller.abort();
  }, [url, canLoad]);

  return { catalog, enabled, data: current?.data,
    toggle: () => setEnabled((show) => !show), retry: () => setLoaded(undefined) };
}

type Layer = ReturnType<typeof useCatalogOverlay>;

export function CatalogToggle({ layer }: { layer: Layer }) {
  const { catalog, enabled, data, toggle, retry } = layer;
  const { name, band } = catalogs[catalog];
  const trigger = useRef<HTMLButtonElement>(null);
  const details = useRef<HTMLElement>(null);
  const detailsId = useId();
  const titleId = `${detailsId}-title`;
  const state = data === undefined ? 'loading' : data === null ? 'failed' : 'ready';
  const announcement = {
    loading: `Loading ${name} catalog`,
    failed: `Could not load ${name} catalog`,
    ready: `${name} catalog loaded`,
  }[state];
  const statusLabel = { loading: announcement, failed: `Retry ${name}`, ready: `${name} catalog details` }[state];

  useEffect(() => {
    const button = trigger.current;
    const popup = details.current;
    if (!enabled || !data || !button || !popup) return;
    const position = () => {
      if (popup.matches(':popover-open')) positionDetails(popup, button);
    };
    const onToggle = () => {
      if (popup.matches(':popover-open')) {
        position();
        popup.style.opacity = '1';
        popup.focus({ preventScroll: true });
      } else {
        popup.style.removeProperty('opacity');
      }
    };
    popup.addEventListener('toggle', onToggle);
    window.addEventListener('resize', position);
    window.addEventListener('scroll', position, true);
    return () => {
      popup.removeEventListener('toggle', onToggle);
      window.removeEventListener('resize', position);
      window.removeEventListener('scroll', position, true);
    };
  }, [enabled, data]);

  return (
    <div className="catalog-toggle" data-catalog={catalog} data-enabled={enabled}>
      <button type="button" className="control catalog-label" aria-pressed={enabled}
        title={`Overlay ${band} sources`} onClick={toggle}>{name}</button>
      <button ref={trigger} type="button" className="control catalog-status" hidden={!enabled}
        aria-label={statusLabel} aria-disabled={state === 'loading' || undefined}
        popoverTarget={state === 'ready' ? detailsId : undefined}
        title={state === 'failed' ? `${announcement}. Click to retry.` : statusLabel}
        onClick={state === 'failed' ? retry : undefined}>
        <span className="catalog-status-icon" data-state={state} aria-hidden="true">{state === 'ready' && 'i'}</span>
      </button>
      <span className="visually-hidden" role="status">{enabled ? announcement : ''}</span>
      {enabled && data && <section ref={details} id={detailsId} className="catalog-details"
        popover="auto" tabIndex={-1} aria-labelledby={titleId}>
        <h2 id={titleId}><span className="catalog-symbol" aria-hidden="true" />{band}</h2>
        <p>{catalogSummary(data)}</p>
        {data.sources.length > 0 && <p className="lookup-status">Positions at catalog epochs.</p>}
      </section>}
    </div>
  );
}

function positionDetails(popup: HTMLElement, trigger: HTMLButtonElement) {
  const anchor = trigger.getBoundingClientRect();
  const rail = trigger.closest('.comparison-rail');
  const sideRail = rail && getComputedStyle(rail).flexDirection === 'column';
  const verticalBounds = rail && !sideRail ? rail.getBoundingClientRect() : anchor;
  const { clientWidth, clientHeight } = document.documentElement;
  const { offsetWidth: width, offsetHeight: height } = popup;
  const gap = 6;
  const edge = 8;
  const right = anchor.right + gap;
  const above = verticalBounds.top - height - gap;
  const below = verticalBounds.bottom + gap;
  const fitsBeside = sideRail && right + width <= clientWidth - edge;
  const fitsBelow = below + height <= clientHeight - edge;
  const closerToBottom = anchor.top - verticalBounds.top > verticalBounds.bottom - anchor.bottom;
  const left = fitsBeside ? right : anchor.right - width;
  let top = above;
  if (fitsBeside) top = anchor.top;
  else if (above < edge || (closerToBottom && fitsBelow)) top = below;
  popup.style.left = `${Math.max(edge, Math.min(left, clientWidth - width - edge))}px`;
  popup.style.top = `${Math.max(edge, Math.min(top, clientHeight - height - edge))}px`;
}

function catalogSummary({ sources, limited }: CatalogData) {
  const count = sources.length;
  if (count === 0) return limited ? 'No returned sources in this patch' : 'No sources in this patch';
  if (limited) return `Showing the ${count} brightest sources`;
  return count === 1 ? '1 source' : `${count} sources`;
}

export function CatalogMarkers({ layers }: { layers: Layer[] }) {
  return layers.map(({ catalog, enabled, data }) => !enabled || !data?.sources.length ? null : (
    <svg key={catalog} className="catalog-markers" data-catalog={catalog} width="100%" height="100%" aria-hidden="true">
      {data.sources.map(({ x, y }, index) => (
        <svg key={index} x={`${x * 100}%`} y={`${y * 100}%`} width="1" height="1" overflow="visible">
          {catalog === 'gaia' ? <circle r="4" /> : <path d="M 0,-6 L 6,0 0,6 -6,0 Z" />}
        </svg>
      ))}
    </svg>
  ));
}
