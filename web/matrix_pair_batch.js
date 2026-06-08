import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

/*
 * KSKNODES 矩阵配对批量 (图像 × 视频)
 * ----------------------------------
 * 视频侧输出与 VHS_LoadVideo 完全一致（视频帧/帧数/音频/视频信息），即插即用。
 * UI 提供两种视图：
 *   - 矩阵视图：行=图像 列=视频，格子=配对，可点选；行/列表头可整行整列切换
 *   - 树状视图：图像为父节点，其下挂选中的视频，体现"排列组合"的树状思维
 * 「批量执行」把每个勾选组合各排成一条独立队列任务（方案二）。
 */

const NODE_TYPE = "KSK_MatrixPairBatch";
const STORAGE_WIDGETS = ["image_files", "video_files", "selection", "active_index"];
const SIZE_WIDGETS = ["custom_width", "custom_height"];
const MANUAL_SIZE_SOURCE = "手动宽高";

// ---------- 工具 ----------
const safeParse = (t, f) => { try { const v = JSON.parse(t); return v == null ? f : v; } catch { return f; } };
const keyOf = (i, j) => `${i},${j}`;
const keyOfCell = (cell) => keyOf(cell.imageIndex, cell.videoIndex);
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const baseName = (n) => (n || "").split(/[\\/]/).pop();
const inputThumbUrl = (name) => api.apiURL(`/view?filename=${encodeURIComponent(name)}&type=input&subfolder=&t=${Date.now()}`);
const isImageName = (n) => /\.(png|jpe?g|webp|bmp)$/i.test(n || "");
const isVideoName = (n) => /\.(mp4|webm|mkv|mov|avi|gif|m4v)$/i.test(n || "");
const uniqAppend = (target, incoming) => {
    const seen = new Set(target);
    let added = 0;
    for (const name of incoming || []) {
        if (!name || seen.has(name)) continue;
        target.push(name);
        seen.add(name);
        added++;
    }
    return added;
};
const widgetValue = (node, name, fallback = "") => node.widgets?.find(w => w.name === name)?.value ?? fallback;
const inputSlotIndex = (node, name) => node.inputs?.findIndex(i => i?.name === name || i?.widget?.name === name) ?? -1;
const linkedSizeInputs = (node) => SIZE_WIDGETS.filter(name => {
    const idx = inputSlotIndex(node, name);
    return idx >= 0 && node.inputs?.[idx]?.link != null;
});
const disconnectSizeInputs = (node) => {
    let count = 0;
    for (const name of SIZE_WIDGETS) {
        const idx = inputSlotIndex(node, name);
        if (idx >= 0 && node.inputs?.[idx]?.link != null) {
            node.disconnectInput(idx);
            count++;
        }
    }
    if (count) node.graph?.setDirtyCanvas?.(true, true);
    return count;
};
const assertContinuousDisplayOrder = (orderedSelections, context = "selection") => {
    const orders = orderedSelections.map(cell => cell.displayOrder);
    const n = orderedSelections.length;
    const max = orders.length ? Math.max(...orders) : 0;
    const unique = new Set(orders);
    const continuous = orders.slice().sort((a, b) => a - b).every((order, index) => order === index + 1);
    if (max !== n || unique.size !== n || !continuous) {
        const message = `[KSK] ${context} 编号不连续：count=${n}, max=${max}, orders=${orders.join(",")}`;
        console.error(message);
        throw new Error(message);
    }
};

// ---------- 状态 ----------
class MatrixState {
    constructor(node) {
        this.node = node;
        this.images = [];
        this.videos = [];
        this.selected = new Map(); // "i,j" -> 顺序号
        this._order = 0;
        this.load();
    }
    get wImages() { return this.node.widgets?.find(w => w.name === "image_files"); }
    get wVideos() { return this.node.widgets?.find(w => w.name === "video_files"); }
    get wSelection() { return this.node.widgets?.find(w => w.name === "selection"); }
    get wActive() { return this.node.widgets?.find(w => w.name === "active_index"); }

