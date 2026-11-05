/**
 * AIKSK Pose x Costume Matrix - dark styles and tiny DOM helpers.
 *
 * No top-level side effects: `ensureStyles()` injects the <style> element once
 * (idempotent) and the helper functions are only invoked by the UI factory.
 * ComfyUI imports every file in the extension web directory, so importing this
 * module alone must never touch the document.
 */

export const STYLE_ID = "aiksk-pc-styles";
export const PREFIX = "aiksk-pc";
export const HIDDEN_WIDGET_TYPE = "aiksk-hidden";

const CSS = String.raw`
.aiksk-pc-panel {
    --aiksk-pc-bg: #141b26;
    --aiksk-pc-bg-alt: #1b2534;
    --aiksk-pc-panel: #202c3e;
    --aiksk-pc-border: #33465f;
    --aiksk-pc-border-strong: #4a6483;
    --aiksk-pc-text: #e7eef9;
    --aiksk-pc-muted: #9fb0c7;
    --aiksk-pc-accent: #5c9aff;
    --aiksk-pc-accent-strong: #3f7fe6;
    --aiksk-pc-good: #4ccf9d;
    --aiksk-pc-warn: #f0b64b;
    --aiksk-pc-bad: #ff6b7a;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 5px;
    width: 100%;
    height: 100%;
    min-height: 0;
    padding: 7px 9px 9px;
    overflow-x: hidden;
    overflow-y: auto;
    color: var(--aiksk-pc-text);
    background: linear-gradient(180deg, #182231 0%, #131a25 100%);
    border: 1px solid #2b3b52;
    border-radius: 6px;
    font: 12px/1.45 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    scrollbar-width: thin;
    scrollbar-color: #33465f transparent;
    user-select: none;
}
.aiksk-pc-panel::-webkit-scrollbar { width: 8px; }
.aiksk-pc-panel::-webkit-scrollbar-thumb { background: #33465f; border-radius: 4px; }
.aiksk-pc-advanced {
    flex: 0 0 auto; border: 1px dashed #2f4258; border-radius: 6px;
    background: rgba(13, 20, 30, .45); padding: 2px 6px 6px;
}
.aiksk-pc-advanced > summary {
    cursor: pointer; color: #9fc4ff; font-size: 11.5px; font-weight: 600;
    padding: 3px 2px; user-select: none;
}
.aiksk-pc-advanced-body { display: flex; flex-direction: column; gap: 6px; padding-top: 4px; }
.aiksk-pc-panel *,
.aiksk-pc-overlay * { box-sizing: border-box; }
.aiksk-pc-panel button,
.aiksk-pc-overlay button { font: inherit; }

/* ---------- header ---------- */
.aiksk-pc-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    flex: 0 0 auto; min-height: 20px;
}
.aiksk-pc-title { font-size: 13px; font-weight: 650; letter-spacing: .2px; }
.aiksk-pc-title small { color: var(--aiksk-pc-muted); font-weight: 400; margin-left: 6px; }
.aiksk-pc-head-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }

/* ---------- buttons ---------- */
.aiksk-pc-btn {
    display: inline-flex; align-items: center; gap: 5px; justify-content: center;
    padding: 4px 9px; border-radius: 6px; border: 1px solid var(--aiksk-pc-border);
    background: var(--aiksk-pc-bg-alt); color: var(--aiksk-pc-text);
    cursor: pointer; white-space: nowrap; font-size: 12px; line-height: 1.3;
    transition: background .12s ease, border-color .12s ease, opacity .12s ease;
}
.aiksk-pc-btn:hover { background: #26344a; border-color: var(--aiksk-pc-border-strong); }
.aiksk-pc-btn:active { transform: translateY(1px); }
.aiksk-pc-btn:disabled { opacity: .42; cursor: not-allowed; transform: none; }
.aiksk-pc-btn--primary { background: var(--aiksk-pc-accent-strong); border-color: var(--aiksk-pc-accent-strong); color: #fff; }
.aiksk-pc-btn--primary:hover { background: var(--aiksk-pc-accent); border-color: var(--aiksk-pc-accent); }
.aiksk-pc-btn--danger { border-color: #7f3a44; color: #ffb3bb; }
.aiksk-pc-btn--danger:hover { background: #48222a; }
.aiksk-pc-btn--small { padding: 2px 6px; font-size: 11px; border-radius: 5px; }
.aiksk-pc-btn--ghost { background: transparent; border-color: transparent; color: var(--aiksk-pc-muted); }
.aiksk-pc-btn--ghost:hover { background: #26344a; color: var(--aiksk-pc-text); }
.aiksk-pc-btn b { font-weight: 700; }

/* ---------- gallery buttons ---------- */
.aiksk-pc-libs { display: flex; gap: 6px; flex: 0 0 auto; }
.aiksk-pc-lib {
    flex: 1 1 0; min-width: 0; justify-content: space-between;
    padding: 6px 10px; border-radius: 7px; font-weight: 600;
    background: linear-gradient(180deg, #26364d, #1b2738);
}
.aiksk-pc-lib .aiksk-pc-count-badge {
    min-width: 24px; padding: 0 7px; border-radius: 999px; text-align: center;
    background: #0e1622; color: #9fc4ff; font-size: 11px; font-weight: 700;
}
.aiksk-pc-lib--costume .aiksk-pc-count-badge { color: #ffc79a; }

/* ---------- selection chips ---------- */
.aiksk-pc-selected-block { flex: 0 0 auto; display: flex; flex-direction: column; gap: 3px; min-height: 0; }
.aiksk-pc-selected-head { display: flex; align-items: center; justify-content: space-between; color: var(--aiksk-pc-muted); font-size: 11px; }
.aiksk-pc-selected-list {
    display: flex; flex-wrap: nowrap; gap: 4px; overflow-x: auto; overflow-y: hidden;
    padding-bottom: 2px; min-height: 34px; align-items: stretch; flex: 0 0 auto;
}
.aiksk-pc-selected-list::-webkit-scrollbar { height: 6px; }
.aiksk-pc-selected-list:empty::after {
    content: attr(data-empty); color: #6d7f98; font-style: italic; align-self: center; padding: 5px 2px;
}
.aiksk-pc-chip {
    position: relative; display: flex; align-items: center; gap: 5px; flex: 0 0 auto;
    max-width: 190px; padding: 2px 18px 2px 2px; border-radius: 6px;
    border: 1px solid var(--aiksk-pc-border); background: #182131;
}
.aiksk-pc-chip img { width: 26px; height: 30px; object-fit: contain; border-radius: 4px; background: #0d141d; flex: 0 0 auto; }
.aiksk-pc-chip .aiksk-pc-chip-name {
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    font-size: 10.5px; line-height: 1.2; color: #cbd8ea; word-break: break-all;
}
.aiksk-pc-chip-del {
    position: absolute; top: 1px; right: 2px; width: 15px; height: 15px; padding: 0;
    border: 0; border-radius: 50%; background: transparent; color: #8396ae;
    cursor: pointer; line-height: 1; font-size: 12px;
}
.aiksk-pc-chip-del:hover { background: #58303a; color: #ffb3bb; }

/* ---------- config ---------- */
.aiksk-pc-section {
    flex: 0 0 auto; border-top: 1px solid #263447; padding-top: 6px;
    display: flex; flex-direction: column; gap: 6px;
}
.aiksk-pc-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.aiksk-pc-row--grow { flex: 1 1 auto; min-height: 0; }
.aiksk-pc-label { color: var(--aiksk-pc-muted); font-size: 11px; white-space: nowrap; }
.aiksk-pc-field { display: flex; align-items: center; gap: 5px; min-width: 0; }
.aiksk-pc-field > .aiksk-pc-label { flex: 0 0 auto; }

.aiksk-pc-input, .aiksk-pc-select, .aiksk-pc-textarea {
    min-width: 0; padding: 3px 6px; border-radius: 5px; border: 1px solid var(--aiksk-pc-border);
    background: #0e1622; color: var(--aiksk-pc-text); outline: none;
}
.aiksk-pc-input:focus, .aiksk-pc-select:focus, .aiksk-pc-textarea:focus { border-color: var(--aiksk-pc-accent); }
.aiksk-pc-input--number { width: 88px; }
.aiksk-pc-input--tiny { width: 58px; }
.aiksk-pc-select { cursor: pointer; }
.aiksk-pc-textarea {
    width: 100%; min-height: 42px; max-height: 120px; resize: vertical; user-select: text;
    font-family: ui-monospace, SFMono-Regular, Consolas, "Microsoft YaHei", monospace; font-size: 11px; line-height: 1.35;
}
.aiksk-pc-check { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; white-space: nowrap; }
.aiksk-pc-check input { accent-color: var(--aiksk-pc-accent); margin: 0; }
.aiksk-pc-check--model {
    padding: 3px 9px; border-radius: 999px; border: 1px solid #33465f;
    background: #0e1622; font-size: 12px; font-weight: 600; color: #cfe0f6;
    transition: border-color .12s ease, background .12s ease;
}
.aiksk-pc-check--model:hover { border-color: #5c7ea6; }
.aiksk-pc-check--model:has(input:checked) {
    border-color: var(--aiksk-pc-accent); background: rgba(92, 154, 255, .14); color: #eaf2ff;
}
.aiksk-pc-check--model input { transform: scale(1.15); }
.aiksk-pc-model-status {
    font-size: 10.8px; line-height: 1.45; padding: 2px 2px 0;
    color: #8ea2bb; white-space: normal; word-break: break-word;
}
.aiksk-pc-model-status--ok { color: var(--aiksk-pc-good); }
.aiksk-pc-model-status--warn { color: var(--aiksk-pc-warn); }
.aiksk-pc-model-status--bad { color: var(--aiksk-pc-bad); }
.aiksk-pc-hint { color: #7f93ad; font-size: 10.5px; line-height: 1.35; }
.aiksk-pc-hint--warn { color: var(--aiksk-pc-warn); }
.aiksk-pc-hint--bad { color: var(--aiksk-pc-bad); }
.aiksk-pc-hint--ok { color: var(--aiksk-pc-good); }

/* ---------- task count ---------- */
.aiksk-pc-summary {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    flex: 0 0 auto; padding: 5px 8px; border-radius: 6px;
    background: #101a27; border: 1px dashed #2f4258;
}
.aiksk-pc-count { font-size: 17px; font-weight: 750; color: #9fc4ff; }
.aiksk-pc-count-unit { color: var(--aiksk-pc-muted); font-size: 11px; margin-left: 3px; }
.aiksk-pc-count-detail { color: var(--aiksk-pc-muted); font-size: 11px; text-align: right; }

/* ---------- actions ---------- */
.aiksk-pc-actions { display: flex; flex-wrap: wrap; gap: 5px; flex: 0 0 auto; align-items: center; }
.aiksk-pc-actions .aiksk-pc-btn--primary { padding: 6px 12px; font-weight: 650; }

/* ---------- status / job ---------- */
.aiksk-pc-status {
    flex: 0 0 auto; min-height: 18px; color: #b9cbe2; font-size: 11.5px;
    white-space: pre-wrap; word-break: break-word; user-select: text;
}
.aiksk-pc-status--error { color: #ff9aa5; }
.aiksk-pc-status--warn { color: #ffd08a; }
.aiksk-pc-status--ok { color: #86e6bf; }
.aiksk-pc-log {
    flex: 1 1 auto; min-height: 34px; overflow: auto; border-radius: 6px;
    border: 1px solid #2a3a4f; background: #0c131d; padding: 5px 7px;
    font: 11px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; color: #b6c8de;
    white-space: pre-wrap; word-break: break-word; user-select: text;
}
.aiksk-pc-log:empty::after { content: "预演 / 任务状态会显示在这里"; color: #5f728b; }
.aiksk-pc-log .aiksk-pc-log-err { color: #ff9aa5; }
.aiksk-pc-log .aiksk-pc-log-warn { color: #ffd08a; }
.aiksk-pc-log .aiksk-pc-log-ok { color: #86e6bf; }
.aiksk-pc-progress { height: 6px; border-radius: 999px; background: #0b131d; overflow: hidden; flex: 0 0 auto; }
.aiksk-pc-progress > i { display: block; height: 100%; width: 0; background: linear-gradient(90deg, #3f7fe6, #6fb0ff); transition: width .25s ease; }
.aiksk-pc-job-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 0 0 auto; }
.aiksk-pc-pill {
    display: inline-flex; align-items: center; gap: 4px; padding: 1px 7px; border-radius: 999px;
    background: #1d2a3c; border: 1px solid #33465f; font-size: 10.5px; color: #b9cbe2;
}
.aiksk-pc-pill--ok { color: #86e6bf; border-color: #2c5a4a; background: #12271f; }
.aiksk-pc-pill--bad { color: #ff9aa5; border-color: #6b333c; background: #2a161b; }
.aiksk-pc-pill--warn { color: #ffd08a; border-color: #6b542a; background: #2a2212; }
.aiksk-pc-bindings { display: flex; flex-direction: column; gap: 2px; flex: 0 0 auto; }
.aiksk-pc-binding { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--aiksk-pc-muted); flex-wrap: wrap; }
.aiksk-pc-binding b { color: var(--aiksk-pc-text); font-weight: 600; }
.aiksk-pc-dot { width: 7px; height: 7px; border-radius: 50%; background: #5f728b; flex: 0 0 auto; }
.aiksk-pc-dot--ok { background: var(--aiksk-pc-good); box-shadow: 0 0 5px rgba(76, 207, 157, .7); }
.aiksk-pc-dot--bad { background: var(--aiksk-pc-bad); }
.aiksk-pc-dot--warn { background: var(--aiksk-pc-warn); }

/* ---------- modal ---------- */
.aiksk-pc-overlay {
    position: fixed; inset: 0; z-index: 12000; display: flex; align-items: center; justify-content: center;
    background: rgba(4, 8, 13, .62); backdrop-filter: blur(2px);
    font: 13px/1.5 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: #e7eef9;
}
.aiksk-pc-modal {
    display: flex; flex-direction: column; gap: 8px; min-height: 0;
    width: min(1120px, 94vw); height: min(780px, 90vh);
    padding: 12px 14px; border-radius: 12px; border: 1px solid #3a5069;
    background: linear-gradient(180deg, #1a2432 0%, #121a25 100%);
    box-shadow: 0 24px 60px rgba(0, 0, 0, .55);
}
.aiksk-pc-modal--small { width: min(720px, 92vw); height: auto; max-height: 88vh; }
.aiksk-pc-modal-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex: 0 0 auto; }
.aiksk-pc-modal-title { font-size: 16px; font-weight: 700; }
.aiksk-pc-modal-title small { color: #9fb0c7; font-weight: 400; font-size: 12px; margin-left: 8px; }
.aiksk-pc-modal-body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 8px; overflow: hidden; }
.aiksk-pc-modal-foot { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; border-top: 1px solid #2b3b52; padding-top: 8px; }
.aiksk-pc-modal-foot-group { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.aiksk-pc-filters { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; flex: 0 0 auto; }
.aiksk-pc-search { flex: 1 1 220px; min-width: 160px; padding: 5px 9px; border-radius: 6px; border: 1px solid #33465f; background: #0e1622; color: #e7eef9; outline: none; }
.aiksk-pc-search:focus { border-color: var(--aiksk-pc-accent); }
.aiksk-pc-grid {
    flex: 1 1 auto; min-height: 0; overflow: auto; display: grid; gap: 8px;
    grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
    grid-auto-rows: 198px;
    align-content: start; padding: 2px;
}
.aiksk-pc-grid::-webkit-scrollbar { width: 9px; }
.aiksk-pc-grid::-webkit-scrollbar-thumb { background: #33465f; border-radius: 5px; }
.aiksk-pc-card {
    position: relative; display: flex; flex-direction: column; min-width: 0;
    height: 198px; min-height: 198px;
    border: 1px solid #33465f; border-radius: 8px; overflow: hidden; cursor: pointer;
    background: #111a27; transition: border-color .12s ease, box-shadow .12s ease;
}
.aiksk-pc-card:hover { border-color: #5c7ea6; }
.aiksk-pc-card--selected { border-color: var(--aiksk-pc-accent); box-shadow: 0 0 0 1px var(--aiksk-pc-accent), 0 0 12px rgba(92, 154, 255, .35); }
.aiksk-pc-card img { display: block; width: 100%; height: 152px; flex: 0 0 152px; min-height: 0; object-fit: contain; background: #0d141d; }
.aiksk-pc-card img.aiksk-pc-img--missing { opacity: .25; }
.aiksk-pc-card-body { padding: 4px 6px 6px; display: flex; flex-direction: column; gap: 1px; flex: 1 1 auto; min-height: 0; overflow: hidden; justify-content: center; }
.aiksk-pc-card-title { font-size: 11.5px; font-weight: 600; line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aiksk-pc-card-meta { font-size: 10px; color: #8ea2bb; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aiksk-pc-card-order {
    position: absolute; top: 4px; right: 4px; min-width: 20px; height: 20px; padding: 0 5px;
    border-radius: 999px; background: var(--aiksk-pc-accent); color: #06101e;
    font-size: 11px; font-weight: 800; display: none; align-items: center; justify-content: center;
}
.aiksk-pc-card--selected .aiksk-pc-card-order { display: flex; }
.aiksk-pc-grid-empty { grid-column: 1 / -1; padding: 28px; text-align: center; color: #7f93ad; }
.aiksk-pc-load-more { flex: 0 0 auto; align-self: center; }

/* ---------- matrix ---------- */
.aiksk-pc-matrix-wrap { flex: 1 1 auto; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 8px; }
.aiksk-pc-matrix { display: grid; gap: 3px; align-self: start; }
.aiksk-pc-matrix-head, .aiksk-pc-matrix-corner {
    display: flex; align-items: center; justify-content: center; min-height: 30px; padding: 2px 4px;
    background: #1d2a3c; border: 1px solid #33465f; border-radius: 5px;
    font-size: 10.5px; color: #a9bdd6; cursor: pointer; overflow: hidden;
}
.aiksk-pc-matrix-head { max-width: 120px; }
.aiksk-pc-matrix-head:hover, .aiksk-pc-matrix-corner:hover { border-color: var(--aiksk-pc-accent); color: #d9e8ff; }
.aiksk-pc-matrix-corner { cursor: pointer; }
.aiksk-pc-matrix-cell {
    min-height: 30px; display: flex; align-items: center; justify-content: center;
    border: 1px solid #2c3d52; border-radius: 5px; background: #101a27; color: #6f86a2;
    font-size: 11px; font-weight: 700; cursor: pointer; user-select: none;
}
.aiksk-pc-matrix-cell:hover { border-color: #5c7ea6; background: #172333; }
.aiksk-pc-matrix-cell--on { background: #1c3a5e; border-color: var(--aiksk-pc-accent); color: #bfe0ff; }
.aiksk-pc-matrix-cell--on:hover { background: #24476f; }
.aiksk-pc-tree { display: flex; flex-direction: column; gap: 4px; }
.aiksk-pc-tree-group { border: 1px solid #2c3d52; border-radius: 6px; background: #111a27; overflow: hidden; }
.aiksk-pc-tree-group > summary { padding: 5px 8px; cursor: pointer; color: #b9cbe2; font-size: 11.5px; list-style: none; display: flex; align-items: center; gap: 6px; }
.aiksk-pc-tree-group > summary::-webkit-details-marker { display: none; }
.aiksk-pc-tree-group > summary::before { content: "▸"; color: #7f93ad; font-size: 10px; }
.aiksk-pc-tree-group[open] > summary::before { content: "▾"; }
.aiksk-pc-tree-row { display: flex; align-items: center; gap: 7px; padding: 3px 8px 3px 18px; font-size: 11px; color: #a9bdd6; }
.aiksk-pc-tree-row + .aiksk-pc-tree-row { border-top: 1px solid #1c2839; }
.aiksk-pc-tree-row b { color: #cfe2f9; font-weight: 600; }
.aiksk-pc-tree-row .aiksk-pc-tree-idx { color: #6f86a2; min-width: 30px; }
.aiksk-pc-tree-row button { margin-left: auto; }

/* ---------- misc ---------- */
.aiksk-pc-plan-table { display: flex; flex-direction: column; gap: 1px; font-size: 10.5px; color: #a9bdd6; }
.aiksk-pc-plan-table > div { display: flex; gap: 6px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.aiksk-pc-plan-table > div > span { overflow: hidden; text-overflow: ellipsis; }
.aiksk-pc-plan-table .aiksk-pc-plan-key { color: #cfe2f9; min-width: 130px; }
.aiksk-pc-sep { height: 1px; background: #263447; margin: 2px 0; flex: 0 0 auto; }
.aiksk-pc-file-input { display: none; }
.aiksk-pc-spacer { flex: 1 1 auto; }
.aiksk-pc-hidden { display: none !important; }
`;

