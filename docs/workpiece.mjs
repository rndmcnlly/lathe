/**
 * workpiece.mjs — Three.js hero cylinder for lathe.tools
 *
 * A slowly rotating brutalist workpiece that responds to mouse presence.
 * Spins up when the mouse moves, coasts down when idle. Zero GPU when stopped.
 *
 * Usage:
 *   <script type="module">
 *     import { initWorkpiece } from './workpiece.mjs'
 *     initWorkpiece('canvas-container')
 *   </script>
 *
 * The container element must exist and have explicit dimensions (height via CSS).
 * A <canvas> is prepended inside it; any existing children (overlays) are preserved.
 *
 * Headless screenshots (requires playwright, uses SwiftShader for WebGL):
 *
 *   # /// script
 *   # requires-python = ">=3.11"
 *   # dependencies = ["playwright"]
 *   # ///
 *   from playwright.sync_api import sync_playwright
 *   with sync_playwright() as p:
 *       browser = p.chromium.launch(headless=True,
 *           args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-webgl"])
 *       page = browser.new_page(viewport={"width": 1280, "height": 900})
 *       page.goto("http://localhost:8000/", wait_until="networkidle")
 *       page.wait_for_timeout(2000)
 *       page.screenshot(path="idle.png")
 *       page.mouse.move(640, 450); page.wait_for_timeout(500)
 *       page.mouse.move(650, 440); page.wait_for_timeout(2000)
 *       page.screenshot(path="active.png")
 *       browser.close()
 */

import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';

