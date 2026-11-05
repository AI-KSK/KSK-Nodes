/**
 * AIKSK Pose x Costume Matrix - pure planning core.
 *
 * This module is intentionally free of DOM / ComfyUI / network dependencies so
 * it can run unchanged in the browser (loaded as an extension module) and in
 * Node (unit + parity tests).  It mirrors the Python planner semantics defined
 * by docs/CONTRACT_node_v1.md.
 *
 * Everything exported here is a pure function or a frozen constant.  The module
 * has no top-level side effects because ComfyUI lists every file in the
 * extension web directory (`/extensions/...`) as a javascript file and will
 * import each of them at page load.
 */

/* ------------------------------------------------------------------ */
/* Constants (mirror comfyui_nodes/KSK_PoseCostumeMatrixBatch/services/__init__.py) */
/* ------------------------------------------------------------------ */

export const SCHEMA_PLAN = "aiksk.pose_costume.plan.v1";
export const SCHEMA_TASK = "aiksk.pc.node_task.v1";
export const NODE_TYPE = "KSK_PoseCostumeMatrixBatch";
export const NODE_DISPLAY_NAME = "AIKSK｜姿势 × 服装矩阵批量";

export const MODEL_KEYS = Object.freeze(["qwen21", "flux3klein"]);
export const MODEL_LABELS = Object.freeze({
    qwen21: "Qwen-Image 2.1",
    flux3klein: "FLUX.3 Klein",
});
export const MODEL_SHORT_LABELS = Object.freeze({
    qwen21: "Qwen",
    flux3klein: "FLUX",
});
export const PAIR_MODES = Object.freeze(["cartesian", "zip", "manual"]);
export const PAIR_MODE_LABELS = Object.freeze({
    cartesian: "全交叉（每姿势 × 每服装）",
    zip: "顺序配对（按选中顺序一对一）",
    manual: "手动矩阵（逐格勾选）",
});
export const SEED_POLICIES = Object.freeze(["fixed", "increment", "random_recorded"]);
export const SEED_POLICY_LABELS = Object.freeze({
    fixed: "固定基线（每样本 +index）",
    increment: "递增 Seed（基础值 + 任务序号）",
    random_recorded: "确定性随机（可复现）",
});
export const RESIZE_MODES = Object.freeze(["原始尺寸", "等比留边", "居中裁剪", "拉伸"]);
export const GALLERY_KINDS = Object.freeze(["pose", "costume"]);
export const GALLERY_KIND_LABELS = Object.freeze({ pose: "姿势", costume: "服装" });

export const U32 = 4294967296;
export const MAX_PAIRS = 10000;
export const MAX_TASKS = 10000;
export const WARN_OVER_TASKS = 100;
export const MAX_SAMPLES_PER_PAIR = 8;
export const DEFAULT_SEED = 9527;
export const DEFAULT_SAMPLES = 1;

export const DEFAULT_PROMPT =
    "Full-body photorealistic fashion photograph of one complete person, head to toe " +
    "(head, face, hair, hands and feet visible), clean neutral studio background, natural lighting, sharp details. " +
    "Strictly keep the body pose, limb placement and camera framing from the pose reference image " +
    "(the skeleton pose guide). Dress the subject in the complete outfit from the costume reference image; " +
    "preserve garment structure, fabric texture, colors and accessories. " +
    "Pose hint: {pose}. Outfit keywords: {costume}.";

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */

export function clampInt(value, min, max, fallback = min) {
    const number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.min(max, Math.max(min, Math.trunc(number)));
}

export function toU32(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    const truncated = Math.trunc(number);
    return ((truncated % U32) + U32) % U32;
}

function pad2(value) {
    return String(value).padStart(2, "0");
}

/** ISO-8601 timestamp carrying the local timezone offset (e.g. +08:00). */
export function localIsoString(date = new Date()) {
    const timezone = -date.getTimezoneOffset();
    const sign = timezone >= 0 ? "+" : "-";
    const total = Math.abs(timezone);
    return (
        `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}` +
        `T${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}` +
        `${sign}${pad2(Math.floor(total / 60))}:${pad2(total % 60)}`
    );
}

function utf8Bytes(text) {
    return new TextEncoder().encode(String(text));
}

function rotateLeft(value, bits) {
    return ((value << bits) | (value >>> (32 - bits))) >>> 0;
}

/** SHA-1 (hex lowercase) - used for pair_id truncation, matching hashlib.sha1. */
export function sha1Hex(input) {
    const bytes = utf8Bytes(input);
    const bitLength = bytes.length * 8;
    const padded = new Uint8Array((((bytes.length + 8) >> 6) + 1) << 6);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    const view = new DataView(padded.buffer);
    view.setUint32(padded.length - 8, Math.floor(bitLength / U32));
    view.setUint32(padded.length - 4, bitLength >>> 0);
    const words = new Uint32Array(80);
    let h0 = 0x67452301;
    let h1 = 0xefcdab89;
    let h2 = 0x98badcfe;
    let h3 = 0x10325476;
    let h4 = 0xc3d2e1f0;
    for (let offset = 0; offset < padded.length; offset += 64) {
        for (let i = 0; i < 16; i += 1) words[i] = view.getUint32(offset + i * 4);
        for (let i = 16; i < 80; i += 1) {
            words[i] = rotateLeft(words[i - 3] ^ words[i - 8] ^ words[i - 14] ^ words[i - 16], 1);
        }
        let a = h0;
        let b = h1;
        let c = h2;
        let d = h3;
        let e = h4;
        for (let i = 0; i < 80; i += 1) {
            let f;
            let k;
            if (i < 20) {
                f = (b & c) | (~b & d);
                k = 0x5a827999;
            } else if (i < 40) {
                f = b ^ c ^ d;
                k = 0x6ed9eba1;
            } else if (i < 60) {
                f = (b & c) | (b & d) | (c & d);
                k = 0x8f1bbcdc;
            } else {
                f = b ^ c ^ d;
                k = 0xca62c1d6;
            }
            const temp = (rotateLeft(a, 5) + f + e + k + words[i]) >>> 0;
            e = d;
            d = c;
            c = rotateLeft(b, 30);
            b = a;
            a = temp;
        }
        h0 = (h0 + a) >>> 0;
        h1 = (h1 + b) >>> 0;
        h2 = (h2 + c) >>> 0;
        h3 = (h3 + d) >>> 0;
        h4 = (h4 + e) >>> 0;
    }
    return [h0, h1, h2, h3, h4].map((word) => word.toString(16).padStart(8, "0")).join("");
}

