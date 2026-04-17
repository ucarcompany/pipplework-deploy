/* viewer.js — Three.js model viewer for GLB files (ES Module) */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

let scene, camera, renderer, controls, currentModel;
let animFrame = null;

function initViewer() {
    const canvas = document.getElementById('viewerCanvas');
    if (!canvas) return;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x09090b);

    // Grid
    const grid = new THREE.GridHelper(10, 20, 0x7c3aed, 0x16161e);
    scene.add(grid);

    // Lights
    const ambientLight = new THREE.AmbientLight(0x404040, 2);
    scene.add(ambientLight);

    const dirLight1 = new THREE.DirectionalLight(0xa855f7, 3);
    dirLight1.position.set(5, 10, 5);
    scene.add(dirLight1);

    const dirLight2 = new THREE.DirectionalLight(0x06b6d4, 1.5);
    dirLight2.position.set(-5, 5, -5);
    scene.add(dirLight2);

    const pointLight = new THREE.PointLight(0x7c3aed, 1, 20);
    pointLight.position.set(0, 5, 0);
    scene.add(pointLight);

    // Camera
    camera = new THREE.PerspectiveCamera(50, canvas.clientWidth / canvas.clientHeight, 0.01, 1000);
    camera.position.set(3, 3, 3);

    // Renderer
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.setSize(canvas.clientWidth, canvas.clientHeight);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.2;

    // Controls
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.autoRotate = true;
    controls.autoRotateSpeed = 1.5;

    function animate() {
        animFrame = requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();

    // Resize
    const resizeObserver = new ResizeObserver(() => {
        const w = canvas.clientWidth, h = canvas.clientHeight;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
    });
    resizeObserver.observe(canvas.parentElement);
}

window.loadModel = function (url, info) {
    if (!scene) initViewer();

    // Remove previous model
    if (currentModel) {
        scene.remove(currentModel);
        currentModel.traverse(child => {
            if (child.geometry) child.geometry.dispose();
            if (child.material) {
                if (Array.isArray(child.material)) child.material.forEach(m => m.dispose());
                else child.material.dispose();
            }
        });
        currentModel = null;
    }

    const loader = new GLTFLoader();
    loader.load(url, (gltf) => {
        const model = gltf.scene;

        // Auto-scale: fit model in a unit bounding box
        const box = new THREE.Box3().setFromObject(model);
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        const maxDim = Math.max(size.x, size.y, size.z);
        const scale = 3 / maxDim;
        model.scale.setScalar(scale);
        model.position.sub(center.multiplyScalar(scale));

        // Apply green emissive tint for sci-fi look
        model.traverse(child => {
            if (child.isMesh) {
                if (child.material) {
                    child.material = child.material.clone();
                    child.material.metalness = 0.3;
                    child.material.roughness = 0.6;
                    if (!child.material.map) {
                        child.material.color = new THREE.Color(0xb8a0d8);
                    }
                }
            }
        });

        scene.add(model);
        currentModel = model;

        // Reset camera
        camera.position.set(3, 3, 3);
        controls.target.set(0, 0, 0);
        controls.update();
    }, undefined, (err) => {
        console.error('GLTFLoader error:', err);
    });

    // Update info panel
    const infoEl = document.getElementById('viewerInfo');
    if (infoEl && info) {
        infoEl.innerHTML = `
            <span>📐 <b>顶点:</b> ${(info.vertex_count || 0).toLocaleString()}</span>
            <span>🔺 <b>面片:</b> ${(info.face_count || 0).toLocaleString()}</span>
            <span>💧 <b>水密:</b> ${info.is_watertight ? '是' : '否'}</span>
            <span>🧩 <b>流形:</b> ${info.is_manifold ? '是' : '否'}</span>
            <span>📦 <b>大小:</b> ${((info.file_size || 0)/1024).toFixed(1)} KB</span>
            <span>🌐 <b>来源:</b> ${info.source || '—'}</span>
        `;
    }
};

window.destroyViewer = function () {
    if (animFrame) cancelAnimationFrame(animFrame);
    if (renderer) renderer.dispose();
    scene = camera = renderer = controls = currentModel = null;
};
