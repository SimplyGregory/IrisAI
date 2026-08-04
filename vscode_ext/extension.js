/**
 * The Iris bridge.
 *
 * Iris can already read and write files, and can drive most apps through the
 * accessibility tree. VS Code is the exception: it is Electron and exposes
 * window chrome only, so "fix the errors in this file" or "what have I got
 * selected" were unanswerable, and editing a file the editor had open meant
 * writing underneath a dirty buffer.
 *
 * This serves a small JSON protocol over a named pipe. Everything it exposes
 * is something the extension API knows and the filesystem does not: the
 * language server's diagnostics, the active selection, the command palette,
 * and edits that land in the editor's own undo stack.
 *
 * Plain JavaScript with no dependencies, on purpose - it means the .vsix can
 * be packed by the installer without a Node toolchain on the machine.
 */

const vscode = require('vscode');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');

// One pipe per window, not one per machine. Two VS Code windows both want to
// serve, and whichever loaded first would otherwise own the name and answer
// for a window you are not looking at. Each announces itself in a small file
// and stamps it when focused, so Iris can pick the one in front of you.
const REGISTRY = path.join(os.homedir(), '.iris', 'vscode');

// A named pipe on Windows, a Unix socket everywhere else. net.createServer
// listens on both from the same call - only the address differs - so this is
// the whole of the transport's platform handling.
const WINDOWS = process.platform === 'win32';
const PIPE = WINDOWS
  ? '\\\\.\\pipe\\iris-vscode-' + process.pid
  : path.join(REGISTRY, 'sock-' + process.pid);

const SEVERITY = ['error', 'warning', 'information', 'hint'];
const MAX_TEXT = 4000; // selections and command results are truncated to this
const MAX_READ = 20000; // a whole file being read back is allowed more room

// SymbolKind is a numeric enum; turned round so results say "function" rather
// than 11. Guarded because the shape is only there when really inside VS Code.
const KIND = {};
for (const [name, value] of Object.entries(vscode.SymbolKind || {})) {
  if (typeof value === 'number') KIND[value] = name.toLowerCase();
}

let server = null;
let entry = null;

// --- the window registry ---------------------------------------------------

