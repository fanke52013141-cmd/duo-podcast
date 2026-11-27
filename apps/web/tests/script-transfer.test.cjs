const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../src/lib/scriptTransfer.ts'), 'utf8');
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: exportsObject });
const { parseScriptFile, getScriptTemplate } = exportsObject;
const plain = (value) => JSON.parse(JSON.stringify(value));

test('TXT parses one A/B turn per line, blank lines, BOM and full-width punctuation', () => {
  const result = parseScriptFile('dialogue.TXT', '\ufeffA: 你好，主播。\r\n\r\nＢ：你好，嘉宾。\na: 继续聊。  \n');
  assert.equal(result.format, 'txt');
  assert.equal(result.turnCount, 3);
  assert.equal(result.content, 'A: 你好，主播。\nB: 你好，嘉宾。\nA: 继续聊。');
  assert.deepEqual(plain(result.payload), { turns: [
    { speaker: 'A', lines: [{ displayText: '你好，主播。', spokenText: '你好，主播。' }] },
    { speaker: 'B', lines: [{ displayText: '你好，嘉宾。', spokenText: '你好，嘉宾。' }] },
    { speaker: 'A', lines: [{ displayText: '继续聊。', spokenText: '继续聊。' }] },
  ] });
});

test('TXT reports the physical line for malformed or empty dialogue', () => {
  assert.throws(() => parseScriptFile('a.txt', 'A: 正常\n这句话没有说话人'), /TXT 第 2 行格式错误/);
  assert.throws(() => parseScriptFile('a.txt', 'A: 正常\nB:   '), /TXT 第 2 行台词为空/);
  assert.throws(() => parseScriptFile('a.txt', ' \n\r\n'), /TXT 内容为空/);
});

test('CSV supports arbitrary column order, quoted commas, escaped quotes and multiline fields', () => {
  const result = parseScriptFile('dialogue.csv',
    '\ufefftext,speaker,spokenText\r\n' +
    '"你好, 大家",A,"你好, 大家"\r\n' +
    '"他说:""开始吧""\n第二行",B,口播文本\r\n');
  assert.equal(result.turnCount, 2);
  assert.deepEqual(plain(result.payload.turns), [
    { speaker: 'A', lines: [{ displayText: '你好, 大家', spokenText: '你好, 大家' }] },
    { speaker: 'B', lines: [{ displayText: '他说:"开始吧"\n第二行', spokenText: '口播文本' }] },
  ]);
  assert.equal(result.content, 'A: 你好, 大家\nB: 他说:"开始吧" 第二行');
});

test('CSV accepts speaker,text and gives actionable header, row and quote errors', () => {
  assert.equal(parseScriptFile('a.csv', 'speaker,text\nA,你好').turnCount, 1);
  assert.throws(() => parseScriptFile('a.csv', 'foo,bar\nA,你好'), /CSV 表头错误/);
  assert.throws(() => parseScriptFile('a.csv', 'speaker,text\nA,你好\nC,无效'), /CSV 第 3 行 speaker 无效/);
  assert.throws(() => parseScriptFile('a.csv', 'speaker,text\nA,'), /CSV 第 2 行台词为空/);
  assert.throws(() => parseScriptFile('a.csv', 'speaker,text\nA,"未闭合'), /CSV 第 2 行存在未闭合的双引号/);
  assert.throws(() => parseScriptFile('a.csv', 'speaker,text\nA,"闭合"错误'), /闭合双引号后只能是逗号或换行/);
  assert.throws(() => parseScriptFile('a.csv', 'speaker,text'), /CSV 没有可导入的话轮/);
});

test('JSON accepts an envelope or bare payload and preserves structured fields', () => {
  const payload = { label: '保留标签', turns: [
    { id: 'T07', speaker: 'A', intent: 'probe', speedRatio: 1.1, lines: [{ spokenText: '只有朗读文本' }] },
    { speaker: 'B', lines: [{ displayText: '显示文本', spokenText: '不同读音' }] },
  ] };
  const enclosed = parseScriptFile('script.json', JSON.stringify({ schemaVersion: '1.0', kind: 'script', payload }));
  assert.equal(enclosed.turnCount, 2);
  assert.equal(enclosed.content, 'A: 只有朗读文本\nB: 显示文本');
  assert.deepEqual(plain(enclosed.payload), payload);
  assert.deepEqual(plain(parseScriptFile('script.JSON', JSON.stringify(payload)).payload), payload);
});

test('JSON rejects malformed syntax and invalid turn or line structures with locations', () => {
  assert.throws(() => parseScriptFile('a.json', '{oops'), /JSON 解析失败/);
  assert.throws(() => parseScriptFile('a.json', '[]'), /根节点必须是对象/);
  assert.throws(() => parseScriptFile('a.json', '{"payload":null}'), /payload 必须是/);
  assert.throws(() => parseScriptFile('a.json', '{"turns":[]}'), /turns 必须是非空数组/);
  assert.throws(() => parseScriptFile('a.json', '{"turns":[{"speaker":"C","lines":[{"displayText":"x"}]}]}'), /第 1 个话轮 speaker 无效/);
  assert.throws(() => parseScriptFile('a.json', '{"turns":[{"speaker":"A","lines":[]}]}'), /lines 必须是非空数组/);
  assert.throws(() => parseScriptFile('a.json', '{"turns":[{"speaker":"A","lines":[{}]}]}'), /话轮 1 第 1 行缺少/);
  assert.throws(() => parseScriptFile('a.json', '{"turns":[{"speaker":"A","lines":[{"displayText":" "}]}]}'), /话轮 1 第 1 行台词为空/);
});

test('only JSON, TXT and CSV extensions are accepted, case-insensitively', () => {
  assert.equal(parseScriptFile('  SCRIPT.CSV  ', 'speaker,text\nA,你好').format, 'csv');
  assert.throws(() => parseScriptFile('script.md', 'A: 你好'), /请选择 \.json、\.txt 或 \.csv/);
  assert.throws(() => parseScriptFile('script', 'A: 你好'), /不支持的脚本文件/);
});

test('all downloadable templates round-trip through the importer', () => {
  for (const format of ['json', 'txt', 'csv']) {
    const template = getScriptTemplate(format);
    assert.ok(template.fileName.endsWith(`.${format}`));
    assert.match(template.mimeType, new RegExp(format === 'json' ? 'application/json' : `text/${format === 'txt' ? 'plain' : 'csv'}`));
    const result = parseScriptFile(template.fileName, template.content);
    assert.equal(result.format, format);
    assert.equal(result.turnCount, 2);
    assert.match(result.content, /^A: 大家好/);
  }
});