    load() {
        this.images = (safeParse(this.wImages?.value, []) || []).filter(isImageName);
        this.videos = (safeParse(this.wVideos?.value, []) || []).filter(isVideoName);
        const sel = safeParse(this.wSelection?.value, []) || [];
        this.selected.clear(); this._order = 0;
        for (const p of sel) {
            if (!Array.isArray(p) || p.length !== 2) continue;
            const i = Number(p[0]);
            const j = Number(p[1]);
            if (Number.isInteger(i) && Number.isInteger(j) && i >= 0 && i < this.images.length && j >= 0 && j < this.videos.length) {
                this.selected.set(keyOf(i, j), this._order++);
            }
        }
    }
    save() {
        this.normalize();
        assertContinuousDisplayOrder(this.orderedSelections(), "保存选择");
        if (this.wImages) this.wImages.value = JSON.stringify(this.images);
        if (this.wVideos) this.wVideos.value = JSON.stringify(this.videos);
        if (this.wSelection) this.wSelection.value = JSON.stringify(this.orderedPairs());
        if (this.wActive) this.wActive.value = Math.max(0, Number(this.wActive.value) || 0);
        this.node.graph?.setDirtyCanvas?.(true, true);
    }
    orderedPairs() {
        return this.orderedSelections().map(cell => [cell.imageIndex, cell.videoIndex]);
    }
    orderedSelections() {
        const seen = new Set();
        const result = [];
        for (const [k] of [...this.selected.entries()].sort((a, b) => a[1] - b[1])) {
            const [imageIndex, videoIndex] = k.split(",").map(Number);
            if (!Number.isInteger(imageIndex) || !Number.isInteger(videoIndex)) continue;
            if (imageIndex < 0 || imageIndex >= this.images.length) continue;
            if (videoIndex < 0 || videoIndex >= this.videos.length) continue;
            const key = keyOf(imageIndex, videoIndex);
            if (seen.has(key)) continue;
            seen.add(key);
            result.push({ imageIndex, videoIndex });
        }
        return result.map((cell, index) => ({ ...cell, displayOrder: index + 1 }));
    }
    normalize() {
        const nx = new Map();
        for (const cell of this.orderedSelections()) nx.set(keyOfCell(cell), cell.displayOrder - 1);
        this.selected = nx;
        this._order = nx.size;
    }
    orderMap() {
        return new Map(this.orderedSelections().map(cell => [keyOfCell(cell), cell.displayOrder]));
    }
    isSel(i, j) { return this.selected.has(keyOf(i, j)); }
    toggle(i, j) { const k = keyOf(i, j); this.selected.has(k) ? this.selected.delete(k) : this.selected.set(k, this._order++); this.save(); }
    selectAll() { this.selected.clear(); this._order = 0; for (let i = 0; i < this.images.length; i++) for (let j = 0; j < this.videos.length; j++) this.selected.set(keyOf(i, j), this._order++); this.save(); }
    selectDiagonal() { this.selected.clear(); this._order = 0; const n = Math.min(this.images.length, this.videos.length); for (let i = 0; i < n; i++) this.selected.set(keyOf(i, i), this._order++); this.save(); }
    clear() { this.selected.clear(); this._order = 0; this.save(); }
    clearAll() { this.images = []; this.videos = []; this.selected.clear(); this._order = 0; if (this.wActive) this.wActive.value = 0; this.save(); }
    invert() { const nx = new Map(); let o = 0; for (let i = 0; i < this.images.length; i++) for (let j = 0; j < this.videos.length; j++) if (!this.isSel(i, j)) nx.set(keyOf(i, j), o++); this.selected = nx; this._order = o; this.save(); }
    toggleRow(i) { const on = this.videos.every((_, j) => this.isSel(i, j)); for (let j = 0; j < this.videos.length; j++) { const k = keyOf(i, j); on ? this.selected.delete(k) : (!this.selected.has(k) && this.selected.set(k, this._order++)); } this.save(); }
    toggleCol(j) { const on = this.images.every((_, i) => this.isSel(i, j)); for (let i = 0; i < this.images.length; i++) { const k = keyOf(i, j); on ? this.selected.delete(k) : (!this.selected.has(k) && this.selected.set(k, this._order++)); } this.save(); }
    removeImage(i) { this.images.splice(i, 1); this._reindex(i, null); }
    removeVideo(j) { this.videos.splice(j, 1); this._reindex(null, j); }
    _reindex(delI, delJ) {
        const nx = new Map(); let o = 0;
        for (const [k] of [...this.selected.entries()].sort((a, b) => a[1] - b[1])) {
            let [r, c] = k.split(",").map(Number);
            if (delI != null) { if (r === delI) continue; if (r > delI) r--; }
            if (delJ != null) { if (c === delJ) continue; if (c > delJ) c--; }
            nx.set(keyOf(r, c), o++);
        }
        this.selected = nx; this._order = o; this.save();
    }
}