function announce(focused) {
  const folders = (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
  const record = {
    pipe: PIPE,
    pid: process.pid,
    folder: folders[0] || '',
    name: vscode.workspace.name || '(no folder)',
    focused: focused ? Date.now() : (entry ? entry.focused : 0),
  };
  entry = record;
  try {
    fs.mkdirSync(REGISTRY, { recursive: true });
    fs.writeFileSync(path.join(REGISTRY, process.pid + '.json'), JSON.stringify(record));
  } catch (err) {
    // Nowhere to announce ourselves is not a reason to take the editor down.
  }
}

function withdraw() {
  for (const leftover of [path.join(REGISTRY, process.pid + '.json'), PIPE]) {
    try {
      // The socket file is ours to remove on the way out; on Windows PIPE is
      // not a path at all and this simply fails, which is the same as nothing.
      fs.unlinkSync(leftover);
    } catch (err) {
      /* already gone */
    }
  }
}

// --- reading ---------------------------------------------------------------

function state() {
  const editor = vscode.window.activeTextEditor;
  return {
    vscode: vscode.version,
    // Which shell the integrated terminal runs. Worth reporting rather than
    // assuming: the quoting rules that decide whether `echo hello world`
    // prints one line or two are not the same in PowerShell, cmd and bash.
    shell: vscode.env.shell || '',
    folders: (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath),
    active: editor
      ? {
          file: editor.document.uri.fsPath,
          language: editor.document.languageId,
          lines: editor.document.lineCount,
          dirty: editor.document.isDirty,
          // 1-based going out. VS Code counts from zero internally and the
          // conversion happens here, at the edge, so nothing downstream has
          // to remember which convention it is holding.
          cursor: editor.selection.active.line + 1,
          // Column too: every "what is this symbol" query needs a point, not
          // a line, and without this there was no way to name one.
          column: editor.selection.active.character + 1,
          selection: editor.document.getText(editor.selection).slice(0, MAX_TEXT),
          selected_lines: editor.selection.isEmpty
            ? null
            : [editor.selection.start.line + 1, editor.selection.end.line + 1],
        }
      : null,
    open: vscode.workspace.textDocuments
      .filter((doc) => doc.uri.scheme === 'file')
      .map((doc) => ({ file: doc.uri.fsPath, dirty: doc.isDirty })),
  };
}

function diagnostics(args) {
  const wanted = args.file ? path.resolve(args.file).toLowerCase() : null;
  const found = [];

  for (const [uri, list] of vscode.languages.getDiagnostics()) {
    if (uri.scheme !== 'file') continue;
    if (wanted && uri.fsPath.toLowerCase() !== wanted) continue;
    for (const problem of list) {
      found.push({
        file: uri.fsPath,
        line: problem.range.start.line + 1,
        severity: SEVERITY[problem.severity] || 'error',
        message: problem.message,
        source: problem.source || '',
      });
    }
  }

  // Errors before warnings before hints: a hundred style hints must not push
  // the one real error out of the truncated tail.
  found.sort((a, b) => SEVERITY.indexOf(a.severity) - SEVERITY.indexOf(b.severity));
  const limit = args.limit || 60;
  return { total: found.length, shown: Math.min(limit, found.length), problems: found.slice(0, limit) };
}

async function commands(args) {
  const all = await vscode.commands.getCommands(true);
  const contains = (args.contains || '').toLowerCase();
  const matched = all.filter((id) => id.toLowerCase().includes(contains)).sort();
  // There are a couple of thousand. Unfiltered they are useless to read and
  // expensive to send, so a search term is the point of the operation.
  return { total: matched.length, commands: matched.slice(0, args.limit || 80) };
}

// --- finding your way to a point -------------------------------------------

async function docFor(args) {
  if (args.file) return vscode.workspace.openTextDocument(vscode.Uri.file(args.file));
  const editor = vscode.window.activeTextEditor;
  if (!editor) throw new Error('no file given, and nothing is open to assume');
  return editor.document;
}

function positionOf(document, args) {
  // By symbol name first, because that is what a request actually carries -
  // "where is greet defined" - and nobody says which column it starts at.
  if (args.symbol) {
    const escaped = String(args.symbol).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pattern = new RegExp('\\b' + escaped + '\\b');
    for (let line = Math.max(0, (args.line || 1) - 1); line < document.lineCount; line++) {
      const at = document.lineAt(line).text.search(pattern);
      if (at >= 0) return new vscode.Position(line, at);
    }
    throw new Error(`could not find ${args.symbol} in ${document.uri.fsPath}`);
  }

  if (args.line) {
    const line = Math.max(0, Math.min(document.lineCount - 1, args.line - 1));
    const text = document.lineAt(line).text;
    // Landing on the indentation of a line asks the language server about
    // whitespace, which it has nothing to say about. First real character.
    const column = args.column ? args.column - 1 : Math.max(0, text.search(/\S/));
    return new vscode.Position(line, column);
  }

  const editor = vscode.window.activeTextEditor;
  if (editor && editor.document.uri.toString() === document.uri.toString()) {
    return editor.selection.active;
  }
  throw new Error('say which symbol, or which line, to look at');
}

function place(location) {
  // Providers return Location or LocationLink depending on the language, and
  // which one is not something the caller should have to care about.
  const uri = location.uri || location.targetUri;
  const range = location.range || location.targetSelectionRange || location.targetRange;
  return {
    file: uri.fsPath,
    line: range.start.line + 1,
    column: range.start.character + 1,
  };
}

async function read(args) {
  const document = await docFor(args);
  const from = Math.max(1, args.from || 1);
  const to = Math.min(document.lineCount, args.to || document.lineCount);

  const lines = [];
  for (let i = from - 1; i < to; i++) lines.push(`${i + 1}\t${document.lineAt(i).text}`);

  return {
    file: document.uri.fsPath,
    lines: document.lineCount,
    // The whole point: this is the buffer, unsaved edits included, which is
    // what the edit operation will act on. Reading the file off disk instead
    // gives line numbers that do not match what is in front of the user.
    dirty: document.isDirty,
    from,
    to,
    text: lines.join('\n').slice(0, MAX_READ),
  };
}

async function symbols(args) {
  if (args.query) {
    const found = (await vscode.commands.executeCommand(
      'vscode.executeWorkspaceSymbolProvider', String(args.query),
    )) || [];
    return {
      scope: 'workspace',
      symbols: found.slice(0, args.limit || 50).map((item) => ({
        name: item.name,
        kind: KIND[item.kind] || String(item.kind),
        file: item.location.uri.fsPath,
        line: item.location.range.start.line + 1,
      })),
    };
  }

  const document = await docFor(args);
  const found = (await vscode.commands.executeCommand(
    'vscode.executeDocumentSymbolProvider', document.uri,
  )) || [];

  const flat = [];
  const walk = (items, depth) => {
    for (const item of items) {
      const range = item.range || (item.location && item.location.range);
      flat.push({
        name: item.name,
        kind: KIND[item.kind] || String(item.kind),
        line: range.start.line + 1,
        depth,
      });
      if (item.children && item.children.length) walk(item.children, depth + 1);
    }
  };
  walk(found, 0);

  return { scope: 'document', file: document.uri.fsPath, symbols: flat.slice(0, args.limit || 200) };
}

async function locate(args, provider) {
  const document = await docFor(args);
  const at = positionOf(document, args);
  const found = (await vscode.commands.executeCommand(provider, document.uri, at)) || [];
  return {
    asked_about: { file: document.uri.fsPath, line: at.line + 1, column: at.character + 1 },
    places: found.slice(0, args.limit || 60).map(place),
    total: found.length,
  };
}

const definition = (args) => locate(args, 'vscode.executeDefinitionProvider');
const references = (args) => locate(args, 'vscode.executeReferenceProvider');
const implementations = (args) => locate(args, 'vscode.executeImplementationProvider');

async function hover(args) {
  const document = await docFor(args);
  const at = positionOf(document, args);
  const found = (await vscode.commands.executeCommand(
    'vscode.executeHoverProvider', document.uri, at,
  )) || [];

  const parts = [];
  for (const item of found) {
    for (const piece of item.contents || []) {
      parts.push(typeof piece === 'string' ? piece : (piece.value || ''));
    }
  }
  return {
    file: document.uri.fsPath,
    line: at.line + 1,
    text: parts.join('\n').trim().slice(0, MAX_TEXT),
  };
}

async function codeActions(args) {
  const document = await docFor(args);
  const line = args.line
    ? Math.max(0, Math.min(document.lineCount - 1, args.line - 1))
    : positionOf(document, args).line;
  const found = (await vscode.commands.executeCommand(
    'vscode.executeCodeActionProvider', document.uri, document.lineAt(line).range,
  )) || [];
  return { file: document.uri.fsPath, line: line + 1, found };
}

async function fixes(args) {
  const { file, line, found } = await codeActions(args);
  return {
    file,
    line,
    fixes: found.map((action, index) => ({
      index: index + 1,
      title: action.title,
      kind: action.kind ? action.kind.value : '',
    })),
  };
}

// --- changing --------------------------------------------------------------

async function open(args) {
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(args.file));
  const editor = await vscode.window.showTextDocument(document, { preview: false });
  if (args.line) {
    const where = new vscode.Position(Math.max(0, args.line - 1), 0);
    editor.selection = new vscode.Selection(where, where);
    editor.revealRange(new vscode.Range(where, where), vscode.TextEditorRevealType.InCenter);
  }
  return { opened: document.uri.fsPath, lines: document.lineCount };
}

