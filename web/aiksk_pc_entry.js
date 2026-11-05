/**
 * AIKSK Pose x Costume Matrix - ComfyUI extension entry point.
 *
 * The only module in this extension that imports ComfyUI's frontend scripts.
 * It registers a canvas-native UI for the KSK_PoseCostumeMatrixBatch node; it does
 * NOT monkey-patch anything globally.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createNodeUI, NODE_TYPE, UI_WIDGET_NAME } from "./aiksk_pc_ui.js";
import { ensureStyles } from "./aiksk_pc_style.js";

const EXTENSION_NAME = "KSKNODES.PoseCostumeMatrix";
const EXTENSION_VERSION = "1.0.0";
const UI_KEY = "__aikskPcUI";

function reportError(where, error) {
    // eslint-disable-next-line no-console
    console.error(`[AIKSK-PC] ${where} failed:`, error);
}

function chainNodeMethod(nodeType, method, after) {
    const previous = nodeType.prototype[method];
    nodeType.prototype[method] = function (...args) {
        const result = previous?.apply(this, args);
        try {
            after.call(this, result, args);
        } catch (error) {
            reportError(`${method} hook`, error);
        }
        return result;
    };
    return previous;
}

app.registerExtension({
    name: EXTENSION_NAME,

    setup() {
        ensureStyles();
        globalThis.__aikskPcExtension = {
            name: EXTENSION_NAME,
            version: EXTENSION_VERSION,
            nodeType: NODE_TYPE,
            widgetName: UI_WIDGET_NAME,
            loadedAt: new Date().toISOString(),
        };
    },

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!nodeData || nodeData.name !== NODE_TYPE) return;
        ensureStyles();

        chainNodeMethod(nodeType, "onNodeCreated", function () {
            try {
                createNodeUI({ node: this, app, api });
            } catch (error) {
                reportError("createNodeUI", error);
            }
        });

        // Workflow load: LiteGraph restores widgets_values before onConfigure.
        chainNodeMethod(nodeType, "onConfigure", function () {
            try {
                const existing = this[UI_KEY];
                if (existing && !existing.disposed && existing.node === this) {
                    existing.syncFromWidgets?.("configure");
                    existing.enforceNodeSize?.("configure");
                }
            } catch (error) {
                reportError("syncFromWidgets", error);
            }
        });

        // Connection changes: refresh the person-reference hint and bindings.
        chainNodeMethod(nodeType, "onConnectionsChange", function () {
            try {
                this[UI_KEY]?.onGraphChanged?.();
            } catch (error) {
                reportError("onGraphChanged", error);
            }
        });

        chainNodeMethod(nodeType, "onRemoved", function () {
            try {
                this[UI_KEY]?.dispose?.("node-removed");
            } catch (error) {
                reportError("dispose", error);
            }
        });
    },
});