// ---------- 样式 ----------
function injectStyleOnce() {
    if (document.getElementById("ksk-matrix-style")) return;
    const css = `
.ksk-mx{--bg:#1b1d22;--bg2:#23262d;--line:#33373f;--accent:#4f9dff;--ok:#37b24d;--txt:#e6e8ec;--mut:#8b9099;
  font-family:-apple-system,"Segoe UI",sans-serif;font-size:12px;color:var(--txt);display:flex;flex-direction:column;
  width:100%;max-width:100%;height:100%;min-width:0;min-height:0;box-sizing:border-box;background:var(--bg);border-radius:8px;overflow:hidden;}
.ksk-mx *{box-sizing:border-box;}
.ksk-tabs{display:flex;gap:2px;padding:6px 8px 0;background:var(--bg2);flex:0 0 auto;min-width:0;}
.ksk-tab{padding:5px 14px;border-radius:6px 6px 0 0;cursor:pointer;color:var(--mut);font-weight:600;user-select:none;}
.ksk-tab.active{background:var(--bg);color:var(--txt);}
.ksk-bar{display:flex;flex-wrap:wrap;gap:5px;align-items:center;padding:7px 8px;background:var(--bg2);border-bottom:1px solid var(--line);flex:0 0 auto;min-width:0;}
.ksk-btn{background:#2c313a;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:4px 9px;cursor:pointer;
  font-size:12px;display:inline-flex;align-items:center;gap:4px;transition:.12s;}
.ksk-btn:hover{background:#363c47;border-color:#4a515c;}
.ksk-btn:disabled{opacity:.55;cursor:not-allowed;filter:none;}
.ksk-btn.primary{background:var(--ok);border-color:var(--ok);color:#fff;font-weight:700;}
.ksk-btn.primary:hover{filter:brightness(1.1);}
.ksk-btn.accent{background:var(--accent);border-color:var(--accent);color:#fff;}
.ksk-sel{background:#2c313a;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:4px 6px;}
.ksk-stat{margin-left:auto;color:var(--mut);font-size:11px;min-width:0;flex:1 1 120px;text-align:right;}
.ksk-stat b{color:var(--accent);}
.ksk-body{flex:1 1 auto;overflow:auto;padding:8px;min-height:0;}
.ksk-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--mut);text-align:center;padding:20px;line-height:1.7;}
/* 矩阵 */
.ksk-grid{--cell:84px;--grid-gap:6px;display:grid;grid-auto-rows:var(--cell);gap:var(--grid-gap);
  width:100%;min-width:0;align-items:stretch;justify-content:start;}
.ksk-corner,.ksk-grid-head,.ksk-row-head,.ksk-cellbox{width:var(--cell);height:var(--cell);display:flex;align-items:center;justify-content:center;}
.ksk-corner{position:sticky;left:0;top:0;z-index:5;background:var(--bg2);border:1px solid var(--line);border-radius:7px;color:var(--mut);font-size:clamp(10px, calc(var(--cell) * .12), 13px);}
.ksk-grid-head{position:sticky;top:0;z-index:3;background:var(--bg);border-radius:7px;}
.ksk-row-head{position:sticky;left:0;z-index:2;background:var(--bg);border-radius:7px;}
.ksk-head{background:var(--bg2);border:1px solid var(--line);border-radius:7px;padding:clamp(2px, calc(var(--cell) * .05), 6px);cursor:pointer;transition:.12s;
  width:calc(var(--cell) - 6px);height:calc(var(--cell) - 6px);display:flex;flex-direction:column;align-items:center;justify-content:center;}
.ksk-head:hover{border-color:var(--accent);}
.ksk-media{position:relative;width:clamp(32px, calc(var(--cell) - 26px), 126px);height:clamp(32px, calc(var(--cell) - 26px), 126px);border-radius:5px;overflow:hidden;background:#111;display:block;flex:0 0 auto;}
.ksk-thumb{width:100%;height:100%;object-fit:cover;display:block;background:#000;}
.ksk-cap{width:calc(var(--cell) - 14px);font-size:clamp(8px, calc(var(--cell) * .11), 11px);color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px;text-align:center;}
.ksk-media.video::after{content:"▶";position:absolute;top:3px;left:5px;font-size:10px;color:#fff;text-shadow:0 0 3px #000;}
.ksk-fallback{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--mut);font-size:18px;background:#171a20;}
.ksk-media.broken .ksk-fallback{display:flex;}
.ksk-media.broken .ksk-thumb{display:none;}
.ksk-cellbox{background:#20232a;border:1px solid #2d323a;border-radius:7px;}
.ksk-cell{width:clamp(28px, calc(var(--cell) * .5), 62px);height:clamp(28px, calc(var(--cell) * .5), 62px);border-radius:clamp(5px, calc(var(--cell) * .08), 10px);background:#2a2e36;cursor:pointer;position:relative;transition:.1s;border:1px solid transparent;}
.ksk-cell:hover{border-color:var(--accent);transform:scale(1.08);}
.ksk-cell.on{background:var(--ok);box-shadow:0 0 6px rgba(55,178,77,.5);}
.ksk-cell.on::after{content:attr(data-ord);position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-size:clamp(9px, calc(var(--cell) * .12), 15px);font-weight:700;}
/* 树状 */
.ksk-tree{display:flex;flex-direction:column;gap:8px;}
.ksk-tnode{background:var(--bg2);border:1px solid var(--line);border-radius:8px;overflow:hidden;}
.ksk-tparent{display:flex;align-items:center;gap:8px;padding:6px 8px;cursor:pointer;}
.ksk-tparent:hover{background:#2a2e36;}
.ksk-tparent .ksk-media{width:42px;height:42px;flex:0 0 42px;}
.ksk-tparent .name{font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ksk-tparent .cnt{color:var(--mut);font-size:11px;}
.ksk-tchildren{display:flex;flex-wrap:wrap;gap:6px;padding:4px 8px 10px 40px;}
.ksk-chip{display:flex;align-items:center;gap:5px;padding:3px 8px 3px 4px;border-radius:14px;background:#2a2e36;border:1px solid var(--line);cursor:pointer;font-size:11px;transition:.1s;}
.ksk-chip .ksk-media{width:24px;height:24px;flex:0 0 24px;border-radius:50%;}
.ksk-chip .ksk-media.video::after{font-size:8px;top:6px;left:8px;}
.ksk-chip.on{background:var(--ok);border-color:var(--ok);color:#fff;}
.ksk-chip:hover{border-color:var(--accent);}
.ksk-mini{color:var(--mut);font-size:10px;}
.ksk-status{color:var(--mut);font-size:11px;padding:0 8px 7px;background:var(--bg2);min-height:18px;flex:0 0 auto;}
.ksk-status.warn{color:#ffd43b;}
.ksk-status.ok{color:#8ce99a;}
.ksk-status.err{color:#ff8787;}
`;
    const s = document.createElement("style"); s.id = "ksk-matrix-style"; s.textContent = css; document.head.appendChild(s);
}