async function apply(uri, range, text) {
  const edit = new vscode.WorkspaceEdit();
  edit.replace(uri, range, text);
  // applyEdit rather than writing the file: this goes through the editor, so
  // it survives a dirty buffer instead of fighting it, and Ctrl+Z undoes it
  // like anything the user typed.
  if (!(await vscode.workspace.applyEdit(edit))) {
    throw new Error('VS Code refused the edit');
  }
}

async function edit(args) {
  const uri = vscode.Uri.file(args.file);
  const document = await vscode.workspace.openTextDocument(uri);
  const first = Math.max(0, (args.line_start || 1) - 1);
  const last = Math.min(document.lineCount - 1, (args.line_end || args.line_start || 1) - 1);
  if (first > last) throw new Error('line_start is after line_end');

  const range = new vscode.Range(
    new vscode.Position(first, 0),
    document.lineAt(last).range.end,
  );
  const before = document.getText(range);
  await apply(uri, range, args.text || '');
  return { file: uri.fsPath, replaced_lines: [first + 1, last + 1], was: before.slice(0, MAX_TEXT) };
}

async function insert(args) {
  const uri = vscode.Uri.file(args.file);
  const document = await vscode.workspace.openTextDocument(uri);
  const at = Math.max(0, Math.min(document.lineCount, (args.line || 1) - 1));
  const where = new vscode.Position(at, 0);
  const text = args.text.endsWith('\n') ? args.text : args.text + '\n';
  await apply(uri, new vscode.Range(where, where), text);
  return { file: uri.fsPath, inserted_before_line: at + 1 };
}

