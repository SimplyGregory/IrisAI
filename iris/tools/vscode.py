"""Seeing and driving VS Code, through the bridge extension.

Two tools rather than twenty, with the operation as an argument. Every tool's
schema is re-sent on every request, and twenty near-identical VS Code schemas
would be a permanent tax on every conversation - including the ones that never
mention the editor. The split is where the confirmation gate wants it: looking
is safe, changing is not.

These are only registered when the VS Code connection was turned on during
setup, so anyone who does not use it pays nothing for it.
"""

import json

from anthropic import beta_tool

from iris import editor
from iris.confirm import confirm
from iris.redact import scrubbed


def _unavailable(exc: Exception) -> str:
    return f"VS Code is not reachable. {exc}"


def _call(op: str, **args) -> dict | str:
    """Run an operation, or return the sentence explaining why it could not."""
    try:
        return editor.call(op, **args)
    except editor.EditorUnavailable as exc:
        return _unavailable(exc)
    except ValueError as exc:
        return f"VS Code refused that: {exc}"


# --- turning results into something worth reading -------------------------

def _describe_state(got: dict) -> str:
    lines = [f"VS Code {got.get('vscode', '?')}"]
    if got.get("shell"):
        lines.append(f"  terminal shell: {got['shell']}")
    for folder in got.get("folders", []):
        lines.append(f"  folder: {folder}")

    active = got.get("active")
    if not active:
        lines.append("  nothing open in the editor")
    else:
        dirty = ", unsaved changes" if active.get("dirty") else ""
        # Only stated when the extension actually reported one. Defaulting it
        # to 1 would read as fact and send a position query to the wrong place.
        column = f" column {active['column']}" if active.get("column") else ""
        lines.append(
            f"  active: {active['file']} ({active['language']}, {active['lines']} lines, "
            f"cursor line {active['cursor']}{column}{dirty})"
        )
        if active.get("selected_lines"):
            first, last = active["selected_lines"]
            lines.append(f"  selected: lines {first}-{last}")
            lines.append("  " + "\n  ".join(active["selection"].splitlines()[:40]))

    others = [doc for doc in got.get("open", []) if not active or doc["file"] != active["file"]]
    if others:
        lines.append("  also loaded: " + ", ".join(
            doc["file"] + ("*" if doc["dirty"] else "") for doc in others[:15]
        ))
    return "\n".join(lines)


def _describe_read(got: dict) -> str:
    head = f"{got['file']}, lines {got['from']}-{got['to']} of {got['lines']}"
    if got.get("dirty"):
        head += " (UNSAVED CHANGES - this is the buffer, which is what an edit will act on)"
    return f"{head}\n{got['text']}"


def _describe_problems(got: dict) -> str:
    if not got.get("total"):
        return "No errors or warnings."
    lines = [f"{got['total']} problem(s), showing {got['shown']}:"]
    for problem in got["problems"]:
        source = f" [{problem['source']}]" if problem.get("source") else ""
        lines.append(
            f"  {problem['severity']:<8}{problem['file']}:{problem['line']}"
            f"{source} {problem['message']}"
        )
    return "\n".join(lines)


def _describe_symbols(got: dict) -> str:
    found = got.get("symbols", [])
    if not found:
        return "No symbols found."
    if got.get("scope") == "workspace":
        return "\n".join(
            f"  {s['kind']:<12} {s['name']}  {s['file']}:{s['line']}" for s in found
        )
    lines = [f"{got['file']}:"]
    for item in found:
        lines.append(f"  {'  ' * item['depth']}{item['kind']:<10} {item['name']}  line {item['line']}")
    return "\n".join(lines)


def _describe_places(got: dict, what: str) -> str:
    asked = got["asked_about"]
    where = f"{asked['file']}:{asked['line']}:{asked['column']}"
    places = got.get("places", [])
    if not places:
        return f"No {what} found for what is at {where}."
    lines = [f"{got['total']} {what} for what is at {where}:"]
    for place in places:
        lines.append(f"  {place['file']}:{place['line']}:{place['column']}")
    return "\n".join(lines)


