/* 临时自检（跑完即删）：node frontend/_lc_check.js */
const path = process.argv[2] || "frontend/livecard.js";
global.window = {};
require(require("path").resolve(path));
const LC = global.window.LiveCard;

function bad(msg) { console.error("FAIL " + msg); process.exit(1); }
function ok(cond, msg) { if (!cond) bad(msg); }

ok(LC && typeof LC.renderCard === "function", "livecard.js 没有导出 renderCard");

const payloads = {
  weather: { city: "杭州", temp: 25, desc: "晴",
             days: [{ date: "明天", text: "多云", high: 28, low: 19 }] },
  map: { from: "西湖", to: "灵隐寺", distance: "6.2km", duration: "20 分钟",
         steps: [{ road: "南山路", distance: "1.2km" }] },
  doc: { title: "季度报告", path: "out/q3.docx", pages: ["概况", "结论"] },
  image: { title: "晚霞", src: "x.png", width: 1024, height: 768, prompt: "海边日落" },
};

function fakeHost() {
  return {
    childNodes: [], children: [],
    appendChild: function (c) { this.childNodes.push(c); this.children.push(c); return c; },
  };
}

["weather", "map", "doc", "image"].forEach(function (kind) {
  const node = LC.renderCard(kind, payloads[kind]);
  ok(node && typeof node === "object", kind + " 没返回节点");
  const cls = String(node.className || "");
  ok(cls.indexOf("resultcard") >= 0, kind + " 没有 resultcard class：" + cls);
  ok(cls.indexOf("resultcard-" + kind) >= 0, kind + " class 里没有 resultcard-" + kind + "：" + cls);
  ok(cls.split(/\s+/).indexOf(kind) >= 0, kind + " class 里没有裸 kind：" + cls);

  const host = fakeHost();
  const before = host.childNodes.length;
  const mounted = LC.mountCard(host, kind, payloads[kind]);
  ok(mounted, kind + " mountCard 没返回节点");
  ok(host.childNodes.length === before + 1,
     kind + " 挂载后容器子节点数没有增加：" + before + " -> " + host.childNodes.length);

  const host2 = fakeHost();
  host2.appendChild(LC.renderCard(kind, payloads[kind]));
  ok(host2.childNodes.length === 1, kind + " 返回值不能直接 append");
});

let fb = null;
try { fb = LC.renderCard("bogus_kind", { a: 1, b: "x" }); }
catch (e) { bad("未知 kind 抛了异常：" + e.message); }
ok(fb && String(fb.className || "").indexOf("resultcard") >= 0, "未知 kind 没有兜底卡");

[null, undefined, "", 0].forEach(function (k) {
  try { LC.renderCard(k, null); } catch (e) { bad("空 kind 抛了异常：" + e.message); }
});
["weather", "map", "doc", "image"].forEach(function (k) {
  let n = null;
  try { n = LC.renderCard(k, null); } catch (e) { bad(k + " 空 payload 抛了异常：" + e.message); }
  ok(n, k + " 空 payload 没返回节点");
});

console.log("livecard renderCard ok");
