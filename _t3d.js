  const _vizNum = (v, unit) => {
    const n = Number(v) || 0;
    const r = Math.round(n * 10) / 10;
    const s = Math.abs(r) >= 1000 ? (Math.round(r / 100) / 10) + "k" : String(r);
    return s + (unit || "");
  };
  const _vizSeries = (spec, it) => {
    if (Array.isArray(spec.series) && spec.series.length) {
      return spec.series.slice(0, 4).map((s, i) => ({
        name: String((s && s.name) || ("系列" + (i + 1))),
        values: (Array.isArray(s && s.values) ? s.values : []).map((v) => Number(v) || 0)
      }));
    }
    return [{ name: String(spec.series_name || ""), values: it.map((x) => x.value) }];
  };
  const _vizCurve = (p, lo, hi) => {
    if (!p.length) return "";
    if (p.length < 3) return "M" + p.map((q) => q[0].toFixed(1) + "," + q[1].toFixed(1)).join("L");
    const cl = (y) => Math.max(lo, Math.min(hi, y));
    let d = "M" + p[0][0].toFixed(1) + "," + p[0][1].toFixed(1);
    for (let i = 0; i < p.length - 1; i++) {
      const p0 = p[i - 1] || p[i], p1 = p[i], p2 = p[i + 1], p3 = p[i + 2] || p2;
      const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = cl(p1[1] + (p2[1] - p0[1]) / 6);
      const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = cl(p2[1] - (p3[1] - p1[1]) / 6);
      d += "C" + c1x.toFixed(1) + "," + c1y.toFixed(1) + " " + c2x.toFixed(1) + "," + c2y.toFixed(1) +
        " " + p2[0].toFixed(1) + "," + p2[1].toFixed(1);
    }
    return d;
  };
  // ★ 2026-11-21 用户要求「图表要三维形状」。
  //   做的是伪 3D（等轴测）：柱状图拆成正面/顶面/右侧面三块，饼图用多层圆盘堆出厚度。
  //   好处是不依赖任何库、数据仍是精确的（AI 生成的立体图会把数字画糊，这里不会）。
  //   f > 0 提亮，f < 0 压暗；用于从同一个主色派生三面的明暗。
  //   ⚠️ 必须定义在 _vizXY 外面：饼图分支在 _vizHtml 里也要用，放在 _vizXY 内部会变成局部变量。
  const _vizShade = (hex, f) => {
    const h = String(hex || "#4f8cff").replace("#", "");
    const p = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
    const m = (c) => Math.max(0, Math.min(255, Math.round(f > 0 ? c + (255 - c) * f : c * (1 + f))));
    return "#" + p.map((c) => m(c).toString(16).padStart(2, "0")).join("");
  };
  function _vizXY(o) {
    const W = 680, H = 252;
    // ★ 3D 时柱顶再向上抬 11px，数值标签还要在顶面上方留一行，
    //   所以上边距必须从 20 加到 34 —— 否则最高柱的标签会跑到 y<0 被画布裁掉。
    const padL = 56, padR = 30, padT = o.d3 ? 34 : 20, padB = 38;
    const cats = o.cats && o.cats.length ? o.cats : [" "];
    const ser = o.ser && o.ser.length ? o.ser : [{ name: "", values: [0] }];
    const P = o.palette;
    const flat = [];
    ser.forEach((s) => s.values.forEach((v) => flat.push(Math.abs(Number(v) || 0))));
    const rawMax = Math.max.apply(null, [0].concat(flat));
    const _step = rawMax > 0 ? _vizStep(rawMax / 4) : 1;
    const _T = rawMax > 0 ? Math.max(1, Math.ceil(rawMax / _step)) : 1;
    const max = rawMax > 0 ? _step * _T : 1;
  function _viz3DView(yawDeg, pitchDeg, fov, dist) {
    const yaw = (yawDeg == null ? -20 : yawDeg) * Math.PI / 180;
    const pit = (pitchDeg == null ? 20 : pitchDeg) * Math.PI / 180;
    const fv = fov || 900, dz = dist || 1000;
    const cyw = Math.cos(yaw), syw = Math.sin(yaw);
    const cpt = Math.cos(pit), spt = Math.sin(pit);
    const proj = (x, y, z) => {
      const x1 = x * cyw - z * syw;
      const z1 = x * syw + z * cyw;
      const y2 = y * cpt - z1 * spt;
      const z2 = y * spt + z1 * cpt;
      const d = z2 + dz;
      const s = fv / (d > 90 ? d : 90);
      return [x1 * s, -y2 * s, d];
    };
    // 把一批世界点装进 W×H：整体等比缩放 + 平移，不破坏透视形状
    const fit = (wp, W, H, pad) => {
      let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
      for (let i = 0; i < wp.length; i++) {
        const w = wp[i];
        if (!w) continue;
        const q = proj(w[0], w[1], w[2]);
        if (!isFinite(q[0]) || !isFinite(q[1])) continue;
        if (q[0] < x0) x0 = q[0];
        if (q[0] > x1) x1 = q[0];
        if (q[1] < y0) y0 = q[1];
        if (q[1] > y1) y1 = q[1];
      }
      if (!(x1 > x0) || !(y1 > y0)) { x0 = -1; x1 = 1; y0 = -1; y1 = 1; }
      const k = Math.min((W - pad * 2) / (x1 - x0), (H - pad * 2) / (y1 - y0));
      const ox = W / 2 - ((x0 + x1) / 2) * k;
      const oy = H / 2 - ((y0 + y1) / 2) * k;
      return (x, y, z) => {
        const q = proj(x, y, z);
        return [q[0] * k + ox, q[1] * k + oy, q[2]];
      };
    };
    return { proj: proj, fit: fit };
  }
  const _viz3DPt = (p) => p[0].toFixed(1) + "," + p[1].toFixed(1);
  const _viz3DPoly = (pts, fill, tip, sw) =>
    '<polygon points="' + pts.map(_viz3DPt).join(" ") + '" fill="' + fill + '"' +
    (sw ? ' stroke="' + fill + '" stroke-width="' + sw + '" stroke-linejoin="round"' : "") + ">" +
    (tip || "") + "</polygon>";
  // 画家算法：远的先画，近的覆盖上去
  const _viz3DPaint = (faces) => {
    for (let i = 0; i < faces.length; i++) {
      const w = faces[i].w;
      let s = 0;
      for (let j = 0; j < w.length; j++) s += w[j][2];
      faces[i].dep = s / (w.length || 1);
    }
    faces.sort((a, b) => b.dep - a.dep);
    let out = "";
    for (let i = 0; i < faces.length; i++) {
      out += _viz3DPoly(faces[i].w, faces[i].fill, faces[i].tip, faces[i].sw);
    }
    return out;
  };
  const _viz3DLegend = (ser, P) =>
    (ser.length > 1 || (ser[0] && ser[0].name))
      ? '<div class="viz-legend">' + ser.map((s, i) =>
          '<span><i style="background:' + P[i % P.length] + '"></i>' + escapeHtml(s.name) + "</span>").join("") + "</div>"
      : "";

  // ── 真透视柱状图：立方体五面 + 地面网格 + 地面阴影 + 透视刻度 ──
  const _viz3DPt = (p) => p[0].toFixed(1) + "," + p[1].toFixed(1);
  const _viz3DPoly = (pts, fill, tip, sw) =>
    '<polygon points="' + pts.map(_viz3DPt).join(" ") + '" fill="' + fill + '"' +
    (sw ? ' stroke="' + fill + '" stroke-width="' + sw + '" stroke-linejoin="round"' : "") + ">" +
    (tip || "") + "</polygon>";
  // 画家算法：远的先画，近的覆盖上去
  const _viz3DPaint = (faces) => {
    for (let i = 0; i < faces.length; i++) {
      const w = faces[i].w;
      let s = 0;
      for (let j = 0; j < w.length; j++) s += w[j][2];
      faces[i].dep = s / (w.length || 1);
    }
    faces.sort((a, b) => b.dep - a.dep);
    let out = "";
    for (let i = 0; i < faces.length; i++) {
      out += _viz3DPoly(faces[i].w, faces[i].fill, faces[i].tip, faces[i].sw);
    }
    return out;
  };
  const _viz3DLegend = (ser, P) =>
    (ser.length > 1 || (ser[0] && ser[0].name))
      ? '<div class="viz-legend">' + ser.map((s, i) =>
          '<span><i style="background:' + P[i % P.length] + '"></i>' + escapeHtml(s.name) + "</span>").join("") + "</div>"
      : "";

  // ── 真透视柱状图：立方体五面 + 地面网格 + 地面阴影 + 透视刻度 ──
  function _viz3DPie(o) {
    const W = 680, H = 350, pad = 22;
    const it = o.items || [];
    const P = o.palette || ["#4f8cff"];
    const unit = o.unit || "";
    const donut = !!o.donut;
    const total = it.reduce((a, b) => a + Math.abs(Number(b.value) || 0), 0) || 1;
    // ★ 立体感 = 椭圆压缩(pitch) × 侧壁厚度(TH)。TH=34 时侧壁投影后仅 ~29，太薄看不出。
    const R = 128, TH = 46, rIn = donut ? 60 : 0;

    const wp = [];
    for (let i = 0; i < 36; i++) {
      const a = i / 36 * Math.PI * 2;
      wp.push([Math.cos(a) * R, TH, Math.sin(a) * R]);
      wp.push([Math.cos(a) * R, 0, Math.sin(a) * R]);
    }
    const view = _viz3DView(-20, 30, 780, 760);
    const T = view.fit(wp, W, H, pad);

    // ★ 顶面：**每个扇区一个整体多边形**，沿弧采样后一次成形。
    //   之前把顶面也切成 4° 小块，相邻小块之间的径向接缝会漏出下面的暗色侧壁，
    //   叠出来就是一圈「放射状黑色纹理」。整块成形就没有接缝。
    //   侧壁仍按小块：它需要逐块判断可见性（外法线旋转后 z 分量为负才朝相机）。
    const ya = -20 * Math.PI / 180;
    const syw = Math.sin(ya), cyw = Math.cos(ya);
    const nz1 = (ang) => Math.cos(ang) * syw + Math.sin(ang) * cyw;
    const walls = [], tops = [];
    let acc = 0;
    it.forEach((x, i) => {
      const v = Math.abs(Number(x.value) || 0);
      const a0 = acc / total * Math.PI * 2 - Math.PI / 2;
      acc += v;
      const a1 = acc / total * Math.PI * 2 - Math.PI / 2;
      if (a1 - a0 < 1e-5) return;
      const col = P[i % P.length];
      const tip = "<title>" + escapeHtml(x.label + "：" + _vizNum(v, unit)) + "</title>";
      const pt = (ang, rad, yy) => T(Math.cos(ang) * rad, yy, Math.sin(ang) * rad);

      // —— 顶面：整扇区一块 ——
      const steps = Math.max(2, Math.ceil((a1 - a0) / (Math.PI / 60)));
      const ring = [];
      for (let k = 0; k <= steps; k++) ring.push(pt(a0 + (a1 - a0) * k / steps, R, TH));
      if (donut) {
        for (let k = steps; k >= 0; k--) ring.push(pt(a0 + (a1 - a0) * k / steps, rIn, TH));
      } else {
        ring.push(pt((a0 + a1) / 2, 0, TH));   // 实心饼图收到圆心，成扇形
      }
      tops.push({ w: ring, fill: col, tip: tip, sw: 0.8 });

      // —— 侧壁：按小块做可见性裁剪 ——
      const wseg = Math.max(1, Math.ceil((a1 - a0) / (Math.PI / 45)));
      for (let k = 0; k < wseg; k++) {
        const b0 = a0 + (a1 - a0) * k / wseg;
        const b1 = a0 + (a1 - a0) * (k + 1) / wseg;
        const vis = nz1((b0 + b1) / 2);
        if (vis < 0) {
          walls.push({ w: [pt(b0, R, TH), pt(b1, R, TH), pt(b1, R, 0), pt(b0, R, 0)], fill: _vizShade(col, -0.3), tip: tip, sw: 1.4 });
        }
        // 环图内壁法线朝内，可见条件相反（透过中间空洞能看到后侧内壁）
        if (donut && vis > 0) {
          walls.push({ w: [pt(b0, rIn, TH), pt(b1, rIn, TH), pt(b1, rIn, 0), pt(b0, rIn, 0)], fill: _vizShade(col, -0.48), tip: tip, sw: 1.4 });
        }
      }
    });

    let ctr = "";
    if (donut) {
      const c = T(0, TH, 0);
      ctr = '<div class="viz-3d-ctr" style="left:' + (c[0] / W * 100).toFixed(2) + "%;top:" +
        (c[1] / H * 100).toFixed(2) + '%"><b>' + escapeHtml(_vizNum(total, unit)) + "</b><span>合计</span></div>";
    }
    const legend = '<div class="viz-legend">' + it.map((x, i) =>
      '<span><i style="background:' + P[i % P.length] + '"></i>' + escapeHtml(x.label) + " <b>" +
      escapeHtml(x.value) + unit + "</b> · " + Math.round(Math.abs(Number(x.value) || 0) / total * 1000) / 10 + "%</span>"
    ).join("") + "</div>";

    return '<div class="viz-chart viz-3d-chart"><div class="viz-3d-hold" style="aspect-ratio:' + W + "/" + H + '">' +
      '<svg class="viz-svg" viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="xMidYMid meet" role="img">' +
      _viz3DPaint(walls) + _viz3DPaint(tops) + "</svg>" + ctr + "</div>" + legend + "</div>";
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showLightbox(mime, b64, prompt) {
    const ov = document.createElement("div");
    ov.className = "lightbox";
    ov.innerHTML = `<div class="lb-box">
      <img src="data:${mime || "image/png"};base64,${b64}">
      <div class="lb-cap"></div>

const out = _viz3DPie({items:[{label:"北美本土",value:80},{label:"中国内地",value:44},{label:"其他地区",value:226}], unit:"亿美元", palette:["#4f8cff","#39c98f","#ffb64d"], donut:false});
console.log("len:", out.length);
const polys = out.match(/<polygon[^>]*>/g) || [];
console.log("polygons:", polys.length);
let bad = 0, nan = 0, oob = 0;
const pts = [...out.matchAll(/points="([^"]+)"/g)].map(m=>m[1]);
console.log("point-sets:", pts.length);
pts.forEach(s=>{
  s.split(" ").forEach(pp=>{
    const [x,y] = pp.split(",").map(Number);
    if (!isFinite(x)||!isFinite(y)) nan++;
    else if (x<-2||x>682||y<-2||y>352) oob++;
  });
});
console.log("nan:", nan, "oob:", oob);
console.log("has svg:", /<svg/.test(out), "| has hold:", /viz-3d-hold/.test(out));
console.log("first 600:", out.slice(0,600));
