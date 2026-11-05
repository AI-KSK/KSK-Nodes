/**
 * AIKSK Pose x Costume Matrix - canvas-native node UI factory.
 *
 * Dependency-injected (node / app / api) so the ComfyUI imports stay confined
 * to the entry module.  Every file in the extension web directory is loaded by
 * ComfyUI on its own, therefore this module has NO top-level side effects.
 */

import * as core from "./aiksk_pc_core.js";
import {
    HIDDEN_WIDGET_TYPE,
    btn,
    checkbox,
    createModal,
    el,
    ensureStyles,
    field,
    numberInput,
    pill,
    progressBar,
    selectBox,
    textArea,
    textInput,
    thumbnail,
} from "./aiksk_pc_style.js";

export const NODE_TYPE = core.NODE_TYPE;
export const NODE_DISPLAY_NAME = core.NODE_DISPLAY_NAME;
export const UI_WIDGET_NAME = "aiksk_pc_panel";
export const UI_KEY = "__aikskPcUI";

const PANEL_MIN_HEIGHT = 320;
const PANEL_MAX_HEIGHT = 2400;
const NODE_MIN_WIDTH = 800;
const NODE_DEFAULT_WIDTH = 800;
const NODE_MIN_HEIGHT = 360;
const NODE_DEFAULT_HEIGHT = 620;
const NODE_MIGRATE_MAX_HEIGHT = 700;
const GALLERY_PAGE_SIZE = 120;
const SELECTED_PREVIEW_LIMIT = 40;
const JOB_POLL_MS = 1500;
const MAX_GALLERY_ITEMS = 20000;

/** Native widgets mirrored by the DOM panel and therefore hidden on canvas. */
const HIDDEN_NATIVE_WIDGETS = Object.freeze([
    "pose_ids_json",
    "costume_ids_json",
    "selection_json",
    "active_index",
    "pair_mode",
    "model_qwen",
    "model_flux",
    "samples_per_pair",
    "seed",
    "control_after_generate",
    "prompt_template",
    "width",
    "height",
    "resize_mode",
    "external_prompt",
    "seed_override",
]);

const GALLERY_UPSTREAM = Object.freeze({
    pose: { base: "/pose_gallery", label: "姿势库", emoji: "📚" },
    costume: { base: "/costume_gallery", label: "服装库", emoji: "👗" },
});

const STATUS_CLASS = { "": "", ok: "aiksk-pc-status--ok", warn: "aiksk-pc-status--warn", error: "aiksk-pc-status--error" };

function clamp(value, min, max) {
    return Math.min(max, Math.max(min, Number(value)));
}

function errorMessage(error) {
    if (!error) return "未知错误";
    const data = error.data || {};
    const detail = data.error && typeof data.error === "object" ? data.error : null;
    const code = error.code || data.code || detail?.code || "";
    const message = error.message || data.message || detail?.message || data.error || String(error);
    return code ? `[${code}] ${message}` : String(message);
}

function installElement(parent, child) {
    if (child) parent.append(child);
    return child;
}

function makeReadOnlyRow(...children) {
    return el("div", "aiksk-pc-row", children);
}

/**
 * Build the UI for one node instance.
 * @returns {AikskPcNodeUI}
 */
function ensureGraphNodeHook(app) {
    const graph = app?.graph;
    if (!graph || graph.__aikskPcNodeHook) return;
    graph.__aikskPcNodeHook = true;
    const previous = graph.onNodeAdded;
    graph.onNodeAdded = function (added) {
        try {
            previous?.call(this, added);
        } catch (error) {
            console.warn("[AIKSK-PC] previous onNodeAdded failed", error);
        }
        try {
            if (added?.type !== NODE_TYPE) return;
            const existing = added.__aikskPcUI;
            if (existing && !existing.disposed && existing.node === added) return;
            createNodeUI({ node: added, app, api: app?.api });
        } catch (error) {
            console.error("[AIKSK-PC] graph onNodeAdded UI failed", error);
        }
    };
    // 兜底扫描：复制/粘贴/反序列化在不同前端版本触发的 hook 不一致，确保每个本节点都有独立 UI。
    if (!graph.__aikskPcScanTimer && typeof setInterval === "function") {
        graph.__aikskPcScanTimer = setInterval(() => {
            try {
                for (const node of graph._nodes || []) {
                    if (node?.type !== NODE_TYPE) continue;
                    const ui = node.__aikskPcUI;
                    if (!ui || ui.disposed || ui.node !== node) {
                        createNodeUI({ node, app, api: app?.api });
                    }
                }
                const nodes = graph._nodes || [];
                for (const stale of Array.from(document.querySelectorAll('[data-testid="aiksk-pc-root"]'))) {
                    const owner = stale.__aikskNode;
                    if (owner && !nodes.includes(owner)) stale.remove();
                }
            } catch (error) {
                console.error("[AIKSK-PC] UI scan failed", error);
            }
        }, 600);
    }
}

export function createNodeUI({ node, app, api }) {
    const existing = node?.[UI_KEY];
    if (existing && typeof existing.syncFromWidgets === "function" && !existing.disposed) {
        existing.syncFromWidgets("recreate");
        return existing;
    }
    ensureGraphNodeHook(app);
    const ui = new AikskPcNodeUI({ node, app, api });
    node[UI_KEY] = ui;
    return ui;
}

export class AikskPcNodeUI {
    constructor({ node, app, api }) {
        if (!node) throw new Error("createNodeUI 需要 ComfyUI 节点实例");
        this.node = node;
        this.app = app;
        this.api = api;
        this.disposed = false;
        this.dom = {};
        this.state = null;
        this.manualPairs = [];
        this.planResult = null;
        this.planPreview = null;
        this.bindings = { templates: {}, bindings: [], errors: [], warnings: [], valid: false };
        this.canvasTemplate = null;
        this.graphDetection = { qwen: false, flux: false, model: "", dual: false };
        this.bindingsRefreshing = false;
        this.bindingsDirty = false;
        this.graphModelTouched = false;
        this.configured = false;
        this.job = { id: "", summary: null, submit: null };
        this.pollTimer = null;
        this.persistTimer = null;
        this.gallery = null;
        this.matrix = null;
        this.itemCache = { pose: new Map(), costume: new Map() };
        this.catalog = {
            pose: { items: [], map: new Map(), loaded: false, loading: false, error: "", promise: null, count: 0 },
            costume: { items: [], map: new Map(), loaded: false, loading: false, error: "", promise: null, count: 0 },
        };

        ensureStyles();
        this.ensureNativeWidgets();
        this.hideNativeWidgets();
        this.loadStateFromWidgets();
        this.buildDom();
        this.mountPanel();
        this.attachWidgetHooks();
        this.renderAll();
        this.onGraphChanged();
        // Capture the initial canvas binding without blocking node creation.
        queueMicrotask(() => this.refreshBindings({ reason: "init" }));
        queueMicrotask(() => this.refreshModelStatus().catch(() => {}));
    }

    /* ------------------------------------------------------------------ */
    /* Native widgets                                                      */
    /* ------------------------------------------------------------------ */

    getWidget(name) {
        return (this.node.widgets || []).find((widget) => widget?.name === name) || null;
    }

    readWidget(name, fallback = undefined) {
        const widget = this.getWidget(name);
        return widget ? widget.value : fallback;
    }

    writeWidget(name, value, { silent = true } = {}) {
        const widget = this.getWidget(name);
        if (!widget) return false;
        if (widget.value === value) return false;
        widget.value = value;
        if (!silent && typeof widget.callback === "function") {
            try {
                widget.callback(value);
            } catch (error) {
                console.warn("[AIKSK-PC] widget callback failed", name, error);
            }
        }
        return true;
    }

    /** Mirror widgets that the DOM panel renders itself are hidden on canvas. */
    hideNativeWidgets() {
        for (const name of HIDDEN_NATIVE_WIDGETS) {
            const widget = this.getWidget(name);
            if (!widget) continue;
            widget.computeSize = () => [0, -4];
            widget.type = HIDDEN_WIDGET_TYPE;
            widget.hidden = true;
            widget.options = { ...(widget.options || {}), serialize: true, hidden: true };
            if (typeof widget.draw === "function") widget.draw = () => {};
        }
    }

    /**
     * Defensive fallback: should the backend widget list ever drift from the
     * contract, create the missing STRING widgets so the UI keeps working and
     * still persists its state with the workflow.
     */
    ensureNativeWidgets() {
        const defaults = {
            pose_ids_json: "[]",
            costume_ids_json: "[]",
            selection_json: "{}",
        };
        if (typeof this.node.addWidget !== "function") return;
        const order = ["pose_ids_json", "costume_ids_json", "selection_json"];
        for (const name of order) {
            if (this.getWidget(name)) continue;
            const created = this.node.addWidget("text", name, defaults[name], () => {});
            if (created) {
                created.serialize = true;
                console.warn(`[AIKSK-PC] node is missing the "${name}" widget; created a fallback widget.`);
            }
        }
    }

    attachWidgetHooks() {
        // Prompt textarea is mirrored inside the DOM panel; keep the canvas
        // widget in sync if another extension edits it.
        const promptWidget = this.getWidget("prompt_template");
        if (promptWidget) {
            const previous = promptWidget.callback;
            promptWidget.callback = (...args) => {
                const result = previous?.apply(promptWidget, args);
                if (this.dom.prompt && document.activeElement !== this.dom.prompt) {
                    this.dom.prompt.value = String(promptWidget.value ?? "");
                }
                return result;
            };
        }
    }

    /* ------------------------------------------------------------------ */
    /* State                                                               */
    /* ------------------------------------------------------------------ */

    defaultConfigFromWidgets() {
        const qwen = Boolean(this.readWidget("model_qwen", true));
        const flux = Boolean(this.readWidget("model_flux", true));
        return {
            mode: core.normalizePairMode(this.readWidget("pair_mode", "cartesian")),
            models: core.selectedModelKeys({ model_qwen: qwen, model_flux: flux }),
            samples: core.clampInt(this.readWidget("samples_per_pair", 1), 1, core.MAX_SAMPLES_PER_PAIR),
            seed: core.toU32(this.readWidget("seed", core.DEFAULT_SEED)),
            prompt: String(this.readWidget("prompt_template", "") || core.DEFAULT_PROMPT),
            dualCompare: false,
            graphModel: "qwen21",
            externalTemplates: {},
            width: core.clampInt(this.readWidget("width", 768), 64, 4096),
            height: core.clampInt(this.readWidget("height", 1152), 64, 4096),
            resizeMode: String(this.readWidget("resize_mode", "等比留边")),
            activeIndex: core.clampInt(this.readWidget("active_index", 0), 0, 1000000),
            seedPolicy: "fixed",
            externalPrompt: String(this.readWidget("external_prompt", "") || ""),
            seedOverride: core.clampInt(this.readWidget("seed_override", -1), -1, core.U32 - 1, -1),
        };
    }

    loadStateFromWidgets() {
        const persisted = core.readSelectionState({
            pose_ids_json: this.readWidget("pose_ids_json", "[]"),
            costume_ids_json: this.readWidget("costume_ids_json", "[]"),
            selection_json: this.readWidget("selection_json", "{}"),
        });
        const defaults = this.defaultConfigFromWidgets();
        const hasPlan = Boolean(persisted.plan && persisted.plan.pairs);
        const state = {
            poses: persisted.poses,
            costumes: persisted.costumes,
            plan: persisted.plan,
            mode: hasPlan ? persisted.mode : defaults.mode,
            models: hasPlan ? persisted.models : defaults.models,
            samples: hasPlan ? persisted.samples : defaults.samples,
            seed: hasPlan ? persisted.seed : defaults.seed,
            prompt: hasPlan && persisted.prompt ? persisted.prompt : defaults.prompt,
            dualCompare: hasPlan ? persisted.dualCompare : defaults.dualCompare,
            graphModel: hasPlan ? persisted.graphModel : defaults.graphModel,
            externalTemplates: persisted.externalTemplates || {},
            seedPolicy: hasPlan ? persisted.seedPolicy : defaults.seedPolicy,
            width: defaults.width,
            height: defaults.height,
            resizeMode: defaults.resizeMode,
            activeIndex: defaults.activeIndex,
            externalPrompt: defaults.externalPrompt,
            seedOverride: defaults.seedOverride,
            errors: persisted.errors,
            warnings: persisted.warnings,
        };
        this.state = state;
        this.loadErrors = persisted.errors;
        this.loadWarnings = persisted.warnings;
        this.manualPairs = [];
        if (state.mode === "manual") {
            this.manualPairs = (persisted.manualPairs || []).slice();
        }
        this.graphModelTouched = hasPlan && Boolean(persisted.plan?.graph_model);
    }