const SHA256_K = Object.freeze([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotateRight(value, bits) {
    return ((value >>> bits) | (value << (32 - bits))) >>> 0;
}

/** SHA-256 (hex lowercase) - used for deterministic seeds and task keys. */
export function sha256Hex(input) {
    const bytes = utf8Bytes(input);
    const bitLength = bytes.length * 8;
    const padded = new Uint8Array((((bytes.length + 8) >> 6) + 1) << 6);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    const view = new DataView(padded.buffer);
    view.setUint32(padded.length - 8, Math.floor(bitLength / U32));
    view.setUint32(padded.length - 4, bitLength >>> 0);
    const words = new Uint32Array(64);
    const hash = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    for (let offset = 0; offset < padded.length; offset += 64) {
        for (let i = 0; i < 16; i += 1) words[i] = view.getUint32(offset + i * 4);
        for (let i = 16; i < 64; i += 1) {
            const s0 = rotateRight(words[i - 15], 7) ^ rotateRight(words[i - 15], 18) ^ (words[i - 15] >>> 3);
            const s1 = rotateRight(words[i - 2], 17) ^ rotateRight(words[i - 2], 19) ^ (words[i - 2] >>> 10);
            words[i] = (words[i - 16] + s0 + words[i - 7] + s1) >>> 0;
        }
        let [a, b, c, d, e, f, g, h] = hash;
        for (let i = 0; i < 64; i += 1) {
            const s1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
            const ch = (e & f) ^ (~e & g);
            const temp1 = (h + s1 + ch + SHA256_K[i] + words[i]) >>> 0;
            const s0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
            const maj = (a & b) ^ (a & c) ^ (b & c);
            const temp2 = (s0 + maj) >>> 0;
            h = g;
            g = f;
            f = e;
            e = (d + temp1) >>> 0;
            d = c;
            c = b;
            b = a;
            a = (temp1 + temp2) >>> 0;
        }
        hash[0] = (hash[0] + a) >>> 0;
        hash[1] = (hash[1] + b) >>> 0;
        hash[2] = (hash[2] + c) >>> 0;
        hash[3] = (hash[3] + d) >>> 0;
        hash[4] = (hash[4] + e) >>> 0;
        hash[5] = (hash[5] + f) >>> 0;
        hash[6] = (hash[6] + g) >>> 0;
        hash[7] = (hash[7] + h) >>> 0;
    }
    return hash.map((word) => word.toString(16).padStart(8, "0")).join("");
}

/* ------------------------------------------------------------------ */
/* Selection normalisation                                             */
/* ------------------------------------------------------------------ */

const SOURCE_ALIASES = Object.freeze({
    builtin: "builtin",
    "内置素材库": "builtin",
    "内置": "builtin",
    custom: "custom",
    "自定义素材库": "custom",
    "自定义": "custom",
});

export function normalizeSource(source) {
    if (source == null) return "builtin";
    const text = String(source).trim();
    if (!text) return "builtin";
    return SOURCE_ALIASES[text.toLowerCase()] ?? SOURCE_ALIASES[text] ?? "builtin";
}

/** Accepts "id" or {id, source} and returns a clean {id, source} (or null). */
export function normalizeIdEntry(value) {
    if (value == null) return null;
    if (typeof value === "string" || typeof value === "number") {
        const id = String(value).trim();
        return id ? { id, source: "builtin" } : null;
    }
    if (typeof value !== "object") return null;
    const id = String(value.id ?? value.pose_id ?? value.costume_id ?? "").trim();
    if (!id) return null;
    return { id, source: normalizeSource(value.source ?? value.asset_source ?? value.素材库) };
}

/**
 * Normalise + dedupe a selection list.  Matching the Python planner, entries
 * are de-duplicated by id only (first occurrence wins, source of that entry is
 * kept).  Order is preserved.
 */
export function normalizeSelections(values) {
    const list = Array.isArray(values) ? values : [];
    const seen = new Set();
    const result = [];
    for (const value of list) {
        const entry = normalizeIdEntry(value);
        if (!entry) continue;
        if (seen.has(entry.id)) continue;
        seen.add(entry.id);
        result.push(entry);
    }
    return result;
}

export function normalizePairMode(mode) {
    const raw = String(mode ?? "").trim();
    if (raw === "zip" || raw.includes("顺序")) return "zip";
    if (raw === "manual" || raw.includes("手动")) return "manual";
    return "cartesian";
}

export function normalizeSeedPolicy(policy) {
    const raw = String(policy ?? "").trim();
    if (raw === "increment" || raw.includes("递增")) return "increment";
    if (raw === "random_recorded" || raw.includes("随机")) return "random_recorded";
    return "fixed";
}

const MODEL_ALIASES = Object.freeze({
    qwen: "qwen21",
    qwen21: "qwen21",
    "qwen-image": "qwen21",
    qwenimage: "qwen21",
    "qwen-image 2.1": "qwen21",
    flux: "flux3klein",
    flux3: "flux3klein",
    flux3klein: "flux3klein",
    "flux.3 klein": "flux3klein",
    "flux.3": "flux3klein",
});

export function normalizeModelKey(value) {
    const raw = String(value ?? "").trim().toLowerCase();
    if (!raw) return "";
    if (MODEL_ALIASES[raw]) return MODEL_ALIASES[raw];
    if (raw.includes("qwen")) return "qwen21";
    if (raw.includes("flux")) return "flux3klein";
    return "";
}

/** Ordered, de-duplicated canonical model list. */
export function normalizeModelList(value) {
    let list = value;
    if (value && typeof value === "object" && !Array.isArray(value)) {
        list = [];
        if (value.qwen21 || value.qwen || value.model_qwen) list.push("qwen21");
        if (value.flux3klein || value.flux || value.model_flux) list.push("flux3klein");
    }
    if (!Array.isArray(list)) return [];
    const seen = new Set();
    const result = [];
    for (const entry of list) {
        const key = normalizeModelKey(entry);
        if (!key || seen.has(key)) continue;
        seen.add(key);
        result.push(key);
    }
    return MODEL_KEYS.filter((key) => seen.has(key) && result.includes(key));
}

export function selectedModelKeys({ model_qwen = true, model_flux = true } = {}) {
    const list = [];
    if (model_qwen) list.push("qwen21");
    if (model_flux) list.push("flux3klein");
    return list;
}

/**
 * Pair-id segment sanitising (contract §3, mirroring services/planner.py):
 * non [A-Za-z0-9._-] -> "_", strip leading/trailing "._-", empty -> "x",
 * max 48 chars; when truncating append 6 hex chars of the sha1 of the *raw*
 * segment value (Python hashes the original string, not the cleaned one).
 */
export function safeSegment(value, limit = 48) {
    const text = value == null ? "" : String(value);
    let stripped = text.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^[._-]+|[._-]+$/g, "");
    if (!stripped) stripped = "x";
    if (stripped.length > limit) {
        const digest = sha1Hex(text).slice(0, 6);
        stripped = `${stripped.slice(0, Math.max(1, limit - 6))}${digest}`;
        stripped = stripped.slice(0, limit);
    }
    return stripped;
}

export function pairId(poseId, costumeId) {
    return `PC-${safeSegment(poseId)}-${safeSegment(costumeId)}`;
}

const MAX_FILENAME_PREFIX = 180;
const SAFE_PREFIX_RE = /^[A-Za-z0-9._/-]+$/;

/** Contract §2.3: AIKSK_PoseCostume/{pair_id}_s{seed}, safe chars, <= 180. */
export function filenamePrefix(pairIdValue, seedValue) {
    const head = "AIKSK_PoseCostume/";
    const tail = `_s${toU32(seedValue)}`;
    let body = String(pairIdValue ?? "").replace(/[^A-Za-z0-9._-]/g, "_").replace(/^[._-]+|[._-]+$/g, "") || "pair";
    const room = MAX_FILENAME_PREFIX - head.length - tail.length;
    if (room < 1) return `${head}${tail}`.slice(0, MAX_FILENAME_PREFIX);
    if (body.length > room) body = body.slice(0, room).replace(/[._-]+$/g, "") || "pair";
    const result = `${head}${body}${tail}`;
    return result.length > MAX_FILENAME_PREFIX ? result.slice(0, MAX_FILENAME_PREFIX) : result;
}

export function isSafeFilenamePrefix(value) {
    return typeof value === "string" && value.length > 0 && value.length <= MAX_FILENAME_PREFIX && SAFE_PREFIX_RE.test(value);
}

/* ------------------------------------------------------------------ */
/* Pair building                                                       */
/* ------------------------------------------------------------------ */

export function normalizeManualPairs(value) {
    if (!Array.isArray(value)) return [];
    const pairs = [];
    for (const entry of value) {
        let i;
        let j;
        if (Array.isArray(entry)) {
            [i, j] = entry;
        } else if (entry && typeof entry === "object") {
            i = entry.i ?? entry.pose_index ?? entry.poseIndex;
            j = entry.j ?? entry.costume_index ?? entry.costumeIndex;
        } else {
            continue;
        }
        const pi = Number(i);
        const cj = Number(j);
        if (!Number.isInteger(pi) || !Number.isInteger(cj) || pi < 0 || cj < 0) continue;
        pairs.push([pi, cj]);
    }
    return pairs;
}

/** De-duplicate (pose.id, costume.id) pairs preserving first occurrence. */
function dedupeCombos(combos) {
    const seen = new Set();
    const result = [];
    for (const [pose, costume] of combos) {
        const key = `${pose.id}\u0000${costume.id}`;
        if (seen.has(key)) continue;
        seen.add(key);
        result.push([pose, costume]);
    }
    return result;
}

/**
 * Build the ordered pair list for a selection.
 *
 * @param {Array} poses   selection entries ({id, source}) in user order
 * @param {Array} costumes selection entries ({id, source}) in user order
 * @param {{mode?: string, manualPairs?: Array}} options
 * @returns {{mode, pairs, warnings, errors, poseCount, costumeCount}}
 */
export function buildPairs(poses, costumes, options = {}) {
    const poseList = normalizeSelections(poses);
    const costumeList = normalizeSelections(costumes);
    const mode = normalizePairMode(options.mode ?? options.pairMode ?? "cartesian");
    const warnings = [];
    const errors = [];
    let combos = [];

    if (mode === "zip") {
        const length = Math.min(poseList.length, costumeList.length);
        if (poseList.length && costumeList.length && poseList.length !== costumeList.length) {
            warnings.push({
                code: "ZIP_LENGTH_MISMATCH",
                message: `顺序配对需要姿势与服装数量一致（当前 ${poseList.length} 个姿势 / ${costumeList.length} 套服装），将按较短一侧取 ${length} 组。`,
            });
        }
        if (!length) {
            errors.push({ code: "EMPTY_SELECTION", message: "顺序配对需要至少各选 1 个姿势和 1 套服装。" });
        }
        for (let index = 0; index < length; index += 1) {
            combos.push([poseList[index], costumeList[index]]);
        }
    } else if (mode === "manual") {
        const manual = normalizeManualPairs(options.manualPairs ?? options.manual_pairs ?? []);
        if (!manual.length) {
            errors.push({ code: "EMPTY_MANUAL_MATRIX", message: "手动矩阵还没有勾选任何组合。" });
        }
        for (const [poseIndex, costumeIndex] of manual) {
            if (poseIndex >= poseList.length || costumeIndex >= costumeList.length) {
                warnings.push({
                    code: "MANUAL_INDEX_OUT_OF_RANGE",
                    message: `手动矩阵组合 (${poseIndex + 1}, ${costumeIndex + 1}) 超出当前选择范围，已忽略。`,
                });
                continue;
            }
            combos.push([poseList[poseIndex], costumeList[costumeIndex]]);
        }
    } else {
        if (!poseList.length || !costumeList.length) {
            errors.push({ code: "EMPTY_SELECTION", message: "全交叉组合需要先选择姿势与服装。" });
        }
        for (const pose of poseList) {
            for (const costume of costumeList) {
                combos.push([pose, costume]);
            }
        }
    }

    const unique = dedupeCombos(combos);
    if (unique.length !== combos.length) {
        warnings.push({
            code: "DUPLICATE_COMBO",
            message: `已去除 ${combos.length - unique.length} 个重复组合，保留首次出现的顺序。`,
        });
    }
    if (unique.length > MAX_PAIRS) {
        errors.push({ code: "TOO_MANY_PAIRS", message: `组合数量 ${unique.length} 超过上限 ${MAX_PAIRS}。` });
    }

    const pairs = unique.map(([pose, costume], order) => ({
        pair_id: pairId(pose.id, costume.id),
        order,
        pose: { id: pose.id, source: pose.source },
        costume: { id: costume.id, source: costume.source },
    }));
    return {
        mode,
        pairs,
        warnings,
        errors,
        poseCount: poseList.length,
        costumeCount: costumeList.length,
    };
}

export function normalizePairs(pairs) {
    if (!Array.isArray(pairs)) return [];
    return pairs.map((entry, index) => {
        const pose = normalizeIdEntry(entry?.pose) ?? { id: "", source: "builtin" };
        const costume = normalizeIdEntry(entry?.costume) ?? { id: "", source: "builtin" };
        return {
            pair_id: String(entry?.pair_id || pairId(pose.id, costume.id)),
            order: Number.isInteger(entry?.order) ? entry.order : index,
            pose,
            costume,
        };
    }).filter((entry) => entry.pose.id && entry.costume.id);
}

/** Derive de-duplicated selection lists from a pair list. */
export function selectionsFromPairs(pairs) {
    const poses = [];
    const costumes = [];
    for (const pair of normalizePairs(pairs)) {
        poses.push(pair.pose);
        costumes.push(pair.costume);
    }
    return {
        poses: normalizeSelections(poses),
        costumes: normalizeSelections(costumes),
    };
}

/** Convert a pair list back to matrix coordinates (for restoring manual mode). */
export function pairsToManualPairs(pairs, poses, costumes) {
    const poseIndex = new Map(normalizeSelections(poses).map((entry, index) => [entry.id, index]));
    const costumeIndex = new Map(normalizeSelections(costumes).map((entry, index) => [entry.id, index]));
    const result = [];
    const seen = new Set();
    for (const pair of normalizePairs(pairs)) {
        const i = poseIndex.get(pair.pose.id);
        const j = costumeIndex.get(pair.costume.id);
        if (i === undefined || j === undefined) continue;
        const key = `${i},${j}`;
        if (seen.has(key)) continue;
        seen.add(key);
        result.push([i, j]);
    }
    return result;
}

/* ------------------------------------------------------------------ */
/* Task expansion                                                      */
/* ------------------------------------------------------------------ */

export function computeTaskCount({ pairCount = 0, modelCount = 0, samplesPerPair = 1, dualCompare = false } = {}) {
    const pairs = Math.max(0, Math.trunc(Number(pairCount) || 0));
    const samples = clampInt(samplesPerPair, 1, MAX_SAMPLES_PER_PAIR);
    const models = dualCompare ? 1 : Math.max(0, Math.trunc(Number(modelCount) || 0));
    return pairs * models * samples;
}

/**
 * Deterministic seed for one task (mirrors services/planner.py resolve_seed):
 *  fixed           -> (base + sample_index) % 2^32  （每组 N 张 seed 不同、可复现）
 *  increment       -> (base + global task order) % 2^32
 *  random_recorded -> sha256("aiksk.pc.seed.v1|job|pair|model|sample|order|base"),
 *                     first 64 bits big-endian, modulo 2^32.
 */
export function seedForTask({
    seedPolicy = "fixed",
    seed = DEFAULT_SEED,
    orderIndex = 0,
    jobId = "local",
    pairId: pairIdValue = "",
    model = "",
    sampleIndex = 0,
} = {}) {
    const policy = normalizeSeedPolicy(seedPolicy);
    const base = toU32(seed);
    const order = Math.trunc(Number(orderIndex) || 0);
    if (policy === "increment") return toU32(base + order);
    if (policy === "random_recorded") {
        const material = `aiksk.pc.seed.v1|${jobId}|${pairIdValue}|${model}|${sampleIndex}|${order}|${base}`;
        const digest = sha256Hex(material);
        // int.from_bytes(digest[:8], "big") % 2**32 == last 4 bytes as u32.
        return (parseInt(digest.slice(8, 16), 16) % U32) >>> 0;
    }
    return base;
}

/** task_key = "pc1:" + sha256(job_id|pair_id|model|sample)[:24] (contract §3). */
export function taskKey(jobId, pairIdValue, model, sampleIndex) {
    return `pc1:${sha256Hex(`${jobId}|${pairIdValue}|${model}|${sampleIndex}`).slice(0, 24)}`;
}

/**
 * Expand pairs into the ordered task list (mirrors planner.expand_tasks).
 * Non-comparison: pair -> model (selection order) -> sample.
 * Dual compare:   pair -> sample (one prompt fans out to two branches).
 */
export function expandTasks({
    pairs,
    models = ["qwen21", "flux3klein"],
    samplesPerPair = 1,
    seed = DEFAULT_SEED,
    seedPolicy = "fixed",
    dualCompare = false,
    jobId = "local",
} = {}) {
    const pairList = normalizePairs(pairs);
    const modelList = normalizeModelList(models);
    const samples = clampInt(samplesPerPair, 1, MAX_SAMPLES_PER_PAIR);
    const warnings = [];
    const errors = [];
    const isDual = Boolean(dualCompare);
    const pairTotal = pairList.length;

    if (!pairList.length) errors.push({ code: "NO_PAIRS", message: "计划中没有任何姿势×服装组合。" });
    if (!modelList.length) errors.push({ code: "NO_MODELS", message: "计划中没有任何模型。" });

    const tasks = [];
    for (const pair of pairList) {
        const modelsForPair = isDual ? ["dual"] : modelList;
        for (const model of modelsForPair) {
            for (let sampleIndex = 0; sampleIndex < samples; sampleIndex += 1) {
                const orderIndex = tasks.length;
                const seedValue = seedForTask({
                    seedPolicy, seed, orderIndex, jobId,
                    pairId: pair.pair_id, model, sampleIndex,
                });
                tasks.push({
                    task_key: taskKey(jobId, pair.pair_id, model, sampleIndex),
                    index: orderIndex,
                    pair_id: pair.pair_id,
                    pair_index: pair.order,
                    pair_total: pairTotal,
                    pose: { ...pair.pose },
                    costume: { ...pair.costume },
                    pose_id: pair.pose.id,
                    costume_id: pair.costume.id,
                    model,
                    sample_index: sampleIndex,
                    seed: seedValue,
                    filename_prefix: filenamePrefix(pair.pair_id, seedValue),
                    dual_compare: isDual,
                    images: isDual ? modelList.length : 1,
                });
            }
        }
    }

    if (tasks.length > MAX_TASKS) {
        errors.push({ code: "TOO_MANY_TASKS", message: `任务数 ${tasks.length} 超过上限 ${MAX_TASKS}，请拆分后再提交。` });
    } else if (tasks.length > WARN_OVER_TASKS) {
        warnings.push({
            code: "LARGE_BATCH",
            message: `预计生成 ${isDual ? tasks.length * modelList.length : tasks.length} 张图（${tasks.length} 个任务），超过 ${WARN_OVER_TASKS}，提交时需要二次确认。`,
            confirm_large: true,
        });
    }

    const images = isDual ? tasks.length * modelList.length : tasks.length;
    return {
        tasks,
        dual_compare: isDual,
        models: modelList,
        samples_per_pair: samples,
        counts: {
            pairs: pairTotal,
            models: modelList.length,
            samples,
            tasks: tasks.length,
            images,
        },
        warnings,
        errors,
    };
}

/* ------------------------------------------------------------------ */
/* Plan JSON                                                           */
/* ------------------------------------------------------------------ */

export function normalizeExternalTemplates(value) {
    const result = {};
    if (!value || typeof value !== "object" || Array.isArray(value)) return result;
    for (const [rawKey, rawTemplate] of Object.entries(value)) {
        const key = rawKey === "dual" ? "dual" : normalizeModelKey(rawKey);
        if (!key) continue;
        if (!rawTemplate || typeof rawTemplate !== "object") continue;
        const prompt = rawTemplate.prompt;
        if (!prompt || typeof prompt !== "object" || Array.isArray(prompt)) continue;
        const template = {
            node_id: String(rawTemplate.node_id ?? rawTemplate.nodeId ?? ""),
            prompt,
        };
        if (key === "dual" && Array.isArray(rawTemplate.models)) {
            template.models = normalizeModelList(rawTemplate.models);
        }
        result[key] = template;
    }
    return result;
}

/**
 * Build the plan JSON persisted in `selection_json` (contract §3).
 * Mirrors `services/planner.py::build_plan`.
 * Returns {ok, plan, pairs, warnings, errors}.
 */
export function buildPlan(input = {}) {
    const warnings = [];
    const errors = [];
    const poses = normalizeSelections(input.poses ?? input.pose_ids ?? []);
    const costumes = normalizeSelections(input.costumes ?? input.costume_ids ?? []);
    const pairMode = normalizePairMode(input.pairMode ?? input.pair_mode ?? "cartesian");
    const manualPairs = normalizeManualPairs(input.manualPairs ?? input.manual_pairs ?? []);
    const models = normalizeModelList(input.models ?? selectedModelKeys(input));
    const samplesPerPair = clampInt(input.samplesPerPair ?? input.samples_per_pair ?? DEFAULT_SAMPLES, 1, MAX_SAMPLES_PER_PAIR);
    const seed = toU32(input.seed ?? DEFAULT_SEED);
    const seedPolicy = normalizeSeedPolicy(input.seedPolicy ?? input.seed_policy ?? "fixed");
    let promptTemplate = String(input.promptTemplate ?? input.prompt_template ?? "");
    if (!promptTemplate.trim()) promptTemplate = DEFAULT_PROMPT;
    const dualCompare = Boolean(input.dualCompare ?? input.dual_compare ?? false);
    const externalTemplates = normalizeExternalTemplates(input.externalTemplates ?? input.external_templates);

    let graphModel = normalizeModelKey(input.graphModel ?? input.graph_model ?? "");
    if (!graphModel) graphModel = models.includes("qwen21") ? "qwen21" : (models[0] ?? "qwen21");

    let pairs;
    const suppliedPairs = input.pairs;
    if (Array.isArray(suppliedPairs) && suppliedPairs.length) {
        pairs = dedupeNormalizedPairs(normalizePairs(suppliedPairs), warnings);
    } else {
        const built = buildPairs(poses, costumes, { mode: pairMode, manualPairs });
        warnings.push(...built.warnings);
        errors.push(...built.errors);
        pairs = built.pairs;
    }

    if (!models.length) {
        errors.push({ code: "NO_MODEL", message: "请至少选择一个模型（Qwen-Image 2.1 或 FLUX.3 Klein）。" });
    }
    if (!dualCompare && models.length && !models.includes(graphModel)) {
        warnings.push({ code: "GRAPH_MODEL_UNUSED", message: `当前画布模型 ${graphModel} 不在 models 中，节点开关将以 models 为准。` });
    }
    if (dualCompare && models.length !== 2) {
        warnings.push({ code: "DUAL_MODELS", message: `dual_compare 需要两个模型，当前 models=${JSON.stringify(models)}。` });
    }
    if (pairs.length > MAX_PAIRS) {
        errors.push({ code: "TOO_MANY_PAIRS", message: `组合数 ${pairs.length} 超过上限 ${MAX_PAIRS}。` });
    }

    const plan = {
        schema: SCHEMA_PLAN,
        pair_mode: pairMode,
        models,
        samples_per_pair: samplesPerPair,
        seed,
        seed_policy: seedPolicy,
        prompt_template: promptTemplate,
        dual_compare: dualCompare,
        graph_model: graphModel,
        external_templates: externalTemplates,
        pairs,
        pair_count: pairs.length,
        created_at: input.createdAt ?? localIsoString(),
    };
    const counts = planCounts(plan);
    if (counts.tasks > MAX_TASKS) {
        errors.push({ code: "TOO_MANY_TASKS", message: `本次计划共 ${counts.tasks} 个任务，超过上限 ${MAX_TASKS}，请拆分后再提交。` });
    }
    return {
        ok: errors.length === 0,
        plan,
        pairs,
        poses,
        costumes,
        warnings,
        errors,
        counts,
    };
}

/** Dedupe an already-normalised pair list by (pose.id, costume.id). */
function dedupeNormalizedPairs(pairs, warnings) {
    const result = [];
    const seen = new Set();
    let dropped = 0;
    for (const pair of pairs) {
        const key = `${pair.pose.id}\u0000${pair.costume.id}`;
        if (seen.has(key)) {
            dropped += 1;
            continue;
        }
        seen.add(key);
        result.push({ ...pair, order: result.length });
    }
    if (dropped) warnings.push({ code: "DUPLICATE_COMBO", message: `已去除 ${dropped} 个重复的姿势×服装组合。` });
    return result;
}

export function planCounts(plan) {
    const pairs = Array.isArray(plan?.pairs) ? plan.pairs.length : 0;
    const models = normalizeModelList(plan?.models);
    const samples = clampInt(plan?.samples_per_pair ?? DEFAULT_SAMPLES, 1, MAX_SAMPLES_PER_PAIR);
    const dualCompare = Boolean(plan?.dual_compare);
    const tasks = computeTaskCount({ pairCount: pairs, modelCount: models.length, samplesPerPair: samples, dualCompare });
    // planner.plan_counts: dual -> images = tasks * len(models), else images = tasks.
    return {
        pairs,
        models: models.length,
        samples,
        tasks,
        images: dualCompare ? tasks * Math.max(1, models.length) : tasks,
    };
}

/** Parse `selection_json`; tolerant of empty/garbage values. */
export function parsePlan(text) {
    const warnings = [];
    const errors = [];
    let raw = text;
    if (typeof raw === "string") {
        const trimmed = raw.trim();
        if (!trimmed || trimmed === "{}" || trimmed === "null" || trimmed === "[]") {
            return { ok: true, empty: true, plan: null, warnings, errors };
        }
        if (trimmed.startsWith("[")) {
            // Legacy selection arrays are handled by parseIdsJson, not the plan.
            return { ok: true, empty: true, plan: null, warnings, errors };
        }
        try {
            raw = JSON.parse(trimmed);
        } catch (error) {
            return {
                ok: false,
                empty: false,
                plan: null,
                warnings,
                errors: [{ code: "PLAN_JSON_INVALID", message: `selection_json 不是合法 JSON：${error.message}` }],
            };
        }
    }
    if (raw == null || typeof raw !== "object" || Array.isArray(raw)) {
        return {
            ok: false,
            empty: true,
            plan: null,
            warnings,
            errors: [{ code: "PLAN_JSON_INVALID", message: "selection_json 必须是 JSON 对象。" }],
        };
    }

    const plan = {
        schema: String(raw.schema || SCHEMA_PLAN),
        pair_mode: normalizePairMode(raw.pair_mode ?? raw.pairMode ?? "cartesian"),
        models: normalizeModelList(raw.models),
        samples_per_pair: clampInt(raw.samples_per_pair ?? raw.samplesPerPair ?? DEFAULT_SAMPLES, 1, MAX_SAMPLES_PER_PAIR),
        seed: toU32(raw.seed ?? DEFAULT_SEED),
        seed_policy: normalizeSeedPolicy(raw.seed_policy ?? raw.seedPolicy ?? "fixed"),
        prompt_template: String(raw.prompt_template ?? raw.promptTemplate ?? ""),
        dual_compare: Boolean(raw.dual_compare ?? raw.dualCompare ?? false),
        graph_model: normalizeModelKey(raw.graph_model ?? raw.graphModel ?? "") || "qwen21",
        external_templates: normalizeExternalTemplates(raw.external_templates ?? raw.externalTemplates),
        pairs: normalizePairs(raw.pairs),
        created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    };
    plan.pair_count = Number.isFinite(Number(raw.pair_count)) ? Number(raw.pair_count) : plan.pairs.length;

    if (raw.schema && raw.schema !== SCHEMA_PLAN) {
        warnings.push({
            code: "PLAN_SCHEMA_UNKNOWN",
            message: `selection_json schema 为 ${raw.schema}，已按 ${SCHEMA_PLAN} 读取。`,
        });
    }
    if (!Array.isArray(raw.pairs) || !raw.pairs.length) {
        warnings.push({ code: "PLAN_NO_PAIRS", message: "selection_json 中没有组合（pairs），将按当前选择重新生成。" });
    }
    if (!Object.keys(plan.external_templates).length) delete plan.external_templates;

    return { ok: errors.length === 0, empty: false, plan, warnings, errors };
}

/**
 * Parse `pose_ids_json` / `costume_ids_json`.  Accepts the canonical
 * `[{"id","source"}]` array, a plain `["id"]` array, or a newline/space list
 * (legacy widgets).
 */
export function parseIdsJson(text) {
    const errors = [];
    if (text == null) return { ok: true, entries: [], errors };
    if (Array.isArray(text)) return { ok: true, entries: normalizeSelections(text), errors };
    const source = String(text).trim();
    if (!source || source === "[]") return { ok: true, entries: [], errors };
    let parsed;
    try {
        parsed = JSON.parse(source);
    } catch {
        if (source.startsWith("[") || source.startsWith("{")) {
            return {
                ok: false,
                entries: [],
                errors: [{ code: "IDS_JSON_INVALID", message: "ID 清单不是合法 JSON，请重新选择。" }],
            };
        }
        const entries = source
            .split(/[\s,，;；]+/)
            .map((value) => value.trim())
            .filter(Boolean);
        if (entries.length) {
            return { ok: true, entries: normalizeSelections(entries), errors };
        }
        return {
            ok: false,
            entries: [],
            errors: [{ code: "IDS_JSON_INVALID", message: "ID 清单不是合法 JSON，请重新选择。" }],
        };
    }
    if (!Array.isArray(parsed)) {
        return {
            ok: false,
            entries: [],
            errors: [{ code: "IDS_JSON_INVALID", message: "ID 清单必须是 JSON 数组。" }],
        };
    }
    return { ok: true, entries: normalizeSelections(parsed), errors };
}

export function serializeIdsJson(entries) {
    return JSON.stringify(normalizeSelections(entries).map((entry) => ({ id: entry.id, source: entry.source })));
}

export function serializePlan(plan) {
    if (!plan) return "{}";
    return JSON.stringify(plan);
}

/* ------------------------------------------------------------------ */
/* Templates (API prompt snapshots)                                    */
/* ------------------------------------------------------------------ */

export const TEMPLATE_NODE_CLASS = NODE_TYPE;
export const SAVE_NODE_CLASSES = Object.freeze(["SaveImage", "SaveImageAdvanced"]);

/**
 * Find the matrix node + SaveImage nodes in an API-format prompt object.
 * Mirrors planner.validate_workflow_template: multiple matrix nodes are a
 * warning (the first one is used); a missing node / save node / inputs object
 * is an error.
 */
export function detectTemplate(prompt, options = {}) {
    const nodeType = options.nodeType || TEMPLATE_NODE_CLASS;
    const errors = [];
    const warnings = [];
    if (!prompt || typeof prompt !== "object" || Array.isArray(prompt) || !Object.keys(prompt).length) {
        return {
            ok: false,
            node_ids: [],
            node_id: "",
            save_image_ids: [],
            class_types: [],
            errors: [{ code: "TEMPLATE_INVALID", message: "API 工作流必须是非空 JSON 对象（节点 ID → 节点定义）。" }],
            warnings,
        };
    }
    const nodeIds = [];
    const saveImageIds = [];
    const classTypes = new Set();
    for (const [id, node] of Object.entries(prompt)) {
        if (!node || typeof node !== "object" || Array.isArray(node)) {
            errors.push({ code: "TEMPLATE_INVALID", message: `节点 ${id} 不是对象。` });
            continue;
        }
        const classType = String(node.class_type ?? "");
        if (!classType) {
            errors.push({ code: "TEMPLATE_INVALID", message: `节点 ${id} 缺少 class_type。` });
            continue;
        }
        classTypes.add(classType);
        if (!node.inputs || typeof node.inputs !== "object" || Array.isArray(node.inputs)) {
            errors.push({ code: "TEMPLATE_INVALID", message: `节点 ${id} 缺少 inputs 对象。` });
        }
        if (classType === nodeType) nodeIds.push(String(id));
        else if (SAVE_NODE_CLASSES.includes(classType)) saveImageIds.push(String(id));
    }
    if (!nodeIds.length) {
        errors.push({
            code: "TEMPLATE_NO_NODE",
            message: `API 工作流中找不到 ${nodeType} 节点，请确认导入的是本节点参与的工作流。`,
        });
    } else if (nodeIds.length > 1) {
        warnings.push({
            code: "TEMPLATE_MULTIPLE_NODES",
            message: `API 工作流中包含 ${nodeIds.length} 个 ${nodeType} 节点，只会使用第一个（${nodeIds[0]}）。`,
        });
    }
    if (!saveImageIds.length) {
        errors.push({
            code: "TEMPLATE_NO_SAVE_IMAGE",
            message: "API 工作流中找不到 SaveImage / SaveImageAdvanced 节点，无法保存生成结果。",
        });
    }
    if (options.dual && saveImageIds.length < 2) {
        warnings.push({
            code: "TEMPLATE_DUAL_SAVE",
            message: `双模型对比模板只有 ${saveImageIds.length} 个保存节点，预期每个模型分支各一个。`,
        });
    }
    return {
        ok: errors.length === 0,
        node_ids: nodeIds,
        node_id: nodeIds[0] || "",
        save_image_ids: saveImageIds,
        class_types: [...classTypes].sort(),
        errors,
        warnings,
    };
}

function collectStringValues(value, found, depth = 0) {
    if (depth > 6 || found.length > 200) return;
    if (typeof value === "string") {
        found.push(value);
        return;
    }
    if (Array.isArray(value)) {
        for (const entry of value) collectStringValues(entry, found, depth + 1);
        return;
    }
    if (value && typeof value === "object") {
        for (const entry of Object.values(value)) collectStringValues(entry, found, depth + 1);
    }
}

/**
 * Detect which model family an API prompt is wired for.  Only real downstream
 * nodes are inspected (the matrix node itself carries model_qwen/model_flux
 * booleans and must not create false positives).
 */
export function detectGraphModel(prompt) {
    if (!prompt || typeof prompt !== "object") return { qwen: false, flux: false, model: "", dual: false };
    let qwen = false;
    let flux = false;
    for (const node of Object.values(prompt)) {
        if (!node || typeof node !== "object" || Array.isArray(node)) continue;
        const classType = String(node.class_type ?? "");
        if (!classType || classType === NODE_TYPE) continue;
        const strings = [];
        collectStringValues(node.inputs, strings);
        const blob = `${classType} ${strings.join(" ")}`.toLowerCase();
        if (blob.includes("qwen")) qwen = true;
        if (blob.includes("flux")) flux = true;
        if (qwen && flux) break;
    }
    return {
        qwen,
        flux,
        model: qwen && !flux ? "qwen21" : flux && !qwen ? "flux3klein" : "",
        dual: qwen && flux,
    };
}

/**
 * Import an API workflow JSON file (string or parsed object) as an external
 * model template.  Returns {ok, template, detection, errors, warnings}.
 */
export function importApiTemplate(text, options = {}) {
    const warnings = [];
    let parsed = text;
    if (typeof text === "string") {
        const cleaned = text.replace(/^\uFEFF/, "").trim();
        if (!cleaned) {
            return {
                ok: false,
                template: null,
                detection: null,
                errors: [{ code: "TEMPLATE_EMPTY", message: "文件内容为空。" }],
                warnings,
            };
        }
        try {
            parsed = JSON.parse(cleaned);
        } catch (error) {
            return {
                ok: false,
                template: null,
                detection: null,
                errors: [{ code: "TEMPLATE_JSON_INVALID", message: `不是合法 JSON：${error.message}` }],
                warnings,
            };
        }
    }
    if (parsed && typeof parsed === "object" && Array.isArray(parsed.nodes) && Array.isArray(parsed.links)) {
        return {
            ok: false,
            template: null,
            detection: null,
            errors: [{
                code: "TEMPLATE_NOT_API_FORMAT",
                message: "这是 ComfyUI 界面格式工作流，请使用「保存（API）」导出的 API 格式 JSON。",
            }],
            warnings,
        };
    }
    const detection = detectTemplate(parsed, options);
    if (!detection.ok) {
        return { ok: false, template: null, detection, errors: detection.errors, warnings: detection.warnings };
    }
    const modelHint = detectGraphModel(parsed);
    return {
        ok: true,
        template: { node_id: detection.node_id, prompt: parsed },
        detection: { ...detection, model_hint: modelHint.model, dual_hint: modelHint.dual },
        errors: [],
        warnings: warnings.concat(detection.warnings),
    };
}

/**
 * Validate one bound template exactly like planner.validate_workflow_template.
 * Returns {ok, errors, warnings, detection}.
 */
export function validateTemplateBinding(template, key = "", { dual = false } = {}) {
    if (!template || typeof template !== "object" || !template.prompt) {
        return {
            ok: false,
            detection: null,
            errors: [{ code: "TEMPLATE_MISSING", model: key, message: `模板 ${key || ""} 缺少 prompt（API 工作流）。` }],
            warnings: [],
        };
    }
    const detection = detectTemplate(template.prompt, { dual });
    return {
        ok: detection.ok,
        detection,
        errors: detection.errors.map((error) => ({ ...error, model: key })),
        warnings: detection.warnings.map((warning) => ({ ...warning, model: key })),
    };
}

/**
 * Build the `templates` payload for POST /aiksk_pc/plan and /submit.
 * Mirrors planner.resolve_templates: required keys are the selected models,
 * or "dual" for dual-compare mode.
 *
 * @param {object} options
 * @param {string} options.graphModel       model key implemented by the current canvas
 * @param {boolean} options.dualCompare
 * @param {{node_id, prompt}|null} options.canvasTemplate snapshot from app.graphToPrompt()
 * @param {object} options.externalTemplates persisted map {model: {node_id, prompt}}
 * @param {Array} options.models            selected models
 */
export function buildTemplates({ graphModel = "qwen21", dualCompare = false, canvasTemplate = null, externalTemplates = null, models = ["qwen21", "flux3klein"] } = {}) {
    const modelList = normalizeModelList(models);
    const external = normalizeExternalTemplates(externalTemplates);
    const templates = {};
    const bindings = [];
    const errors = [];
    const warnings = [];

    const addBinding = (key, label, template, source) => {
        if (template) {
            // Keep the stored node_id consistent with the actual API prompt: the
            // backend auto-corrects mismatches but logs a warning, so do the same
            // thing up-front and surface it in the UI.
            const detection = detectTemplate(template.prompt, { dual: dualCompare });
            if (detection.node_id) {
                const recorded = String(template.node_id ?? "");
                if (recorded !== detection.node_id) {
                    if (recorded) {
                        warnings.push({
                            code: "TEMPLATE_NODE_ID_FIXED",
                            message: `模板记录的本节点 ID=${recorded} 与工作流不一致，已自动改用 ${detection.node_id}。`,
                            model: key,
                        });
                    }
                    template = { ...template, node_id: detection.node_id };
                }
            }
            templates[key] = template;
        }
        bindings.push({
            model: key,
            label,
            bound: Boolean(template),
            source: template ? source : "none",
            node_id: template?.node_id ?? "",
        });
        if (!template) {
            errors.push({
                code: "TEMPLATE_MISSING",
                model: key,
                message: dualCompare
                    ? "双模型对比模式缺少当前画布模板：请使用含本节点 + 两条模型分支的工作流。"
                    : `${label} 未绑定 API 模板：当前画布只能提供一个模型的模板，另一个模型请先「导入 API 工作流」。`,
            });
            return;
        }
        const checked = validateTemplateBinding(template, key, { dual: dualCompare });
        errors.push(...checked.errors);
        warnings.push(...checked.warnings);
    };

    if (dualCompare) {
        let template = null;
        let source = "none";
        if (canvasTemplate && canvasTemplate.prompt) {
            template = {
                node_id: String(canvasTemplate.node_id ?? ""),
                prompt: canvasTemplate.prompt,
                models: modelList.slice(),
            };
            source = "canvas";
        } else if (external.dual && external.dual.prompt) {
            template = {
                node_id: String(external.dual.node_id ?? ""),
                prompt: external.dual.prompt,
                models: normalizeModelList(external.dual.models ?? modelList),
            };
            source = "external";
        }
        addBinding("dual", "双模型对比", template, source);
        return { templates, bindings, errors, warnings };
    }

    for (const model of modelList) {
        const label = MODEL_LABELS[model] || model;
        let template = null;
        let source = "none";
        if (model === graphModel && canvasTemplate && canvasTemplate.prompt) {
            template = { node_id: String(canvasTemplate.node_id ?? ""), prompt: canvasTemplate.prompt };
            source = "canvas";
        } else if (external[model] && external[model].prompt) {
            template = { node_id: String(external[model].node_id ?? ""), prompt: external[model].prompt };
            source = "external";
        }
        addBinding(model, label, template, source);
    }
    if (!modelList.length) {
        errors.push({ code: "NO_MODELS", message: "至少勾选一个模型。" });
    }
    return { templates, bindings, errors, warnings };
}

/* ------------------------------------------------------------------ */
/* Validation                                                          */
/* ------------------------------------------------------------------ */

/**
 * Pre-submit validation used by the UI before /plan and /submit.
 * Returns {ok, errors, warnings, counts, confirm_large}.
 */
export function validatePlanForSubmit({ plan, templates = {}, dualCompare = null } = {}) {
    const errors = [];
    const warnings = [];
    const counts = planCounts(plan);
    const isDual = dualCompare == null ? Boolean(plan?.dual_compare) : Boolean(dualCompare);
    const modelList = normalizeModelList(plan?.models);

    if (!counts.pairs) {
        errors.push({ code: "NO_PAIRS", message: "还没有可执行的组合：请先选择姿势与服装（或在手动矩阵中勾选）。" });
    }
    if (!isDual && !modelList.length) {
        errors.push({ code: "NO_MODELS", message: "至少勾选一个模型（Qwen-Image 2.1 / FLUX.3 Klein）。" });
    }
    if (counts.pairs > MAX_PAIRS) {
        errors.push({ code: "TOO_MANY_PAIRS", message: `组合数 ${counts.pairs} 超过上限 ${MAX_PAIRS}。` });
    }
    if (counts.tasks > MAX_TASKS) {
        errors.push({ code: "TOO_MANY_TASKS", message: `本次计划共 ${counts.tasks} 个任务，超过上限 ${MAX_TASKS}，请拆分后再提交。` });
    }
    const confirmLarge = counts.tasks > WARN_OVER_TASKS;
    if (confirmLarge && counts.tasks <= MAX_TASKS) {
        warnings.push({
            code: "LARGE_BATCH",
            message: `预计生成 ${counts.images} 张图（${counts.tasks} 个任务），超过 ${WARN_OVER_TASKS}，提交时需要二次确认（confirm_large=true）。`,
            confirm_large: true,
        });
    }
    if (isDual) {
        warnings.push({
            code: "DUAL_COMPARE",
            message: "双模型对比模式：每个任务同时跑两条分支，单任务产出 2 张图。",
        });
    }

    const templateMap = templates || {};
    const required = isDual ? ["dual"] : modelList;
    for (const key of required) {
        const template = templateMap[key];
        if (!template || !template.prompt) {
            errors.push({
                code: "TEMPLATE_MISSING",
                model: key,
                message: isDual
                    ? "双模型对比模式缺少当前画布模板（需含本节点与两条模型分支）。"
                    : `${MODEL_LABELS[key] || key} 还没有可用的 API 工作流模板，无法提交该模型。`,
            });
            continue;
        }
        const checked = validateTemplateBinding(template, key, { dual: isDual });
        errors.push(...checked.errors);
        warnings.push(...checked.warnings);
    }

    return {
        ok: errors.length === 0,
        errors,
        warnings,
        counts,
        confirm_large: confirmLarge,
    };
}

/* ------------------------------------------------------------------ */
/* Prompt rendering                                                    */
/* ------------------------------------------------------------------ */

export function renderPrompt(template, { pose = "", costume = "" } = {}) {
    return String(template ?? "")
        .replace(/\{pose\}/gi, String(pose))
        .replace(/\{costume\}/gi, String(costume));
}

/** Short human-readable description for a pose / costume item. */
export function describeItem(kind, item) {
    if (!item) return "";
    const parts = [];
    if (kind === "pose") {
        if (item.label) parts.push(item.label);
        if (item.category) parts.push(item.category);
        if (item.angle) parts.push(item.angle);
    } else {
        if (item.label) parts.push(item.label);
        if (item.category) parts.push(item.category);
    }
    return parts.filter(Boolean).join(" · ") || String(item.id || "");
}

/* ------------------------------------------------------------------ */
/* Node widget <-> state helpers                                       */
/* ------------------------------------------------------------------ */

export const WIDGET_NAMES = Object.freeze({
    poseIds: "pose_ids_json",
    costumeIds: "costume_ids_json",
    selection: "selection_json",
});

/**
 * Read the persisted node widgets into a UI-friendly state object.
 * Used on node creation and after a workflow is loaded.
 */
export function readSelectionState(values = {}) {
    const warnings = [];
    const errors = [];
    const poseParsed = parseIdsJson(values[WIDGET_NAMES.poseIds]);
    const costumeParsed = parseIdsJson(values[WIDGET_NAMES.costumeIds]);
    const planParsed = parsePlan(values[WIDGET_NAMES.selection]);
    errors.push(...poseParsed.errors, ...costumeParsed.errors, ...planParsed.errors);
    warnings.push(...planParsed.warnings);

    let poses = poseParsed.entries;
    let costumes = costumeParsed.entries;
    const plan = planParsed.plan;
    if (!poses.length && !costumes.length && plan?.pairs?.length) {
        const derived = selectionsFromPairs(plan.pairs);
        poses = derived.poses;
        costumes = derived.costumes;
        warnings.push({
            code: "SELECTION_RESTORED_FROM_PLAN",
            message: "已从 selection_json 的组合恢复姿势/服装选择。",
        });
    }

    const models = normalizeModelList(plan?.models);
    const state = {
        poses,
        costumes,
        plan,
        mode: plan?.pair_mode ?? "cartesian",
        models: models.length ? models : selectedModelKeys({}),
        samples: plan?.samples_per_pair ?? DEFAULT_SAMPLES,
        seed: plan?.seed ?? DEFAULT_SEED,
        seedPolicy: plan?.seed_policy ?? "fixed",
        prompt: plan?.prompt_template || DEFAULT_PROMPT,
        dualCompare: Boolean(plan?.dual_compare),
        graphModel: plan?.graph_model ?? "qwen21",
        externalTemplates: plan?.external_templates ?? {},
        manualPairs: [],
        errors,
        warnings,
    };
    if (state.mode === "manual") {
        state.manualPairs = pairsToManualPairs(plan?.pairs ?? [], poses, costumes);
    }
    return state;
}

/**
 * Convert a UI state object to the three persisted widget values.
 * `planResult` comes from buildPlan(); the plan always carries the current
 * configuration even when some validation errors exist.
 */
export function persistState(state) {
    const planResult = buildPlan({
        poses: state.poses,
        costumes: state.costumes,
        pairMode: state.mode,
        manualPairs: state.manualPairs,
        models: state.models,
        samplesPerPair: state.samples,
        seed: state.seed,
        seedPolicy: state.seedPolicy,
        promptTemplate: state.prompt,
        dualCompare: state.dualCompare,
        graphModel: state.graphModel,
        externalTemplates: state.externalTemplates,
    });
    return {
        values: {
            [WIDGET_NAMES.poseIds]: serializeIdsJson(state.poses),
            [WIDGET_NAMES.costumeIds]: serializeIdsJson(state.costumes),
            [WIDGET_NAMES.selection]: serializePlan(planResult.plan),
        },
        planResult,
    };
}

/* ------------------------------------------------------------------ */
/* Job status                                                          */
/* ------------------------------------------------------------------ */

const COMPLETED_STATES = new Set(["completed", "complete", "done", "succeeded", "success", "finished"]);
const FAILED_STATES = new Set(["failed", "error", "errored"]);
const RUNNING_STATES = new Set(["running", "executing", "in_progress", "inprogress", "active", "started"]);
const PENDING_STATES = new Set(["pending", "queued", "queue", "waiting", "submitted", "created", "ready", "scheduled"]);
const PAUSED_STATES = new Set(["paused", "suspended", "stopped"]);
const CANCELED_STATES = new Set(["canceled", "cancelled", "aborted"]);

export function normalizeTaskStatus(status) {
    const raw = String(status ?? "").trim().toLowerCase().replace(/[-\s]+/g, "_");
    if (COMPLETED_STATES.has(raw)) return "completed";
    if (FAILED_STATES.has(raw)) return "failed";
    if (RUNNING_STATES.has(raw)) return "running";
    if (PAUSED_STATES.has(raw)) return "paused";
    if (CANCELED_STATES.has(raw)) return "canceled";
    if (PENDING_STATES.has(raw)) return "pending";
    if (!raw) return "unknown";
    return raw;
}

function safeNumber(...values) {
    for (const value of values) {
        const number = Number(value);
        if (Number.isFinite(number)) return number;
    }
    return null;
}

/**
 * Normalise any /aiksk_pc/jobs/{id} response into a display summary.
 * Tolerant by design: the backend may return counts directly, as `stats`, or
 * only a task list.
 */
export function summarizeJob(job) {
    if (!job || typeof job !== "object") return null;
    const tasks = Array.isArray(job.tasks) ? job.tasks : null;
    const counts = (job.counts && typeof job.counts === "object" ? job.counts : null)
        || (job.stats && typeof job.stats === "object" ? job.stats : null)
        || {};

    const tally = { total: 0, completed: 0, failed: 0, running: 0, pending: 0, paused: 0, canceled: 0, unknown: 0 };
    if (tasks) {
        for (const task of tasks) {
            const status = normalizeTaskStatus(task?.status ?? task?.state);
            tally.total += 1;
            if (status in tally && status !== "total") tally[status] += 1;
            else tally.unknown += 1;
        }
    }

    const pick = (...keys) => {
        for (const key of keys) {
            const value = safeNumber(counts[key], job[key]);
            if (value != null) return value;
        }
        return null;
    };
    const total = pick("total", "tasks", "total_tasks", "task_total", "count") ?? (tasks ? tally.total : 0);
    const completed = pick("completed", "done", "succeeded", "success", "finished") ?? (tasks ? tally.completed : 0);
    const failed = pick("failed", "failed_tasks", "errors", "error_count") ?? (tasks ? tally.failed : 0);
    const running = pick("running", "in_flight", "inflight", "active", "executing") ?? (tasks ? tally.running : 0);
    const pending = pick("pending", "queued", "waiting", "submitted") ?? (tasks ? tally.pending : 0);
    const pausedCount = pick("paused") ?? (tasks ? tally.paused : 0);
    const canceled = pick("canceled", "cancelled") ?? (tasks ? tally.canceled : 0);
    const unknown = pick("unknown") ?? (tasks ? tally.unknown : 0);

    const status = String(job.status ?? job.state ?? job.job_status ?? "") || (tasks ? "" : "unknown");
    const normalizedStatus = normalizeTaskStatus(status);
    const paused = Boolean(job.paused) || pausedCount > 0 || normalizedStatus === "paused";
    const finished = completed + failed + canceled + unknown;
    const progress = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;

    const failures = [];
    if (tasks) {
        for (const task of tasks) {
            const taskStatus = normalizeTaskStatus(task?.status ?? task?.state);
            if (taskStatus !== "failed" && taskStatus !== "unknown") continue;
            const error = task?.error ?? task?.failure ?? {};
            failures.push({
                task_key: String(task?.task_key ?? task?.key ?? ""),
                pair_id: String(task?.pair_id ?? ""),
                model: String(task?.model ?? ""),
                sample_index: Number.isFinite(Number(task?.sample_index)) ? Number(task.sample_index) : null,
                code: String(error.code ?? task?.error_code ?? task?.code ?? ""),
                message: String(error.message ?? task?.error_message ?? task?.message ?? ""),
                status: taskStatus,
            });
        }
    }
    if (!failures.length && Array.isArray(job.failures)) {
        for (const failure of job.failures) {
            if (!failure || typeof failure !== "object") continue;
            failures.push({
                task_key: String(failure.task_key ?? ""),
                pair_id: String(failure.pair_id ?? ""),
                model: String(failure.model ?? ""),
                sample_index: Number.isFinite(Number(failure.sample_index)) ? Number(failure.sample_index) : null,
                code: String(failure.code ?? failure.error_code ?? ""),
                message: String(failure.message ?? failure.error_message ?? ""),
                status: "failed",
            });
        }
    }

    return {
        job_set_id: String(job.job_set_id ?? job.id ?? job.job_id ?? ""),
        status,
        normalized_status: normalizedStatus,
        paused,
        total,
        completed,
        failed,
        running,
        pending,
        canceled,
        unknown,
        finished,
        progress,
        failures,
        tasks,
        raw: job,
    };
}

/** The list of run states in which the UI should keep polling. */
export function isJobActive(summary) {
    if (!summary) return false;
    if (summary.paused) return false;
    const status = summary.normalized_status;
    if (COMPLETED_STATES.has(status) || FAILED_STATES.has(status) || CANCELED_STATES.has(status)) {
        if (summary.total > 0 && summary.finished >= summary.total) return false;
        return summary.total === 0 ? false : true;
    }
    return true;
}

/* ------------------------------------------------------------------ */
/* Gallery item normalisation                                          */
/* ------------------------------------------------------------------ */

/**
 * Normalise catalogue entries coming from the upstream gallery plugins
 * (Chinese keys) or from this plugin's /aiksk_pc/catalogue fallback
 * (English keys) into one shape:
 *   {kind, id, source, label, category, angle, group, system, tags,
 *    keywords_en, keywords_zh, search, thumb_url, full_url, raw}
 */
export function normalizeGalleryItem(kind, raw, source = "builtin") {
    if (!raw || typeof raw !== "object") return null;
    const id = String(raw.id ?? raw.pose_id ?? raw.costume_id ?? "").trim();
    if (!id) return null;
    const isPose = kind === "pose";
    const tags = Array.isArray(raw.tags)
        ? raw.tags.map(String)
        : Array.isArray(raw.标签)
            ? raw.标签.map(String)
            : typeof raw.tags === "string"
                ? raw.tags.split(/[,，、\s]+/).filter(Boolean)
                : [];
    const label = String(
        raw.label
        ?? (isPose ? raw.姿势名称 : raw.关键词中文)
        ?? raw.title
        ?? raw.name
        ?? id,
    );
    const category = String(raw.category ?? raw.分类 ?? raw["动作分类"] ?? "");
    const angle = String(raw.angle ?? raw.角度 ?? raw.view ?? "");
    const group = String(raw.group ?? raw.pose_group ?? raw.姿势编号 ?? "");
    const system = String(raw.system ?? raw.体系 ?? "");
    const keywordsZh = String(raw.keywords_zh ?? raw.关键词中文 ?? raw.检索 ?? "");
    const keywordsEn = String(raw.keywords_en ?? raw.关键词 ?? "");
    const search = [
        id, label, category, angle, group, system, keywordsZh, keywordsEn, tags.join(" "), raw.检索,
    ].filter(Boolean).join(" ").toLowerCase();
    return {
        kind,
        id,
        source: normalizeSource(raw.source ?? source),
        label,
        category,
        angle,
        group,
        system,
        tags,
        keywords_en: keywordsEn,
        keywords_zh: keywordsZh,
        search,
        thumb_url: String(raw.thumb_url ?? raw.thumb ?? raw.缩略图 ?? ""),
        full_url: String(raw.full_url ?? raw.image ?? raw.大图 ?? raw.原图 ?? ""),
        raw,
    };
}

/** Filter + group gallery items for the picker modal. */
export function filterGalleryItems(items, { query = "", category = "", angle = "", group = "全部" } = {}) {
    const words = String(query ?? "").trim().toLowerCase().split(/\s+/).filter(Boolean);
    return (Array.isArray(items) ? items : []).filter((item) => {
        if (category && item.category !== category) return false;
        if (angle && item.angle !== angle) return false;
        if (group && group !== "全部" && item.group !== group) return false;
        if (!words.length) return true;
        return words.every((word) => item.search.includes(word));
    });
}

export function galleryCategories(items) {
    const values = new Set();
    for (const item of items || []) if (item.category) values.add(item.category);
    return [...values].sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
}

export function galleryAngles(items) {
    const values = new Set();
    for (const item of items || []) if (item.angle) values.add(item.angle);
    return [...values];
}
