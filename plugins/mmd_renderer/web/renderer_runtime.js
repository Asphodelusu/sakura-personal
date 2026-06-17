// renderer_runtime.js — three.js MMD 渲染运行时。
//
// 用 vendored three.js（本地，不走 CDN）初始化 WebGL 场景，并用 MMDLoader
// 加载本地 pmx 模型。本阶段实现静态模型显示（不含 vmd 动作/物理），动作与口型
// 的实际驱动留后续接入 MMDAnimationHelper。
(function () {
  "use strict";

  var Runtime = {
    initialized: false,
    canvas: null,
    renderer: null,
    scene: null,
    camera: null,
    mesh: null,
    target: null,
    _boxProbe: null,
    _clearColor: [0, 0, 0, 0],
    _needsRender: false,
    _warmupFrames: 0,
    // 视线追踪：头/首/眼骨与其初始 quaternion 缓存（换模型时由 _resetRigState 失效）。
    _gazeReady: false,
    _headBone: null,
    _neckBone: null,
    _eyeBones: null,
    _boneInit: null,
    // morph 分组状态：分别记录表情组写入的下标与口型下标，重设时先清零避免残留/互相覆盖。
    _expressionIndices: null,
    _lipIndex: -1,
    // 已警告过的缺失 morph 名（避免逐帧刷屏）。
    _warnedMorphs: null,

    init: function (canvas) {
      if (this.initialized) return;
      this.canvas = canvas;
      if (typeof THREE === "undefined") {
        console.warn("[SakuraMMD] THREE 未加载，无法初始化渲染");
        return;
      }

      var w = window.innerWidth || 400;
      var h = window.innerHeight || 600;

      this.renderer = new THREE.WebGLRenderer({
        canvas: canvas,
        alpha: true,
        antialias: true,
        premultipliedAlpha: false,
        preserveDrawingBuffer: true
      });
      // 角色层背景保持透明，由 Qt 主窗口上的气泡/输入栏负责前景 UI。
      this.renderer.setClearColor(0x000000, 0);
      if (THREE.sRGBEncoding !== undefined) {
        this.renderer.outputEncoding = THREE.sRGBEncoding;
      }
      this.renderer.setPixelRatio(window.devicePixelRatio || 1);
      this.renderer.setSize(w, h, false);

      this.scene = new THREE.Scene();
      this.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 2000);
      this.camera.position.set(0, 12, 35);   // MMD 模型约 20 单位高，默认机位
      this.target = new THREE.Vector3(0, 10, 0);
      this.camera.lookAt(this.target);

      // 偏亮的环境光 + 一盏主方向光，避免 MMD 卡通材质过暗。
      this.scene.add(new THREE.AmbientLight(0xffffff, 0.95));
      var dir = new THREE.DirectionalLight(0xffffff, 0.55);
      dir.position.set(0.5, 1.0, 0.8);
      this.scene.add(dir);

      this.initialized = true;
      var self = this;
      window.addEventListener("resize", function () { self._resize(); });
      this._requestRender();
      this._animate();
      console.log("[SakuraMMD] runtime initialized (three r" + THREE.REVISION + ")");
    },

    _resize: function () {
      if (!this.renderer || !this.camera) return;
      var w = window.innerWidth || 400;
      var h = window.innerHeight || 600;
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(w, h, false);
      this._requestRender();
    },

    _animate: function () {
      var self = this;
      requestAnimationFrame(function () { self._animate(); });
      if ((this._needsRender || this._warmupFrames > 0) && this.renderer && this.scene && this.camera) {
        this._needsRender = false;
        if (this._warmupFrames > 0) {
          this._warmupFrames -= 1;
        }
        this.renderer.render(this.scene, this.camera);
      }
    },

    _requestRender: function () {
      this._needsRender = true;
    },

    _startWarmupRender: function (frames) {
      this._warmupFrames = Math.max(this._warmupFrames, frames || 1);
      this._requestRender();
    },

    _asMaterialArray: function (materials) {
      if (!materials) return [];
      return Array.isArray(materials) ? materials : [materials];
    },

    _prepareMeshForDisplay: function (mesh) {
      mesh.visible = true;
      mesh.frustumCulled = false;
      if (typeof mesh.pose === "function") {
        try {
          mesh.pose();
        } catch (err) {
          console.warn("[SakuraMMD] mesh.pose failed: " + (err && err.message ? err.message : String(err)));
        }
      }

      var materials = this._asMaterialArray(mesh.material);
      for (var i = 0; i < materials.length; i += 1) {
        var mat = materials[i];
        if (!mat) continue;
        mat.visible = true;
        mat.opacity = 1;
        mat.transparent = false;
        mat.alphaTest = 0;
        mat.side = THREE.DoubleSide;
        mat.blending = THREE.NoBlending;
        mat.depthTest = true;
        mat.depthWrite = true;
        if ("skinning" in mat) mat.skinning = true;
        if ("morphTargets" in mat && mesh.morphTargetInfluences) mat.morphTargets = true;
        mat.needsUpdate = true;
      }
      mesh.updateMatrixWorld(true);
    },

    _applyFallbackMaterial: function (mesh) {
      var sourceMaterials = this._asMaterialArray(mesh.material);
      var compatibleMaterials = [];
      for (var i = 0; i < sourceMaterials.length; i += 1) {
        compatibleMaterials.push(this._createCompatibleMaterial(sourceMaterials[i], mesh));
      }
      mesh.material = compatibleMaterials.length > 0
        ? (compatibleMaterials.length === 1 ? compatibleMaterials[0] : compatibleMaterials)
        : this._createSolidDebugMaterial(mesh);
      mesh.visible = true;
      mesh.frustumCulled = false;
      mesh.updateMatrixWorld(true);
    },

    _applySolidDebugMaterial: function (mesh) {
      mesh.material = this._createSolidDebugMaterial(mesh);
      mesh.visible = true;
      mesh.frustumCulled = false;
      mesh.updateMatrixWorld(true);
    },

    _createCompatibleMaterial: function (source, mesh) {
      source = source || {};
      var color = new THREE.Color(0xffffff);
      if (source.color && !source.map) {
        color.copy(source.color);
      }
      var map = source.map || null;
      var material = new THREE.MeshBasicMaterial({
        color: color,
        map: this._textureHasImage(map) ? map : null,
        side: THREE.DoubleSide,
        transparent: false,
        opacity: 1,
        alphaTest: 0,
        blending: THREE.NoBlending,
        depthTest: true,
        depthWrite: true,
        fog: false
      });
      material.name = (source.name || "material") + "_SakuraCompat";
      material.skinning = !!mesh.skeleton;
      material.morphTargets = !!mesh.morphTargetInfluences;
      material.toneMapped = false;
      material.userData = material.userData || {};
      material.userData.sakuraPendingMap = map;
      this._bindTextureWhenReady(material, map);
      material.needsUpdate = true;
      return material;
    },

    _textureHasImage: function (texture) {
      var image = texture && texture.image;
      return !!(image && (image.width > 0 || image.naturalWidth > 0 || image.data));
    },

    _bindTextureWhenReady: function (material, texture) {
      if (!texture) return;
      var self = this;
      if (this._textureHasImage(texture)) {
        this._configureTextureForDisplay(texture);
        material.map = texture;
        material.needsUpdate = true;
        return;
      }
      if (Array.isArray(texture.readyCallbacks)) {
        texture.readyCallbacks.push(function (readyTexture) {
          readyTexture = readyTexture || texture;
          self._configureTextureForDisplay(readyTexture);
          material.map = readyTexture;
          material.userData.sakuraPendingMap = null;
          material.needsUpdate = true;
          self._requestRender();
          console.log("[SakuraMMD] texture ready for material " + material.name);
        });
      } else {
        console.warn("[SakuraMMD] texture has no image and no readyCallbacks for material " + material.name);
      }
    },

    _configureTextureForDisplay: function (texture) {
      if (!texture || !this._textureHasImage(texture)) return;
      if (THREE.sRGBEncoding !== undefined) {
        texture.encoding = THREE.sRGBEncoding;
      }
      texture.premultiplyAlpha = false;
      texture.needsUpdate = true;
    },

    _attachPendingMaps: function (mesh, label) {
      if (!mesh) return;
      var materials = this._asMaterialArray(mesh.material);
      var attached = 0;
      var pending = 0;
      for (var i = 0; i < materials.length; i += 1) {
        var mat = materials[i];
        if (!mat || !mat.userData || !mat.userData.sakuraPendingMap) continue;
        var texture = mat.userData.sakuraPendingMap;
        if (this._textureHasImage(texture)) {
          this._configureTextureForDisplay(texture);
          mat.map = texture;
          mat.userData.sakuraPendingMap = null;
          mat.needsUpdate = true;
          attached += 1;
        } else {
          pending += 1;
        }
      }
      if (attached > 0) {
        console.log("[SakuraMMD] attached pending textures " + JSON.stringify({
          label: label || "",
          attached: attached,
          pending: pending
        }));
        this._requestRender();
      }
    },

    _createSolidDebugMaterial: function (mesh) {
      var material = new THREE.MeshBasicMaterial({
        color: 0xff66cc,
        side: THREE.DoubleSide,
        transparent: false,
        opacity: 1,
        blending: THREE.NoBlending,
        depthTest: true,
        depthWrite: true,
        fog: false
      });
      material.name = "SakuraSolidDebugMaterial";
      material.skinning = !!mesh.skeleton;
      material.morphTargets = !!mesh.morphTargetInfluences;
      material.needsUpdate = true;
      return material;
    },

    _showBoundingBoxProbe: function (mesh) {
      try {
        if (this._boxProbe) {
          this.scene.remove(this._boxProbe);
          this._boxProbe = null;
        }
        var box = new THREE.Box3().setFromObject(mesh);
        this._boxProbe = new THREE.Box3Helper(box, 0x00ff66);
        this._boxProbe.frustumCulled = false;
        this.scene.add(this._boxProbe);
        console.warn("[SakuraMMD] added bounds probe because mesh still produced no pixels");
      } catch (err) {
        console.warn("[SakuraMMD] bounds probe failed: " + (err && err.message ? err.message : String(err)));
      }
    },

    loadModel: function (url, scale) {
      if (!this.initialized) {
        console.warn("[SakuraMMD] runtime 未初始化，跳过 loadModel");
        return;
      }
      if (typeof THREE.MMDLoader === "undefined") {
        console.warn("[SakuraMMD] THREE.MMDLoader 缺失");
        return;
      }
      var self = this;
      var loader = new THREE.MMDLoader();
      console.log("[SakuraMMD] MMDLoader.load start: " + url);
      loader.load(
        url,
        function (mesh) {
          if (self.mesh) {
            self.scene.remove(self.mesh);
          }
          if (scale && scale !== 1.0) {
            mesh.scale.setScalar(scale);
          }
          self._prepareMeshForDisplay(mesh);
          console.log("[SakuraMMD] source material diagnostics " + JSON.stringify(self._materialDiagnostics(mesh.material)));
          self._applyFallbackMaterial(mesh);
          self.scene.add(mesh);
          self.mesh = mesh;
          self._resetRigState();
          self._frameModel(mesh);
          self._requestRender();
          // 贴图通常在 MMDLoader 返回 mesh 后异步补齐，短暂预热可避免长期空白/白模。
          self._startWarmupRender(180);
          self._logModelDiagnostics(mesh);
          self._dumpRig(mesh);
          window.setTimeout(function () {
            self._attachPendingMaps(mesh, "200ms");
            var stats = self._logFrameStats("after-compatible-material-200ms");
            if (stats && stats.nonBackground === 0) {
              console.warn("[SakuraMMD] compatible material rendered no pixels; switching to solid debug material");
              self._applySolidDebugMaterial(mesh);
              self._requestRender();
              self._logFrameStats("after-solid-debug-material");
            }
          }, 200);
          window.setTimeout(function () {
            self._attachPendingMaps(mesh, "1000ms");
            var stats = self._logFrameStats("after-model-1000ms");
            if (stats && stats.nonBackground === 0) {
              self._showBoundingBoxProbe(mesh);
              self._requestRender();
              self._logFrameStats("after-bounds-probe");
            }
          }, 1000);
          window.setTimeout(function () {
            self._attachPendingMaps(mesh, "1800ms");
            self._requestRender();
            self._logFrameStats("after-model-1800ms");
          }, 1800);
          window.setTimeout(function () {
            self._attachPendingMaps(mesh, "2500ms");
            self._requestRender();
            self._logFrameStats("after-model-2500ms");
          }, 2500);
          console.log("[SakuraMMD] model loaded OK");
        },
        function (xhr) {
          if (xhr && xhr.lengthComputable) {
            var pct = Math.round((xhr.loaded / xhr.total) * 100);
            console.log("[SakuraMMD] loading " + pct + "%");
          }
        },
        function (err) {
          var msg = err && err.message ? err.message : String(err);
          console.error("[SakuraMMD] model load FAILED: " + msg);
        }
      );
    },

    captureFrame: function () {
      if (!this.initialized || !this.renderer || !this.scene || !this.camera || !this.canvas) {
        return { ok: false, error: "runtime not ready" };
      }
      try {
        this.renderer.render(this.scene, this.camera);
        return {
          ok: true,
          dataUrl: this.canvas.toDataURL("image/png"),
          width: this.canvas.width,
          height: this.canvas.height,
          hasMesh: !!this.mesh
        };
      } catch (err) {
        return {
          ok: false,
          error: err && err.message ? err.message : String(err)
        };
      }
    },

    // ---- 口型 / 表情 / 视线 / 缩放（morph 与骨骼驱动） ----

    _clamp01: function (v) {
      v = Number(v);
      if (isNaN(v)) return 0;
      return v < 0 ? 0 : (v > 1 ? 1 : v);
    },

    // 查 morph 名对应的 influence 下标；找不到只警告一次，避免逐帧刷屏。
    _morphIndex: function (name) {
      if (!this.mesh || !this.mesh.morphTargetDictionary) return -1;
      var dict = this.mesh.morphTargetDictionary;
      if (Object.prototype.hasOwnProperty.call(dict, name)) {
        return dict[name];
      }
      if (!this._warnedMorphs) this._warnedMorphs = {};
      if (!this._warnedMorphs[name]) {
        this._warnedMorphs[name] = true;
        console.warn("[SakuraMMD] morph not found: " + name);
      }
      return -1;
    },

    // 表情组：一次应用一组 morph；先清零上次写入的下标，避免表情残留。
    setExpression: function (morphMap) {
      if (!this.mesh || !this.mesh.morphTargetInfluences) return;
      var influences = this.mesh.morphTargetInfluences;
      var prev = this._expressionIndices;
      if (prev) {
        for (var i = 0; i < prev.length; i += 1) {
          influences[prev[i]] = 0;
        }
      }
      var applied = [];
      if (morphMap) {
        for (var name in morphMap) {
          if (!Object.prototype.hasOwnProperty.call(morphMap, name)) continue;
          var idx = this._morphIndex(name);
          if (idx < 0) continue;
          influences[idx] = this._clamp01(morphMap[name]);
          applied.push(idx);
        }
      }
      this._expressionIndices = applied;
      this._requestRender();
    },

    // 口型：单个嘴部 morph 跟随开合值（0~1）× 强度；与表情组分开管理。
    setLipSync: function (value, morphName, strength) {
      if (!this.mesh || !this.mesh.morphTargetInfluences) return;
      var influences = this.mesh.morphTargetInfluences;
      var idx = morphName ? this._morphIndex(morphName) : -1;
      // 口型 morph 切换时清零旧下标。
      if (this._lipIndex >= 0 && this._lipIndex !== idx) {
        influences[this._lipIndex] = 0;
      }
      this._lipIndex = idx;
      if (idx >= 0) {
        var s = (typeof strength === "number") ? strength : 0.8;
        influences[idx] = this._clamp01(Number(value) * s);
      }
      this._requestRender();
    },

    // 收集头/首/眼骨与初始 quaternion；只查一次，换模型由 _resetRigState 失效。
    _collectGazeBones: function () {
      if (this._gazeReady) return;
      this._gazeReady = true;
      this._headBone = null;
      this._neckBone = null;
      this._eyeBones = [];
      this._boneInit = {};
      if (!this.mesh || !this.mesh.skeleton || !this.mesh.skeleton.bones) return;
      var bones = this.mesh.skeleton.bones;
      for (var i = 0; i < bones.length; i += 1) {
        var b = bones[i];
        if (b.name === "頭") this._headBone = b;
        else if (b.name === "首") this._neckBone = b;
        else if (b.name === "左目" || b.name === "右目") this._eyeBones.push(b);
      }
      var all = [this._headBone, this._neckBone].concat(this._eyeBones);
      for (var j = 0; j < all.length; j += 1) {
        if (all[j]) this._boneInit[all[j].uuid] = all[j].quaternion.clone();
      }
      console.log("[SakuraMMD] gaze bones " + JSON.stringify({
        head: !!this._headBone, neck: !!this._neckBone, eyes: this._eyeBones.length
      }));
    },

    // 基于骨骼初始姿态叠加 yaw(绕Y)/pitch(绕X) 旋转。
    _applyGazeBone: function (bone, yaw, pitch) {
      if (!bone) return false;
      var init = this._boneInit && this._boneInit[bone.uuid];
      var q = new THREE.Quaternion().setFromEuler(new THREE.Euler(pitch, yaw, 0, "XYZ"));
      if (init) {
        bone.quaternion.copy(init).multiply(q);
      } else {
        bone.quaternion.copy(q);
      }
      return true;
    },

    // 视线追踪：归一化 (x,y)∈[-1,1]，x>0 看向右、y>0 看向下（方向若相反需真机翻转符号）。
    lookAt: function (x, y) {
      if (!this.mesh) return;
      this._collectGazeBones();
      var maxYaw = 25 * Math.PI / 180;
      var maxPitch = 20 * Math.PI / 180;
      var nx = Math.max(-1, Math.min(1, Number(x) || 0));
      var ny = Math.max(-1, Math.min(1, Number(y) || 0));
      var yaw = nx * maxYaw;
      var pitch = ny * maxPitch;
      var applied = false;
      // 头骨承担主要朝向，首骨与眼骨各分担一部分，叠加更自然。
      applied = this._applyGazeBone(this._headBone, yaw * 0.6, pitch * 0.6) || applied;
      applied = this._applyGazeBone(this._neckBone, yaw * 0.3, pitch * 0.3) || applied;
      var eyes = this._eyeBones || [];
      for (var i = 0; i < eyes.length; i += 1) {
        applied = this._applyGazeBone(eyes[i], yaw * 0.5, pitch * 0.5) || applied;
      }
      if (applied) {
        if (this.mesh.skeleton && this.mesh.skeleton.update) {
          this.mesh.skeleton.update();
        }
        this._requestRender();
      }
    },

    // 运行时缩放：重设 mesh 缩放并重新取景。
    setScale: function (scale) {
      if (!this.mesh) return;
      var s = Number(scale);
      if (isNaN(s) || s <= 0) return;
      this.mesh.scale.setScalar(s);
      this.mesh.updateMatrixWorld(true);
      this._frameModel(this.mesh);
      this._requestRender();
    },

    // 换模型时重置 morph/骨骼缓存状态。
    _resetRigState: function () {
      this._gazeReady = false;
      this._headBone = null;
      this._neckBone = null;
      this._eyeBones = null;
      this._boneInit = null;
      this._expressionIndices = null;
      this._lipIndex = -1;
      this._warnedMorphs = null;
    },

    // 打印模型的 morph 名与骨骼名清单，便于照着配 expressions/lip_sync。
    _dumpRig: function (mesh) {
      try {
        var morphs = mesh.morphTargetDictionary ? Object.keys(mesh.morphTargetDictionary) : [];
        var bones = (mesh.skeleton && mesh.skeleton.bones)
          ? mesh.skeleton.bones.map(function (b) { return b.name; })
          : [];
        console.log("[SakuraMMD] rig morphs(" + morphs.length + "): " + JSON.stringify(morphs));
        console.log("[SakuraMMD] rig bones(" + bones.length + "): " + JSON.stringify(bones));
      } catch (err) {
        console.warn("[SakuraMMD] dump rig failed: " + (err && err.message ? err.message : String(err)));
      }
    },

    // 根据模型包围盒自动取景，保证不同身高模型都完整入镜。
    _frameModel: function (mesh) {
      var box = new THREE.Box3().setFromObject(mesh);
      if (box.isEmpty()) return;
      var size = box.getSize(new THREE.Vector3());
      var center = box.getCenter(new THREE.Vector3());
      var maxDim = Math.max(size.x, size.y, size.z);
      var fov = this.camera.fov * Math.PI / 180;
      var dist = (maxDim / 2) / Math.tan(fov / 2) * 1.4;
      this.camera.position.set(center.x, center.y, center.z + dist);
      this.camera.near = Math.max(0.01, dist / 100);
      this.camera.far = dist * 100;
      this.camera.updateProjectionMatrix();
      this.camera.lookAt(center);
      this.target = center;
    },

    _logModelDiagnostics: function (mesh) {
      try {
        var box = new THREE.Box3().setFromObject(mesh);
        var size = box.getSize(new THREE.Vector3());
        var center = box.getCenter(new THREE.Vector3());
        console.log("[SakuraMMD] model bounds " + JSON.stringify({
          size: { x: size.x, y: size.y, z: size.z },
          center: { x: center.x, y: center.y, z: center.z },
          geometry: this._geometryDiagnostics(mesh),
          materials: this._materialDiagnostics(mesh.material),
          camera: {
            x: this.camera.position.x,
            y: this.camera.position.y,
            z: this.camera.position.z,
            near: this.camera.near,
            far: this.camera.far
          }
        }));
      } catch (err) {
        console.warn("[SakuraMMD] model diagnostics failed: " + (err && err.message ? err.message : String(err)));
      }
    },

    _geometryDiagnostics: function (mesh) {
      var geometry = mesh.geometry || {};
      var position = geometry.attributes && geometry.attributes.position;
      var skinIndex = geometry.attributes && geometry.attributes.skinIndex;
      var skinWeight = geometry.attributes && geometry.attributes.skinWeight;
      var sphere = geometry.boundingSphere;
      if (!sphere && geometry.computeBoundingSphere) {
        try {
          geometry.computeBoundingSphere();
          sphere = geometry.boundingSphere;
        } catch (err) {
          sphere = null;
        }
      }
      return {
        isSkinnedMesh: !!mesh.isSkinnedMesh,
        visible: mesh.visible,
        frustumCulled: mesh.frustumCulled,
        vertices: position ? position.count : 0,
        index: geometry.index ? geometry.index.count : 0,
        groups: geometry.groups ? geometry.groups.length : 0,
        bones: geometry.bones ? geometry.bones.length : 0,
        skeletonBones: mesh.skeleton && mesh.skeleton.bones ? mesh.skeleton.bones.length : 0,
        hasSkinIndex: !!skinIndex,
        hasSkinWeight: !!skinWeight,
        morphTargets: geometry.morphAttributes ? Object.keys(geometry.morphAttributes).length : 0,
        boundingSphere: sphere ? {
          x: sphere.center.x,
          y: sphere.center.y,
          z: sphere.center.z,
          radius: sphere.radius
        } : null
      };
    },

    _materialDiagnostics: function (materials) {
      var list = this._asMaterialArray(materials);
      var summary = {
        count: list.length,
        transparent: 0,
        invisible: 0,
        opacityMin: null,
        opacityMax: null,
        skinningFalse: 0,
        maps: 0,
        mapsReady: 0,
        mapsPending: 0,
        sample: []
      };
      for (var i = 0; i < list.length; i += 1) {
        var mat = list[i];
        if (!mat) continue;
        if (mat.transparent) summary.transparent += 1;
        if (mat.visible === false) summary.invisible += 1;
        if (mat.skinning === false) summary.skinningFalse += 1;
        if (mat.map) {
          summary.maps += 1;
          if (this._textureHasImage(mat.map)) {
            summary.mapsReady += 1;
          } else {
            summary.mapsPending += 1;
          }
        }
        if (typeof mat.opacity === "number") {
          summary.opacityMin = summary.opacityMin === null ? mat.opacity : Math.min(summary.opacityMin, mat.opacity);
          summary.opacityMax = summary.opacityMax === null ? mat.opacity : Math.max(summary.opacityMax, mat.opacity);
        }
        if (summary.sample.length < 8) {
          summary.sample.push({
            name: mat.name || "",
            type: mat.type || "",
            opacity: mat.opacity,
            transparent: mat.transparent,
            visible: mat.visible,
            side: mat.side,
            skinning: mat.skinning,
            hasMap: !!mat.map,
            mapReady: this._textureHasImage(mat.map)
          });
        }
      }
      return summary;
    },

    _logFrameStats: function (label) {
      if (!this.renderer || !this.scene || !this.camera || !this.canvas) return;
      try {
        this.renderer.info.reset();
        this.renderer.render(this.scene, this.camera);
        var gl = this.renderer.getContext();
        var w = this.canvas.width || 1;
        var h = this.canvas.height || 1;
        var pixel = new Uint8Array(4);
        var stepX = Math.max(1, Math.floor(w / 24));
        var stepY = Math.max(1, Math.floor(h / 24));
        var bg = this._clearColor;
        var samples = 0;
        var nonBackground = 0;
        var transparent = 0;
        var min = [255, 255, 255, 255];
        var max = [0, 0, 0, 0];
        var examples = [];

        for (var y = 0; y < h; y += stepY) {
          for (var x = 0; x < w; x += stepX) {
            gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
            samples += 1;
            for (var i = 0; i < 4; i += 1) {
              if (pixel[i] < min[i]) min[i] = pixel[i];
              if (pixel[i] > max[i]) max[i] = pixel[i];
            }
            if (pixel[3] < 250) {
              transparent += 1;
            }
            var isBackground =
              Math.abs(pixel[0] - bg[0]) <= 3 &&
              Math.abs(pixel[1] - bg[1]) <= 3 &&
              Math.abs(pixel[2] - bg[2]) <= 3 &&
              Math.abs(pixel[3] - bg[3]) <= 5;
            if (!isBackground) {
              nonBackground += 1;
              if (examples.length < 8) {
                examples.push({ x: x, y: y, rgba: [pixel[0], pixel[1], pixel[2], pixel[3]] });
              }
            }
          }
        }

        var stats = {
          label: label,
          width: w,
          height: h,
          samples: samples,
          nonBackground: nonBackground,
          transparent: transparent,
          min: min,
          max: max,
          render: {
            calls: this.renderer.info.render.calls,
            triangles: this.renderer.info.render.triangles,
            points: this.renderer.info.render.points,
            lines: this.renderer.info.render.lines
          },
          examples: examples
        };
        console.log("[SakuraMMD] frame stats " + JSON.stringify(stats));
        return stats;
      } catch (err) {
        console.warn("[SakuraMMD] frame stats failed: " + (err && err.message ? err.message : String(err)));
      }
    }
  };

  window.SakuraMMDRuntime = Runtime;
})();
