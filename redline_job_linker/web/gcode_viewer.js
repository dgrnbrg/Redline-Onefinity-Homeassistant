/**
 * High-performance, zero-dependency WebGL 3D G-code toolpath visualizer.
 * Supports orbit, pan, pinch zoom, toolhead cursor rendering, and camera view presets.
 */

const GcodeViewer = (() => {
  let canvas, gl;
  let points = [];
  let lineBuffer = null;
  let toolheadBuffer = null;
  let shaderProgram = null;

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

  function loadGeometry(pointList) {
    points = pointList || [];
    cursorIndex = 0;
    if (points.length === 0) return;

    // Build vertex array with color attributes
    const verts = [];
    let minX = Infinity, maxX = -Infinity;
    let minY = Infinity, maxY = -Infinity;
    let minZ = Infinity, maxZ = -Infinity;

    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
      minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
      minZ = Math.min(minZ, p.z); maxZ = Math.max(maxZ, p.z);

      if (i > 0) {
        const prev = points[i - 1];
        let r = 0.05, g = 0.65, b = 0.95; // Blue-cyan for standard linear cutting G1
        if (p.type === 'rapid') {
          r = 0.95; g = 0.25; b = 0.25; // Red for rapid travel G0
        } else if (p.type === 'arc') {
          r = 0.1; g = 0.85; b = 0.35; // Green for arcs G2/G3
        }

        verts.push(prev.x, prev.y, prev.z, r, g, b);
        verts.push(p.x, p.y, p.z, r, g, b);
      }
    }

    // Grid floor for visual bed context
    const spanX = Math.max(50, maxX - minX);
    const spanY = Math.max(50, maxY - minY);
    modelSize = Math.max(spanX, spanY, Math.max(20, maxZ - minZ));
    modelCenter = [(minX + maxX) / 2 || 0, (minY + maxY) / 2 || 0, (minZ + maxZ) / 2 || 0];

    lineBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, lineBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(verts), gl.STATIC_DRAW);
    lineBuffer.numVertices = verts.length / 6;

    resetCamera();
  }

  function setCursor(idx) {
    if (!points || points.length === 0) return;
    cursorIndex = Math.max(0, Math.min(points.length - 1, Math.round(idx)));
    const p = points[cursorIndex];
    cursorCoords = [p.x, p.y, p.z];
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

    gl.clearColor(0.05, 0.07, 0.12, 1.0); // Dark machine background
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);

    const aspect = canvas.width / Math.max(1, canvas.height);
    const projMat = createPerspectiveMatrix(45, aspect, 1, 5000);

    // View matrix
    const dist = (modelSize * 1.8) / Math.max(0.1, zoom);
    let viewMat = createIdentityMatrix();
    viewMat = translateMatrix(viewMat, panX, panY, -dist);
    viewMat = rotateXMatrix(viewMat, (rotX - 90) * Math.PI / 180);
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
      toolMvp = rotateXMatrix(toolMvp, (rotX - 90) * Math.PI / 180);
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
        rotZ += dx * 0.5;
        rotX = Math.max(0, Math.min(180, rotX + dy * 0.5));
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
        rotZ += dx * 0.5;
        rotX = Math.max(0, Math.min(180, rotX + dy * 0.5));
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
    return multiplyMatrices(trans, m);
  }

  function rotateXMatrix(m, rad) {
    const rot = createIdentityMatrix();
    const c = Math.cos(rad), s = Math.sin(rad);
    rot[5] = c; rot[6] = s; rot[9] = -s; rot[10] = c;
    return multiplyMatrices(rot, m);
  }

  function rotateZMatrix(m, rad) {
    const rot = createIdentityMatrix();
    const c = Math.cos(rad), s = Math.sin(rad);
    rot[0] = c; rot[1] = s; rot[4] = -s; rot[5] = c;
    return multiplyMatrices(rot, m);
  }

  return {
    init,
    loadGeometry,
    setCursor,
    resetCamera,
    setView,
  };
})();
