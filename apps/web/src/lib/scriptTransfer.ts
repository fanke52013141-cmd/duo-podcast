/* ============================================================================
   阶段① 外部文案导入：JSON / TXT / CSV 的纯解析与下载模板。
   文件内容只在浏览器内解析，结构化草稿仍通过现有 script/import 端点创建。
   ========================================================================== */

export type ScriptFileFormat = 'json' | 'txt' | 'csv';
export type ScriptSpeaker = 'A' | 'B';

export interface ScriptTransferLine {
  displayText: string;
  spokenText: string;
}

export interface ScriptTransferTurn {
  speaker: ScriptSpeaker;
  lines: ScriptTransferLine[];
}

export interface ParsedScriptFile {
  format: ScriptFileFormat;
  payload: Record<string, unknown>;
  /** 回填“已有脚本”输入框的 A:/B: 文本，沿用现有本机草稿恢复。 */
  content: string;
  turnCount: number;
}

export interface ScriptTemplate {
  fileName: string;
  mimeType: string;
  content: string;
}

type CsvRow = { cells: string[]; line: number };

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function withoutBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

function asSourceText(turns: ScriptTransferTurn[]): string {
  return turns.flatMap((turn) => turn.lines.map((line) => {
    const text = (line.displayText || line.spokenText).replace(/\s*\r?\n\s*/g, ' ').trim();
    return `${turn.speaker}: ${text}`;
  })).join('\n');
}

function parseTxt(text: string): ScriptTransferTurn[] {
  const turns: ScriptTransferTurn[] = [];
  const lines = withoutBom(text).split(/\r\n?|\n/);
  lines.forEach((raw, index) => {
    if (!raw.trim()) return;
    const match = /^\s*([ABＡＢ])\s*[:：]\s*(.*?)\s*$/i.exec(raw);
    if (!match) {
      throw new Error(`TXT 第 ${index + 1} 行格式错误：请使用“A: 内容”或“B: 内容”，每条发言占一行。`);
    }
    const text = match[2].trim();
    if (!text) throw new Error(`TXT 第 ${index + 1} 行台词为空：请在冒号后填写内容。`);
    const speaker = (match[1] === 'Ａ' ? 'A' : match[1] === 'Ｂ' ? 'B' : match[1].toUpperCase()) as ScriptSpeaker;
    turns.push({ speaker, lines: [{ displayText: text, spokenText: text }] });
  });
  if (!turns.length) throw new Error('TXT 内容为空：请至少填写一条 A: 或 B: 发言。');
  return turns;
}

/** 读取 RFC 4180 常见写法：逗号、双引号转义以及引号内换行。 */
function parseCsvRows(text: string): CsvRow[] {
  const input = withoutBom(text);
  const rows: CsvRow[] = [];
  let row: string[] = [];
  let cell = '';
  let line = 1;
  let rowLine = 1;
  let inQuotes = false;
  let afterQuote = false;

  const finishRow = () => {
    row.push(cell);
    if (row.some((value) => value.trim())) rows.push({ cells: row, line: rowLine });
    row = [];
    cell = '';
    afterQuote = false;
  };

  for (let i = 0; i < input.length; i++) {
    const char = input[i];
    if (inQuotes) {
      if (char === '"') {
        if (input[i + 1] === '"') { cell += '"'; i++; }
        else { inQuotes = false; afterQuote = true; }
      } else if (char === '\r' || char === '\n') {
        if (char === '\r' && input[i + 1] === '\n') i++;
        cell += '\n';
        line++;
      } else cell += char;
      continue;
    }
    if (afterQuote) {
      if (char === ',') { row.push(cell); cell = ''; afterQuote = false; continue; }
      if (char === '\r' || char === '\n') {
        finishRow();
        if (char === '\r' && input[i + 1] === '\n') i++;
        line++; rowLine = line; continue;
      }
      throw new Error(`CSV 第 ${line} 行格式错误：闭合双引号后只能是逗号或换行。`);
    }
    if (char === '"') {
      if (cell) throw new Error(`CSV 第 ${line} 行格式错误：双引号必须位于字段开头。`);
      inQuotes = true;
    } else if (char === ',') {
      row.push(cell); cell = '';
    } else if (char === '\r' || char === '\n') {
      finishRow();
      if (char === '\r' && input[i + 1] === '\n') i++;
      line++; rowLine = line;
    } else cell += char;
  }
  if (inQuotes) throw new Error(`CSV 第 ${rowLine} 行存在未闭合的双引号。`);
  if (row.length || cell || afterQuote) finishRow();
  return rows;
}

function parseCsv(text: string): ScriptTransferTurn[] {
  const rows = parseCsvRows(text);
  if (!rows.length) throw new Error('CSV 内容为空：首行请使用 speaker,text 表头。');
  const headers = rows[0].cells.map((value) => value.trim().toLowerCase().replace(/[\s_-]/g, ''));
  const findColumn = (names: string[]) => headers.findIndex((header) => names.includes(header));
  const speakerColumn = findColumn(['speaker', 'role', '说话人']);
  const displayColumn = findColumn(['text', 'content', 'displaytext', '内容', '文案', '台词']);
  const spokenColumn = findColumn(['spoken', 'spokentext', '口播文本', '朗读文本']);
  if (speakerColumn < 0 || (displayColumn < 0 && spokenColumn < 0)) {
    throw new Error('CSV 表头错误：必须包含 speaker 和 text 两列（也支持 displayText/spokenText）。');
  }

  const turns: ScriptTransferTurn[] = [];
  rows.slice(1).forEach((row) => {
    const speaker = (row.cells[speakerColumn] ?? '').trim().toUpperCase();
    if (speaker !== 'A' && speaker !== 'B') {
      throw new Error(`CSV 第 ${row.line} 行 speaker 无效：只能填写 A 或 B。`);
    }
    const display = (displayColumn >= 0 ? row.cells[displayColumn] : '')?.trim() ?? '';
    const spoken = (spokenColumn >= 0 ? row.cells[spokenColumn] : '')?.trim() ?? '';
    if (!display && !spoken) throw new Error(`CSV 第 ${row.line} 行台词为空。`);
    turns.push({
      speaker,
      lines: [{ displayText: display || spoken, spokenText: spoken || display }],
    });
  });
  if (!turns.length) throw new Error('CSV 没有可导入的话轮：请在表头下至少填写一行。');
  return turns;
}