export function initWorkpiece(containerId) {
    var container = document.getElementById(containerId);
    if (!container) throw new Error('workpiece: no element #' + containerId);

    // ── Scene ──────────────────────────────────────────────
    var BG = 0x0a0908;
    var scene = new THREE.Scene();
    scene.background = new THREE.Color(BG);
    scene.fog = new THREE.FogExp2(BG, 0.035);

    var camera = new THREE.PerspectiveCamera(
        50, container.clientWidth / container.clientHeight, 0.1, 100
    );
    camera.position.set(0.4, 1.0, 3.2);
    camera.lookAt(0, 0, 0);

    var renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(container.clientWidth, container.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.3;
    container.insertBefore(renderer.domElement, container.firstChild);

    // ── Procedural textures ───────────────────────────────

    function generateDiffuse(w, h) {
        var c = document.createElement('canvas');
        c.width = w; c.height = h;
        var ctx = c.getContext('2d');

        ctx.fillStyle = '#6b6866';
        ctx.fillRect(0, 0, w, h);

        var imgData = ctx.getImageData(0, 0, w, h);
        var d = imgData.data;
        for (var i = 0; i < d.length; i += 4) {
            var n = (Math.random() - 0.5) * 45;
            d[i]     = Math.max(0, Math.min(255, d[i] + n));
            d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + n * 0.95));
            d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + n * 0.85));
        }
        ctx.putImageData(imgData, 0, 0);

        for (var y = 0; y < h; y++) {
            var a = 0.05 + Math.random() * 0.1;
            var v = Math.floor(45 + Math.random() * 40);
            ctx.strokeStyle = 'rgba(' + v + ',' + (v-5) + ',' + (v-12) + ',' + a + ')';
            ctx.lineWidth = 1 + Math.random() * 1.5;
            ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();

            if (y % 6 < 1) {
                ctx.strokeStyle = 'rgba(25, 20, 15, 0.3)';
                ctx.lineWidth = 2 + Math.random() * 2.5;
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
            if (Math.random() < 0.012) {
                ctx.strokeStyle = 'rgba(180, 165, 135, 0.12)';
                ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
        }

        for (var i = 0; i < 25; i++) {
            var x = Math.random() * w, sy = Math.random() * h;
            var len = 100 + Math.random() * 500;
            ctx.strokeStyle = 'rgba(35, 30, 22, ' + (0.06 + Math.random() * 0.1) + ')';
            ctx.lineWidth = 0.5 + Math.random() * 2;
            ctx.beginPath(); ctx.moveTo(x, sy);
            ctx.lineTo(x + (Math.random() - 0.5) * 6, sy + len); ctx.stroke();
        }

        for (var i = 0; i < 25; i++) {
            var x = Math.random() * w, y = Math.random() * h;
            var r = 60 + Math.random() * 200;
            var grad = ctx.createRadialGradient(x, y, 0, x, y, r);
            grad.addColorStop(0, 'rgba(12, 10, 6, ' + (0.08 + Math.random() * 0.15) + ')');
            grad.addColorStop(0.6, 'rgba(12, 10, 6, ' + (0.03 + Math.random() * 0.05) + ')');
            grad.addColorStop(1, 'rgba(12, 10, 6, 0)');
            ctx.fillStyle = grad;
            ctx.fillRect(x - r, y - r, r * 2, r * 2);
        }

        for (var i = 0; i < 15; i++) {
            var x = Math.random() * w;
            var startY = Math.random() * h * 0.3;
            var endY = startY + 200 + Math.random() * 600;
            var grad = ctx.createLinearGradient(x, startY, x, endY);
            grad.addColorStop(0, 'rgba(20, 18, 12, ' + (0.1 + Math.random() * 0.1) + ')');
            grad.addColorStop(1, 'rgba(20, 18, 12, 0)');
            ctx.strokeStyle = grad;
            ctx.lineWidth = 3 + Math.random() * 8;
            ctx.beginPath(); ctx.moveTo(x, startY);
            ctx.lineTo(x + (Math.random() - 0.5) * 15, endY); ctx.stroke();
        }

        for (var i = 0; i < 6; i++) {
            var bandY = Math.random() * h, bandH = 80 + Math.random() * 250;
            var darker = Math.random() > 0.5;
            ctx.fillStyle = darker
                ? 'rgba(10, 8, 5, ' + (0.08 + Math.random() * 0.12) + ')'
                : 'rgba(120, 115, 105, ' + (0.06 + Math.random() * 0.08) + ')';
            ctx.fillRect(0, bandY, w, bandH);
        }

        for (var i = 0; i < 5; i++) {
            var x = Math.random() * w, y = Math.random() * h;
            var r = 30 + Math.random() * 70;
            var grad = ctx.createRadialGradient(x, y, 0, x, y, r);
            grad.addColorStop(0, 'rgba(140, 100, 50, ' + (0.03 + Math.random() * 0.03) + ')');
            grad.addColorStop(1, 'rgba(140, 100, 50, 0)');
            ctx.fillStyle = grad;
            ctx.fillRect(x - r, y - r, r * 2, r * 2);
        }

        var tex = new THREE.CanvasTexture(c);
        tex.wrapS = THREE.RepeatWrapping; tex.wrapT = THREE.RepeatWrapping;
        tex.repeat.set(1, 4);
        return tex;
    }

    function generateBump(w, h) {
        var c = document.createElement('canvas');
        c.width = w; c.height = h;
        var ctx = c.getContext('2d');

        ctx.fillStyle = '#808080';
        ctx.fillRect(0, 0, w, h);

        for (var y = 0; y < h; y++) {
            if (y % 6 < 2) {
                var v = 45 + Math.random() * 25;
                ctx.strokeStyle = 'rgb(' + v + ',' + v + ',' + v + ')';
                ctx.lineWidth = 2;
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
            if (Math.random() < 0.25) {
                var v = 105 + Math.random() * 50;
                ctx.strokeStyle = 'rgb(' + v + ',' + v + ',' + v + ')';
                ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
            if (Math.random() < 0.005) {
                ctx.strokeStyle = 'rgb(25, 25, 25)';
                ctx.lineWidth = 3 + Math.random() * 4;
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
        }

        for (var i = 0; i < 500; i++) {
            var x = Math.random() * w, y = Math.random() * h;
            var r = 1 + Math.random() * 4;
            ctx.fillStyle = 'rgba(30,30,30,' + (0.2 + Math.random() * 0.5) + ')';
            ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
        }

        var tex = new THREE.CanvasTexture(c);
        tex.wrapS = THREE.RepeatWrapping; tex.wrapT = THREE.RepeatWrapping;
        tex.repeat.set(1, 4);
        return tex;
    }

    function generateRoughness(w, h) {
        var c = document.createElement('canvas');
        c.width = w; c.height = h;
        var ctx = c.getContext('2d');

        ctx.fillStyle = '#c0c0c0';
        ctx.fillRect(0, 0, w, h);

        for (var y = 0; y < h; y++) {
            if (y % 6 < 2) {
                ctx.strokeStyle = 'rgba(60, 60, 60, 0.4)';
                ctx.lineWidth = 2;
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
            if (Math.random() < 0.15) {
                var v = Math.random() > 0.5 ? 180 + Math.random() * 40 : 80 + Math.random() * 40;
                ctx.strokeStyle = 'rgba(' + v + ',' + v + ',' + v + ', 0.2)';
                ctx.lineWidth = 1 + Math.random() * 2;
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            }
        }

        for (var i = 0; i < 15; i++) {
            var x = Math.random() * w, y = Math.random() * h;
            var r = 20 + Math.random() * 80;
            var grad = ctx.createRadialGradient(x, y, 0, x, y, r);
            grad.addColorStop(0, 'rgba(40, 40, 40, ' + (0.15 + Math.random() * 0.2) + ')');
            grad.addColorStop(1, 'rgba(40, 40, 40, 0)');
            ctx.fillStyle = grad; ctx.fillRect(x - r, y - r, r * 2, r * 2);
        }

        for (var i = 0; i < 10; i++) {
            var x = Math.random() * w, y = Math.random() * h;
            var r = 30 + Math.random() * 100;
            var grad = ctx.createRadialGradient(x, y, 0, x, y, r);
            grad.addColorStop(0, 'rgba(220, 220, 220, ' + (0.1 + Math.random() * 0.15) + ')');
            grad.addColorStop(1, 'rgba(220, 220, 220, 0)');
            ctx.fillStyle = grad; ctx.fillRect(x - r, y - r, r * 2, r * 2);
        }

        var imgData = ctx.getImageData(0, 0, w, h);
        var d = imgData.data;
        for (var i = 0; i < d.length; i += 4) {
            var n = (Math.random() - 0.5) * 30;
            d[i] = Math.max(0, Math.min(255, d[i] + n));
            d[i + 1] = d[i]; d[i + 2] = d[i];
        }
        ctx.putImageData(imgData, 0, 0);

        var tex = new THREE.CanvasTexture(c);
        tex.wrapS = THREE.RepeatWrapping; tex.wrapT = THREE.RepeatWrapping;
        tex.repeat.set(1, 4);
        return tex;
    }

    // ── Geometry + material ───────────────────────────────

    var cylinderGeo = new THREE.CylinderGeometry(1.3, 1.3, 12, 64, 1, false);

    var TEX = 1024;
    var cylinderMat = new THREE.MeshStandardMaterial({
        map: generateDiffuse(TEX, TEX),
        bumpMap: generateBump(TEX, TEX),
        bumpScale: 0.07,
        roughnessMap: generateRoughness(TEX, TEX),
        roughness: 1.0,
        metalness: 0.18,
        color: new THREE.Color(0x757068),
    });

    var cylinder = new THREE.Mesh(cylinderGeo, cylinderMat);
    cylinder.rotation.z = Math.PI / 2;
    scene.add(cylinder);

    // ── Lighting ──────────────────────────────────────────
    // All lights are always present. Spinning brightens them subtly.
    // [rest, active] intensity pairs per light.

    var SUN_REST = 3.5, SUN_ACTIVE = 4.2;
    var sun = new THREE.DirectionalLight(0xffd8a8, SUN_REST);
    sun.position.set(3, 5, 3);
    scene.add(sun);

    var FILL_REST = 0.1, FILL_ACTIVE = 0.2;
    var fill = new THREE.DirectionalLight(0x556677, FILL_REST);
    fill.position.set(-2, -3, 1);
    scene.add(fill);

    var RIM_REST = 0.5, RIM_ACTIVE = 0.7;
    var rim = new THREE.DirectionalLight(0xd4a050, RIM_REST);
    rim.position.set(-1, 0.5, -5);
    scene.add(rim);

    var AMBIENT_REST = 0.25, AMBIENT_ACTIVE = 0.4;
    var ambient = new THREE.AmbientLight(0x201810, AMBIENT_REST);
    scene.add(ambient);

    var WORKLIGHT_REST = 2.2, WORKLIGHT_ACTIVE = 2.8;
    var WORKLIGHT_ANGLE_TIGHT = Math.PI / 5;
    var WORKLIGHT_ANGLE_WIDE  = Math.PI / 3.5;

    var worklight = new THREE.SpotLight(0xffcc88, WORKLIGHT_REST, 12, WORKLIGHT_ANGLE_TIGHT, 0.6, 1);
    worklight.position.set(-2, 2, 4);
    worklight.target.position.set(0, 0, 0);
    scene.add(worklight);
    scene.add(worklight.target);

    // ── State machine ─────────────────────────────────────
    //
    //  IDLE ──(mousemove)──> SPIN_UP ──(1.5s)──> ACTIVE
    //                            ^                  │
    //                            │            (3s no mouse)
    //                            │                  v
    //                         SPIN_DOWN <───────────┘
    //                            │
    //                          (2.5s)
    //                            v
    //                          IDLE  (kill RAF)

    var STATE = { IDLE: 0, SPIN_UP: 1, ACTIVE: 2, SPIN_DOWN: 3 };
    var state = STATE.IDLE;
    var stateT = 0;
    var spinFactor = 0;
    var lastMouseMove = 0;
    var rafId = null;
    var rotationAngle = 0;

    var SPIN_UP_DUR  = 1.5;
    var SPIN_DOWN_DUR = 2.5;
    var IDLE_TIMEOUT = 3.0;
    var TARGET_RPS = 3.0;
    var RAD_PER_SEC = TARGET_RPS * Math.PI * 2;

    var BUMP_REST = 0.07;
    var BUMP_SPIN = 0.0;

    function transition(s) { state = s; stateT = 0; }

    function easeInOut(t) {
        return t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2;
    }

    // ── Input ─────────────────────────────────────────────

    var mouseX = 0, mouseY = 0;

    container.addEventListener('mousemove', function(e) {
        var rect = container.getBoundingClientRect();
        mouseX = ((e.clientX - rect.left) / rect.width - 0.5) * 2;
        mouseY = ((e.clientY - rect.top) / rect.height - 0.5) * 2;
        lastMouseMove = performance.now() / 1000;

        if (state === STATE.IDLE) {
            transition(STATE.SPIN_UP);
            startLoop();
        } else if (state === STATE.SPIN_DOWN) {
            stateT = spinFactor * SPIN_UP_DUR;
            state = STATE.SPIN_UP;
            startLoop();
        }
    });

    // ── Loop ──────────────────────────────────────────────

    var basePos = camera.position.clone();
    var prevTime = performance.now() / 1000;
    var lastRenderTime = performance.now() / 1000;

    function animate() {
        var now = performance.now() / 1000;
        var dt = Math.min(now - prevTime, 0.1);
        prevTime = now;
        stateT += dt;

        switch (state) {
            case STATE.SPIN_UP:
                spinFactor = easeInOut(Math.min(stateT / SPIN_UP_DUR, 1));
                if (stateT >= SPIN_UP_DUR) {
                    spinFactor = 1;
                    transition(STATE.ACTIVE);
                    lastMouseMove = now;
                }
                break;
            case STATE.ACTIVE:
                spinFactor = 1;
                if (now - lastMouseMove > IDLE_TIMEOUT) transition(STATE.SPIN_DOWN);
                break;
            case STATE.SPIN_DOWN:
                spinFactor = 1 - easeInOut(Math.min(stateT / SPIN_DOWN_DUR, 1));
                if (stateT >= SPIN_DOWN_DUR) {
                    spinFactor = 0;
                    transition(STATE.IDLE);
                    render();
                    rafId = null;
                    return;
                }
                break;
        }

        render();
        rafId = requestAnimationFrame(animate);
    }

    function render() {
        var now = performance.now() / 1000;
        var rdt = Math.min(now - lastRenderTime, 0.1);
        lastRenderTime = now;

        // Motor pulse
        var speedMod = 1.0;
        if (spinFactor > 0.9) {
            speedMod = 1.0 + Math.sin(now * 4.7) * 0.03 + Math.sin(now * 7.1) * 0.015;
        }

        rotationAngle += RAD_PER_SEC * spinFactor * speedMod * rdt;
        cylinder.rotation.x = rotationAngle;

        // All lights lerp between rest and active
        sun.intensity = SUN_REST + (SUN_ACTIVE - SUN_REST) * spinFactor;
        fill.intensity = FILL_REST + (FILL_ACTIVE - FILL_REST) * spinFactor;
        rim.intensity = RIM_REST + (RIM_ACTIVE - RIM_REST) * spinFactor;
        ambient.intensity = AMBIENT_REST + (AMBIENT_ACTIVE - AMBIENT_REST) * spinFactor;
        worklight.intensity = WORKLIGHT_REST + (WORKLIGHT_ACTIVE - WORKLIGHT_REST) * spinFactor;
        worklight.angle = WORKLIGHT_ANGLE_TIGHT
            + (WORKLIGHT_ANGLE_WIDE - WORKLIGHT_ANGLE_TIGHT) * spinFactor;
        worklight.penumbra = 0.6 + spinFactor * 0.2;

        // Analytic motion blur
        cylinderMat.bumpScale = BUMP_REST + (BUMP_SPIN - BUMP_REST) * spinFactor;
        cylinderMat.roughness = 1.0 - spinFactor * 0.25;

        // Parallax
        camera.position.x = basePos.x + mouseX * 0.2;
        camera.position.y = basePos.y - mouseY * 0.12;
        camera.lookAt(0, 0, 0);

        renderer.render(scene, camera);
    }

    function startLoop() {
        if (rafId !== null) return;
        prevTime = performance.now() / 1000;
        rafId = requestAnimationFrame(animate);
    }

    // Initial static render
    render();

    // Resize
    window.addEventListener('resize', function() {
        camera.aspect = container.clientWidth / container.clientHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(container.clientWidth, container.clientHeight);
        render();
    });
}