def _describe_terminals(got: dict) -> str:
    open_now = got.get("terminals", [])
    if not open_now:
        return "No terminals are open in VS Code. Sending one a command opens one."
    lines = [f"{len(open_now)} terminal(s) open, shell {got.get('shell', '?')}:"]
    for term in open_now:
        marks = ["active"] if term["active"] else []
        marks.append("output readable" if term["reads_output"] else "output not readable")
        lines.append(f"  {term['index']}. {term['name']} ({', '.join(marks)})")
    return "\n".join(lines)


def _describe_tabs(got: dict) -> str:
    found = got.get("tabs", [])
    if not found:
        return "No tabs are open."
    lines = [f"{len(found)} tab(s) open:"]
    for tab in found:
        marks = []
        if tab["active"]:
            marks.append("active")
        if tab["dirty"]:
            marks.append("unsaved")
        suffix = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"  [{tab['group']}] {tab['label']}{suffix}  {tab['file']}")
    return "\n".join(lines)


def _describe_fixes(got: dict) -> str:
    found = got.get("fixes", [])
    if not found:
        return f"Nothing is offered at {got['file']}:{got['line']}."
    lines = [f"Offered at {got['file']}:{got['line']}:"]
    for fix in found:
        kind = f"  [{fix['kind']}]" if fix.get("kind") else ""
        lines.append(f"  {fix['index']}. {fix['title']}{kind}")
    return "\n".join(lines)


def _describe_tasks(got: dict) -> str:
    found = got.get("tasks", [])
    if not found:
        return "No tasks are defined in this workspace."
    lines = [f"{len(found)} task(s):"]
    for task in found:
        group = f"  ({task['group']})" if task.get("group") else ""
        lines.append(f"  {task['index']}. {task['name']}  from {task['source']}{group}")
    return "\n".join(lines)


# --- looking ---------------------------------------------------------------

_LOOK = (
    "state", "read", "diagnostics", "symbols", "definition", "references",
    "implementations", "hover", "fixes", "commands", "terminals", "tabs",
    "settings", "tasks",
)