async function save(args) {
  if (args.file) {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(args.file));
    return { saved: (await document.save()) ? document.uri.fsPath : null };
  }
  const editor = vscode.window.activeTextEditor;
  if (!editor) throw new Error('nothing is open to save');
  await editor.document.save();
  return { saved: editor.document.uri.fsPath };
}

async function command(args) {
  const result = await vscode.commands.executeCommand(args.id, ...(args.args || []));
  let readable = null;
  try {
    // Command results are whatever the command felt like returning, including
    // objects that do not survive JSON. Losing the return value is fine; the
    // command still ran. Most return nothing at all, which is null rather than
    // the string "undefined".
    readable = result === undefined ? null : JSON.parse(JSON.stringify(result));
  } catch (err) {
    readable = String(result);
  }
  return { ran: args.id, result: readable };
}

function terminals() {
  const open = vscode.window.terminals;
  const active = vscode.window.activeTerminal;
  return {
    shell: vscode.env.shell || '',
    terminals: open.map((term, index) => ({
      // 1-based to match the numbers VS Code puts on the tabs, so "terminal 2"
      // means the same thing to the user and to us.
      index: index + 1,
      name: term.name,
      active: term === active,
      reads_output: Boolean(term.shellIntegration),
    })),
  };
}

function pickTerminal(args) {
  const open = vscode.window.terminals;
  if (args.index) {
    const chosen = open[args.index - 1];
    if (!chosen) throw new Error(`there is no terminal ${args.index}; ${open.length} are open`);
    return chosen;
  }
  if (args.name) {
    const wanted = String(args.name).toLowerCase();
    const chosen = open.find((term) => term.name.toLowerCase().includes(wanted));
    if (!chosen) throw new Error('no terminal with a name like ' + args.name);
    return chosen;
  }
  return vscode.window.activeTerminal || vscode.window.createTerminal('Iris');
}

// Terminal output arrives with the escape sequences that colour it, which are
// noise to read and expensive to send.
function plain(text) {
  // Written with explicit escapes rather than the literal ESC bytes that were
  // here before. A control character sitting in source survives most tools but
  // not all of them, and one that gets mangled turns this into a regex that
  // quietly matches ordinary text instead of colour codes.
  return text
    // OSC: window titles, and the markers shell integration itself writes.
    .replace(/\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)/g, '')
    // CSI: colour and cursor movement.
    .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, '')
    .replace(/\r/g, '');
}