function makeMediaThumb(name, kind = "image") {
    const wrap = document.createElement("span");
    const videoFile = kind === "video" || isVideoName(name);
    const useVideoTag = videoFile && !/\.gif$/i.test(name || "");
    wrap.className = "ksk-media" + (videoFile ? " video" : "");
    const media = document.createElement(useVideoTag ? "video" : "img");
    media.className = "ksk-thumb";
    media.src = inputThumbUrl(name);
    media.title = name;
    if (useVideoTag) {
        media.muted = true;
        media.loop = true;
        media.playsInline = true;
        media.preload = "metadata";
        media.onmouseenter = () => media.play?.().catch(() => {});
        media.onmouseleave = () => { media.pause?.(); try { media.currentTime = 0; } catch {} };
    } else {
        media.loading = "lazy";
    }
    media.onerror = () => wrap.classList.add("broken");
    const fallback = document.createElement("span");
    fallback.className = "ksk-fallback";
    fallback.textContent = videoFile ? "🎬" : "▧";
    wrap.append(media, fallback);
    return wrap;
}

// ---------- 上传 ----------
async function uploadFiles(fileList) {
    const added = { images: [], videos: [] };
    for (const file of fileList) {
        const fd = new FormData(); fd.append("file", file);
        try {
            const r = await api.fetchApi("/ksk/matrix_pair/upload", { method: "POST", body: fd });
            const d = await r.json();
            if (d.error) { console.error("[KSK] 上传失败:", d.error); continue; }
            (d.kind === "image" ? added.images : added.videos).push(d.name);
        } catch (e) { console.error("[KSK] 上传异常:", e); }
    }
    return added;
}

async function queueCurrentGraph(number = 0) {
    if (typeof app.graphToPrompt !== "function") {
        await app.queuePrompt(number, 1);
        return;
    }

    const promptData = await app.graphToPrompt();
    if (!promptData?.output || !promptData?.workflow) {
        throw new Error("当前画布无法转换为可执行队列数据");
    }

    const promptId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const response = await api.fetchApi("/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            number,
            prompt_id: promptId,
            prompt: promptData.output,
            extra_data: { workflow: promptData.workflow },
        }),
    });

    if (!response.ok) {
        let detail = "";
        try {
            const data = await response.json();
            detail = data?.error?.message || data?.error || JSON.stringify(data);
        } catch {
            detail = await response.text().catch(() => "");
        }
        throw new Error(detail || `HTTP ${response.status}`);
    }

    return await response.json();
}

// ---------- 批量排队 ----------
async function runBatch(node, state, order, hooks = {}) {
    const orderedSelections = state.orderedSelections();
    assertContinuousDisplayOrder(orderedSelections, "批量执行选择");
    const pairs = orderedSelections.map(cell => [cell.imageIndex, cell.videoIndex]);
    if (!pairs.length) { alert("没有选中的组合，请先在网格/树上勾选「图像 × 视频」。"); return; }
    const idxs = pairs.map((_, i) => i);
    const wActive = state.wActive;
    hooks.setBusy?.(true);
    try {
        state.save();
        for (let n = 0; n < idxs.length; n++) {
            const idx = idxs[n];
            if (wActive) wActive.value = idx;
            node.graph?.setDirtyCanvas?.(true, true);
            hooks.setStatus?.(`正在排队 ${n + 1}/${idxs.length} ...`, "warn");
            const result = await queueCurrentGraph(0);
            console.log(`[KSK] queued ${n + 1}/${idxs.length}`, result?.prompt_id);
        }
        hooks.setStatus?.(`已排入 ${idxs.length} 条任务`, "ok");
        console.log(`[KSK] 已排入 ${idxs.length} 条任务，顺序=UI编号`);
    } catch (e) {
        console.error("[KSK] 批量排队失败:", e);
        hooks.setStatus?.(`批量排队失败：${e?.message || e}`, "err");
    } finally {
        hooks.setBusy?.(false);
    }
}

