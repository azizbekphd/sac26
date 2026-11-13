declare module 'aladin-lite' {
  type Source = object;
  type Catalog = { removeAll(): void; addSources(sources: Source[]): void };
  type Overlay = { removeAll(): void; add(shape: object): void; reportChange(): void };
  type ClickPosition = { ra: number; dec: number; isDragging?: boolean };

  interface Atlas {
    addCatalog(catalog: Catalog): void;
    addOverlay(overlay: Overlay): void;
    on(event: 'click', listener: (position: ClickPosition) => void): void;
    off(event: 'click'): void;
    gotoRaDec(ra: number, dec: number): void;
    setFoV(degrees: number): void;
  }

  const A: {
    init: Promise<void>;
    aladin(element: HTMLElement, options: {
      survey: string;
      target: string;
      fov: number;
      cooFrame: 'ICRSd';
      mode: 'dark';
      showReticle: boolean;
      showLayersControl: boolean;
      showSettingsControl: boolean;
      showFullscreenControl: boolean;
      showShareControl: boolean;
      showSimbadPointerControl: boolean;
      showCooGridControl: boolean;
      showCooLocation: boolean;
      showFrame: boolean;
      showFov: boolean;
      showProjectionControl: boolean;
      showStatusBar: boolean;
    }): Atlas;
    catalog(options: { name: string; color: string; hoverColor: string; selectionColor: string; sourceSize: number; shape: 'circle' }): Catalog;
    graphicOverlay(options: { name: string; color: string; lineWidth: number }): Overlay;
    polygon(corners: [number, number][]): object;
    source(ra: number, dec: number): Source;
  };

  export default A;
}