    /** Persist the UI state into the three STRING widgets + mirrored natives. */
    persist({ silent = false } = {}) {
        if (!this.state) return null;
        const result = core.persistState(this.state);
        const { values } = result;
        this.writeWidget("pose_ids_json", values.pose_ids_json);
        this.writeWidget("costume_ids_json", values.costume_ids_json);
        this.writeWidget("selection_json", values.selection_json);
        this.writeWidget("pair_mode", this.state.mode);
        this.writeWidget("model_qwen", this.state.models.includes("qwen21"));
        this.writeWidget("model_flux", this.state.models.includes("flux3klein"));
        this.writeWidget("samples_per_pair", this.state.samples);
        this.writeWidget("seed", this.state.seed);
        this.writeWidget("prompt_template", this.state.prompt);
        this.writeWidget("width", this.state.width);
        this.writeWidget("height", this.state.height);
        this.writeWidget("resize_mode", this.state.resizeMode);
        this.writeWidget("active_index", this.state.activeIndex);
        this.writeWidget("external_prompt", this.state.externalPrompt || "");
        this.writeWidget("seed_override", this.state.seedOverride);
        this.planResult = result;
        this.markDirty();
        if (!silent) this.renderTaskCount();
        return result;
    }

    schedulePersist() {
        clearTimeout(this.persistTimer);
        this.persistTimer = setTimeout(() => {
            this.persist({ silent: true });
            this.renderTaskCount();
            this.renderSelected();
        }, 120);
    }

    markDirty() {
        try {
            this.node.graph?.setDirtyCanvas?.(true, true);
            this.app?.graph?.setDirtyCanvas?.(true, true);
        } catch {
            /* canvas may be disposed */
        }
    }

    syncFromWidgets(reason = "sync") {
        if (this.disposed) return;
        this.ensureNativeWidgets();
        this.hideNativeWidgets();
        this.loadStateFromWidgets();
        this.configured = reason === "configure";
        this.renderAll();
        this.onGraphChanged();
        this.refreshBindings({ reason }).catch(() => {});
        this.enforceNodeSize(reason);

    }

    /* ------------------------------------------------------------------ */
    /* DOM                                                                 */
    /* ------------------------------------------------------------------ */