@beta_tool
@scrubbed
def vscode_inspect(
    op: str,
    file: str = "",
    symbol: str = "",
    line: int = 0,
    column: int = 0,
    query: str = "",
    contains: str = "",
    key: str = "",
    from_line: int = 0,
    to_line: int = 0,
    limit: int = 0,
) -> str:
    """Look at what is happening in the user's VS Code, without changing it.

    This sees what the filesystem cannot: what the language server knows, what
    the user has selected, and the contents of files as they are RIGHT NOW
    including edits they have not saved.

    If several VS Code windows are open, this talks to the most recently
    focused one - the one in front of the user.

    Reading:
      state       What is open: folders, the active file, cursor line and
                  column, the selection, which files are unsaved.
      read        The text of a file AS THE EDITOR HAS IT, with line numbers,
                  unsaved changes included. Use this rather than read_file for
                  anything open in VS Code: read_file returns what is on disk,
                  and if the user has unsaved edits the line numbers will not
                  match what vscode_change acts on. Narrow with from_line/to_line.

    Understanding code (this is what the language server knows, not guesswork):
      symbols     The outline of a file - its functions, classes, methods, with
                  line numbers. With `query` instead, searches the whole project
                  for a symbol by name.
      definition  Where the thing at a position is defined. Say `symbol` and it
                  finds it for you; or give line (and column).
      references  Everywhere the thing at a position is used. Answers "what
                  calls this" and "is this still used".
      implementations  Implementations of an interface or abstract method.
      hover       Type information and documentation at a position - the same
                  thing the user sees when hovering.
      fixes       The quick fixes the language server offers at a line, numbered
                  so vscode_change can apply one. Use this on a line that has an
                  error before writing your own fix: the language server's
                  suggestion is usually right and always cheaper.

    The rest:
      diagnostics The errors and warnings from the Problems panel. Trust these
                  over your own reading, and check again after a fix.
      commands    Search command palette ids. Needs `contains`.
      terminals   The open terminals, numbered as VS Code numbers them.
      tabs        The tabs actually open, which is not the same as the files
                  loaded in memory that `state` reports.
      settings    The value of one VS Code setting. Needs `key`.
      tasks       The tasks defined in this workspace, for vscode_change to run.

    Args:
        op: Which of the operations above.
        file: Full path to act on. Defaults to the active file.
        symbol: For definition/references/hover, the name to look up. Usually
            easier and more reliable than working out a line and column.
        line: A 1-based line number, when you know it.
        column: A 1-based column. Rarely needed if you pass `symbol`.
        query: For symbols, search the whole project instead of one file.
        contains: For commands, the text to match command ids against.
        key: For settings, which setting, e.g. "editor.fontSize".
        from_line: For read, the first line to return.
        to_line: For read, the last line to return.
        limit: Cap on results. 0 uses a sensible default.
    """
    op = (op or "").strip().lower()
    if op not in _LOOK:
        return f"Unknown operation {op!r}. Use one of: {', '.join(_LOOK)}."

    args: dict = {}
    for name, value in (
        ("file", file), ("symbol", symbol), ("line", line), ("column", column),
        ("query", query), ("contains", contains), ("key", key),
        ("from", from_line), ("to", to_line), ("limit", limit),
    ):
        if value:
            args[name] = value

    if op == "commands" and not contains:
        return "Searching every command returns thousands. Give `contains` a word to match."
    if op == "settings" and not key:
        return "Which setting? Give `key`, e.g. editor.fontSize."

    got = _call(op, **args)
    if isinstance(got, str):
        return got

    if op == "state":
        return _describe_state(got)
    if op == "read":
        return _describe_read(got)
    if op == "diagnostics":
        return _describe_problems(got)
    if op == "symbols":
        return _describe_symbols(got)
    if op in ("definition", "references", "implementations"):
        return _describe_places(got, {"definition": "definition(s)"}.get(op, op))
    if op == "hover":
        return got.get("text") or "The language server has nothing to say about that."
    if op == "fixes":
        return _describe_fixes(got)
    if op == "terminals":
        return _describe_terminals(got)
    if op == "tabs":
        return _describe_tabs(got)
    if op == "tasks":
        return _describe_tasks(got)
    if op == "settings":
        return f"{got['key']} = {json.dumps(got['value'])}"

    found = got.get("commands", [])
    if not found:
        return f"No command ids matching {contains!r}."
    return f"{got['total']} matching command(s), showing {len(found)}:\n  " + "\n  ".join(found)


# --- changing --------------------------------------------------------------

_CHANGE = (
    "open", "edit", "insert", "save", "save_all", "create", "delete",
    "rename_file", "rename_symbol", "apply_fix", "close_tab", "setting",
    "command", "terminal", "run_task", "breakpoint", "start_debugging",
)


