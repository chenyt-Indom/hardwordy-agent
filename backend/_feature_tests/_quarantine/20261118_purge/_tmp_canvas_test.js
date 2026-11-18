const fs = require('fs');
const { JSDOM } = require('jsdom');
const html = '<!doctype html><html><body><div class="bubble" id="bub"></div><textarea id="input"></textarea></body></html>';
const dom = new JSDOM(html, { url: 'http://localhost:8000/', runScripts: 'outside-only' });
const { window } = dom;
window.__mdToHtml = (s) => '<p class="md-p">' + String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</p>';
const src = fs.readFileSync(__dirname + '/artifacts.js', 'utf8');
window.eval(src);
const A = window.__Artifacts;
if (!A || typeof A.renderCanvas !== 'function') throw new Error('renderCanvas 未导出');
const blocks = [
  { id: 'b0', type: 'text', content: '方案 A：先出结构图，再补接口文档。' },
  { id: 'b1', type: 'image', content: '![结构图](data/library/x.png)' },
  { id: 'b2', type: 'ppt', content: 'data/library/deck.pptx' },
  { id: 'b3', type: 'web', content: '[示例](https://example.com/a)' },
];
const grid = A.renderCanvas(blocks);
if (!grid) throw new Error('renderCanvas 返回 null');
if (!grid.classList.contains('canvas-grid')) throw new Error('缺 canvas-grid');
const cards = grid.querySelectorAll('.canvas-card');
if (cards.length !== 4) throw new Error('卡片数不对：' + cards.length);
const ids = [...cards].map((c) => c.getAttribute('data-block-id'));
if (ids.join(',') !== 'b0,b1,b2,b3') throw new Error('data-block-id 不对：' + ids);
if (!cards[0].querySelector('.canvas-text')) throw new Error('文字块没内容');
if (!cards[1].querySelector('.canvas-fig img')) throw new Error('图片块没缩略图');
const imgSrc = cards[1].querySelector('img').getAttribute('src');
if (!/\/api\/doclib\/download\?rel=data%2Flibrary%2Fx\.png/.test(imgSrc)) throw new Error('图片地址不对：' + imgSrc);
if (!cards[2].querySelector('.canvas-file')) throw new Error('文件块结构不对');
if (!/deck\.pptx/.test(cards[2].textContent)) throw new Error('文件块没文件名');
if (!/example\.com/.test(cards[3].textContent)) throw new Error('网页块没链接');
if (A.renderCanvas([]) !== null) throw new Error('空 blocks 应返回 null');
if (A.renderCanvas(null) !== null) throw new Error('null 应返回 null');
const g2 = A.renderCanvas([{ id: 'z9', type: 'weird', content: '兜底内容' }]);
if (!g2 || !g2.querySelector('.canvas-text')) throw new Error('未知类型没兜底成文字块');
const bub = window.document.getElementById('bub');
bub.appendChild(grid);
const inp = window.document.getElementById('input');
cards[1].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
if (inp.value !== '就这块：b1 再改改') throw new Error('输入框预填不对：' + JSON.stringify(inp.value));
if (!cards[1].classList.contains('sel')) throw new Error('选中的卡片没高亮');
cards[0].dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
if (inp.value !== '就这块：b0 再改改') throw new Error('第二张卡片预填不对：' + inp.value);
if (cards[1].classList.contains('sel')) throw new Error('旧的选中没取消');
// 长文块默认收起，点「展开全文」后不再 cut
const longTxt = '很长的正文。'.repeat(200);
const g3 = A.renderCanvas([{ id: 'L', type: 'text', content: longTxt }]);
const longBody = g3.querySelector('.canvas-text');
if (!longBody.classList.contains('cut')) throw new Error('长文没默认收起');
const more = g3.querySelector('.canvas-more');
more.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
if (longBody.classList.contains('cut')) throw new Error('点展开后仍然收起');
// 拖拽：dragstart 后 dragover 必须被 preventDefault
window.document.body.appendChild(bub);
const c0 = grid.querySelector('.canvas-card[data-block-id="b0"]');
const c2 = grid.querySelector('.canvas-card[data-block-id="b2"]');
const dtEv = new window.Event('dragstart', { bubbles: true, cancelable: true });
Object.defineProperty(dtEv, 'dataTransfer', { value: { setData() {}, effectAllowed: '' } });
c0.dispatchEvent(dtEv);
const overEv = new window.Event('dragover', { bubbles: true, cancelable: true });
Object.defineProperty(overEv, 'dataTransfer', { value: { dropEffect: '' } });
c2.dispatchEvent(overEv);
if (!overEv.defaultPrevented) throw new Error('dragover 没 preventDefault，drop 不会触发');
const dropEv = new window.Event('drop', { bubbles: true, cancelable: true });
dropEv.clientX = 9999;
Object.defineProperty(dropEv, 'dataTransfer', { value: { getData() { return 'b0'; } } });
c2.dispatchEvent(dropEv);
const after = [...grid.querySelectorAll('.canvas-card')].map((c) => c.getAttribute('data-block-id')).join(',');
if (after !== 'b1,b2,b0,b3') throw new Error('拖拽换位结果不对：' + after);
console.log('artifacts canvas OK');