console.log("[KSKNODES] 矩阵配对批量 前端已加载");

// ==========================================
// 构建节点 UI
// ==========================================
function setupMatrixUI(node) {
    if (node.__kskMatrixUIReady) return;
    node.__kskMatrixUIReady = true;
    hideStorageWidgets(node);
    injectStyleOnce();
    const state = new MatrixState(node);
    let view = "matrix"; // matrix | tree

    const root = document.createElement("div");
    root.className = "ksk-mx";

    // ---- 视图切换标签 ----
    const tabs = document.createElement("div");
    tabs.className = "ksk-tabs";
    const tabMatrix = document.createElement("div");
    tabMatrix.className = "ksk-tab active"; tabMatrix.textContent = "▦ 矩阵视图";
    const tabTree = document.createElement("div");
    tabTree.className = "ksk-tab"; tabTree.textContent = "🌳 树状视图";
    tabs.append(tabMatrix, tabTree);

    // ---- 工具栏 ----
    const bar = document.createElement("div");
    bar.className = "ksk-bar";
    const mkBtn = (label, title, cb, cls) => {
        const b = document.createElement("button");
        b.className = "ksk-btn" + (cls ? " " + cls : "");
        b.textContent = label; if (title) b.title = title; b.onclick = cb;
        return b;
    };
    const fileInput = document.createElement("input");
    fileInput.type = "file"; fileInput.multiple = true; fileInput.style.display = "none";
    const status = document.createElement("div");
    status.className = "ksk-status";
    const setStatus = (text = "", tone = "") => {
        status.textContent = text;
        status.className = "ksk-status" + (tone ? " " + tone : "");
    };
    fileInput.onchange = async () => {
        if (!fileInput.files?.length) return;
        const files = [...fileInput.files];
        setBusy(true);
        setStatus(`正在上传 ${files.length} 个素材 ...`, "warn");
        try {
            const added = await uploadFiles(files);
            const imgAdded = uniqAppend(state.images, added.images);
            const vidAdded = uniqAppend(state.videos, added.videos);
            state.save(); render();
            setStatus(`上传完成：图像 +${imgAdded}，视频 +${vidAdded}`, "ok");
        } catch (e) {
            console.error(e);
            setStatus(`上传失败：${e?.message || e}`, "err");
        } finally {
            setBusy(false);
            fileInput.value = "";
        }
    };
    const stat = document.createElement("span");
    stat.className = "ksk-stat";
    const orderSel = document.createElement("select");
    orderSel.className = "ksk-sel"; orderSel.title = "批量执行顺序";
    orderSel.innerHTML = `<option value="increment">按编号执行</option>`;
    const runBtn = mkBtn("🎬 批量执行", "每个勾选组合各排一条独立队列任务", () => runBatch(node, state, orderSel.value, { setStatus, setBusy }), "primary");
    const clearAllBtn = mkBtn("🗑 批量清空", "清空图像、视频和所有勾选组合", () => {
        if (!state.images.length && !state.videos.length && !state.orderedSelections().length) {
            setStatus("当前没有可清空的批量素材", "");
            return;
        }
        if (!confirm("清空本节点中的全部图像、视频和勾选组合？")) return;
        state.clearAll();
        setStatus("已批量清空图像、视频和勾选组合", "ok");
        render();
    });
    const unlinkSizeBtn = mkBtn("🔓 断开尺寸", "断开 custom_width/custom_height 输入，避免从本节点下游回接造成依赖环", () => {
        const count = disconnectSizeInputs(node);
        setStatus(count ? `已断开 ${count} 个尺寸输入` : "没有已连接的尺寸输入", count ? "ok" : "");
        render();
    });
    unlinkSizeBtn.style.display = "none";
    const sizeSourceWidget = node.widgets?.find(w => w.name === "size_source");
    if (sizeSourceWidget && !sizeSourceWidget.__kskMatrixSizeHooked) {
        sizeSourceWidget.__kskMatrixSizeHooked = true;
        const oldCallback = sizeSourceWidget.callback;
        sizeSourceWidget.callback = function (value) {
            const r = oldCallback?.apply(this, arguments);
            const mode = value ?? sizeSourceWidget.value;
            if (mode !== MANUAL_SIZE_SOURCE) {
                const count = disconnectSizeInputs(node);
                if (count) setStatus(`已切到「${mode}」，并断开 ${count} 个尺寸输入，避免依赖环`, "ok");
            }
            render();
            return r;
        };
    }
    function setBusy(on) {
        for (const el of bar.querySelectorAll("button,select")) el.disabled = !!on;
        fileInput.disabled = !!on;
    }

    function updateSizeLinkStatus() {
        const linked = linkedSizeInputs(node);
        const mode = widgetValue(node, "size_source", MANUAL_SIZE_SOURCE);
        if (linked.length && mode !== MANUAL_SIZE_SOURCE) {
            const count = disconnectSizeInputs(node);
            if (count) setStatus(`已切到「${mode}」，并断开 ${count} 个尺寸输入，避免依赖环`, "ok");
            unlinkSizeBtn.style.display = "none";
            return;
        }
        unlinkSizeBtn.style.display = linked.length ? "" : "none";
        if (linked.length && !status.textContent) {
            setStatus("检测到尺寸输入已连接；如果它来自本节点下游会形成依赖环。需要跟参考图时改为「参考图尺寸」。", "warn");
        }
    }

    bar.append(
        mkBtn("➕ 添加素材", "上传图像/视频到 input", () => fileInput.click(), "accent"),
        mkBtn("🔄 刷新", "扫描 input 目录已有素材", refresh),
        mkBtn("✅ 全选", "笛卡尔积：所有图×所有视频", () => { state.selectAll(); render(); }),
        mkBtn("↘ 对角线", "一一对应：图i × 视频i", () => { state.selectDiagonal(); render(); }),
        mkBtn("🔃 反选", "反转当前勾选", () => { state.invert(); render(); }),
        mkBtn("🧹 清空", "清空所有勾选", () => { state.clear(); render(); }),
        clearAllBtn,
        unlinkSizeBtn,
        orderSel,
        runBtn,
        stat, fileInput
    );

    const body = document.createElement("div");
    body.className = "ksk-body";
    root.append(tabs, bar, status, body);

    function bodyInnerSize() {
        const cs = getComputedStyle(body);
        const padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
        const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
        const fallbackWidth = Math.max(260, (Number(node.size?.[0]) || 360) - 40);
        return {
            width: Math.max(0, (body.clientWidth || fallbackWidth) - padX),
            height: Math.max(0, body.clientHeight - padY),
        };
    }

    function fitMatrixGrid(grid = body.querySelector(".ksk-grid")) {
        if (!grid) return;
        const columns = Math.max(1, state.videos.length + 1);
        const rows = Math.max(1, state.images.length + 1);
        const gap = 6;
        const { width, height } = bodyInnerSize();
        const minCell = 52;
        const maxCell = 168;
        const fitWidth = Math.floor((width - gap * (columns - 1)) / columns);
        const fitHeight = height > 120 ? Math.floor((height - gap * (rows - 1)) / rows) : maxCell;
        const target = Math.min(
            Number.isFinite(fitWidth) && fitWidth > 0 ? fitWidth : 84,
            Number.isFinite(fitHeight) && fitHeight > 0 ? fitHeight : maxCell,
        );
        const cell = clamp(target, minCell, maxCell);
        const requiredWidth = cell * columns + gap * (columns - 1);

        grid.style.setProperty("--cell", `${cell}px`);
        grid.style.setProperty("--grid-gap", `${gap}px`);
        grid.style.gridTemplateColumns = `var(--cell) repeat(${state.videos.length}, var(--cell))`;
        grid.style.minWidth = `${requiredWidth}px`;
        grid.style.width = requiredWidth <= width + 1 ? "100%" : `${requiredWidth}px`;
    }

    let matrixFitFrame = 0;
    function scheduleMatrixFit() {
        if (matrixFitFrame) cancelAnimationFrame(matrixFitFrame);
        matrixFitFrame = requestAnimationFrame(() => {
            matrixFitFrame = 0;
            fitMatrixGrid();
        });
    }

    async function refresh() {
        setBusy(true);
        setStatus("正在扫描 input 目录 ...", "warn");
        try {
            const r = await api.fetchApi("/ksk/matrix_pair/list");
            const d = await r.json();
            if (d.error) throw new Error(d.error);
            const imgAdded = uniqAppend(state.images, (d.images || []).filter(isImageName));
            const vidAdded = uniqAppend(state.videos, (d.videos || []).filter(isVideoName));
            state.save(); render();
            setStatus(`刷新完成：图像 +${imgAdded}，视频 +${vidAdded}`, "ok");
        } catch (e) {
            console.error(e);
            setStatus(`刷新失败：${e?.message || e}`, "err");
        } finally {
            setBusy(false);
        }
    }

    tabMatrix.onclick = () => { view = "matrix"; tabMatrix.classList.add("active"); tabTree.classList.remove("active"); render(); };
    tabTree.onclick = () => { view = "tree"; tabTree.classList.add("active"); tabMatrix.classList.remove("active"); render(); };

    function headThumb(name, onClick, onDel, isVid) {
        const wrap = document.createElement("div");
        wrap.className = "ksk-head"; wrap.onclick = onClick;
        const cap = document.createElement("div");
        cap.className = "ksk-cap"; cap.textContent = baseName(name);
        cap.title = name;
        wrap.append(makeMediaThumb(name, isVid ? "video" : "image"), cap);
        wrap.oncontextmenu = (e) => { e.preventDefault(); if (confirm(`移除「${baseName(name)}」？`)) onDel(); };
        return wrap;
    }

    function renderMatrix() {
        const orderMap = state.orderMap();
        const grid = document.createElement("div");
        grid.className = "ksk-grid";
        grid.style.gridTemplateColumns = `var(--cell) repeat(${state.videos.length}, var(--cell))`;
        const corner = document.createElement("div");
        corner.className = "ksk-corner";
        corner.innerHTML = "图&nbsp;＼&nbsp;视";
        grid.appendChild(corner);
        state.videos.forEach((v, j) => {
            const head = document.createElement("div");
            head.className = "ksk-grid-head";
            head.appendChild(headThumb(v, () => { state.toggleCol(j); render(); }, () => state.removeVideo(j) || render(), true));
            grid.appendChild(head);
        });
        state.images.forEach((im, i) => {
            const rowHead = document.createElement("div");
            rowHead.className = "ksk-row-head";
            rowHead.appendChild(headThumb(im, () => { state.toggleRow(i); render(); }, () => state.removeImage(i) || render(), false));
            grid.appendChild(rowHead);
            state.videos.forEach((v, j) => {
                const label = orderMap.get(keyOf(i, j));
                const cellBox = document.createElement("div");
                cellBox.className = "ksk-cellbox";
                const cell = document.createElement("div");
                cell.className = "ksk-cell" + (label ? " on" : "");
                if (label) cell.setAttribute("data-ord", label);
                cell.title = `图像[${i}] × 视频[${j}]`;
                cell.onclick = () => { state.toggle(i, j); render(); };
                cellBox.appendChild(cell);
                grid.appendChild(cellBox);
            });
        });
        body.appendChild(grid);
        fitMatrixGrid(grid);
        scheduleMatrixFit();
    }

    function renderTree() {
        const orderMap = state.orderMap();
        const tree = document.createElement("div");
        tree.className = "ksk-tree";
        state.images.forEach((im, i) => {
            const cnt = state.videos.filter((_, j) => state.isSel(i, j)).length;
            const tnode = document.createElement("div");
            tnode.className = "ksk-tnode";
            const parent = document.createElement("div");
            parent.className = "ksk-tparent";
            const pname = document.createElement("div"); pname.className = "name"; pname.textContent = `🖼 ${baseName(im)}`;
            const pcnt = document.createElement("div"); pcnt.className = "cnt"; pcnt.textContent = `${cnt}/${state.videos.length} 个视频`;
            parent.append(makeMediaThumb(im, "image"), pname, pcnt);
            parent.title = "点击：切换该图下全部视频";
            parent.onclick = () => { state.toggleRow(i); render(); };
            const children = document.createElement("div");
            children.className = "ksk-tchildren";
            if (!state.videos.length) {
                const m = document.createElement("span"); m.className = "ksk-mini"; m.textContent = "（无视频，请先添加素材）";
                children.appendChild(m);
            }
            state.videos.forEach((v, j) => {
                const labelOrder = orderMap.get(keyOf(i, j));
                const chip = document.createElement("div");
                chip.className = "ksk-chip" + (labelOrder ? " on" : "");
                const label = document.createElement("span");
                label.textContent = (labelOrder ? `#${labelOrder} ` : "") + baseName(v);
                chip.append(makeMediaThumb(v, "video"), label);
                chip.onclick = (e) => { e.stopPropagation(); state.toggle(i, j); render(); };
                children.appendChild(chip);
            });
            tnode.append(parent, children);
            tree.appendChild(tnode);
        });
        body.appendChild(tree);
    }

    function render() {
        const orderedSelections = state.orderedSelections();
        assertContinuousDisplayOrder(orderedSelections, "UI 选择");
        stat.innerHTML = `图像 <b>${state.images.length}</b> · 视频 <b>${state.videos.length}</b> · 已选 <b>${orderedSelections.length}</b>`;
        updateSizeLinkStatus();
        body.innerHTML = "";
        if (!state.images.length || !state.videos.length) {
            const e = document.createElement("div");
            e.className = "ksk-empty";
            e.innerHTML = "请先「➕ 添加素材」或「🔄 刷新」<br>至少需要 1 张图像 和 1 个视频";
            body.appendChild(e); return;
        }
        view === "matrix" ? renderMatrix() : renderTree();
    }

    const minNodeWidth = 360;
    const minNodeHeight = 620;
    const minWidgetHeight = 260;
    const defaultWidgetHeight = 420;
    const maxWidgetHeight = 1400;
    let applyingSize = false;
    node.min_size = [
        Math.max(node.min_size?.[0] || 0, minNodeWidth),
        Math.max(node.min_size?.[1] || 0, minNodeHeight),
    ];
    node.resizable = true;
    const setNodeSize = (width, height) => {
        if (applyingSize) return;
        applyingSize = true;
        const next = [Math.max(minNodeWidth, Math.floor(width || minNodeWidth)), Math.max(minNodeHeight, Math.floor(height || minNodeHeight))];
        try {
            if (node.setSize) node.setSize(next);
            else node.size = next;
            node.graph?.setDirtyCanvas?.(true, true);
        } finally {
            applyingSize = false;
        }
    };
    const enforceNodeSize = (size = node.size) => {
        const width = Number(size?.[0]) || 0;
        const height = Number(size?.[1]) || 0;
        if (width >= minNodeWidth && height >= minNodeHeight) return false;
        setNodeSize(Math.max(width, minNodeWidth), Math.max(height, minNodeHeight));
        return true;
    };
    node.properties ??= {};
    if (!Number.isFinite(node.properties.ksk_matrix_widget_height)) {
        node.properties.ksk_matrix_widget_height = defaultWidgetHeight;
    }
    const clampWidgetHeight = (height) => Math.max(minWidgetHeight, Math.min(maxWidgetHeight, Math.floor(height || defaultWidgetHeight)));
    const widgetHeight = () => clampWidgetHeight(node.properties.ksk_matrix_widget_height);
    let domWidget = null;
    const updateDOMSize = () => {
        const h = widgetHeight();
        root.style.height = `${h}px`;
        root.style.minHeight = `${minWidgetHeight}px`;
        root.style.width = "100%";
        root.style.maxWidth = "100%";
        if (domWidget) {
            domWidget.computeSize = (width) => {
                const nodeWidth = Number(node.size?.[0]);
                const fullWidth = Number.isFinite(nodeWidth) && nodeWidth > 0 ? nodeWidth - 24 : (Number(width) || minNodeWidth);
                return [Math.max(260, Math.floor(fullWidth)), h];
            };
        }
        scheduleMatrixFit();
    };
    const syncHeightFromNode = (size = node.size) => {
        const top = Number(domWidget?.last_y);
        const nodeHeight = Number(size?.[1]);
        if (!Number.isFinite(top) || top < 80 || !Number.isFinite(nodeHeight)) return false;
        const next = clampWidgetHeight(nodeHeight - top - 10);
        if (Math.abs(next - widgetHeight()) > 2) {
            node.properties.ksk_matrix_widget_height = next;
            return true;
        }
        return false;
    };
    domWidget = node.addDOMWidget("ksk_matrix_ui", "matrix", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: widgetHeight,
        getMaxHeight: widgetHeight,
        getHeight: widgetHeight,
    });
    const oldOnResize = node.onResize;
    node.onResize = function (size) {
        const r = oldOnResize?.apply(this, arguments);
        if (enforceNodeSize(size)) return r;
        syncHeightFromNode(size);
        updateDOMSize();
        scheduleMatrixFit();
        return r;
    };
    const currentHeight = Number(node.size?.[1]) || 0;
    const repairedHeight = currentHeight > 1800 ? 760 : Math.max(currentHeight, minNodeHeight);
    setNodeSize(Math.max(node.size?.[0] || 0, minNodeWidth), repairedHeight);
    updateDOMSize();
    render();
    let syncAttempts = 0;
    const syncAfterLayout = () => {
        enforceNodeSize(node.size);
        const changed = syncHeightFromNode(node.size);
        updateDOMSize();
        if (changed) node.graph?.setDirtyCanvas?.(true, true);
        state.load();
        render();
        scheduleMatrixFit();
        if (++syncAttempts < 8) setTimeout(syncAfterLayout, 120);
    };
    setTimeout(syncAfterLayout, 80);
    if (typeof ResizeObserver !== "undefined") {
        const resizeObserver = new ResizeObserver(scheduleMatrixFit);
        resizeObserver.observe(root);
        resizeObserver.observe(body);
        node.__kskMatrixResizeObserver = resizeObserver;
    }
    window.addEventListener("resize", scheduleMatrixFit, { passive: true });
}

