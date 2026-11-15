import { useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { patchQuery, type SubmittedPatch } from './patchQuery';

const htmlEntity: Record<string, string> = { '&': '&amp;', '"': '&quot;', '<': '&lt;', '>': '&gt;' };
const escapeAttribute = (text: string) => text.replace(/[&"<>]/g, (char) => htmlEntity[char]);

export function SharePatch({ patch }: { patch: SubmittedPatch }) {
  const [message, setMessage] = useState('');
  const linkId = useId();
  const query = patchQuery(patch);
  const viewer = new URL(`/?${query}`, window.location.origin);
  const embed = new URL(`/embed?${query}`, viewer);
  const title = `SPHEREx comparison at RA ${query.get('ra')}°, Dec ${query.get('dec')}°`;
  const markup = `<iframe src="${escapeAttribute(embed.href)}" title="${escapeAttribute(title)}" loading="lazy" width="800" height="560" style="width: 100%; border: 0"></iframe>`;

  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      setMessage(`${label} copied.`);
    } catch {
      setMessage(`Select the ${label.toLowerCase()} text to copy it.`);
    }
  }

  return <div className="share-patch-fields">
    <p className="lookup-status">Links and embeds run Auto for this patch and its search filters.</p>
    {([['Patch', viewer.href], ['Embed', embed.href]] as const).map(([name, url]) => {
      const id = `${linkId}-${name}`;
      return <div className="share-link-field" key={name}>
        <label htmlFor={id}>{name} URL</label>
        <input id={id} readOnly value={url} onFocus={(event) => event.currentTarget.select()} />
        <button className="control" type="button" aria-label={`Copy ${name.toLowerCase()} link`}
          onClick={() => void copy(url, `${name} link`)}>Copy</button>
      </div>;
    })}
    <label>
      Iframe HTML
      <textarea rows={3} readOnly value={markup} onFocus={(event) => event.currentTarget.select()} />
    </label>
    <button className="control" type="button" onClick={() => void copy(markup, 'Iframe HTML')}>Copy iframe HTML</button>
    {message && <p className="lookup-status" role="status">{message}</p>}
  </div>;
}

export function SharePatchDialog({ patch }: { patch: SubmittedPatch }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  return <>
    <button ref={trigger} className="control" type="button" aria-haspopup="dialog"
      onClick={() => dialog.current?.showModal()}>Share patch</button>
    {createPortal(
      <dialog ref={dialog} className="share-patch-dialog" aria-labelledby={titleId}
        onClose={() => trigger.current?.focus({ preventScroll: true })}
        onKeyDown={(event) => {
          if (event.key === 'Escape') event.stopPropagation();
        }}>
        <header className="share-patch-heading">
          <h2 id={titleId}>Share patch</h2>
          <button className="control" type="button" onClick={() => dialog.current?.close()}>Close</button>
        </header>
        <SharePatch key={patchQuery(patch).toString()} patch={patch} />
      </dialog>,
      document.body,
    )}
  </>;
}
