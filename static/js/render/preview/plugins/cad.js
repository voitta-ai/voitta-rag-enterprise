// Preview plugin: CAD files (STEP, IGES) with Three.js WebGL viewer.
//
// Three.js and its STLLoader/OrbitControls addons are loaded lazily from
// esm.sh only when the first CAD file is previewed — no impact on initial
// page load.
//
// Pipeline:
//   1. GET /api/files/{id}/stl  →  backend converts STEP/IGES to binary STL
//   2. Three.js STLLoader parses the buffer into a BufferGeometry
//   3. MeshPhongMaterial + directional lights
//   4. OrbitControls for mouse-rotate / scroll-zoom / right-drag pan
//   5. ResizeObserver keeps the canvas sized to the container

import { registerPlugin } from "../index.js";

const CAD_EXTS = new Set([".step", ".stp", ".iges", ".igs", ".fcstd"]);

// STL files can be loaded directly without backend conversion.
const STL_EXTS = new Set([".stl"]);

// Module-level renderer reference so unmount can dispose it.
let _renderer = null;
let _animFrame = null;
let _abortCtrl = null;
let _resizeObserver = null;

const plugin = {
    canPreview(file) {
        const e = _ext(file.rel_path);
        return CAD_EXTS.has(e) || STL_EXTS.has(e);
    },

    async mount(container, file) {
        container.classList.add("preview-cad");
        container.innerHTML = '<p class="preview-loading">Loading 3D model…</p>';
        _abortCtrl = new AbortController();
        const { signal } = _abortCtrl;

        try {
            // Load Three.js lazily.
            const [THREE, { OrbitControls }, { STLLoader }] = await Promise.all([
                import("https://esm.sh/three@0.168.0"),
                import("https://esm.sh/three@0.168.0/examples/jsm/controls/OrbitControls.js"),
                import("https://esm.sh/three@0.168.0/examples/jsm/loaders/STLLoader.js"),
            ]);
            if (signal.aborted) return;

            // Fetch STL bytes.
            const e = _ext(file.rel_path);
            const url = STL_EXTS.has(e)
                ? `/api/files/${file.id}/raw`
                : `/api/files/${file.id}/stl`;
            const resp = await fetch(url, { credentials: "same-origin", signal });
            if (signal.aborted) return;
            if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
            const buffer = await resp.arrayBuffer();
            if (signal.aborted) return;

            // Build Three.js scene.
            container.innerHTML = "";
            const canvas = document.createElement("canvas");
            canvas.className = "preview-cad-canvas";
            container.append(canvas);

            const W = container.clientWidth || 320;
            const H = Math.round(W * 0.85);
            canvas.width = W;
            canvas.height = H;

            const scene = new THREE.Scene();
            scene.background = new THREE.Color(0x1a1a1a);

            const camera = new THREE.PerspectiveCamera(45, W / H, 0.001, 10000);

            _renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
            _renderer.setSize(W, H);
            _renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

            // Lights.
            scene.add(new THREE.AmbientLight(0xffffff, 0.5));
            const dir = new THREE.DirectionalLight(0xffffff, 1.2);
            dir.position.set(1, 2, 3);
            scene.add(dir);
            const dir2 = new THREE.DirectionalLight(0x8888ff, 0.4);
            dir2.position.set(-2, -1, -2);
            scene.add(dir2);

            // Load geometry.
            const geometry = new STLLoader().parse(buffer);
            geometry.computeVertexNormals();
            const material = new THREE.MeshPhongMaterial({
                color: 0x6699cc,
                specular: 0x444444,
                shininess: 40,
                side: THREE.DoubleSide,
            });
            const mesh = new THREE.Mesh(geometry, material);
            scene.add(mesh);

            // Centre and fit camera.
            geometry.computeBoundingBox();
            const box = geometry.boundingBox;
            const centre = new THREE.Vector3();
            box.getCenter(centre);
            const size = new THREE.Vector3();
            box.getSize(size);
            const maxDim = Math.max(size.x, size.y, size.z);
            mesh.position.sub(centre);
            // CAD convention is Z-up — STEP / IGES / FCStd all author
            // models with Z as the vertical axis. Three.js defaults to
            // Y-up, which rotates parts 90° (a scissor lift comes out
            // lying on its side). Setting camera.up to +Z before
            // OrbitControls is created propagates the convention to
            // the orbit/pan/zoom handler.
            camera.up.set(0, 0, 1);
            // Standard mechanical-drawing isometric in a Z-up world:
            // camera at front-right-above looking at the origin.
            const d = maxDim * 1.4;
            camera.position.set(d, -d, d * 0.7);
            camera.lookAt(0, 0, 0);
            camera.near = maxDim * 0.001;
            camera.far = maxDim * 100;
            camera.updateProjectionMatrix();

            const controls = new OrbitControls(camera, canvas);
            controls.enableDamping = true;
            controls.dampingFactor = 0.08;

            // Render loop.
            function animate() {
                _animFrame = requestAnimationFrame(animate);
                controls.update();
                _renderer.render(scene, camera);
            }
            animate();

            // Keep canvas sized to the container.
            _resizeObserver = new ResizeObserver(() => {
                const w = container.clientWidth || 320;
                const h = Math.round(w * 0.85);
                _renderer.setSize(w, h);
                camera.aspect = w / h;
                camera.updateProjectionMatrix();
            });
            _resizeObserver.observe(container);
        } catch (err) {
            if (signal.aborted) return;
            container.innerHTML = `<p class="preview-error">3D preview failed: ${err.message}</p>`;
        }
    },

    unmount(container) {
        _abortCtrl?.abort();
        _abortCtrl = null;
        if (_animFrame !== null) {
            cancelAnimationFrame(_animFrame);
            _animFrame = null;
        }
        _resizeObserver?.disconnect();
        _resizeObserver = null;
        if (_renderer) {
            _renderer.dispose();
            _renderer = null;
        }
        container.classList.remove("preview-cad");
        container.innerHTML = "";
    },
};

function _ext(relPath) {
    const dot = relPath.toLowerCase().lastIndexOf(".");
    return dot >= 0 ? relPath.toLowerCase().slice(dot) : "";
}

registerPlugin(plugin);