@beta_tool
@confirm("confirm")
def vscode_change(
    op: str,
    file: str = "",
    text: str = "",
    line: int = 0,
    line_start: int = 0,
    line_end: int = 0,
    to: str = "",
    symbol: str = "",
    new_name: str = "",
    title: str = "",
    key: str = "",
    value: str = "",
    scope: str = "",
    name: str = "",
    command_id: str = "",
    command_args: str = "",
    index: int = 0,
    run: bool = True,
    every: bool = False,
) -> str:
    """Change something in the user's VS Code.

    Everything here goes through the editor rather than the filesystem, which
    means it applies over unsaved changes instead of fighting them, and Ctrl+Z
    undoes it like anything the user typed. Prefer this over edit_file and
    write_file for anything open in VS Code.

    Line numbers are 1-based and inclusive. Before editing by line number, read
    the file with vscode_inspect("read") - NOT read_file, which returns what is
    on disk and will be a different shape if there are unsaved changes.

    Editing:
      open      Bring a file up, optionally scrolled to `line`.
      edit      Replace lines `line_start` to `line_end` with `text`. Empty
                `text` deletes them.
      insert    Insert `text` before `line`.
      save      Save the active file, or `file`.
      save_all  Save every file with unsaved changes.

    Letting the language server do the work - prefer these over hand-editing:
      apply_fix     Apply one of the fixes vscode_inspect("fixes") listed, by
                    `title` or by `index`. This is how you act on an error:
                    list the fixes, then apply the right one.
      rename_symbol Rename a symbol everywhere it appears, across every file,
                    with `new_name`. Say `symbol` to point at it. Far safer
                    than find-and-replace, which cannot tell a variable from
                    the same word in a comment.

    Files (undoable, and language servers update imports for you):
      create      Make a new file, with optional `text`.
      delete      Delete a file.
      rename_file Rename or move `file` to `to`.

    The editor itself:
      close_tab Close the tab for `file`, or the active one, or every one with
                `every` true.
      setting   Change a VS Code setting: `key` and `value`. `value` is read as
                JSON, so true/false and numbers work. `scope` "workspace" makes
                it apply to this project only, otherwise it is global.
      command   Run a command palette id. `command_args` passes an argument -
                installing an extension is
                command_id="workbench.extensions.installExtension" with
                command_args="ms-python.python". A JSON array passes several.
                This CANNOT reach commands wanting a Uri or Position object -
                the vscode.execute* family - but you do not need it to: those
                have their own operations above.
      terminal  Run `text` in the integrated terminal. Choose which with
                `index` (1, 2, 3 as VS Code numbers them) or `name`; without
                either it uses the active one. Where the shell supports it the
                output comes back in the result. `run` false types without
                pressing enter. This runs a shell command on the user's
                machine - treat it as seriously as run_shell, and quote
                phrases so PowerShell does not split them into arguments.
      run_task  Run a task from the workspace by `name`.

    Debugging:
      breakpoint      Set a breakpoint at `file` and `line`. `every` is not
                      used; pass `run` false to remove it instead of adding.
      start_debugging Start a launch configuration by `name`.

    Args:
        op: Which of the operations above.
        file: Full path to the file to act on.
        text: Replacement text, text to insert, or the terminal command.
        line: A 1-based line, for insert, open, fixes and breakpoint.
        line_start: For edit, the first line to replace.
        line_end: For edit, the last line to replace, inclusive.
        to: For rename_file, the new path.
        symbol: For rename_symbol, the name to rename.
        new_name: For rename_symbol, what to call it now.
        title: For apply_fix, which fix, matched against its title.
        key: For setting, which setting to change.
        value: For setting, the new value, read as JSON.
        scope: For setting, "workspace" to limit it to this project.
        name: For terminal, which one by name. For run_task and
            start_debugging, which task or launch configuration.
        command_id: For command, the palette id to run.
        command_args: For command, an argument it takes; a JSON array for several.
        index: For terminal, which one by number. For apply_fix, which fix.
        run: For terminal, whether to press enter. For breakpoint, false removes.
        every: For close_tab, close all of them.
    """
    op = (op or "").strip().lower()
    if op not in _CHANGE:
        return f"Unknown operation {op!r}. Use one of: {', '.join(_CHANGE)}."

    needs_file = ("open", "edit", "insert", "create", "delete", "rename_file", "breakpoint")
    if op in needs_file and not file:
        return f"{op} needs a file path."

    args: dict = {}
    if op == "open":
        args = {"file": file}
        if line:
            args["line"] = line
    elif op == "edit":
        if not line_start:
            return "edit needs line_start (and usually line_end)."
        args = {"file": file, "line_start": line_start,
                "line_end": line_end or line_start, "text": text}
    elif op == "insert":
        if not line:
            return "insert needs the line to insert before."
        if not text:
            return "insert needs text."
        args = {"file": file, "line": line, "text": text}
    elif op == "save":
        args = {"file": file} if file else {}
    elif op == "create":
        args = {"file": file}
        if text:
            args["text"] = text
    elif op == "delete":
        args = {"file": file}
    elif op == "rename_file":
        if not to:
            return "rename_file needs `to`, the new path."
        args = {"file": file, "to": to}
    elif op == "rename_symbol":
        if not new_name:
            return "rename_symbol needs new_name."
        if not symbol and not line:
            return "rename_symbol needs `symbol`, or a line to look at."
        args = {"new_name": new_name}
        for key_name, val in (("file", file), ("symbol", symbol), ("line", line)):
            if val:
                args[key_name] = val
    elif op == "apply_fix":
        args = {}
        for key_name, val in (("file", file), ("line", line), ("title", title), ("index", index)):
            if val:
                args[key_name] = val
        if not title and not index:
            return "apply_fix needs `title` or `index`; list them with vscode_inspect('fixes')."
    elif op == "close_tab":
        args = {}
        if file:
            args["file"] = file
        if every:
            args["all"] = True
    elif op == "setting":
        if not key:
            return "setting needs a key, e.g. editor.wordWrap."
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value  # a plain string like "on" is a perfectly good value
        args = {"key": key, "value": parsed}
        if scope:
            args["scope"] = scope
    elif op == "command":
        if not command_id:
            return "command needs a command_id."
        args = {"id": command_id}
        if command_args:
            try:
                parsed = json.loads(command_args)
            except ValueError:
                parsed = command_args
            args["args"] = parsed if isinstance(parsed, list) else [parsed]
    elif op == "terminal":
        if not text:
            return "terminal needs the command to type."
        args = {"text": text, "run": run}
        if index:
            args["index"] = index
        if name:
            args["name"] = name
    elif op == "run_task":
        if not name:
            return "run_task needs the task name; list them with vscode_inspect('tasks')."
        args = {"name": name}
    elif op == "breakpoint":
        if not line:
            return "breakpoint needs a line."
        args = {"file": file, "line": line}
        if not run:
            args["remove"] = True
    elif op == "start_debugging":
        if not name:
            return "start_debugging needs the name of a launch configuration."
        args = {"name": name}

    got = _call(op, **args)
    if isinstance(got, str):
        return got

    if op == "edit":
        first, last = got["replaced_lines"]
        return f"Replaced lines {first}-{last} of {got['file']}. It said:\n{got['was']}"
    if op == "insert":
        return f"Inserted before line {got['inserted_before_line']} of {got['file']}."
    if op == "open":
        return f"Opened {got['opened']} ({got['lines']} lines)."
    if op == "save":
        return f"Saved {got['saved']}." if got.get("saved") else "Nothing needed saving."
    if op == "save_all":
        return "Saved every file with unsaved changes."
    if op == "create":
        return f"Created {got['created']}."
    if op == "delete":
        return f"Deleted {got['deleted']}."
    if op == "rename_file":
        return f"Renamed {got['renamed']} to {got['to']}."
    if op == "rename_symbol":
        return f"Renamed it to {got['renamed_to']}, across {got['files_changed']} file(s)."
    if op == "apply_fix":
        return f"Applied at line {got['line']}: {got['applied']}"
    if op == "close_tab":
        return f"Closed {got['closed']} tab(s)."
    if op == "setting":
        return f"Set {got['key']} to {json.dumps(got['value'])}, {got['scope']}."
    if op == "run_task":
        return f"Started the task {got['started']} (from {got['source']})."
    if op == "breakpoint":
        if got.get("removed") is not None:
            return f"Removed {got['removed']} breakpoint(s) at {got['file']}:{got['line']}."
        return f"Breakpoint set at {got['file']}:{got['line']}."
    if op == "start_debugging":
        return f"Started debugging with {got['started']}."
    if op == "command":
        result = got.get("result")
        return f"Ran {got['ran']}." + (f" It returned: {result}" if result is not None else "")

    where = got.get("terminal") or "the terminal"
    if not got["ran"]:
        return f"Typed into {where} without running it: {got['sent']}"

    # None means the shell could not report its output at all; empty means it
    # ran and printed nothing. Collapsing the two is how a command that failed
    # silently gets reported as having worked.
    output = got.get("output")
    if output:
        return f"Ran in {where}: {got['sent']}\n\nIt printed:\n{output}"
    if output == "":
        return f"Ran in {where}: {got['sent']}\nIt printed nothing."
    return (
        f"Ran in {where}: {got['sent']}\n"
        "This terminal cannot report its output, so say you are unable to see "
        "what it printed rather than assuming it worked."
    )


TOOLS = [vscode_inspect, vscode_change]
