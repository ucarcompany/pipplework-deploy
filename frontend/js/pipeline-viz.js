/* pipeline-viz.js  — Animated pipeline flow diagram (SVG-free, pure DOM) */

const STAGES = [
    { id: 'source',   icon: '🌐', label: '数据源',   countKey: 'discovered' },
    { id: 'crawl',    icon: '🕷️', label: '爬取引擎', countKey: null },
    { id: 'download', icon: '⬇️', label: '下载',     countKey: 'downloaded' },
    { id: 'validate', icon: '🔍', label: '校验',     countKey: null },
    { id: 'clean',    icon: '🧹', label: '清洗',     countKey: null },
    { id: 'convert',  icon: '🔄', label: 'GLB转换',  countKey: null },
    { id: 'store',    icon: '💾', label: '存储',     countKey: 'cleaned' },
];

const REJECT_STAGE = { id: 'reject', icon: '🗑️', label: '脏数据', countKey: 'rejected' };

let pipelineState = {
    activeStage: null,
    counts: { discovered: 0, downloaded: 0, cleaned: 0, rejected: 0 },
};

function initPipeline() {
    const canvas = document.getElementById('pipelineCanvas');
    if (!canvas) return;

    // Build flow
    const flow = document.createElement('div');
    flow.className = 'pipeline-flow';

    STAGES.forEach((stage, i) => {
        const node = document.createElement('div');
        node.className = 'pipe-node';
        node.id = `pipe-${stage.id}`;
        node.innerHTML = `
            <div class="pipe-icon">${stage.icon}</div>
            <div class="pipe-label">${stage.label}</div>
            <div class="pipe-count" id="pipe-count-${stage.id}">—</div>
        `;
        flow.appendChild(node);

        // Arrow between stages
        if (i < STAGES.length - 1) {
            const arrow = document.createElement('span');
            arrow.className = 'pipe-arrow';
            arrow.id = `pipe-arrow-${i}`;
            arrow.textContent = '▸▸';
            flow.appendChild(arrow);
        }

        // After 'clean' stage, add a branch arrow down to reject
        if (stage.id === 'clean') {
            // We'll show reject below, using CSS positioning later
        }
    });

    // Add reject node at the end
    const rejectNode = document.createElement('div');
    rejectNode.className = 'pipe-node';
    rejectNode.id = `pipe-reject`;
    rejectNode.style.borderColor = 'var(--red-dim)';
    rejectNode.innerHTML = `
        <div class="pipe-icon">${REJECT_STAGE.icon}</div>
        <div class="pipe-label" style="color:var(--red)">${REJECT_STAGE.label}</div>
        <div class="pipe-count" id="pipe-count-reject" style="color:var(--red)">—</div>
    `;

    const rejectArrow = document.createElement('span');
    rejectArrow.className = 'pipe-arrow';
    rejectArrow.textContent = '▸▸';
    rejectArrow.style.color = 'var(--red-dim)';
    flow.appendChild(rejectArrow);
    flow.appendChild(rejectNode);

    canvas.appendChild(flow);
}

function updatePipelineStage(stageId) {
    // Clear all active
    document.querySelectorAll('.pipe-node').forEach(n => n.classList.remove('active'));
    const node = document.getElementById(`pipe-${stageId}`);
    if (node) {
        node.classList.add('active');
        // Make arrows flow
        document.querySelectorAll('.pipe-arrow').forEach(a => a.classList.add('flowing'));
    }
}

function updatePipelineDone() {
    document.querySelectorAll('.pipe-node.active').forEach(n => {
        n.classList.remove('active');
        n.classList.add('done');
    });
    document.querySelectorAll('.pipe-arrow').forEach(a => a.classList.remove('flowing'));
}

function updatePipelineCounts(counts) {
    pipelineState.counts = { ...pipelineState.counts, ...counts };
    for (const [key, val] of Object.entries(pipelineState.counts)) {
        // Find matching stage
        const stage = STAGES.find(s => s.countKey === key);
        if (stage) {
            const el = document.getElementById(`pipe-count-${stage.id}`);
            if (el) el.textContent = val;
        }
        if (key === 'rejected') {
            const el = document.getElementById('pipe-count-reject');
            if (el) el.textContent = val;
        }
    }
}

// Init on load
document.addEventListener('DOMContentLoaded', initPipeline);
