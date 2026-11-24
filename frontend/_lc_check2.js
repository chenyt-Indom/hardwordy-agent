/* 临时自检（跑 run_check 前用，跑完即删）：node frontend/_lc_check2.js
   验证 livecard.render 按 type 分派到 renderTempBar / renderRouteThumb /
   renderDocPreview / renderImageCanvas，未知 type 回退 renderPlainCard，且不抛异常。 */
global.window = {};
require(require("path").resolve(__dirname, "livecard.js"));
const LC = global.window.LiveCard;

function bad(msg) { console.error("FAIL " + msg); process.exit(1); }

if (!LC || typeof LC.render !== "function") bad("没有导出 render");

const cases = {
  weather: { city: "杭州", detail: "晴 25℃",
             days: [{ date: "明天", text: "多云", high: 28, low: 19 },
                    { date: "后天", text: "晴", high: 31, low: 22 }] },
  map: { route: { from: "西湖", to: "灵隐寺", mode: "driving", distance: "6.2km", duration: "20 分钟" },
         markers: [{ name: "西湖", lat: 30.24, lon: 120.15 },
                   { name: "灵隐寺", lat: 30.24, lon: 120.10 }] },
  doc: { title: "季度报告", path: "out/q3.docx", pages: ["概况", "结论", "附录"] },
  image: { title: "晚霞", src: "x.png", width: 1024, height: 768, prompt: "海边日落" },
};
const want = { weather: "renderTempBar", map: "renderRouteThumb",
               doc: "renderDocPreview", image: "renderImageCanvas" };

Object.keys(cases).forEach(function (k) {
  const n = LC.render(k, cases[k], {});
  if (!n || typeof n !== "object") bad(k + " 没返回节点");
  const cls = String(n.className || "");
  if (cls.indexOf("resultcard") < 0) bad(k + " 没有 resultcard class：" + cls);
  if (cls.indexOf("resultcard-" + k) < 0) bad(k + " 缺 resultcard-" + k + "：" + cls);
  if (cls.split(/\s+/).indexOf(k) < 0) bad(k + " 缺裸 kind class：" + cls);
  if (n.__lcRenderer !== want[k]) bad(k + " 分派到了 " + n.__lcRenderer + "，应为 " + want[k]);
  if (typeof LC[want[k]] !== "function") bad(want[k] + " 未导出");
  if (LC.RESULT_DISPATCH[k] !== LC[want[k]]) bad(k + " DISPATCH 与渲染器不一致");
  // payload 形状不认识时不能抛
  [null, undefined, "x", 42, [], { random: 1 }].forEach(function (p) {
    try { LC.render(k, p); } catch (e) { bad(k + " 奇怪 payload 抛异常：" + e.message); }
  });
});

const fb = LC.render("bogus_kind", { a: 1, b: "x" });
const fcls = String(fb.className || "");
if (fcls.indexOf("resultcard") < 0) bad("未知 type 没走纯文本卡：" + fcls);
if (fb.__lcRenderer !== "renderPlainCard") bad("未知 type 分派到了 " + fb.__lcRenderer);
if (LC.renderPlainCard("bogus_kind", { a: 1 }).className.indexOf("resultcard-plain") < 0) {
  bad("纯文本卡缺 resultcard-plain");
}

[null, undefined, "", 0, "WEATHER"].forEach(function (k) {
  try {
    const n = LC.render(k, null);
    if (!n || String(n.className || "").indexOf("resultcard") < 0) bad("空 kind 没兜底卡：" + k);
  } catch (e) { bad("空 kind 抛异常：" + e.message); }
});

const host = { childNodes: [], appendChild: function (c) { this.childNodes.push(c); return c; } };
if (!LC.mountResult(host, "weather", cases.weather)) bad("mountResult 没返回节点");
if (host.childNodes.length !== 1) bad("mountResult 没挂上（子节点 " + host.childNodes.length + "）");

// 老入口 renderCard 不能被改坏（回归守卫）
["weather", "map", "doc", "image"].forEach(function (k) {
  const n = LC.renderCard(k, cases[k]);
  if (!n || String(n.className || "").indexOf("resultcard-" + k) < 0) bad("renderCard " + k + " 坏了");
});

console.log("livecard.render dispatch ok");