    buildDom() {
        const dom = this.dom;
        // 清理已从 graph 移除的旧节点遗留 DOM（审计 R6 布局/重载要求）。
        try {
            const nodes = this.app?.graph?._nodes || [];
            for (const stale of Array.from(document.querySelectorAll('[data-testid="aiksk-pc-root"]'))) {
                const owner = stale.__aikskNode;
                if (owner && !nodes.includes(owner)) stale.remove();
            }
        } catch (error) {
            /* 清理失败不影响挂载 */
        }
        const root = el("div", { class: "aiksk-pc-panel", testid: "aiksk-pc-root" });
        root.__aikskNode = this.node;
        dom.root = root;

        /* header */
        dom.personHint = el("span", { class: "aiksk-pc-hint", testid: "aiksk-pc-person-hint" });
        dom.bindingSummary = el("span", { class: "aiksk-pc-hint", testid: "aiksk-pc-binding" });
        dom.head = el("div", "aiksk-pc-head", [
            el("div", "aiksk-pc-title", [
                "AIKSK 姿势 × 服装矩阵",
                el("small", "", "画布原生批量"),
            ]),
            el("div", "aiksk-pc-head-right", [dom.bindingSummary, dom.personHint]),
        ]);

        /* gallery buttons */
        dom.poseCount = el("span", { class: "aiksk-pc-count-badge", testid: "aiksk-pc-pose-count", text: "0" });
        dom.costumeCount = el("span", { class: "aiksk-pc-count-badge", testid: "aiksk-pc-costume-count", text: "0" });
        dom.poseBtn = btn(["📚 姿势库", dom.poseCount], {
            cls: "aiksk-pc-lib", testid: "aiksk-pc-pose-btn",
            title: "打开页面内姿势库弹窗（多选累积）",
            onClick: () => this.openGallery("pose"),
        });
        dom.costumeBtn = btn(["👗 服装库", dom.costumeCount], {
            cls: "aiksk-pc-lib aiksk-pc-lib--costume", testid: "aiksk-pc-costume-btn",
            title: "打开页面内服装库弹窗（多选累积）",
            onClick: () => this.openGallery("costume"),
        });
        dom.libs = el("div", "aiksk-pc-libs", [dom.poseBtn, dom.costumeBtn]);

        /* selected chips */
        dom.poseSelected = el("div", {
            class: "aiksk-pc-selected-list", testid: "aiksk-pc-pose-selected",
            dataset: { empty: "未选姿势 · 点上方「📚 姿势库」多选" },
        });
        dom.costumeSelected = el("div", {
            class: "aiksk-pc-selected-list", testid: "aiksk-pc-costume-selected",
            dataset: { empty: "未选服装 · 点上方「👗 服装库」多选" },
        });
        dom.selectedBlock = el("div", "aiksk-pc-selected-block", [
            el("div", "aiksk-pc-selected-head", [
                el("span", "", "已选姿势"),
                el("span", { class: "aiksk-pc-hint", testid: "aiksk-pc-pose-selected-count" }),
            ]),
            dom.poseSelected,
            el("div", "aiksk-pc-selected-head", [
                el("span", "", "已选服装"),
                el("span", { class: "aiksk-pc-hint", testid: "aiksk-pc-costume-selected-count" }),
            ]),
            dom.costumeSelected,
        ]);

        /* combination controls */
        dom.mode = selectBox(
            core.PAIR_MODES.map((mode) => ({ value: mode, label: core.PAIR_MODE_LABELS[mode] })),
            { testid: "aiksk-pc-mode", ariaLabel: "组合方式", onChange: (value) => this.onModeChange(value) },
        );
        dom.matrixBtn = btn("矩阵 / 树视图", {
            cls: "aiksk-pc-btn--small", testid: "aiksk-pc-matrix-open",
            title: "手动矩阵勾选（点击格子切换，行列头全选）",
            onClick: () => this.openMatrixModal(),
        });
        dom.modeRow = el("div", "aiksk-pc-row", [
            field("组合方式", dom.mode),
            dom.matrixBtn,
            el("span", "aiksk-pc-spacer"),
            dom.personHintClone = el("span", { class: "aiksk-pc-hint" }),
        ]);

        /* models / samples / seed */
        dom.qwen = checkbox("Qwen-Image 2.1", {
            cls: "aiksk-pc-check--model",
            testid: "aiksk-pc-model-qwen",
            title: "勾选后为每对姿势/服装生成 Qwen-Image 2.1 任务",
            onChange: () => this.onModelsChange(),
        });
        dom.flux = checkbox("FLUX.3 Klein", {
            cls: "aiksk-pc-check--model",
            testid: "aiksk-pc-model-flux",
            title: "勾选后为每对姿势/服装生成 FLUX.3 Klein 任务",
            onChange: () => this.onModelsChange(),
        });
        dom.samples = numberInput({
            value: 1, min: 1, max: core.MAX_SAMPLES_PER_PAIR, step: 1,
            testid: "aiksk-pc-samples", title: "每组每模型生成张数（每张拆成 1 条独立队列任务）",
            onChange: (value) => this.onSamplesChange(value),
        });
        dom.seed = numberInput({
            value: core.DEFAULT_SEED, min: 0, max: core.U32 - 1, step: 1,
            testid: "aiksk-pc-seed", title: "基础 Seed",
            onChange: (value) => this.onSeedChange(value),
        });
        dom.seedPolicy = selectBox(
            core.SEED_POLICIES.map((policy) => ({ value: policy, label: core.SEED_POLICY_LABELS[policy] })),
            { testid: "aiksk-pc-seed-policy", ariaLabel: "Seed 策略", onChange: (value) => { this.state.seedPolicy = value; this.persist(); } },
        );
        dom.modelRow = el("div", "aiksk-pc-row", [
            el("span", "aiksk-pc-label", "模型"),
            dom.qwen,
            dom.flux,
            el("span", "aiksk-pc-spacer"),
            field("每组张数", dom.samples),
        ]);
        dom.seedRow = el("div", "aiksk-pc-row", [
            el("span", "aiksk-pc-label", "Seed"),
            dom.seed,
            field("Seed 策略", dom.seedPolicy),
            el("span", "aiksk-pc-spacer"),
        ]);
        dom.modelStatus = el("div", { class: "aiksk-pc-model-status", testid: "aiksk-pc-model-status" });
        dom.modelBlock = el("div", "aiksk-pc-section", [dom.modelRow, dom.modelStatus]);

        /* dual compare + graph model binding */
        dom.dual = checkbox("双模型对比模式（当前画布双分支，1 任务出 2 图）", {
            testid: "aiksk-pc-dual",
            title: "勾选后一个任务同时跑 Qwen 与 FLUX 两条分支（使用 C 工作流）",
            onChange: () => this.onDualChange(),
        });
        dom.graphModel = selectBox(
            core.MODEL_KEYS.map((key) => ({ value: key, label: core.MODEL_LABELS[key] })),
            { testid: "aiksk-pc-graph-model", ariaLabel: "当前画布模型", onChange: (value) => this.onGraphModelChange(value) },
        );
        dom.dualRow = el("div", "aiksk-pc-row", [dom.dual, el("span", "aiksk-pc-spacer"), field("当前画布模型", dom.graphModel)]);

        /* binding status rows */
        dom.bindQwen = el("div", { class: "aiksk-pc-binding", testid: "aiksk-pc-bind-qwen" });
        dom.bindFlux = el("div", { class: "aiksk-pc-binding", testid: "aiksk-pc-bind-flux" });
        dom.importQwen = btn("导入 API 工作流", {
            cls: "aiksk-pc-btn--small", testid: "aiksk-pc-import-qwen",
            onClick: () => this.importTemplateFromFile("qwen21"),
        });
        dom.importFlux = btn("导入 API 工作流", {
            cls: "aiksk-pc-btn--small", testid: "aiksk-pc-import-flux",
            onClick: () => this.importTemplateFromFile("flux3klein"),
        });
        dom.bindingRow = el("div", "aiksk-pc-bindings", [
            el("div", "aiksk-pc-binding", [dom.bindQwen, el("span", "aiksk-pc-spacer"), dom.importQwen]),
            el("div", "aiksk-pc-binding", [dom.bindFlux, el("span", "aiksk-pc-spacer"), dom.importFlux]),
        ]);
        dom.bindingHint = el("div", { class: "aiksk-pc-hint", testid: "aiksk-pc-binding-hint" });

        /* prompt */
        dom.prompt = textArea({
            value: "", rows: 2, testid: "aiksk-pc-prompt",
            placeholder: "统一提示词，支持 {pose} / {costume} 占位",
            onChange: (value) => this.onPromptChange(value),
        });
        dom.promptLabel = el("div", "aiksk-pc-label", "统一提示词（{pose} 姿势 / {costume} 服装）");
        dom.promptBlock = el("div", "aiksk-pc-section", [dom.promptLabel, dom.prompt]);

        /* advanced size / active index */
        dom.width = numberInput({
            value: 768, min: 64, max: 4096, step: 8, cls: "aiksk-pc-input--tiny", testid: "aiksk-pc-width",
            title: "输出参考图宽", onChange: (value) => { this.state.width = core.clampInt(value, 64, 4096, 768); this.persist(); this.renderTaskCount(); },
        });
        dom.height = numberInput({
            value: 1152, min: 64, max: 4096, step: 8, cls: "aiksk-pc-input--tiny", testid: "aiksk-pc-height",
            title: "输出参考图高", onChange: (value) => { this.state.height = core.clampInt(value, 64, 4096, 1152); this.persist(); this.renderTaskCount(); },
        });
        dom.resize = selectBox(core.RESIZE_MODES.map((mode) => ({ value: mode, label: mode })), {
            testid: "aiksk-pc-resize", ariaLabel: "参考图处理方式",
            onChange: (value) => { this.state.resizeMode = value; this.persist(); },
        });
        dom.activeIndex = numberInput({
            value: 0, min: 0, max: 1000000, step: 1, cls: "aiksk-pc-input--tiny", testid: "aiksk-pc-active-index",
            title: "当前输出任务下标（0 基）", onChange: (value) => this.onActiveIndexChange(value),
        });
        dom.externalPrompt = textInput({
            value: "", placeholder: "留空使用上方统一提示词", testid: "aiksk-pc-external-prompt",
            onChange: (value) => { this.state.externalPrompt = String(value ?? ""); this.persist(); },
        });
        dom.seedOverride = numberInput({
            value: -1, min: -1, max: core.U32 - 1, step: 1, cls: "aiksk-pc-input--tiny", testid: "aiksk-pc-seed-override",
            title: ">=0 时覆盖任务 seed（-1 表示不覆盖）",
            onChange: (value) => {
                this.state.seedOverride = core.clampInt(value, -1, core.U32 - 1, -1);
                this.dom.seedOverride.value = String(this.state.seedOverride);
                this.persist();
            },
        });
        dom.advancedRow = el("div", "aiksk-pc-row", [
            el("span", "aiksk-pc-label", "参考图"),
            dom.width, el("span", "aiksk-pc-label", "×"), dom.height,
            dom.resize,
            el("span", "aiksk-pc-spacer"),
            field("输出任务下标", dom.activeIndex),
        ]);
        dom.optionalRow = el("div", "aiksk-pc-row", [
            field("外部提示词（可选覆盖）", dom.externalPrompt),
            field("Seed 覆盖", dom.seedOverride),
        ]);

        /* task count */
        dom.taskCount = el("span", { class: "aiksk-pc-count", testid: "aiksk-pc-task-count", text: "0" });
        dom.taskDetail = el("span", { class: "aiksk-pc-count-detail", testid: "aiksk-pc-task-detail" });
        dom.queueSplit = el("div", {
            class: "aiksk-pc-hint", testid: "aiksk-pc-queue-split",
            text: "每张图 = 1 条独立 ComfyUI 队列任务（按 in-flight 逐条排队，失败只影响单张）",
        });
        dom.summary = el("div", "aiksk-pc-summary", [
            el("div", "", [dom.taskCount, el("span", "aiksk-pc-count-unit", "条独立任务")]),
            dom.taskDetail,
        ]);

        /* actions */
        dom.dryrun = btn("预演", {
            cls: "aiksk-pc-btn--small", testid: "aiksk-pc-dryrun",
            title: "调用 /aiksk_pc/plan 只做预演，不会提交 GPU 任务",
            onClick: () => this.dryRun(),
        });
        dom.submit = btn("确认批量执行", {
            cls: "aiksk-pc-btn--primary", testid: "aiksk-pc-submit",
            title: "调用 /aiksk_pc/submit 提交批量任务",
            onClick: () => this.submit(),
        });
        dom.pause = btn("暂停", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-pause", onClick: () => this.jobAction("pause") });
        dom.resume = btn("恢复", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-resume", onClick: () => this.jobAction("resume") });
        dom.cancel = btn("取消", { cls: "aiksk-pc-btn--small aiksk-pc-btn--danger", testid: "aiksk-pc-cancel", onClick: () => this.jobAction("cancel") });
        dom.retry = btn("重试失败", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-retry", onClick: () => this.jobAction("retry_failed") });
        dom.actions = el("div", "aiksk-pc-actions", [
            dom.dryrun, dom.submit,
            el("span", "aiksk-pc-sep"),
            dom.pause, dom.resume, dom.cancel, dom.retry,
        ]);

        /* status + job */
        dom.status = el("div", { class: "aiksk-pc-status", testid: "aiksk-pc-status", text: "就绪：选择姿势与服装后先「预演」，确认无误再批量执行。" });
        dom.jobHead = el("div", { class: "aiksk-pc-job-head", testid: "aiksk-pc-job-head" });
        dom.progress = progressBar({ value: 0, testid: "aiksk-pc-progress" });
        dom.log = el("div", { class: "aiksk-pc-log", testid: "aiksk-pc-log" });
        dom.bottom = el("div", "aiksk-pc-section", [dom.jobHead, dom.progress, dom.status, dom.log]);

        const advanced = el("details", "aiksk-pc-advanced", [
            el("summary", "", "高级设置：提示词 / Seed / 分辨率 / API 模板 / 任务日志"),
            el("div", "aiksk-pc-advanced-body", [
                el("button", {
                    class: "aiksk-pc-btn aiksk-pc-btn--small", testid: "aiksk-pc-reset-layout",
                    text: "重置节点布局（800×620）",
                    onClick: () => this.enforceNodeSize("reset"),
                }),
                dom.promptBlock,
                el("div", "aiksk-pc-section", [dom.seedRow, dom.advancedRow, dom.optionalRow]),
                el("div", "aiksk-pc-section", [dom.bindingRow, dom.bindingHint]),
                el("div", "aiksk-pc-section", [dom.log]),
            ]),
        ]);
        advanced.dataset.testid = "aiksk-pc-advanced";
        dom.advanced = advanced;

        root.append(
            dom.head,
            dom.libs,
            dom.selectedBlock,
            el("div", "aiksk-pc-section aiksk-pc-main", [
                dom.modeRow, dom.modelBlock, dom.dualRow, dom.summary, dom.queueSplit, dom.actions,
                dom.jobHead, dom.progress, dom.status,
            ]),
            advanced,
        );

        /* hidden file input for API workflow import */
        dom.fileInput = el("input", {
            type: "file", accept: ".json,application/json", class: "aiksk-pc-file-input",
            testid: "aiksk-pc-template-file",
        });
        dom.fileInput.addEventListener("change", () => this.onTemplateFileChosen());
        root.append(dom.fileInput);

        this.pendingImportModel = "";
    }

    enforceNodeSize(reason = "update") {
        const node = this.node;
        if (!node || typeof node.setSize !== "function") return;
        const raw = node.size;
        let size = [];
        if (Array.isArray(raw)) size = [...raw];
        else if (raw && typeof raw === "object" && Number.isFinite(Number(raw[0])) && Number.isFinite(Number(raw[1]))) {
            size = [Number(raw[0]), Number(raw[1])];
        }
        let width = Number.isFinite(size[0]) ? size[0] : NODE_DEFAULT_WIDTH;
        let height = Number.isFinite(size[1]) ? size[1] : NODE_DEFAULT_HEIGHT;
        width = Math.max(NODE_MIN_WIDTH, width || NODE_DEFAULT_WIDTH);
        if (reason === "create" || reason === "reset" || !Number.isFinite(height)
                || height < NODE_MIN_HEIGHT || height > NODE_MIGRATE_MAX_HEIGHT) {
            width = reason === "reset" ? NODE_DEFAULT_WIDTH : width;
            height = NODE_DEFAULT_HEIGHT;
        }
        node.setSize([Math.round(width), Math.round(height)]);
        this.widget?.setSize?.([Math.round(width), Math.max(240, Math.round(height) - 80)]);
    }

    mountPanel() {
        const node = this.node;
        if (typeof node.addDOMWidget !== "function") {
            console.warn("[AIKSK-PC] node.addDOMWidget is unavailable; UI will not be mounted.");
            return;
        }
        // Cloning a node may copy the DOM widget reference; keep exactly one.
        const stale = (node.widgets || []).filter((widget) => widget?.name === UI_WIDGET_NAME);
        for (const widget of stale) {
            try {
                node.removeWidget?.(widget) ?? widget.onRemove?.();
            } catch {
                /* ignore */
            }
        }
        const widget = node.addDOMWidget(UI_WIDGET_NAME, "aiksk-pc", this.dom.root, {
            serialize: false,
            hideOnZoom: false,
            getMinHeight: () => PANEL_MIN_HEIGHT,
            getMaxHeight: () => PANEL_MAX_HEIGHT,
        });
        widget.serialize = false;
        widget.options = { ...(widget.options || {}), serialize: false };
        widget.onRemove = () => {
            try {
                this.dom.root?.remove();
            } catch (error) {
                /* ignore */
            }
        };
        this.widget = widget;

        this.enforceNodeSize("create");
    }

    /* ------------------------------------------------------------------ */
    /* Render                                                              */
    /* ------------------------------------------------------------------ */

    renderAll() {
        const state = this.state;
        this.dom.mode.value = state.mode;
        this.dom.qwen.input.checked = state.models.includes("qwen21");
        this.dom.flux.input.checked = state.models.includes("flux3klein");
        this.dom.samples.value = String(state.samples);
        this.dom.seed.value = String(state.seed);
        this.dom.seedPolicy.value = state.seedPolicy;
        this.dom.prompt.value = state.prompt;
        this.dom.width.value = String(state.width);
        this.dom.height.value = String(state.height);
        this.dom.resize.value = state.resizeMode;
        this.dom.activeIndex.value = String(state.activeIndex);
        this.dom.externalPrompt.value = state.externalPrompt || "";
        this.dom.seedOverride.value = String(state.seedOverride);
        this.dom.dual.input.checked = Boolean(state.dualCompare);
        this.dom.graphModel.value = state.graphModel;
        this.dom.graphModel.disabled = Boolean(state.dualCompare);
        this.renderSelected();
        this.renderTaskCount();
        this.renderBindings();
        this.renderJob();
        this.updateSubmitState();
        if (this.loadErrors?.length) this.setStatus(`读取已保存选择时发现问题：${this.loadErrors[0].message}`, "warn");
        else if (this.loadWarnings?.length) this.setStatus(this.loadWarnings[0].message, "");
    }

    renderSelected() {
        for (const kind of ["pose", "costume"]) {
            const entries = kind === "pose" ? this.state.poses : this.state.costumes;
            const list = kind === "pose" ? this.dom.poseSelected : this.dom.costumeSelected;
            const counter = kind === "pose" ? this.dom.poseCount : this.dom.costumeCount;
            const headCounter = kind === "pose"
                ? this.dom.selectedBlock.querySelector("[data-testid=aiksk-pc-pose-selected-count]")
                : this.dom.selectedBlock.querySelector("[data-testid=aiksk-pc-costume-selected-count]");
            counter.textContent = String(entries.length);
            if (headCounter) {
                const itemCount = entries.length;
                headCounter.textContent = itemCount ? `${itemCount} 项 · 按选择顺序` : "尚未选择";
            }
            list.replaceChildren(...entries.slice(0, SELECTED_PREVIEW_LIMIT).map((entry) => this.selectionChip(kind, entry)));
            if (entries.length > SELECTED_PREVIEW_LIMIT) {
                list.append(el("span", "aiksk-pc-hint", `+${entries.length - SELECTED_PREVIEW_LIMIT}`));
            }
        }
    }

    selectionChip(kind, entry) {
        const cached = this.itemCache[kind].get(entry.id) || this.catalog[kind].map.get(entry.id) || null;
        const label = cached ? core.describeItem(kind, cached) : entry.id;
        const image = cached
            ? thumbnail(this.imageURLs(kind, cached, "thumb"), label)
            : thumbnail(this.imageURLsByEntry(kind, entry, "thumb"), label);
        const chip = el("div", { class: "aiksk-pc-chip", title: `${entry.id}\n${label}` }, [
            image,
            el("span", { class: "aiksk-pc-chip-name", text: label }),
        ]);
        const remove = el("button", {
            class: "aiksk-pc-chip-del", type: "button", text: "×", title: "移除这一项",
            testid: `aiksk-pc-remove-${kind}-${entry.id}`,
        });
        remove.addEventListener("click", (event) => {
            event.stopPropagation();
            this.removeSelection(kind, entry.id);
        });
        chip.append(remove);
        return chip;
    }

    removeSelection(kind, id) {
        const oldPoses = this.state.poses;
        const oldCostumes = this.state.costumes;
        // In manual mode the ticks are stored as indices, so capture the pair
        // identities first and re-map them after the selection changes.
        const identityPairs = this.state.mode === "manual"
            ? this.effectiveManualPairs()
                .map(([i, j]) => [oldPoses[i]?.id, oldCostumes[j]?.id])
                .filter(([poseId, costumeId]) => poseId && costumeId)
            : null;
        if (kind === "pose") {
            this.state.poses = this.state.poses.filter((entry) => entry.id !== id);
        } else {
            this.state.costumes = this.state.costumes.filter((entry) => entry.id !== id);
        }
        if (identityPairs) {
            const poseIndex = new Map(this.state.poses.map((entry, index) => [entry.id, index]));
            const costumeIndex = new Map(this.state.costumes.map((entry, index) => [entry.id, index]));
            this.manualPairs = identityPairs
                .map(([poseId, costumeId]) => [poseIndex.get(poseId), costumeIndex.get(costumeId)])
                .filter(([i, j]) => i !== undefined && j !== undefined);
        }
        this.persist();
        this.renderSelected();
        this.renderTaskCount();
        this.updateSubmitState();
    }

    currentPairs() {
        return core.buildPairs(this.state.poses, this.state.costumes, {
            mode: this.state.mode,
            manualPairs: this.manualPairs,
        });
    }

    currentCounts() {
        const built = this.currentPairs();
        const models = this.state.models;
        const tasks = core.computeTaskCount({
            pairCount: built.pairs.length,
            modelCount: models.length,
            samplesPerPair: this.state.samples,
            dualCompare: this.state.dualCompare,
        });
        const images = this.state.dualCompare ? tasks * Math.max(1, models.length) : tasks;
        return {
            pairs: built.pairs.length,
            models: models.length,
            samples: this.state.samples,
            tasks,
            images,
            dual: this.state.dualCompare,
            built,
        };
    }

    renderTaskCount() {
        const counts = this.currentCounts();
        this.dom.taskCount.textContent = String(counts.tasks);
        const modelPart = counts.dual
            ? `双模型对比 × ${counts.models} 模型`
            : `${counts.models} 模型`;
        this.dom.taskDetail.textContent = counts.dual
            ? `${counts.pairs} 组 × ${modelPart} × ${counts.samples} 张/组 = ${counts.tasks} 条任务（每条 2 图）`
            : `${counts.pairs} 组 × ${counts.models} 模型 × ${counts.samples} 张/组 = ${counts.tasks} 条独立队列任务`;
        if (this.dom.queueSplit) {
            this.dom.queueSplit.textContent = `当前计划：${counts.images} 张图 = ${counts.tasks} 条独立 ComfyUI 队列任务（每张图 1 条）。批量请点「确认批量执行」；画布顶部「运行」只执行当前 1 张。`;
        }
        if (this.state.activeIndex >= counts.tasks && counts.tasks > 0) {
            this.state.activeIndex = 0;
            this.dom.activeIndex.value = "0";
        }
        const built = counts.built;
        const firstIssue = built.errors[0];
        if (firstIssue && this.state.poses.length && this.state.costumes.length) {
            this.dom.taskDetail.textContent = `⚠ ${firstIssue.message}`;
        }
    }

    renderBindings() {
        const bindings = this.bindings;
        const selected = this.state.models;
        const dual = this.state.dualCompare;
        const byModel = new Map((bindings.bindings || []).map((binding) => [binding.model, binding]));
        const detection = this.graphDetection || {};
        const detectionText = detection.dual
            ? "检测到当前画布含 Qwen + FLUX 两个分支（对比模式可用）"
            : detection.model
                ? `检测到当前画布模型：${core.MODEL_LABELS[detection.model]}`
                : "";

        const rows = [
            { model: "qwen21", element: this.dom.bindQwen, importButton: this.dom.importQwen },
            { model: "flux3klein", element: this.dom.bindFlux, importButton: this.dom.importFlux },
        ];
        // 审计 P1-3：双模型对比模式下两个模型共用 byModel.get("dual")，
        // 不能因为缺少单模型模板而显示“未绑定模板”提示。
        const dualBinding = dual ? byModel.get("dual") : null;
        for (const row of rows) {
            const binding = byModel.get(row.model);
            const external = this.state.externalTemplates?.[row.model];
            const enabled = selected.includes(row.model);
            let dotClass = "aiksk-pc-dot";
            let text;
            let hintText = "";
            let bound = Boolean(binding?.bound);
            let source = binding?.source || "none";
            if (dual) {
                // 对比模式：两个模型行都显示同一份双分支模板状态，隐藏单模型缺失提示。
                const dualReady = Boolean(dualBinding?.bound);
                bound = dualReady;
                source = "dual";
                dotClass += dualReady ? " aiksk-pc-dot--ok" : " aiksk-pc-dot--bad";
                text = `${core.MODEL_LABELS[row.model]}：共用当前双分支模板${dualReady ? ` ✓ node ${dualBinding.node_id}` : "（未就绪）"}`;
                hintText = "";
            } else if (!enabled) {
                text = `${core.MODEL_LABELS[row.model]}（未勾选，不参与本次任务）`;
            } else if (!binding?.bound) {
                dotClass += " aiksk-pc-dot--bad";
                text = `${core.MODEL_LABELS[row.model]}：未绑定模板，提交会被阻止`;
                hintText = "请点「导入 API 工作流」";
            } else if (binding.source === "canvas") {
                dotClass += " aiksk-pc-dot--ok";
                text = `${core.MODEL_LABELS[row.model]}：当前画布模板 ✓ node ${binding.node_id}`;
            } else {
                dotClass += " aiksk-pc-dot--ok";
                text = `${core.MODEL_LABELS[row.model]}：已导入 API 模板 ✓ node ${binding.node_id}`;
            }
            const dot = el("span", dotClass);
            const content = el("span", { style: { display: "flex", alignItems: "center", gap: "6px", minWidth: "0" } }, [
                el("span", { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, text }),
                hintText ? el("span", { class: "aiksk-pc-hint aiksk-pc-hint--warn", text: hintText }) : null,
            ]);
            row.element.replaceChildren(dot, content);
            row.element.dataset.bound = String(bound);
            row.element.dataset.source = source;
            row.element.dataset.dual = String(dual);
            row.element.style.opacity = enabled || dual ? "" : "0.55";
            row.importButton.disabled = dual || !enabled;
            row.importButton.textContent = external ? "重新导入模板" : "导入 API 工作流";
            row.importButton.title = dual
                ? "双模型对比模式使用当前画布双分支模板"
                : `为 ${core.MODEL_LABELS[row.model]} 导入 API 格式工作流 JSON`;
            row.importButton.dataset.model = row.model;
        }

        if (dual) {
            const dualBinding = byModel.get("dual");
            this.dom.bindingHint.textContent = dualBinding?.bound
                ? `双模型对比：当前画布双分支模板已就绪（node ${dualBinding.node_id}）。`
                : "双模型对比：需要当前画布为 C 工作流（本节点同时接 Qwen 与 FLUX 两条分支）。";
            this.dom.bindingHint.className = dualBinding?.bound ? "aiksk-pc-hint aiksk-pc-hint--ok" : "aiksk-pc-hint aiksk-pc-hint--warn";
        } else {
            this.dom.bindingHint.textContent = detectionText;
            this.dom.bindingHint.className = "aiksk-pc-hint";
        }
        const boundCount = (bindings.bindings || []).filter((binding) => binding.bound).length;
        this.dom.bindingSummary.textContent = `模板 ${boundCount}/${dual ? 1 : selected.length || 0}`;
        this.dom.bindingSummary.className = bindings.valid === false && boundCount === 0
            ? "aiksk-pc-hint aiksk-pc-hint--bad"
            : "aiksk-pc-hint";
        this.updateSubmitState();
    }

    renderJob() {
        const summary = this.job.summary;
        if (!summary) {
            this.dom.jobHead.replaceChildren();
            this.dom.progress.setValue(0);
            return;
        }
        const statusText = summary.status || summary.normalized_status || "unknown";
        const pills = [
            el("span", { class: "aiksk-pc-pill", text: `任务集 ${summary.job_set_id || this.job.id}` }),
            el("span", { class: `aiksk-pc-pill${core.isJobActive(summary) ? " aiksk-pc-pill--warn" : summary.failed ? " aiksk-pc-pill--bad" : " aiksk-pc-pill--ok"}`, text: statusText }),
            el("span", { class: "aiksk-pc-pill", text: `完成 ${summary.completed}/${summary.total}` }),
        ];
        if (summary.failed) pills.push(el("span", { class: "aiksk-pc-pill aiksk-pc-pill--bad", text: `失败 ${summary.failed}` }));
        if (summary.paused) pills.push(el("span", { class: "aiksk-pc-pill aiksk-pc-pill--warn", text: "已暂停" }));
        this.dom.jobHead.replaceChildren(...pills);
        this.dom.progress.setValue(summary.progress);
    }

    renderPersonHint() {
        const input = (this.node.inputs || []).find((slot) => slot.name === "person_image");
        const linked = Boolean(input && (input.link != null || input.widget?.value));
        const text = linked ? "人物参考：已输入（输出第 3 路，可自行接线）" : "未接人物参考：第 3 路为 1×1 黑占位，A/B/C 工作流未接线（不伪造人像）";
        this.dom.personHint.textContent = text;
        this.dom.personHint.className = linked ? "aiksk-pc-hint aiksk-pc-hint--ok" : "aiksk-pc-hint aiksk-pc-hint--warn";
    }

    updateSubmitState() {
        const counts = this.currentCounts();
        const templateErrors = (this.bindings.errors || []).filter((error) => error.code === "TEMPLATE_MISSING");
        const blocked = counts.pairs === 0 || !this.bindings.valid || templateErrors.length > 0 || this.bindingsRefreshing;
        this.dom.submit.disabled = blocked;
        this.dom.submit.title = blocked
            ? (templateErrors.length
                ? `无法提交：${templateErrors.map((error) => error.message).join("；")}`
                : counts.pairs === 0
                    ? "无法提交：还没有可执行的组合"
                    : "正在检查模板绑定…")
            : `提交 ${counts.tasks} 个任务（${core.MODEL_LABELS[this.state.graphModel] || this.state.graphModel} 画布模板 + 已导入模板）`;
    }

    /* ------------------------------------------------------------------ */
    /* Control handlers                                                    */
    /* ------------------------------------------------------------------ */

    onModeChange(mode) {
        const next = core.normalizePairMode(mode);
        if (next === "manual") {
            const built = core.buildPairs(this.state.poses, this.state.costumes, { mode: this.state.mode, manualPairs: this.manualPairs });
            const existing = core.pairsToManualPairs(built.pairs, this.state.poses, this.state.costumes);
            if (existing.length) this.manualPairs = existing;
        }
        this.state.mode = next;
        this.persist();
        this.renderTaskCount();
        this.updateSubmitState();
        if (next === "manual" && this.state.poses.length && this.state.costumes.length) {
            this.setStatus("已切换到手动矩阵：点「矩阵 / 树视图」逐格勾选组合。", "");
        }
    }

    onModelsChange() {
        const models = core.selectedModelKeys({
            model_qwen: this.dom.qwen.input.checked,
            model_flux: this.dom.flux.input.checked,
        });
        this.state.models = models;
        if (this.state.dualCompare && models.length !== 2) {
            this.setStatus("双模型对比模式需要同时勾选 Qwen 与 FLUX；已自动关闭对比模式。", "warn");
            this.onDualChange(false);
            return;
        }
        this.persist();
        this.renderTaskCount();
        this.refreshBindings({ reason: "models" }).catch(() => {});
    }

    onSamplesChange(value) {
        this.state.samples = core.clampInt(value, 1, core.MAX_SAMPLES_PER_PAIR, 1);
        this.dom.samples.value = String(this.state.samples);
        this.persist();
        this.renderTaskCount();
    }

    onSeedChange(value) {
        this.state.seed = core.toU32(value);
        this.dom.seed.value = String(this.state.seed);
        this.persist();
    }

    onPromptChange(value) {
        this.state.prompt = String(value ?? "");
        this.persist();
    }

    onDualChange(force = undefined) {
        const enabled = force === undefined ? this.dom.dual.input.checked : Boolean(force);
        this.state.dualCompare = enabled;
        this.dom.dual.input.checked = enabled;
        this.dom.graphModel.disabled = enabled;
        if (enabled && this.state.models.length !== 2) {
            this.state.models = [...core.MODEL_KEYS];
            this.dom.qwen.input.checked = true;
            this.dom.flux.input.checked = true;
        }
        this.persist();
        this.renderTaskCount();
        this.refreshBindings({ reason: "dual" }).catch(() => {});
    }

    onGraphModelChange(value) {
        const key = core.normalizeModelKey(value) || "qwen21";
        this.state.graphModel = key;
        this.graphModelTouched = true;
        this.persist();
        this.refreshBindings({ reason: "graph-model" }).catch(() => {});
    }

    onActiveIndexChange(value) {
        const counts = this.currentCounts();
        const max = Math.max(0, counts.tasks - 1);
        this.state.activeIndex = core.clampInt(value, 0, Math.max(max, 0), 0);
        this.dom.activeIndex.value = String(this.state.activeIndex);
        this.persist();
    }

    onGraphChanged() {
        if (this.disposed) return;
        this.renderPersonHint();
    }

    /* ------------------------------------------------------------------ */
    /* HTTP helpers                                                        */
    /* ------------------------------------------------------------------ */

    resolveURL(path) {
        if (this.api && typeof this.api.apiURL === "function") {
            try {
                return this.api.apiURL(path);
            } catch {
                /* fall back to raw path */
            }
        }
        return path;
    }

    async fetchJSON(path, init = {}) {
        const options = { ...init };
        if (options.body && typeof options.body !== "string") {
            options.body = JSON.stringify(options.body);
            options.headers = { "Content-Type": "application/json", ...(options.headers || {}) };
        }
        let response;
        if (this.api && typeof this.api.fetchApi === "function") {
            response = await this.api.fetchApi(path, options);
        } else {
            response = await fetch(path, options);
        }
        let data = null;
        const text = await response.text().catch(() => "");
        if (text) {
            try {
                data = JSON.parse(text);
            } catch {
                data = { message: text.slice(0, 500) };
            }
        }
        if (!response.ok) {
            const detail = data?.error;
            const error = new Error(
                (detail && typeof detail === "object" ? detail.message : detail)
                || data?.message
                || data?.detail
                || `请求失败（HTTP ${response.status}）`,
            );
            error.status = response.status;
            error.data = data;
            error.code = (detail && typeof detail === "object" ? detail.code : data?.code) || "";
            throw error;
        }
        return data;
    }

    requestPluginJSON(path, init = {}) {
        return this.fetchJSON(`/aiksk_pc${path}`, init);
    }

    /* ------------------------------------------------------------------ */
    /* Gallery modal                                                       */
    /* ------------------------------------------------------------------ */

    imageURLs(kind, item, variant = "thumb") {
        return this.imageURLsById(kind, item.id, item.source || "builtin", variant);
    }

    imageURLsByEntry(kind, entry, variant = "thumb") {
        return this.imageURLsById(kind, entry.id, entry.source || "builtin", variant);
    }

    imageURLsById(kind, id, source = "builtin", variant = "thumb") {
        const meta = GALLERY_UPSTREAM[kind];
        const upstreamKind = variant === "original" ? "original" : "thumb";
        const upstreamQuery = new URLSearchParams({ source, id, kind: upstreamKind });
        const pluginQuery = new URLSearchParams({ kind, id, source, variant });
        return [
            this.resolveURL(`${meta.base}/image?${upstreamQuery}`),
            this.resolveURL(`/aiksk_pc/image?${pluginQuery}`),
        ];
    }

    extractCatalogueItems(kind, data) {
        if (Array.isArray(data)) return data;
        if (!data || typeof data !== "object") return [];
        const candidates = [
            data.items,
            data.catalogue,
            data.entries,
            kind === "pose" ? data.poses : data.costumes,
        ];
        for (const candidate of candidates) {
            if (Array.isArray(candidate)) return candidate;
        }
        return [];
    }

    async ensureCatalogue(kind) {
        const bucket = this.catalog[kind];
        if (bucket.loaded && bucket.items.length) return bucket;
        if (bucket.loading && bucket.promise) {
            await bucket.promise;
            return bucket;
        }
        bucket.loading = true;
        bucket.promise = (async () => {
            let items = [];
            let error = null;
            const meta = GALLERY_UPSTREAM[kind];
            try {
                const upstream = await this.fetchJSON(`${meta.base}/catalogue?source=builtin`, { method: "GET" });
                items = this.extractCatalogueItems(kind, upstream);
            } catch (upstreamError) {
                error = upstreamError;
            }
            if (!items.length) {
                try {
                    const fallback = await this.requestPluginJSON(`/catalogue?kind=${kind}&source=builtin`, { method: "GET" });
                    items = this.extractCatalogueItems(kind, fallback);
                } catch (fallbackError) {
                    if (!error) error = fallbackError;
                }
            }
            items = items.slice(0, MAX_GALLERY_ITEMS);
            bucket.items = items
                .map((raw) => core.normalizeGalleryItem(kind, raw, raw?.source || "builtin"))
                .filter(Boolean);
            bucket.map = new Map(bucket.items.map((item) => [item.id, item]));
            for (const item of bucket.items) this.itemCache[kind].set(item.id, item);
            bucket.count = bucket.items.length;
            bucket.loaded = bucket.items.length > 0;
            bucket.error = bucket.loaded ? "" : (error ? errorMessage(error) : `${meta.label}为空`);
        })().finally(() => {
            bucket.loading = false;
        });
        await bucket.promise;
        return bucket;
    }

    async openGallery(kind, options = {}) {
        if (this.disposed) return null;
        const meta = GALLERY_UPSTREAM[kind];
        if (this.gallery) this.gallery.modal.close("switch");
        const modal = createModal({
            title: `${meta.emoji} ${meta.label}`,
            subtitle: "页面内弹窗 · 多选累积 · 点缩略图切换",
            testid: "aiksk-pc-gallery-modal",
            onClose: () => {
                if (this.gallery?.modal === modal) this.gallery = null;
            },
        });
        const search = el("input", {
            type: "search", class: "aiksk-pc-search", testid: "aiksk-pc-gallery-search",
            placeholder: kind === "pose" ? "搜索姿势名称 / 编号 / 分类 / 角度…" : "搜索服装编号 / 关键词 / 分类 / 标签…",
            "aria-label": `搜索${meta.label}`,
        });
        const categorySelect = selectBox([], { testid: "aiksk-pc-gallery-category", ariaLabel: "分类筛选" });
        const angleSelect = selectBox([], { testid: kind === "pose" ? "aiksk-pc-gallery-angle" : "aiksk-pc-gallery-tag", ariaLabel: kind === "pose" ? "角度筛选" : "标签筛选" });
        const stats = el("span", { class: "aiksk-pc-hint", testid: "aiksk-pc-gallery-stats", text: "正在读取图库…" });
        const filters = el("div", "aiksk-pc-filters", [search, categorySelect, angleSelect, el("span", "aiksk-pc-spacer"), stats]);
        const grid = el("div", { class: "aiksk-pc-grid", testid: "aiksk-pc-gallery-grid" });
        const loadMore = btn("加载更多", { cls: "aiksk-pc-load-more aiksk-pc-btn--small", testid: "aiksk-pc-gallery-more" });
        const body = el("div", "aiksk-pc-modal-body", [filters, grid, loadMore]);
        modal.body.append(body);

        const selectedLabel = el("span", { class: "aiksk-pc-hint", testid: "aiksk-pc-gallery-selected-count", text: "已选 0 项" });
        const selectAllBtn = btn("全选筛选结果", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-gallery-selectall" });
        const clearBtn = btn("清空", { cls: "aiksk-pc-btn--small aiksk-pc-btn--danger", testid: "aiksk-pc-gallery-clear" });
        const cancelBtn = btn("取消", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-gallery-cancel" });
        const confirmBtn = btn("确定", { cls: "aiksk-pc-btn--primary", testid: "aiksk-pc-gallery-confirm" });
        modal.foot.append(
            el("div", "aiksk-pc-modal-foot-group", [selectedLabel, selectAllBtn, clearBtn]),
            el("div", "aiksk-pc-modal-foot-group", [cancelBtn, confirmBtn]),
        );

        const staged = new Map();
        for (const entry of kind === "pose" ? this.state.poses : this.state.costumes) staged.set(entry.id, entry);
        const session = {
            kind,
            modal,
            staged,
            grid,
            search,
            categorySelect,
            angleSelect,
            stats,
            selectedLabel,
            loadMore,
            filters: { query: "", category: "", angle: "" },
            shown: GALLERY_PAGE_SIZE,
            items: [],
            filtered: [],
            renderTimer: null,
        };
        this.gallery = session;

        const rerender = () => this.renderGallery(session);
        search.addEventListener("input", () => {
            session.filters.query = search.value;
            session.shown = GALLERY_PAGE_SIZE;
            clearTimeout(session.renderTimer);
            session.renderTimer = setTimeout(rerender, 140);
        });
        categorySelect.addEventListener("change", () => {
            session.filters.category = categorySelect.value;
            session.shown = GALLERY_PAGE_SIZE;
            rerender();
        });
        angleSelect.addEventListener("change", () => {
            session.filters.angle = angleSelect.value;
            session.shown = GALLERY_PAGE_SIZE;
            rerender();
        });
        loadMore.addEventListener("click", () => {
            session.shown += GALLERY_PAGE_SIZE;
            rerender();
        });
        selectAllBtn.addEventListener("click", () => {
            for (const item of session.filtered) staged.set(item.id, { id: item.id, source: item.source });
            this.updateGallerySelection(session);
        });
        clearBtn.addEventListener("click", () => {
            staged.clear();
            this.updateGallerySelection(session);
        });
        cancelBtn.addEventListener("click", () => modal.close("cancel"));
        confirmBtn.addEventListener("click", () => {
            if (kind === "pose") this.state.poses = [...staged.values()];
            else this.state.costumes = [...staged.values()];
            this.persist();
            this.renderSelected();
            this.renderTaskCount();
            this.updateSubmitState();
            modal.close("confirm");
        });
        grid.addEventListener("click", () => {});
        this.updateGallerySelection(session);

        try {
            const bucket = await this.ensureCatalogue(kind);
            if (!modal.isOpen) return session;
            session.items = bucket.items;
            const categories = core.galleryCategories(bucket.items);
            const categoryOptions = [{ value: "", label: "全部分类" }, ...categories.map((value) => ({ value, label: value }))];
            const previousCategory = session.filters.category;
            categorySelect.replaceChildren(...categoryOptions.map((option) => {
                const optionElement = el("option", { value: option.value, text: option.label });
                return optionElement;
            }));
            categorySelect.value = previousCategory;
            const facetOptions = kind === "pose" ? core.galleryAngles(bucket.items) : this.costumeTags(bucket.items);
            const facetLabel = kind === "pose" ? "全部角度" : "全部标签";
            angleSelect.replaceChildren(
                el("option", { value: "", text: facetLabel }),
                ...facetOptions.map((value) => el("option", { value, text: value })),
            );
            angleSelect.value = "";
            session.stats.textContent = `${bucket.items.length} 个素材 · 已选 ${staged.size} 项`;
            rerender();
            search.focus({ preventScroll: true });
        } catch (error) {
            session.stats.textContent = "图库读取失败";
            grid.replaceChildren(el("div", "aiksk-pc-grid-empty", errorMessage(error)));
        }
        return session;
    }

    costumeTags(items) {
        const weights = new Map();
        for (const item of items || []) {
            for (const tag of item.tags || []) weights.set(tag, (weights.get(tag) || 0) + 1);
        }
        return [...weights.entries()].sort((a, b) => b[1] - a[1]).slice(0, 40).map(([tag]) => tag);
    }

    galleryFiltered(session) {
        const { filters, kind } = session;
        const items = core.filterGalleryItems(session.items, {
            query: filters.query,
            category: filters.category,
            angle: kind === "pose" ? filters.angle : "",
            group: "全部",
        });
        if (kind === "costume" && filters.angle) {
            return items.filter((item) => (item.tags || []).includes(filters.angle));
        }
        return items;
    }

    renderGallery(session) {
        if (!session || !session.modal.isOpen) return;
        session.filtered = this.galleryFiltered(session);
        const items = session.filtered.slice(0, session.shown);
        const cards = items.map((item) => this.galleryCard(session, item));
        if (!cards.length) {
            session.grid.replaceChildren(el("div", "aiksk-pc-grid-empty", session.items.length
                ? "没有符合条件的素材，试试放宽搜索或筛选条件。"
                : "图库中没有素材。"));
        } else {
            session.grid.replaceChildren(...cards);
        }
        session.loadMore.hidden = session.filtered.length <= session.shown;
        session.loadMore.textContent = `加载更多（还有 ${Math.max(0, session.filtered.length - session.shown)} 项）`;
        session.stats.textContent = `显示 ${items.length}/${session.filtered.length} 项 · 共 ${session.items.length} 个素材`;
        this.updateGallerySelection(session);
    }

    galleryCard(session, item) {
        const selected = session.staged.has(item.id);
        const order = selected ? [...session.staged.keys()].indexOf(item.id) + 1 : 0;
        const card = el("div", {
            class: `aiksk-pc-card${selected ? " aiksk-pc-card--selected" : ""}`,
            testid: `aiksk-pc-gallery-item-${item.id}`,
            title: `${item.id}\n${item.label}`,
            dataset: { id: item.id, selected: String(selected) },
        }, [
            thumbnail(this.imageURLs(session.kind, item, "thumb"), item.label),
            el("div", { class: "aiksk-pc-card-order", text: order ? String(order) : "" }),
            el("div", "aiksk-pc-card-body", [
                el("div", { class: "aiksk-pc-card-title", text: item.label || item.id }),
                el("div", { class: "aiksk-pc-card-meta", text: [item.id, item.category, item.angle].filter(Boolean).join(" · ") }),
            ]),
        ]);
        card.addEventListener("click", () => {
            if (session.staged.has(item.id)) session.staged.delete(item.id);
            else session.staged.set(item.id, { id: item.id, source: item.source });
            const nowSelected = session.staged.has(item.id);
            card.classList.toggle("aiksk-pc-card--selected", nowSelected);
            card.dataset.selected = String(nowSelected);
            card.querySelector(".aiksk-pc-card-order").textContent = nowSelected
                ? String([...session.staged.keys()].indexOf(item.id) + 1)
                : "";
            this.updateGallerySelection(session);
        });
        return card;
    }

    updateGallerySelection(session) {
        if (!session) return;
        session.selectedLabel.textContent = `已选 ${session.staged.size} 项（按点击顺序）`;
        session.grid.querySelectorAll(".aiksk-pc-card").forEach((card) => {
            const id = card.dataset.id;
            const order = [...session.staged.keys()].indexOf(id);
            card.classList.toggle("aiksk-pc-card--selected", order >= 0);
            card.dataset.selected = String(order >= 0);
            const badge = card.querySelector(".aiksk-pc-card-order");
            if (badge) badge.textContent = order >= 0 ? String(order + 1) : "";
        });
    }

    /* ------------------------------------------------------------------ */
    /* Matrix / tree modal                                                 */
    /* ------------------------------------------------------------------ */

    effectiveManualPairs() {
        if (this.state.mode === "manual") return this.manualPairs.map((entry) => [...entry]);
        const built = core.buildPairs(this.state.poses, this.state.costumes, {
            mode: this.state.mode,
            manualPairs: this.manualPairs,
        });
        return core.pairsToManualPairs(built.pairs, this.state.poses, this.state.costumes);
    }

    setManualPairs(pairs) {
        this.state.mode = "manual";
        this.manualPairs = pairs.map(([i, j]) => [i, j]);
        this.dom.mode.value = "manual";
        this.persist();
        this.renderTaskCount();
        this.updateSubmitState();
    }

    openMatrixModal() {
        const modal = createModal({
            title: "组合矩阵 / 树视图",
            subtitle: "点击格子切换组合 · 行列头全选 · 树视图可删除单条",
            testid: "aiksk-pc-matrix-modal",
            onClose: () => { if (this.matrix?.modal === modal) this.matrix = null; },
        });
        const tabMatrix = btn("矩阵勾选", { cls: "aiksk-pc-btn--small aiksk-pc-btn--primary", testid: "aiksk-pc-matrix-tab" });
        const tabTree = btn("树视图", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-tree-tab" });
        const tabs = el("div", "aiksk-pc-modal-foot-group", [tabMatrix, tabTree]);
        const hint = el("span", { class: "aiksk-pc-hint" });
        modal.head.insertBefore(tabs, modal.head.lastChild);
        modal.body.append(hint);

        const matrixSection = el("div", "aiksk-pc-matrix-wrap");
        const treeSection = el("div", "aiksk-pc-matrix-wrap aiksk-pc-hidden");
        modal.body.append(matrixSection, treeSection);

        const countLabel = el("span", { class: "aiksk-pc-hint", testid: "aiksk-pc-matrix-count" });
        const resetBtn = btn("按当前组合方式重建", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-matrix-rebuild" });
        const clearBtn = btn("清空全部组合", { cls: "aiksk-pc-btn--small aiksk-pc-btn--danger", testid: "aiksk-pc-matrix-clear" });
        const closeBtn = btn("关闭", { cls: "aiksk-pc-btn--primary", testid: "aiksk-pc-matrix-close" });
        resetBtn.addEventListener("click", () => {
            const built = core.buildPairs(this.state.poses, this.state.costumes, { mode: this.state.mode === "manual" ? "cartesian" : this.state.mode });
            this.setManualPairs(core.pairsToManualPairs(built.pairs, this.state.poses, this.state.costumes));
            this.renderMatrixModal();
        });
        clearBtn.addEventListener("click", () => {
            this.setManualPairs([]);
            this.renderMatrixModal();
        });
        closeBtn.addEventListener("click", () => modal.close("close"));
        modal.foot.append(el("div", "aiksk-pc-modal-foot-group", [countLabel]), el("div", "aiksk-pc-modal-foot-group", [resetBtn, clearBtn, closeBtn]));

        const session = { modal, matrixSection, treeSection, tabMatrix, tabTree, hint };
        this.matrix = session;
        const showTab = (which) => {
            const matrixActive = which === "matrix";
            matrixSection.classList.toggle("aiksk-pc-hidden", !matrixActive);
            treeSection.classList.toggle("aiksk-pc-hidden", matrixActive);
            tabMatrix.classList.toggle("aiksk-pc-btn--primary", matrixActive);
            tabTree.classList.toggle("aiksk-pc-btn--primary", !matrixActive);
        };
        tabMatrix.addEventListener("click", () => showTab("matrix"));
        tabTree.addEventListener("click", () => showTab("tree"));
        this.renderMatrixModal();
        showTab("matrix");
    }

    renderMatrixModal() {
        const session = this.matrix;
        if (!session || !session.modal.isOpen) return;
        const poses = this.state.poses;
        const costumes = this.state.costumes;
        const pairs = this.effectiveManualPairs();
        const selected = new Set(pairs.map(([i, j]) => `${i},${j}`));
        const orderMap = new Map(pairs.map(([i, j], index) => [`${i},${j}`, index + 1]));
        session.hint.textContent = `手动矩阵：${pairs.length} 组已勾选 / 共 ${poses.length * costumes.length} 格。`
            + (this.state.mode === "manual" ? "" : "（点击格子会自动切换为手动矩阵模式）");

        const matrix = el("div", { class: "aiksk-pc-matrix", testid: "aiksk-pc-matrix" });
        matrix.style.gridTemplateColumns = `92px repeat(${Math.max(1, costumes.length)}, minmax(30px, 1fr))`;
        const corner = el("div", {
            class: "aiksk-pc-matrix-corner", testid: "aiksk-pc-matrix-all", text: "全选/清空",
            title: "切换全部组合",
        });
        corner.addEventListener("click", () => this.matrixToggleAll());
        matrix.append(corner);
        costumes.forEach((costume, j) => {
            const cached = this.itemCache.costume.get(costume.id);
            const header = el("div", {
                class: "aiksk-pc-matrix-head", testid: `aiksk-pc-matrix-col-${j}`,
                text: cached ? (cached.label || costume.id) : costume.id,
                title: `${costume.id}（点击全选该列）`,
            });
            header.addEventListener("click", () => this.matrixToggleColumn(j));
            matrix.append(header);
        });
        poses.forEach((pose, i) => {
            const cached = this.itemCache.pose.get(pose.id);
            const header = el("div", {
                class: "aiksk-pc-matrix-head", testid: `aiksk-pc-matrix-row-${i}`,
                text: cached ? (cached.label || pose.id) : pose.id,
                title: `${pose.id}（点击全选该行）`,
            });
            header.addEventListener("click", () => this.matrixToggleRow(i));
            matrix.append(header);
            costumes.forEach((costume, j) => {
                const key = `${i},${j}`;
                const on = selected.has(key);
                const cell = el("div", {
                    class: `aiksk-pc-matrix-cell${on ? " aiksk-pc-matrix-cell--on" : ""}`,
                    testid: `aiksk-pc-cell-${i}-${j}`,
                    text: on ? String(orderMap.get(key)) : "",
                    title: `${pose.id} × ${costume.id}${on ? `（第 ${orderMap.get(key)} 组）` : "（未选）"}`,
                });
                cell.addEventListener("click", () => this.matrixToggleCell(i, j));
                matrix.append(cell);
            });
        });
        session.matrixSection.replaceChildren(matrix);

        const tree = el("div", { class: "aiksk-pc-tree", testid: "aiksk-pc-tree" });
        const groups = new Map();
        for (const [i, j] of pairs) {
            if (!groups.has(i)) groups.set(i, []);
            groups.get(i).push(j);
        }
        for (const [i, columns] of groups) {
            const pose = poses[i];
            const cachedPose = this.itemCache.pose.get(pose.id);
            const details = el("details", { class: "aiksk-pc-tree-group", open: true });
            const summary = el("summary", "", `${cachedPose?.label || pose.id} · ${columns.length} 套服装`);
            details.append(summary);
            for (const j of columns) {
                const costume = costumes[j];
                const cachedCostume = this.itemCache.costume.get(costume.id);
                const orderIndex = orderMap.get(`${i},${j}`);
                const row = el("div", { class: "aiksk-pc-tree-row", testid: `aiksk-pc-tree-row-${orderIndex}` }, [
                    el("span", "aiksk-pc-tree-idx", `#${orderIndex}`),
                    el("b", "", pose.id),
                    el("span", "", "×"),
                    el("span", "", cachedCostume?.label || costume.id),
                    btn("删除", {
                        cls: "aiksk-pc-btn--small aiksk-pc-btn--danger",
                        testid: `aiksk-pc-tree-remove-${orderIndex}`,
                        onClick: () => {
                            this.matrixRemovePair(i, j);
                            this.renderMatrixModal();
                        },
                    }),
                ]);
                details.append(row);
            }
            tree.append(details);
        }
        if (!pairs.length) tree.append(el("div", "aiksk-pc-hint", "手动矩阵为空：在「矩阵勾选」里点格子，或点「按当前组合方式重建」。"));
        session.treeSection.replaceChildren(tree);
        const count = this.matrix?.modal ? session.modal.foot.querySelector("[data-testid=aiksk-pc-matrix-count]") : null;
        if (count) count.textContent = `已勾选 ${pairs.length} 组（共 ${poses.length} 姿势 × ${costumes.length} 服装）`;
    }

    matrixToggleCell(i, j) {
        const pairs = this.effectiveManualPairs();
        const index = pairs.findIndex(([pi, pj]) => pi === i && pj === j);
        if (index >= 0) pairs.splice(index, 1);
        else pairs.push([i, j]);
        this.setManualPairs(pairs);
        this.renderMatrixModal();
    }

    matrixToggleRow(i) {
        const pairs = this.effectiveManualPairs();
        const columns = this.state.costumes.map((_, j) => j);
        const allOn = columns.every((j) => pairs.some(([pi, pj]) => pi === i && pj === j));
        const next = pairs.filter(([pi]) => pi !== i);
        if (!allOn) columns.forEach((j) => next.push([i, j]));
        this.setManualPairs(next);
        this.renderMatrixModal();
    }

    matrixToggleColumn(j) {
        const pairs = this.effectiveManualPairs();
        const rows = this.state.poses.map((_, i) => i);
        const allOn = rows.every((i) => pairs.some(([pi, pj]) => pi === i && pj === j));
        const next = pairs.filter(([, pj]) => pj !== j);
        if (!allOn) rows.forEach((i) => next.push([i, j]));
        this.setManualPairs(next);
        this.renderMatrixModal();
    }

    matrixToggleAll() {
        const pairs = this.effectiveManualPairs();
        const expected = this.state.poses.length * this.state.costumes.length;
        if (pairs.length >= expected && expected > 0) {
            this.setManualPairs([]);
        } else {
            const all = [];
            this.state.poses.forEach((_, i) => this.state.costumes.forEach((__, j) => all.push([i, j])));
            this.setManualPairs(all);
        }
        this.renderMatrixModal();
    }

    matrixRemovePair(i, j) {
        const pairs = this.effectiveManualPairs().filter(([pi, pj]) => !(pi === i && pj === j));
        this.setManualPairs(pairs);
    }

    /* ------------------------------------------------------------------ */
    /* Templates / canvas binding                                          */
    /* ------------------------------------------------------------------ */

    async captureCanvasTemplate() {
        try {
            if (!this.app || typeof this.app.graphToPrompt !== "function") {
                return { ok: false, error: "当前 ComfyUI 前端不支持 graphToPrompt()" };
            }
            const result = await this.app.graphToPrompt();
            const prompt = result?.output;
            if (!prompt || typeof prompt !== "object") {
                return { ok: false, error: "graphToPrompt() 未返回 API 输出" };
            }
            const detection = core.detectTemplate(prompt);
            const nodeId = detection.node_id || String(this.node.id);
            return {
                ok: Boolean(detection.node_id),
                template: { node_id: nodeId, prompt },
                detection,
                modelHint: core.detectGraphModel(prompt),
                error: detection.node_ids.length ? "" : "当前画布快照中找不到本节点（保存工作流后重试）",
            };
        } catch (error) {
            return { ok: false, error: errorMessage(error) };
        }
    }

    async refreshBindings({ reason = "" } = {}) {
        if (this.disposed) return this.bindings;
        if (this.bindingsRefreshing) {
            this.bindingsDirty = true;
            return this.bindings;
        }
        this.bindingsRefreshing = true;
        this.updateSubmitState();
        try {
            const captured = await this.captureCanvasTemplate();
            this.canvasTemplate = captured.template || null;
            this.canvasDetection = captured.detection || null;
            this.graphDetection = captured.modelHint || { qwen: false, flux: false, model: "", dual: false };
            const detected = this.graphDetection;
            if (!this.graphModelTouched && detected.model && detected.model !== this.state.graphModel) {
                this.state.graphModel = detected.model;
                this.dom.graphModel.value = detected.model;
                this.persist({ silent: true });
            }
            const result = core.buildTemplates({
                graphModel: this.state.graphModel,
                dualCompare: this.state.dualCompare,
                canvasTemplate: this.canvasTemplate,
                externalTemplates: this.state.externalTemplates,
                models: this.state.models,
            });
            // A canvas without SaveImage is not a usable batch template; surface
            // the reason but still let the user preview the plan.
            if (captured.detection && !captured.detection.ok) {
                for (const error of captured.detection.errors) {
                    if (!result.errors.some((item) => item.code === error.code && item.message === error.message)) {
                        result.errors.push({ ...error, scope: "canvas" });
                    }
                }
            }
            if (!captured.ok && captured.error) {
                result.warnings.push({ code: "CANVAS_TEMPLATE_UNAVAILABLE", message: captured.error });
            }
            result.valid = result.errors.filter((error) => error.code === "TEMPLATE_MISSING" || error.code === "TEMPLATE_INVALID").length === 0
                && result.errors.length === 0;
            this.bindings = result;
            this.renderBindings();
        } catch (error) {
            this.bindings = { templates: {}, bindings: [], errors: [{ code: "BINDING_FAILED", message: errorMessage(error) }], warnings: [], valid: false };
            this.renderBindings();
        } finally {
            this.bindingsRefreshing = false;
            this.updateSubmitState();
            if (this.bindingsDirty) {
                this.bindingsDirty = false;
                await this.refreshBindings({ reason: `${reason}:queued` });
            }
        }
        return this.bindings;
    }

    async importTemplateFromFile(model) {
        if (this.disposed) return;
        if (this.state.dualCompare) {
            this.setStatus("双模型对比模式使用当前画布双分支模板，无需导入外部模板。", "warn");
            return;
        }
        this.pendingImportModel = model;
        this.dom.fileInput.value = "";
        this.dom.fileInput.click();
    }

    async onTemplateFileChosen() {
        const file = this.dom.fileInput?.files?.[0];
        const model = this.pendingImportModel || "flux3klein";
        this.pendingImportModel = "";
        if (!file) return;
        try {
            const text = await file.text();
            const result = core.importApiTemplate(text);
            if (!result.ok) {
                this.setStatus(`导入模板失败：${result.errors.map((error) => error.message).join("；")}`, "error");
                this.appendLog([`❌ 导入 ${file.name} 失败：${result.errors.map((error) => error.message).join("；")}`], "error");
                return;
            }
            this.state.externalTemplates = { ...this.state.externalTemplates, [model]: result.template };
            this.persist();
            await this.refreshBindings({ reason: "import" });
            const detection = result.detection;
            this.setStatus(`已导入 ${core.MODEL_LABELS[model]} API 模板：节点 ${result.template.node_id}，SaveImage ${detection.save_image_ids.length} 个。`, "ok");
            this.appendLog([
                `✅ 导入模板 ${file.name}`,
                `   模型：${core.MODEL_LABELS[model]}`,
                `   本节点：${result.template.node_id}`,
                `   SaveImage：${detection.save_image_ids.join(", ") || "（无）"}`,
            ], "ok");
        } catch (error) {
            this.setStatus(`导入模板失败：${errorMessage(error)}`, "error");
        }
    }

    /** Test/automation hook: attach an already parsed API prompt object. */
    async setExternalTemplateFromObject(model, promptObject) {
        const result = core.importApiTemplate(promptObject);
        if (!result.ok) return result;
        this.state.externalTemplates = { ...this.state.externalTemplates, [model]: result.template };
        this.persist();
        await this.refreshBindings({ reason: "import-hook" });
        return result;
    }

    clearExternalTemplate(model) {
        const next = { ...this.state.externalTemplates };
        delete next[model];
        this.state.externalTemplates = next;
        this.persist();
        this.refreshBindings({ reason: "clear-import" }).catch(() => {});
    }

    /* ------------------------------------------------------------------ */
    /* Plan / submit / jobs                                                */
    /* ------------------------------------------------------------------ */

    buildCurrentPlan() {
        const result = core.buildPlan({
            poses: this.state.poses,
            costumes: this.state.costumes,
            pairMode: this.state.mode,
            manualPairs: this.manualPairs,
            models: this.state.models,
            samplesPerPair: this.state.samples,
            seed: this.state.seed,
            seedPolicy: this.state.seedPolicy,
            promptTemplate: this.state.prompt,
            dualCompare: this.state.dualCompare,
            graphModel: this.state.graphModel,
            externalTemplates: this.state.externalTemplates,
        });
        return result;
    }


    async refreshModelStatus() {
        if (this.disposed || !this.dom?.modelStatus) return;
        let payload = null;
        try {
            payload = await this.requestPluginJSON("/models", { method: "GET" });
        } catch (error) {
            this.dom.modelStatus.textContent = "模型自检失败：无法读取 /aiksk_pc/models";
            this.dom.modelStatus.className = "aiksk-pc-model-status aiksk-pc-model-status--bad";
            return;
        }
        const models = payload?.models || {};
        const parts = [];
        let bad = 0;
        let unverified = 0;
        for (const key of core.MODEL_KEYS) {
            const model = models[key];
            if (!model) continue;
            const label = model.label || core.MODEL_LABELS[key] || key;
            const visible = Boolean(model.file_visible);
            const structure = Boolean(model.workflow_structure_ok);
            const verified = Boolean(model.load_verified);
            if (!visible) {
                bad += 1;
                parts.push(`○ ${label}：缺权重 ${(model.missing || []).join("、") || "未知"}`);
            } else if (!structure) {
                bad += 1;
                parts.push(`○ ${label}：文件可见，但工作流节点未注册`);
            } else if (!verified) {
                unverified += 1;
                parts.push(`● ${label}：文件✓ 结构✓ 出图待验证`);
            } else {
                const when = String(model.verified_at || "").replace("T", " ").slice(0, 16);
                parts.push(`● ${label}：文件✓ 结构✓ 出图✓${when ? ` (${when})` : ""}`);
            }
        }
        this.dom.modelStatus.textContent = parts.length
            ? parts.join("　")
            : "模型自检不可用（请确认节点包已加载并重启 ComfyUI）";
        const cls = bad ? "aiksk-pc-model-status--bad" : unverified ? "aiksk-pc-model-status--warn" : "aiksk-pc-model-status--ok";
        this.dom.modelStatus.className = `aiksk-pc-model-status ${cls}`;
        this.dom.modelStatus.title = payload?.fingerprint
            ? `模型指纹 ${String(payload.fingerprint).slice(0, 12)} · 文件可见/结构/出图三态分开验证；换环境后出图验证会自动失效`
            : "";
        this.modelStatusPayload = payload;
        if (bad) this.appendLog(["⚠ 模型文件或工作流节点未就绪：批量出图前请核对 ComfyUI 模型目录/节点包（不会阻止预演/选择）。"], "warn");
        else if (unverified) this.appendLog(["ℹ 模型文件与结构已通过；本机尚未登记出图验证（完成一次真实出图后可通过 /aiksk_pc/models/verify 登记）。"], "warn");
    }

    setStatus(text, kind = "") {
        this.dom.status.textContent = String(text ?? "");
        this.dom.status.className = `aiksk-pc-status ${STATUS_CLASS[kind] || ""}`.trim();
    }

    appendLog(lines, kind = "") {
        const fragment = document.createDocumentFragment();
        for (const line of Array.isArray(lines) ? lines : [lines]) {
            const span = el("div", { class: kind ? `aiksk-pc-log-${kind === "error" ? "err" : kind}` : "" });
            span.textContent = String(line);
            fragment.append(span);
        }
        this.dom.log.append(fragment);
        this.dom.log.scrollTop = this.dom.log.scrollHeight;
    }

    clearLog() {
        this.dom.log.replaceChildren();
    }

    async dryRun() {
        if (this.disposed) return;
        const planResult = this.buildCurrentPlan();
        this.persist();
        this.renderTaskCount();
        if (planResult.errors.length) {
            this.setStatus(`预演前检查失败：${planResult.errors.map((error) => error.message).join("；")}`, "error");
            this.clearLog();
            this.appendLog(planResult.errors.map((error) => `❌ [${error.code}] ${error.message}`), "error");
            return null;
        }
        this.setStatus("正在预演（POST /aiksk_pc/plan，不会提交 GPU）…", "warn");
        this.clearLog();
        this.appendLog(["⏳ 正在调用 /aiksk_pc/plan 预演…"]);
        await this.refreshBindings({ reason: "dryrun" });
        const validation = core.validatePlanForSubmit({ plan: planResult.plan, templates: this.bindings.templates });
        try {
            const data = await this.requestPluginJSON("/plan", {
                method: "POST",
                body: { plan: planResult.plan, templates: this.bindings.templates },
            });
            this.planPreview = data;
            this.renderPlanResult(data, validation, planResult);
            return data;
        } catch (error) {
            this.planPreview = null;
            this.setStatus(`预演失败：${errorMessage(error)}`, "error");
            this.appendLog([
                `❌ 预演失败：${errorMessage(error)}`,
                ...(error.status === 404 ? ["   /aiksk_pc/plan 路由不可用：请确认节点包已加载、ComfyUI 已重启。"] : []),
                ...validation.warnings.map((warning) => `⚠ ${warning.message}`),
                ...validation.errors.map((item) => `❌ [${item.code}] ${item.message}`),
            ], "error");
            return null;
        }
    }

    renderPlanResult(data, validation, planResult) {
        const counts = data?.counts || core.planCounts(planResult.plan);
        const warnings = [...(data?.warnings || []), ...(validation?.warnings || []).map((warning) => warning.message)];
        const errors = [...(data?.errors || []), ...(validation?.errors || [])];
        const preview = Array.isArray(data?.tasks_preview) ? data.tasks_preview : core.expandTasks({
            pairs: planResult.pairs,
            models: planResult.plan.models,
            samplesPerPair: planResult.plan.samples_per_pair,
            seed: planResult.plan.seed,
            seedPolicy: planResult.plan.seed_policy,
            dualCompare: planResult.plan.dual_compare,
            jobId: "preview",
        }).tasks.slice(0, 12);

        this.setStatus(
            `预演完成（未提交）：${counts.tasks} 个任务 / ${counts.images} 张图 · ${counts.pairs} 组 × ${counts.models} 模型 × ${counts.samples} 张`
            + (errors.length ? ` · ${errors.length} 个错误` : ""),
            errors.length ? "error" : "ok",
        );
        this.clearLog();
        const lines = [
            `预演结果（dry-run，绝未提交）`,
            `组合：${counts.pairs} 组 · 模型：${counts.models} 个 · 每组张数：${counts.samples}`,
            `任务：${counts.tasks} 个 · 预计图片：${counts.images} 张`,
        ];
        if (planResult.plan.dual_compare) lines.push("模式：双模型对比（每任务 2 张）");
        for (const warning of warnings) lines.push(`⚠ ${warning}`);
        for (const error of errors) lines.push(`❌ ${typeof error === "string" ? error : `[${error.code || "ERROR"}] ${error.message}`}`);
        if (preview.length) {
            lines.push(`任务预览（前 ${preview.length} 条）：`);
            for (const task of preview) {
                const orderNumber = Number.isFinite(Number(task.index))
                    ? Number(task.index)
                    : Number.isFinite(Number(task.order)) ? Number(task.order) : 0;
                const pairId = task.pair_id || "?";
                lines.push(`  #${orderNumber + 1} ${pairId} · ${task.model || "?"} · 采样${Number(task.sample_index ?? 0) + 1} · seed ${task.seed}`);
            }
        }
        this.appendLog(lines, errors.length ? "error" : "");
    }

    async submit() {
        if (this.disposed) return;
        const planResult = this.buildCurrentPlan();
        this.persist();
        this.renderTaskCount();
        if (planResult.errors.length || !planResult.plan.pairs.length) {
            this.setStatus(`无法提交：${planResult.errors.map((error) => error.message).join("；") || "没有可执行的组合"}`, "error");
            return;
        }
        this.setStatus("正在检查模板绑定并提交…", "warn");
        await this.refreshBindings({ reason: "submit" });
        const validation = core.validatePlanForSubmit({ plan: planResult.plan, templates: this.bindings.templates });
        if (!validation.ok) {
            this.setStatus(`无法提交：${validation.errors.map((error) => error.message).join("；")}`, "error");
            this.clearLog();
            this.appendLog(validation.errors.map((error) => `❌ [${error.code}] ${error.message}`), "error");
            if (validation.warnings.length) this.appendLog(validation.warnings.map((warning) => `⚠ ${warning.message}`), "");
            return null;
        }
        const counts = validation.counts;
        const large = validation.confirm_large;
        if (large) {
            const proceed = await this.confirmLargeBatch(counts);
            if (!proceed) {
                this.setStatus("已取消提交（大批量二次确认未通过）。", "");
                return null;
            }
        }
        const countsForQueue = core.planCounts(planResult.plan);
        const body = {
            plan: planResult.plan,
            templates: this.bindings.templates,
            confirm: true,
            start: true,
            // 每张图是独立 prompt；同组多样本时允许把最多 4 条独立 prompt 同时排入 ComfyUI 队列
            in_flight: Math.max(1, Math.min(4, Number(countsForQueue.tasks) || 1)),
        };
        if (large) body.confirm_large = true;
        try {
            const data = await this.requestPluginJSON("/submit", { method: "POST", body });
            const jobId = String(data?.job_set_id || data?.job?.job_set_id || data?.id || "");
            this.job = { id: jobId, summary: null, submit: data };
            this.dom.log.replaceChildren();
            this.appendLog([
                `✅ 已提交任务集：${jobId || "(未返回 job_set_id)"}`,
                `   新建 ${data?.created ?? "?"} 个任务 · 去重 ${data?.deduplicated ?? "?"} 个`,
                `   状态：${data?.status ?? "unknown"}`,
                `   预计图片：${counts.images} 张`,
            ], "ok");
            this.setStatus(`已提交：${counts.tasks} 个任务（${counts.images} 张图）· 任务集 ${jobId || "?"}`, "ok");
            if (jobId) this.startPolling();
            return data;
        } catch (error) {
            this.setStatus(`提交失败：${errorMessage(error)}`, "error");
            this.appendLog([
                `❌ 提交失败：${errorMessage(error)}`,
                ...(error.status === 400 ? ["   后端拒绝了本次提交（如模板缺失 TEMPLATE_MISSING，请检查模型绑定）。"] : []),
            ], "error");
            return null;
        }
    }

    confirmLargeBatch(counts) {
        return new Promise((resolve) => {
            let settled = false;
            const finish = (value) => {
                if (settled) return;
                settled = true;
                resolve(value);
            };
            const modal = createModal({
                title: "确认大批量执行",
                subtitle: `将生成 ${counts.images} 张图片（${counts.tasks} 个任务）`,
                testid: "aiksk-pc-confirm-large",
                small: true,
                onClose: (reason) => finish(reason === "confirm"),
            });
            const info = el("div", "aiksk-pc-hint", `超过 ${core.WARN_OVER_TASKS} 张的任务需要二次确认。提交后将按 in-flight 限流依次执行，可随时暂停/取消。`);
            const cancelBtn = btn("取消", { cls: "aiksk-pc-btn--small", testid: "aiksk-pc-confirm-large-cancel" });
            const okBtn = btn("确认执行", { cls: "aiksk-pc-btn--primary", testid: "aiksk-pc-confirm-large-ok" });
            cancelBtn.addEventListener("click", () => modal.close("cancel"));
            okBtn.addEventListener("click", () => modal.close("confirm"));
            modal.body.append(info);
            modal.foot.append(el("div", "aiksk-pc-modal-foot-group", []), el("div", "aiksk-pc-modal-foot-group", [cancelBtn, okBtn]));
        });
    }

    async jobAction(action) {
        if (!this.job.id) {
            this.setStatus("还没有可操作的任务集：请先提交批量执行。", "warn");
            return;
        }
        const labels = { pause: "暂停", resume: "恢复", cancel: "取消", retry_failed: "重试失败" };
        if (action === "cancel") {
            const modal = createModal({
                title: "确认取消任务集",
                subtitle: this.job.id,
                testid: "aiksk-pc-confirm-cancel",
                small: true,
                onClose: async (reason) => {
                    if (reason !== "confirm") return;
                    await this.jobActionRequest(action);
                },
            });
            const info = el("div", "aiksk-pc-hint", "取消会尽力中断当前执行中的任务；已完成的任务不会重做。");
            const cancelBtn = btn("返回", { cls: "aiksk-pc-btn--small" });
            const okBtn = btn("确认取消", { cls: "aiksk-pc-btn--primary aiksk-pc-btn--danger", testid: "aiksk-pc-confirm-cancel-ok" });
            cancelBtn.addEventListener("click", () => modal.close("cancel"));
            okBtn.addEventListener("click", () => modal.close("confirm"));
            modal.body.append(info);
            modal.foot.append(el("div", "aiksk-pc-modal-foot-group", []), el("div", "aiksk-pc-modal-foot-group", [cancelBtn, okBtn]));
            return;
        }
        await this.jobActionRequest(action, labels[action] || action);
    }

    async jobActionRequest(action, label = action) {
        try {
            this.setStatus(`正在${label}任务集 ${this.job.id}…`, "warn");
            const data = await this.requestPluginJSON(`/jobs/${encodeURIComponent(this.job.id)}/${action}`, {
                method: "POST",
                body: {},
            });
            this.appendLog([`✅ ${label}：${data?.status ?? data?.message ?? "已受理"}`], "ok");
            this.setStatus(`${label}已受理（任务集 ${this.job.id}）`, "ok");
            if (action === "resume") this.startPolling();
            else await this.pollJob();
        } catch (error) {
            this.setStatus(`${label}失败：${errorMessage(error)}`, "error");
            this.appendLog([`❌ ${label}失败：${errorMessage(error)}`], "error");
        }
    }

    startPolling() {
        this.stopPolling();
        this.pollJob().catch(() => {});
        this.pollTimer = setInterval(() => {
            this.pollJob().catch(() => {});
        }, JOB_POLL_MS);
    }

    stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    }

    async pollJob() {
        if (!this.job.id || this.disposed) return null;
        try {
            const data = await this.requestPluginJSON(`/jobs/${encodeURIComponent(this.job.id)}?tasks=1&limit=500`, { method: "GET" });
            const payload = data?.job_set || data?.job || data;
            const summary = core.summarizeJob(payload);
            this.job.summary = summary;
            this.renderJob();
            this.renderJobStatus(summary);
            if (summary && !core.isJobActive(summary)) this.stopPolling();
            return summary;
        } catch (error) {
            this.stopPolling();
            this.setStatus(`轮询任务状态失败：${errorMessage(error)}`, "error");
            return null;
        }
    }

    renderJobStatus(summary) {
        if (!summary) return;
        const failures = summary.failures || [];
        this.setStatus(
            `任务集 ${summary.job_set_id || this.job.id} · ${summary.status || summary.normalized_status} · 完成 ${summary.completed}/${summary.total}`
            + (summary.failed ? ` · 失败 ${summary.failed}` : "")
            + (summary.paused ? " · 已暂停" : ""),
            summary.failed ? "error" : summary.normalized_status === "completed" ? "ok" : "warn",
        );
        if (failures.length) {
            this.clearLog();
            this.appendLog([`失败任务（${failures.length}）：`].concat(failures.slice(0, 60).map((failure) => (
                `❌ [${failure.code || "ERROR"}] ${failure.message || "(无消息)"} · ${failure.pair_id || "?"} · ${failure.model || "?"} · #${failure.sample_index ?? "?"}`
            ))), "error");
        }
        this.dom.pause.disabled = !core.isJobActive(summary);
        this.dom.resume.disabled = !(summary.paused || (!core.isJobActive(summary) && summary.normalized_status !== "completed" && summary.normalized_status !== "canceled"));
        this.dom.cancel.disabled = ["completed", "failed", "canceled"].includes(summary.normalized_status) && summary.finished >= summary.total;
        this.dom.retry.disabled = !(summary.failed > 0);
    }

    /* ------------------------------------------------------------------ */
    /* Test / automation hooks                                             */
    /* ------------------------------------------------------------------ */

    getSummary() {
        const counts = this.currentCounts();
        return {
            poses: this.state.poses.map((entry) => entry.id),
            costumes: this.state.costumes.map((entry) => entry.id),
            mode: this.state.mode,
            models: this.state.models.slice(),
            samples: this.state.samples,
            seed: this.state.seed,
            dualCompare: this.state.dualCompare,
            graphModel: this.state.graphModel,
            externalTemplates: Object.keys(this.state.externalTemplates),
            counts,
            bindings: (this.bindings.bindings || []).map((binding) => ({ ...binding })),
            bindingErrors: (this.bindings.errors || []).map((error) => ({ code: error.code, message: error.message })),
            jobId: this.job.id,
            plan: this.planResult?.plan || null,
        };
    }

    /* ------------------------------------------------------------------ */
    /* Teardown                                                            */
    /* ------------------------------------------------------------------ */

    dispose() {
        if (this.disposed) return;
        this.disposed = true;
        this.stopPolling();
        clearTimeout(this.persistTimer);
        try {
            this.gallery?.modal.close("dispose");
        } catch {
            /* ignore */
        }
        try {
            this.matrix?.modal.close("dispose");
        } catch {
            /* ignore */
        }
        this.gallery = null;
        this.matrix = null;
        this.dom.root?.remove?.();
        if (this.node) {
            delete this.node[UI_KEY];
        }
    }
}

export default { createNodeUI, AikskPcNodeUI, NODE_TYPE, UI_WIDGET_NAME };