// ==========================================
// 注册扩展
// ==========================================
function hideStorageWidget(node, name) {
    const w = node.widgets?.find(x => x.name === name);
    if (!w) return;
    if (!w.__kskOrigComputeSize) w.__kskOrigComputeSize = w.computeSize;
    if (!w.__kskOrigSerializeValue) w.__kskOrigSerializeValue = w.serializeValue;
    if (!w.__kskOrigType) w.__kskOrigType = w.type;
    w.hidden = true;
    w.type = "ksk-hidden";
    w.computeSize = () => [0, -4];
    w.draw = () => {};
    w.serializeValue = () => w.value;
    for (const key of ["element", "inputEl", "textElement"]) {
        const el = w[key];
        if (el?.style) el.style.display = "none";
    }
    if (w.linkedWidgets) {
        for (const linked of w.linkedWidgets) {
            linked.hidden = true;
            linked.computeSize = () => [0, -4];
            linked.draw = () => {};
            if (linked.element?.style) linked.element.style.display = "none";
        }
    }
}

function hideStorageWidgets(node) {
    for (const name of STORAGE_WIDGETS) hideStorageWidget(node, name);
}

app.registerExtension({
    name: "KSKNODES.MatrixPairBatch",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) return;
        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            hideStorageWidgets(this);
            setupMatrixUI(this);
            return r;
        };
    },
});