function structuredTurns(payload: Record<string, unknown>): ScriptTransferTurn[] {
  if (!Array.isArray(payload.turns) || !payload.turns.length) {
    throw new Error('JSON 结构错误：turns 必须是非空数组。');
  }
  return payload.turns.map((rawTurn, turnIndex) => {
    const turnNumber = turnIndex + 1;
    if (!isRecord(rawTurn)) throw new Error(`JSON 第 ${turnNumber} 个话轮必须是对象。`);
    if (rawTurn.speaker !== 'A' && rawTurn.speaker !== 'B') {
      throw new Error(`JSON 第 ${turnNumber} 个话轮 speaker 无效：只能是 A 或 B。`);
    }
    if (!Array.isArray(rawTurn.lines) || !rawTurn.lines.length) {
      throw new Error(`JSON 第 ${turnNumber} 个话轮 lines 必须是非空数组。`);
    }
    const lines = rawTurn.lines.map((rawLine, lineIndex): ScriptTransferLine => {
      const lineNumber = lineIndex + 1;
      if (!isRecord(rawLine)) throw new Error(`JSON 话轮 ${turnNumber} 第 ${lineNumber} 行必须是对象。`);
      const { displayText, spokenText } = rawLine;
      if ((displayText !== undefined && typeof displayText !== 'string') ||
          (spokenText !== undefined && typeof spokenText !== 'string')) {
        throw new Error(`JSON 话轮 ${turnNumber} 第 ${lineNumber} 行 displayText/spokenText 必须是字符串。`);
      }
      if (displayText === undefined && spokenText === undefined) {
        throw new Error(`JSON 话轮 ${turnNumber} 第 ${lineNumber} 行缺少 displayText/spokenText。`);
      }
      const display = displayText ?? spokenText ?? '';
      const spoken = spokenText ?? displayText ?? '';
      if (!display.trim() && !spoken.trim()) {
        throw new Error(`JSON 话轮 ${turnNumber} 第 ${lineNumber} 行台词为空。`);
      }
      return { displayText: display, spokenText: spoken };
    });
    return { speaker: rawTurn.speaker, lines };
  });
}

function parseJson(text: string): { payload: Record<string, unknown>; turns: ScriptTransferTurn[] } {
  let parsed: unknown;
  try { parsed = JSON.parse(withoutBom(text)); }
  catch { throw new Error('JSON 解析失败：请检查括号、引号和逗号是否完整。'); }
  if (!isRecord(parsed)) throw new Error('JSON 结构错误：根节点必须是对象。');
  const payload = 'payload' in parsed ? parsed.payload : parsed;
  if (!isRecord(payload)) throw new Error('JSON 结构错误：payload 必须是包含 turns 的对象。');
  return { payload, turns: structuredTurns(payload) };
}

/** 将文件解析成现有 script/import API 的 payload；不会发起任何网络请求。 */
export function parseScriptFile(fileName: string, text: string): ParsedScriptFile {
  const extension = /\.([^.\\/]+)$/.exec(fileName.trim().toLowerCase())?.[1];
  if (extension !== 'json' && extension !== 'txt' && extension !== 'csv') {
    throw new Error(`不支持的脚本文件“${fileName}”：请选择 .json、.txt 或 .csv 文件。`);
  }
  if (extension === 'json') {
    const { payload, turns } = parseJson(text);
    return { format: extension, payload, content: asSourceText(turns), turnCount: turns.length };
  }
  const turns = extension === 'txt' ? parseTxt(text) : parseCsv(text);
  return { format: extension, payload: { turns }, content: asSourceText(turns), turnCount: turns.length };
}

const JSON_TEMPLATE = {
  label: '双人播客文案',
  turns: [
    { speaker: 'A', lines: [{ displayText: '大家好，欢迎回到本期节目。', spokenText: '大家好，欢迎回到本期节目。' }] },
    { speaker: 'B', lines: [{ displayText: '今天我们聊点具体的。', spokenText: '今天我们聊点具体的。' }] },
  ],
};

/** 三种模板都能直接重新上传，并由 parseScriptFile 解析。 */
export function getScriptTemplate(format: ScriptFileFormat): ScriptTemplate {
  if (format === 'json') {
    return { fileName: 'duocast-script-template.json', mimeType: 'application/json;charset=utf-8', content: `${JSON.stringify(JSON_TEMPLATE, null, 2)}\n` };
  }
  if (format === 'txt') {
    return { fileName: 'duocast-script-template.txt', mimeType: 'text/plain;charset=utf-8', content: 'A: 大家好，欢迎回到本期节目。\nB: 今天我们聊点具体的。\n' };
  }
  if (format === 'csv') {
    return { fileName: 'duocast-script-template.csv', mimeType: 'text/csv;charset=utf-8', content: 'speaker,text\r\nA,大家好，欢迎回到本期节目。\r\nB,今天我们聊点具体的。\r\n' };
  }
  throw new Error('未知文案模板格式。');
}