/**
 * Inject the stylesheet once.  Safe to call repeatedly and safe during early
 * extension setup (document.head exists).
 */
export function ensureStyles(doc = globalThis.document) {
    if (!doc || !doc.head) return false;
    ensureStyles._done = ensureStyles._done || {};
    if (ensureStyles._done[STYLE_ID] || doc.getElementById(STYLE_ID)) {
        ensureStyles._done[STYLE_ID] = true;
        return false;
    }
    const style = doc.createElement("style");
    style.id = STYLE_ID;
    style.textContent = CSS;
    doc.head.append(style);
    ensureStyles._done[STYLE_ID] = true;
    return true;
}

/** Remove the shared stylesheet (used by browser tests between phases). */
export function removeStyles(doc = globalThis.document) {
    const element = doc?.getElementById?.(STYLE_ID);
    if (element) element.remove();
    if (ensureStyles._done) delete ensureStyles._done[STYLE_ID];
}

/* ------------------------------------------------------------------ */
/* DOM helpers                                                         */
/* ------------------------------------------------------------------ */

/**
 * Create an element.
 * @param {string} tag  tag name ("div", "button", ...)
 * @param {string|object} [attrs] class string or attribute map
 * @param {(Node|string)|Array<Node|string>} [children]
 */
export function el(tag, attrs = "", children = []) {
    const node = document.createElement(tag);
    if (typeof attrs === "string") {
        if (attrs) node.className = attrs;
    } else if (attrs && typeof attrs === "object") {
        for (const [key, value] of Object.entries(attrs)) {
            if (value == null || value === false) continue;
            if (key === "class" || key === "className") node.className = value;
            else if (key === "text") node.textContent = String(value);
            else if (key === "dataset") Object.assign(node.dataset, value);
            else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
            else if (key === "testid") node.dataset.testid = value;
            else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
            else if (key === "checked" || key === "disabled" || key === "hidden" || key === "selected") node[key] = Boolean(value);
            else if (key === "value") node.value = value;
            else node.setAttribute(key, String(value));
        }
    }
    const list = Array.isArray(children) ? children : [children];
    for (const child of list) {
        if (child == null || child === false) continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

/** Create a button with an optional data-testid. */
export function btn(label, options = {}) {
    const { cls = "", testid = "", title = "", onClick = null, disabled = false, type = "button" } = options;
    const classes = ["aiksk-pc-btn", cls].filter(Boolean).join(" ");
    const element = el("button", { class: classes, type, title: title || null, testid: testid || null });
    element.append(...(Array.isArray(label) ? label : [label]).map((part) => (part instanceof Node ? part : document.createTextNode(String(part)))));
    if (disabled) element.disabled = true;
    if (onClick) element.addEventListener("click", onClick);
    return element;
}

/** Label + control wrapper. */
export function field(label, control, { cls = "" } = {}) {
    return el("label", { class: `aiksk-pc-field ${cls}`.trim() }, [
        el("span", "aiksk-pc-label", label),
        control,
    ]);
}

export function selectBox(options, { value = "", testid = "", cls = "", onChange = null, ariaLabel = "" } = {}) {
    const select = el("select", { class: `aiksk-pc-select ${cls}`.trim(), testid: testid || null, "aria-label": ariaLabel || null });
    for (const option of options) {
        const item = typeof option === "string" ? { value: option, label: option } : option;
        const element = el("option", { value: String(item.value), text: String(item.label ?? item.value) });
        select.append(element);
    }
    if (value !== "" && value != null) select.value = String(value);
    if (onChange) select.addEventListener("change", () => onChange(select.value));
    return select;
}

export function checkbox(label, { checked = false, testid = "", cls = "", onChange = null, title = "" } = {}) {
    const input = el("input", { type: "checkbox", checked: Boolean(checked), testid: testid || null, title: title || null });
    const wrapper = el("label", { class: `aiksk-pc-check ${cls}`.trim(), title: title || null }, [input, el("span", "", label)]);
    if (onChange) input.addEventListener("change", () => onChange(input.checked, input));
    wrapper.input = input;
    return wrapper;
}

export function numberInput({ value = 0, min = 0, max = 999999, step = 1, testid = "", cls = "", onChange = null, title = "" } = {}) {
    const input = el("input", {
        type: "number", value, min, max, step, testid: testid || null, title: title || null,
        class: `aiksk-pc-input ${cls}`.trim(),
    });
    if (onChange) {
        input.addEventListener("change", () => onChange(input.value, input));
    }
    return input;
}

export function textInput({ value = "", placeholder = "", testid = "", cls = "", onChange = null } = {}) {
    const input = el("input", { type: "text", value, placeholder, testid: testid || null, class: `aiksk-pc-input ${cls}`.trim() });
    if (onChange) input.addEventListener("input", () => onChange(input.value, input));
    return input;
}

export function textArea({ value = "", placeholder = "", testid = "", rows = 3, onChange = null } = {}) {
    const area = el("textarea", { placeholder, testid: testid || null, rows, class: "aiksk-pc-textarea" });
    area.value = value;
    if (onChange) area.addEventListener("input", () => onChange(area.value, area));
    return area;
}

export function progressBar({ value = 0, testid = "" } = {}) {
    const fill = el("i", { style: { width: `${Math.max(0, Math.min(100, value))}%` } });
    const bar = el("div", { class: "aiksk-pc-progress", testid: testid || null }, [fill]);
    bar.setValue = (percent) => { fill.style.width = `${Math.max(0, Math.min(100, percent))}%`; };
    bar.setValue(value);
    return bar;
}

export function pill(text, cls = "") {
    return el("span", { class: `aiksk-pc-pill ${cls}`.trim() }, text);
}

/**
 * Create a full-screen modal (page-level, NOT an iframe).
 * Returns {overlay, modal, head, title, body, foot, close, setTitle}.
 */
export function createModal({ title = "", subtitle = "", testid = "", small = false, onClose = null } = {}) {
    const titleElement = el("div", "aiksk-pc-modal-title", [
        el("span", "", title),
        subtitle ? el("small", "", subtitle) : null,
    ]);
    const closeButton = btn("关闭 ×", { cls: "aiksk-pc-btn--ghost", title: "关闭（保留已应用的选择）" });
    const head = el("div", "aiksk-pc-modal-head", [titleElement, closeButton]);
    const body = el("div", "aiksk-pc-modal-body");
    const foot = el("div", "aiksk-pc-modal-foot");
    const modal = el("div", { class: `aiksk-pc-modal${small ? " aiksk-pc-modal--small" : ""}` }, [head, body, foot]);
    const overlay = el("div", { class: "aiksk-pc-overlay", testid: testid || null }, [modal]);
    let closed = false;
    const close = (reason = "close") => {
        if (closed) return;
        closed = true;
        overlay.remove();
        document.removeEventListener("keydown", onKeyDown, true);
        onClose?.(reason);
    };
    function onKeyDown(event) {
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            close("escape");
        }
    }
    closeButton.addEventListener("click", () => close("close"));
    overlay.addEventListener("mousedown", (event) => {
        if (event.target === overlay) close("backdrop");
    });
    document.addEventListener("keydown", onKeyDown, true);
    document.body.append(overlay);
    return {
        overlay, modal, head, title: titleElement, body, foot,
        close,
        setTitle(text, sub = "") {
            titleElement.replaceChildren(el("span", "", text), sub ? el("small", "", sub) : "");
        },
        get isOpen() { return !closed; },
    };
}

/** Build an <img> with a one-step fallback URL chain. */
export function thumbnail(urls, alt = "", { cls = "" } = {}) {
    const list = (Array.isArray(urls) ? urls : [urls]).filter(Boolean);
    const image = el("img", { class: cls || null, alt: alt || null, loading: "lazy", decoding: "async" });
    let index = 0;
    const apply = () => {
        if (index >= list.length) {
            image.classList.add("aiksk-pc-img--missing");
            image.alt = `${alt || ""}（图片缺失）`;
            return;
        }
        image.src = list[index];
    };
    image.addEventListener("error", () => { index += 1; apply(); });
    apply();
    return image;
}
