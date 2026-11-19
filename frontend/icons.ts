const paths = {
  home: '<rect x="4.5" y="5" width="15" height="16" rx="3"/><path d="M8 3v4m8-4v4M4.5 10h15m-12 5 2.5 2.5 4.5-4.5"/>',
  projects: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M3 10h18"/>',
  settings: '<circle cx="12" cy="12" r="2.7"/><path d="M10 3.7h4l.6 2.2 1.5.9 2.2-.6 2 3.4-1.6 1.5v1.8l1.6 1.5-2 3.4-2.2-.6-1.5.9-.6 2.2h-4l-.6-2.2-1.5-.9-2.2.6-2-3.4 1.6-1.5v-1.8L3.7 9.6l2-3.4 2.2.6 1.5-.9z"/>',
  person: '<circle cx="12" cy="8" r="3.3"/><path d="M5.5 20v-1.5a5 5 0 0 1 5-5h3a5 5 0 0 1 5 5V20"/>',
  edit: '<path d="m14.5 6 3.5 3.5M5 19l3.6-.7L19 8A2.5 2.5 0 1 0 15.5 4.5L5.8 14.8z"/>',
  bookmark: '<path d="M7 5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16l-5-3.5L7 21z"/>',
  document: '<path d="M7 3h7l4 4v12a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z"/><path d="M14 3v5h4M9 12h5m-5 4h5"/>',
  shield: '<path d="M12 3l7 3v6c0 3.8-3.4 6.8-7 9-3.6-2.2-7-5.2-7-9V6z"/><path d="m9 12 2 2 4-4"/>',
  arrow: '<path d="m9 5 7 7-7 7"/>',
  right: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  up: '<path d="M6 18 18 6M7 6h11v11"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  moon: '<path d="M20 15.3A8.5 8.5 0 0 1 8.7 4 8.5 8.5 0 1 0 20 15.3Z"/>',
  plane:
    '<path d="m21 3-5.4 17-4.3-6.4-7.8-2.3L21 3Z"/><path d="m11.3 13.6 5-5"/>',
  sparkle:
    '<path d="m12 2 2.6 7.4L22 12l-7.4 2.6L12 22l-2.6-7.4L2 12l7.4-2.6L12 2Z"/>',
  skills: '<path d="m12 3 8.5 4.5L12 12 3.5 7.5 12 3Zm-8.5 9 8.5 4.5 8.5-4.5m-17 4.5 8.5 4.5 8.5-4.5"/>',
  rss: '<path d="M5 5c7.5 0 14 6.5 14 14M5 11c4.2 0 8 3.8 8 8"/><circle cx="5.5" cy="18.5" r="1.4" fill="currentColor" stroke="none"/>',
  calendar:
    '<rect x="3.5" y="5" width="17" height="16" rx="3"/><path d="M7.5 3v4m9-4v4m-13 3h17m-13 4h3m3 0h3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="m5 12 4.5 4.5L19 7"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  activity: '<path d="M2 12h5l3-7 4 14 3-7h5"/>',
  bell: '<path d="M5 16h14l-1.5-2.6V10a5.5 5.5 0 0 0-11 0v3.4L5 16Zm4.5 3a3 3 0 0 0 5 0"/>',
  globe:
    '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
  trash: '<path d="M4 6h16M9 6V3h6v3M6 6l1 14h10l1-14M10 10v6m4-6v6"/>',
};
export type IconName = keyof typeof paths;

export function icon(name: IconName) {
  return (
    '<svg aria-hidden="true" class="icon" viewBox="0 0 24 24">' +
    paths[name] +
    "</svg>"
  );
}