async function readOutput(execution, ms) {
  const chunks = [];
  const finished = (async () => {
    for await (const chunk of execution.read()) chunks.push(chunk);
  })();
  // A command that never returns - a server, a watch - must not hold the tool
  // open. Whatever it printed by the deadline is still worth having.
  let timer;
  const deadline = new Promise((resolve) => { timer = setTimeout(resolve, ms); });
  await Promise.race([finished, deadline]);
  clearTimeout(timer);
  return plain(chunks.join('')).trim().slice(-MAX_TEXT);
}

async function terminal(args) {
  const chosen = pickTerminal(args);
  chosen.show(true);

  // Shell integration lets us run the command and read what it printed. It is
  // not always there - it needs the shell to have started up and announced
  // itself - so typing into the terminal stays the fallback.
  if (chosen.shellIntegration && args.run !== false) {
    const execution = chosen.shellIntegration.executeCommand(args.text);
    const output = await readOutput(execution, args.timeout || 15000);
    return { sent: args.text, terminal: chosen.name, ran: true, output, shell: vscode.env.shell || '' };
  }

  chosen.sendText(args.text, args.run !== false);
  return {
    sent: args.text,
    terminal: chosen.name,
    ran: args.run !== false,
    output: null,
    shell: vscode.env.shell || '',
  };
}

// --- acting on what the language server offers ------------------------------

async function apply_fix(args) {
  const { found, line } = await codeActions(args);
  if (!found.length) throw new Error(`nothing is offered at line ${line}`);

  let chosen;
  if (args.title) {
    const wanted = String(args.title).toLowerCase();
    chosen = found.find((action) => (action.title || '').toLowerCase().includes(wanted));
    if (!chosen) throw new Error(`no fix here called anything like ${args.title}`);
  } else {
    chosen = found[(args.index || 1) - 1];
    if (!chosen) throw new Error(`there is no fix ${args.index || 1}; ${found.length} are offered`);
  }

  // A code action carries an edit, a command, or both, and which one it is
  // varies by language server - so do whichever it turns out to have.
  if (chosen.edit && !(await vscode.workspace.applyEdit(chosen.edit))) {
    throw new Error('VS Code refused the edit that fix wanted to make');
  }
  if (chosen.command) {
    await vscode.commands.executeCommand(
      chosen.command.command, ...(chosen.command.arguments || []),
    );
  }
  return { applied: chosen.title, line };
}

async function rename_symbol(args) {
  if (!args.new_name) throw new Error('rename_symbol needs new_name');
  const document = await docFor(args);
  const at = positionOf(document, args);

  const edit = await vscode.commands.executeCommand(
    'vscode.executeDocumentRenameProvider', document.uri, at, String(args.new_name),
  );
  if (!edit) throw new Error('there is nothing renameable at that position');

  // Counted before applying: the edit is spent afterwards, and how many files
  // moved is the part worth reporting back.
  const touched = typeof edit.entries === 'function' ? edit.entries().length : 0;
  if (!(await vscode.workspace.applyEdit(edit))) {
    throw new Error('VS Code refused the rename');
  }
  return { renamed_to: args.new_name, files_changed: touched };
}

// --- files ------------------------------------------------------------------
// Through WorkspaceEdit rather than the filesystem, so creating and deleting
// land in the editor's undo history and any language server that renames
// imports for you gets the chance to.

async function create(args) {
  if (!args.file) throw new Error('create needs a file path');
  const uri = vscode.Uri.file(args.file);
  const edit = new vscode.WorkspaceEdit();
  edit.createFile(uri, { overwrite: Boolean(args.overwrite), ignoreIfExists: !args.overwrite });
  if (args.text) edit.insert(uri, new vscode.Position(0, 0), args.text);
  if (!(await vscode.workspace.applyEdit(edit))) throw new Error('VS Code refused to create it');
  return { created: uri.fsPath };
}

