/**
 * High-performance, zero-dependency WebGL 3D G-code toolpath visualizer.
 * Supports orbit, pan, pinch zoom, toolhead cursor rendering, and camera view presets.
 */

const GcodeViewer = (() => {
  let canvas, gl;
  let points = [];
  let lineBuffer = null;
  let rawVertices = null;
  let currentSafeZ = 0.0;
  let currentExtents = { minX: 0, maxX: 100, minY: 0, maxY: 100, minZ: -5, maxZ: 5 };
  let toolheadBuffer = null;
  let shaderProgram = null;
  let highlightStartLine = null;
  let highlightEndLine = null;

  // Camera state
  let rotX = 45; // degrees
  let rotZ = 45; // degrees
  let panX = 0;
  let panY = 0;
  let zoom = 1.0;
  let modelCenter = [0, 0, 0];
  let modelSize = 100;

  // Active cursor
  let cursorIndex = 0;
  let cursorCoords = [0, 0, 0];

  // Mouse / Touch interaction
  let isDragging = false;
  let isPanning = false;
  let lastMouseX = 0;
  let lastMouseY = 0;
  let initialPinchDistance = null;

  const VS_SOURCE = `
    attribute vec3 aPosition;
    attribute vec3 aColor;
    uniform mat4 uMatrix;
    varying vec3 vColor;
    void main() {
      vColor = aColor;
      gl_Position = uMatrix * vec4(aPosition, 1.0);
    }
  `;

  const FS_SOURCE = `
    precision mediump float;
    varying vec3 vColor;
    void main() {
      gl_FragColor = vec4(vColor, 1.0);
    }
  `;

  function init(canvasId) {
    canvas = document.getElementById(canvasId);
    if (!canvas) return;

    gl = canvas.getContext('webgl', { antialias: true, alpha: false });
    if (!gl) {
      console.warn("WebGL not supported on this browser.");
      return;
    }

    initShaders();
    setupEventListeners();
    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);
    requestAnimationFrame(render);
  }

  function initShaders() {
    const vs = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vs, VS_SOURCE);
    gl.compileShader(vs);

    const fs = gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(fs, FS_SOURCE);
    gl.compileShader(fs);

    shaderProgram = gl.createProgram();
    gl.attachShader(shaderProgram, vs);
    gl.attachShader(shaderProgram, fs);
    gl.linkProgram(shaderProgram);

    shaderProgram.aPosition = gl.getAttribLocation(shaderProgram, "aPosition");
    shaderProgram.aColor = gl.getAttribLocation(shaderProgram, "aColor");
    shaderProgram.uMatrix = gl.getUniformLocation(shaderProgram, "uMatrix");

    initToolheadGeometry();
  }

  function initToolheadGeometry() {
    // Inverted cone representing CNC router / spindle tool tip
    const coneVerts = [];
    const segments = 12;
    const r = 3.0;
    const h = 12.0;

    for (let i = 0; i < segments; i++) {
      const th1 = (i / segments) * Math.PI * 2;
      const th2 = ((i + 1) / segments) * Math.PI * 2;
      const x1 = Math.cos(th1) * r;
      const y1 = Math.sin(th1) * r;
      const x2 = Math.cos(th2) * r;
      const y2 = Math.sin(th2) * r;

      // Lines from tip (0,0,0) to circle rim at Z = h
      coneVerts.push(0, 0, 0,  1.0, 0.8, 0.0); // amber
      coneVerts.push(x1, y1, h, 1.0, 0.8, 0.0);

      // Circle rim segment
      coneVerts.push(x1, y1, h, 1.0, 0.8, 0.0);
      coneVerts.push(x2, y2, h, 1.0, 0.8, 0.0);

      // Spindle shaft extension
      coneVerts.push(x1, y1, h,      0.7, 0.7, 0.7);
      coneVerts.push(x1, y1, h + 15, 0.7, 0.7, 0.7);
    }

    toolheadBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, toolheadBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(coneVerts), gl.STATIC_DRAW);
  }

  function _getSegmentBaseColor(prev, p) {
    let r = 0.05, g = 0.65, b = 0.95; // Blue-cyan for standard linear cutting G1
    if (p.type === 'rapid') {
      const isOpChange = Math.max(prev.z || 0, p.z || 0) >= currentSafeZ;
      if (isOpChange) {
        r = 0.98; g = 0.75; b = 0.14; // Amber/gold for op change safe Z retracts
      } else {
        r = 0.95; g = 0.25; b = 0.25; // Red for intra-op stepover rapids
      }
    } else if (p.type === 'arc') {
      r = 0.1; g = 0.85; b = 0.35; // Green for arcs G2/G3
    }
    return [r, g, b];
  }

  function applyColors() {
    if (!rawVertices || !gl || !lineBuffer || points.length <= 1) return;

    const hasHighlight = (highlightStartLine !== null && highlightEndLine !== null);

    for (let i = 1; i < points.length; i++) {
      const p = points[i];
      const prev = points[i - 1];
      let [r, g, b] = _getSegmentBaseColor(prev, p);

      if (hasHighlight) {
        const inSlot = (p.line >= highlightStartLine && p.line <= highlightEndLine);
        if (inSlot) {
          // Vivid, bright, high-contrast glow for the selected slot
          if (p.type === 'linear') {
            r = 0.15; g = 0.88; b = 1.0; // Glowing neon cyan
          } else if (p.type === 'arc') {
            r = 0.2; g = 1.0; b = 0.45;  // Neon green
          } else if (p.type === 'rapid') {
            const isOpChange = Math.max(prev.z || 0, p.z || 0) >= currentSafeZ;
            if (isOpChange) {
              r = 1.0; g = 0.85; b = 0.2; // Vivid gold
            } else {
              r = 1.0; g = 0.3; b = 0.3;  // Bright red
            }
          }
        } else {
          // Dim / ghost background toolpaths
          r = 0.12; g = 0.18; b = 0.26;
        }
      }

      const baseIdx = (i - 1) * 12;
      rawVertices[baseIdx + 3] = r;
      rawVertices[baseIdx + 4] = g;
      rawVertices[baseIdx + 5] = b;

      rawVertices[baseIdx + 9] = r;
      rawVertices[baseIdx + 10] = g;
      rawVertices[baseIdx + 11] = b;
    }

    gl.bindBuffer(gl.ARRAY_BUFFER, lineBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, rawVertices, gl.STATIC_DRAW);
  }

  function setHighlightRange(startLine, endLine) {
    highlightStartLine = (typeof startLine === 'number' && !isNaN(startLine)) ? startLine : null;
    highlightEndLine = (typeof endLine === 'number' && !isNaN(endLine)) ? endLine : null;
    applyColors();
  }

  function loadGeometry(pointList, safeZThreshold) {
    points = pointList || [];
    cursorIndex = 0;
    if (typeof safeZThreshold === 'number') {
      currentSafeZ = safeZThreshold;
    }
    if (points.length === 0) return;

    // Build vertex array with color attributes
    const verts = [];
    let minX = Infinity, maxX = -Infinity;
    let minY = Infinity, maxY = -Infinity;
    let minZ = Infinity, maxZ = -Infinity;

    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      if (typeof p.x === 'number' && isFinite(p.x)) {
        minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
      }
      if (typeof p.y === 'number' && isFinite(p.y)) {
        minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
      }
      if (typeof p.z === 'number' && isFinite(p.z)) {
        minZ = Math.min(minZ, p.z); maxZ = Math.max(maxZ, p.z);
      }

      if (i > 0) {
        const prev = points[i - 1];
        let [r, g, b] = _getSegmentBaseColor(prev, p);
        if (highlightStartLine !== null && highlightEndLine !== null) {
          const inSlot = (p.line >= highlightStartLine && p.line <= highlightEndLine);
          if (inSlot) {
            if (p.type === 'linear') { r = 0.15; g = 0.88; b = 1.0; }
            else if (p.type === 'arc') { r = 0.2; g = 1.0; b = 0.45; }
            else if (p.type === 'rapid') { r = 1.0; g = 0.85; b = 0.2; }
          } else {
            r = 0.12; g = 0.18; b = 0.26;
          }
        }

        verts.push(prev.x, prev.y, prev.z, r, g, b);
        verts.push(p.x, p.y, p.z, r, g, b);
      }
    }

    if (!isFinite(minX)) { minX = 0; maxX = 100; }
    if (!isFinite(minY)) { minY = 0; maxY = 100; }
    if (!isFinite(minZ)) { minZ = -5; maxZ = 5; }

    currentExtents = { minX, maxX, minY, maxY, minZ, maxZ };

    const spanX = Math.max(20, maxX - minX);
    const spanY = Math.max(20, maxY - minY);
    const spanZ = Math.max(10, maxZ - minZ);
    modelSize = Math.max(spanX, spanY, spanZ);
    modelCenter = [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2];

    rawVertices = new Float32Array(verts);
    lineBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, lineBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, rawVertices, gl.STATIC_DRAW);
    lineBuffer.numVertices = verts.length / 6;

    if (points.length > 0) {
      cursorCoords = [points[0].x || 0, points[0].y || 0, points[0].z || 0];
    }
    resetCamera();
  }

  function updateSafeZThreshold(newThreshold) {
    if (typeof newThreshold === 'number') {
      currentSafeZ = newThreshold;
    }
    applyColors();
  }

  function getExtents() {
    return { ...currentExtents, safeZ: currentSafeZ };
  }

  function setCursor(idx, customCoords) {
    if (!points || points.length === 0) return;
    cursorIndex = Math.max(0, Math.min(points.length - 1, Math.round(idx)));
    if (customCoords && customCoords.length >= 3 && typeof customCoords[0] === 'number') {
      cursorCoords = [customCoords[0], customCoords[1], customCoords[2]];
    } else {
      const p = points[cursorIndex];
      cursorCoords = [p.x, p.y, p.z];
    }
  }

  function resetCamera() {
    rotX = 55;
    rotZ = -45;
    panX = 0;
    panY = 0;
    zoom = 1.0;
  }

  function setView(viewName) {
    panX = 0; panY = 0;
    if (viewName === 'top') {
      rotX = 0; rotZ = 0;
    } else if (viewName === 'front') {
      rotX = 90; rotZ = 0;
    } else if (viewName === 'iso') {
      rotX = 55; rotZ = -45;
    }
  }

  function resizeCanvas() {
    if (!canvas || !gl) return;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
  }

  function render() {
    if (!gl || !shaderProgram) return;

    resizeCanvas();

    gl.clearColor(0.05, 0.07, 0.12, 1.0); // Dark machine background
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);

    const aspect = canvas.width / Math.max(1, canvas.height);
    const projMat = createPerspectiveMatrix(45, aspect, 1, 5000);

    // View matrix: Center at origin -> rotate around Z -> tilt around X -> pan & back off in Z
    const dist = (modelSize * 1.8) / Math.max(0.1, zoom);
    let viewMat = createIdentityMatrix();
    viewMat = translateMatrix(viewMat, panX, panY, -dist);
    viewMat = rotateXMatrix(viewMat, -rotX * Math.PI / 180);
    viewMat = rotateZMatrix(viewMat, rotZ * Math.PI / 180);
    viewMat = translateMatrix(viewMat, -modelCenter[0], -modelCenter[1], -modelCenter[2]);

    const mvp = multiplyMatrices(projMat, viewMat);

    gl.useProgram(shaderProgram);
    gl.uniformMatrix4fv(shaderProgram.uMatrix, false, mvp);

    // 1. Render toolpath lines
    if (lineBuffer && lineBuffer.numVertices > 0) {
      gl.bindBuffer(gl.ARRAY_BUFFER, lineBuffer);
      gl.enableVertexAttribArray(shaderProgram.aPosition);
      gl.vertexAttribPointer(shaderProgram.aPosition, 3, gl.FLOAT, false, 24, 0);
      gl.enableVertexAttribArray(shaderProgram.aColor);
      gl.vertexAttribPointer(shaderProgram.aColor, 3, gl.FLOAT, false, 24, 12);
      gl.drawArrays(gl.LINES, 0, lineBuffer.numVertices);
    }

    // 2. Render toolhead cursor at current scrubbed position
    if (toolheadBuffer && points.length > 0) {
      let toolMvp = createIdentityMatrix();
      toolMvp = translateMatrix(toolMvp, panX, panY, -dist);
      toolMvp = rotateXMatrix(toolMvp, -rotX * Math.PI / 180);
      toolMvp = rotateZMatrix(toolMvp, rotZ * Math.PI / 180);
      toolMvp = translateMatrix(
        toolMvp,
        cursorCoords[0] - modelCenter[0],
        cursorCoords[1] - modelCenter[1],
        cursorCoords[2] - modelCenter[2]
      );
      toolMvp = multiplyMatrices(projMat, toolMvp);
      gl.uniformMatrix4fv(shaderProgram.uMatrix, false, toolMvp);

      gl.bindBuffer(gl.ARRAY_BUFFER, toolheadBuffer);
      gl.enableVertexAttribArray(shaderProgram.aPosition);
      gl.vertexAttribPointer(shaderProgram.aPosition, 3, gl.FLOAT, false, 24, 0);
      gl.enableVertexAttribArray(shaderProgram.aColor);
      gl.vertexAttribPointer(shaderProgram.aColor, 3, gl.FLOAT, false, 24, 12);
      gl.drawArrays(gl.LINES, 0, 72);
    }

    requestAnimationFrame(render);
  }

  function setupEventListeners() {
    canvas.addEventListener('mousedown', (e) => {
      isDragging = e.button === 0;
      isPanning = e.button === 2;
      lastMouseX = e.clientX;
      lastMouseY = e.clientY;
    });

    window.addEventListener('mouseup', () => {
      isDragging = false;
      isPanning = false;
    });

    canvas.addEventListener('contextmenu', (e) => e.preventDefault());

    canvas.addEventListener('mousemove', (e) => {
      if (!isDragging && !isPanning) return;
      const dx = e.clientX - lastMouseX;
      const dy = e.clientY - lastMouseY;
      lastMouseX = e.clientX;
      lastMouseY = e.clientY;

      if (isDragging) {
        rotZ -= dx * 0.5;
        rotX = Math.max(0, Math.min(180, rotX - dy * 0.5));
      } else if (isPanning) {
        const factor = modelSize / (zoom * 600);
        panX += dx * factor;
        panY -= dy * factor;
      }
    });

    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.15 : 0.85;
      zoom = Math.max(0.1, Math.min(25.0, zoom * zoomFactor));
    }, { passive: false });

    // Touch controls for mobile / tablet
    canvas.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1) {
        isDragging = true;
        lastMouseX = e.touches[0].clientX;
        lastMouseY = e.touches[0].clientY;
      } else if (e.touches.length === 2) {
        isDragging = false;
        initialPinchDistance = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        );
      }
    });

    canvas.addEventListener('touchmove', (e) => {
      if (e.touches.length === 1 && isDragging) {
        const dx = e.touches[0].clientX - lastMouseX;
        const dy = e.touches[0].clientY - lastMouseY;
        lastMouseX = e.touches[0].clientX;
        lastMouseY = e.touches[0].clientY;
        rotZ -= dx * 0.5;
        rotX = Math.max(0, Math.min(180, rotX - dy * 0.5));
      } else if (e.touches.length === 2 && initialPinchDistance) {
        const dist = Math.hypot(
          e.touches[0].clientX - e.touches[1].clientX,
          e.touches[0].clientY - e.touches[1].clientY
        );
        const ratio = dist / initialPinchDistance;
        zoom = Math.max(0.1, Math.min(25.0, zoom * ratio));
        initialPinchDistance = dist;
      }
    }, { passive: true });

    canvas.addEventListener('touchend', () => {
      isDragging = false;
      initialPinchDistance = null;
    });
  }

  // Basic Matrix Math
  function createIdentityMatrix() {
    return new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]);
  }

  function createPerspectiveMatrix(fovyDeg, aspect, near, far) {
    const f = 1.0 / Math.tan((fovyDeg * Math.PI) / 360.0);
    const nf = 1.0 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, (2 * far * near) * nf, 0
    ]);
  }

  function multiplyMatrices(a, b) {
    const out = new Float32Array(16);
    for (let i = 0; i < 4; i++) {
      for (let j = 0; j < 4; j++) {
        let sum = 0;
        for (let k = 0; k < 4; k++) sum += a[k * 4 + i] * b[j * 4 + k];
        out[j * 4 + i] = sum;
      }
    }
    return out;
  }

  function translateMatrix(m, tx, ty, tz) {
    const trans = createIdentityMatrix();
    trans[12] = tx; trans[13] = ty; trans[14] = tz;
    return multiplyMatrices(m, trans);
  }

  function rotateXMatrix(m, rad) {
    const rot = createIdentityMatrix();
    const c = Math.cos(rad), s = Math.sin(rad);
    rot[5] = c; rot[6] = s; rot[9] = -s; rot[10] = c;
    return multiplyMatrices(m, rot);
  }

  function rotateZMatrix(m, rad) {
    const rot = createIdentityMatrix();
    const c = Math.cos(rad), s = Math.sin(rad);
    rot[0] = c; rot[1] = s; rot[4] = -s; rot[5] = c;
    return multiplyMatrices(m, rot);
  }

  return {
    init,
    loadGeometry,
    setCursor,
    resetCamera,
    setView,
    updateSafeZThreshold,
    setHighlightRange,
    getExtents,
    resizeCanvas,
  };
})();