async function remove(args) {
  if (!args.file) throw new Error('delete needs a file path');
  const uri = vscode.Uri.file(args.file);
  const edit = new vscode.WorkspaceEdit();
  edit.deleteFile(uri, { ignoreIfNotExists: false });
  if (!(await vscode.workspace.applyEdit(edit))) throw new Error('VS Code refused to delete it');
  return { deleted: uri.fsPath };
}

async function rename_file(args) {
  if (!args.file || !args.to) throw new Error('rename_file needs file and to');
  const from = vscode.Uri.file(args.file);
  const to = vscode.Uri.file(args.to);
  const edit = new vscode.WorkspaceEdit();
  edit.renameFile(from, to, { overwrite: Boolean(args.overwrite) });
  if (!(await vscode.workspace.applyEdit(edit))) throw new Error('VS Code refused the rename');
  return { renamed: from.fsPath, to: to.fsPath };
}

async function save_all() {
  await vscode.workspace.saveAll(false);
  return { saved: 'every open file with unsaved changes' };
}

// --- tabs -------------------------------------------------------------------

function tabs() {
  const found = [];
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      const input = tab.input;
      found.push({
        group: group.viewColumn,
        label: tab.label,
        file: input && input.uri ? input.uri.fsPath : '',
        active: tab.isActive,
        dirty: tab.isDirty,
      });
    }
  }
  return { tabs: found };
}

async function close_tab(args) {
  const all = vscode.window.tabGroups.all.flatMap((group) => group.tabs);
  let target;

  if (args.file) {
    const wanted = String(args.file).toLowerCase();
    target = all.filter((tab) => tab.input && tab.input.uri
      && tab.input.uri.fsPath.toLowerCase() === wanted);
    if (!target.length) throw new Error('no tab is open for ' + args.file);
  } else if (args.all) {
    target = all;
  } else {
    target = all.filter((tab) => tab.isActive);
    if (!target.length) throw new Error('no tab is active to close');
  }

  await vscode.window.tabGroups.close(target, true);
  return { closed: target.length };
}

// --- settings ---------------------------------------------------------------

function settings(args) {
  const config = vscode.workspace.getConfiguration();
  if (!args.key) throw new Error('which setting? a key like editor.fontSize');
  return { key: args.key, value: config.get(args.key) };
}

async function setting(args) {
  if (!args.key) throw new Error('setting needs a key, e.g. editor.wordWrap');
  const target = args.scope === 'workspace'
    ? vscode.ConfigurationTarget.Workspace
    : vscode.ConfigurationTarget.Global;
  await vscode.workspace.getConfiguration().update(args.key, args.value, target);
  return {
    key: args.key,
    value: vscode.workspace.getConfiguration().get(args.key),
    scope: args.scope === 'workspace' ? 'this workspace' : 'everywhere',
  };
}

// --- tasks and debugging ----------------------------------------------------

async function tasks(args) {
  const found = await vscode.tasks.fetchTasks();
  return {
    tasks: found.slice(0, args.limit || 40).map((task, index) => ({
      index: index + 1,
      name: task.name,
      source: task.source,
      group: task.group ? task.group.id : '',
    })),
  };
}

async function run_task(args) {
  if (!args.name) throw new Error('run_task needs the task name');
  const found = await vscode.tasks.fetchTasks();
  const wanted = String(args.name).toLowerCase();
  const chosen = found.find((task) => task.name.toLowerCase().includes(wanted));
  if (!chosen) throw new Error(`no task with a name like ${args.name}; ${found.length} are defined`);
  await vscode.tasks.executeTask(chosen);
  return { started: chosen.name, source: chosen.source };
}

function breakpoint(args) {
  if (!args.file || !args.line) throw new Error('breakpoint needs file and line');
  const uri = vscode.Uri.file(args.file);
  const at = new vscode.Position(Math.max(0, args.line - 1), 0);

  if (args.remove) {
    const here = vscode.debug.breakpoints.filter((point) => point.location
      && point.location.uri.fsPath.toLowerCase() === uri.fsPath.toLowerCase()
      && point.location.range.start.line === at.line);
    vscode.debug.removeBreakpoints(here);
    return { removed: here.length, file: uri.fsPath, line: args.line };
  }

  vscode.debug.addBreakpoints([
    new vscode.SourceBreakpoint(new vscode.Location(uri, at)),
  ]);
  return { added: true, file: uri.fsPath, line: args.line };
}

async function start_debugging(args) {
  const folder = (vscode.workspace.workspaceFolders || [])[0];
  if (!args.name) throw new Error('start_debugging needs the name of a launch configuration');
  const started = await vscode.debug.startDebugging(folder, String(args.name));
  if (!started) throw new Error(`VS Code would not start ${args.name}; is it in launch.json?`);
  return { started: args.name };
}

// --- the protocol ----------------------------------------------------------

const OPS = {
  // looking
  ping, state, read, diagnostics, commands, terminals, tabs, settings, tasks,
  symbols, definition, references, implementations, hover, fixes,
  // changing
  open, edit, insert, save, command, terminal,
  apply_fix, rename_symbol, close_tab, setting, run_task,
  create, rename_file, save_all, breakpoint, start_debugging,
  // "delete" cannot be a function name, so the op is mapped to it by hand.
  delete: remove,
};

function ping() {
  return { ok: true, vscode: vscode.version, folder: entry ? entry.folder : '' };
}

async function handle(request) {
  const op = OPS[request.op];
  if (!op) throw new Error('unknown operation: ' + request.op);
  return await op(request.args || {});
}

function serve(socket) {
  let buffer = '';
  socket.on('error', () => {}); // a client that hangs up mid-write is routine
  socket.on('data', async (chunk) => {
    buffer += chunk.toString('utf8');
    let cut;
    while ((cut = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 1);
      if (!line.trim()) continue;

      let request = { id: null };
      try {
        request = JSON.parse(line);
        const result = await handle(request);
        socket.write(JSON.stringify({ id: request.id, ok: true, result }) + '\n');
      } catch (err) {
        socket.write(JSON.stringify({ id: request.id, ok: false, error: String(err && err.message || err) }) + '\n');
      }
    }
  });
}

function activate(context) {
  announce(vscode.window.state.focused);

  // A Unix socket is a file, and a crashed window leaves its own behind - which
  // then refuses the address with EADDRINUSE forever. A named pipe disappears
  // with the process that made it, so this is only ever needed off Windows.
  // Safe because the name carries our pid: anything already at it is ours and
  // is dead, since we are only starting now.
  if (!WINDOWS) {
    try {
      fs.mkdirSync(REGISTRY, { recursive: true });
      fs.unlinkSync(PIPE);
    } catch (err) {
      /* nothing there, which is the normal case */
    }
  }

  server = net.createServer(serve);
  server.on('error', () => {}); // the name is per-pid, so a clash means a stale
  server.listen(PIPE);          // socket we cannot use; the window stays quiet

  context.subscriptions.push(
    vscode.window.onDidChangeWindowState((e) => e.focused && announce(true)),
    vscode.workspace.onDidChangeWorkspaceFolders(() => announce(false)),
    vscode.commands.registerCommand('iris.status', () => {
      vscode.window.showInformationMessage(
        server && server.listening
          ? 'Iris bridge is listening on ' + PIPE
          : 'Iris bridge is not listening. Reload the window to retry.',
      );
    }),
    { dispose: withdraw },
  );
}

function deactivate() {
  withdraw();
  if (server) server.close();
}

module.exports = { activate, deactivate };
