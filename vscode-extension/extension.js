// sqldoc VS Code extension.
//
// Adds right-click / command-palette entries that run the sqldoc CLI against a
// configured database and render its self-contained dark-themed HTML report in
// a VS Code webview panel. Plain CommonJS — no build step.
const vscode = require('vscode');
const cp = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

function workspaceRoot() {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length ? folders[0].uri.fsPath : process.cwd();
}

function getConfig() {
  const cfg = vscode.workspace.getConfiguration('sqldoc');
  return {
    connectionString: (cfg.get('connectionString') || '').trim(),
    dialect: (cfg.get('dialect') || '').trim(),
    sqldocPath: cfg.get('sqldocPath') || 'sqldoc',
    documentArgs: cfg.get('documentArgs') || ['--no-ai'],
  };
}

// Very small .sqldoc.yml reader: pulls connection_string / dialect without a
// YAML dependency (keeps the extension dependency-free). Falls back silently.
function readSqldocYml() {
  try {
    const p = path.join(workspaceRoot(), '.sqldoc.yml');
    if (!fs.existsSync(p)) return {};
    const out = {};
    for (const raw of fs.readFileSync(p, 'utf8').split(/\r?\n/)) {
      const m = raw.match(/^\s*(connection_string|dialect)\s*:\s*(.+?)\s*$/);
      if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
    return out;
  } catch (e) {
    return {};
  }
}

async function resolveConnection() {
  const cfg = getConfig();
  let connectionString = cfg.connectionString;
  let dialect = cfg.dialect;
  if (!connectionString) {
    const yml = readSqldocYml();
    connectionString = yml.connection_string || '';
    dialect = dialect || yml.dialect || '';
  }
  if (!connectionString) {
    connectionString = await vscode.window.showInputBox({
      prompt: 'sqldoc: enter the database connection string',
      ignoreFocusOut: true,
      placeHolder: 'e.g. postgresql://user:pass@host:5432/db',
    });
    connectionString = (connectionString || '').trim();
  }
  return { connectionString, dialect, sqldocPath: cfg.sqldocPath, documentArgs: cfg.documentArgs };
}

function tmpReport(kind) {
  // Timestamp keeps successive runs distinct so a stale webview isn't reused.
  return path.join(os.tmpdir(), `sqldoc-${kind}-${process.pid}-${Date.now()}.html`);
}

function runSqldoc(sqldocPath, command, args) {
  return new Promise((resolve, reject) => {
    // shell:true lets sqldocPath be "sqldoc" or "python -m sqldoc.cli".
    const child = cp.spawn(`${sqldocPath} ${command} ${args.map(quote).join(' ')}`, {
      shell: true,
      cwd: workspaceRoot(),
    });
    let stderr = '';
    child.stderr.on('data', (d) => { stderr += d.toString(); });
    child.on('error', (err) => reject(err));
    child.on('close', (code) => {
      if (code === 0) resolve();
      else reject(new Error(stderr.trim() || `sqldoc exited with code ${code}`));
    });
  });
}

function quote(a) {
  return /[\s"']/.test(a) ? `"${a.replace(/"/g, '\\"')}"` : a;
}

// VS Code webviews apply a strict default CSP that blocks the report's inline
// <style>/<script>. Inject a local-only CSP that permits inline styles/scripts
// and data: images (the reports are self-contained — no external resources).
function injectCsp(html) {
  const csp = '<meta http-equiv="Content-Security-Policy" '
    + "content=\"default-src 'none'; style-src 'unsafe-inline'; "
    + "script-src 'unsafe-inline'; img-src data:; font-src data:;\">";
  if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, (m) => m + csp);
  return csp + html;
}

function showReport(title, htmlPath) {
  const panel = vscode.window.createWebviewPanel(
    'sqldocReport', title, vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  panel.webview.html = injectCsp(fs.readFileSync(htmlPath, 'utf8'));
  return panel;
}

async function runReport(command, label, extra) {
  const conn = await resolveConnection();
  if (!conn.connectionString) {
    vscode.window.showWarningMessage('sqldoc: no connection string provided.');
    return;
  }
  const out = tmpReport(command);
  const args = ['--connection-string', conn.connectionString, '--output', out];
  if (conn.dialect) args.push('--dialect', conn.dialect);
  if (extra && extra.length) args.push(...extra);

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `sqldoc: ${label}…`, cancellable: false },
    async () => {
      try {
        await runSqldoc(conn.sqldocPath, command, args);
        showReport(`sqldoc — ${label}`, out);
      } catch (err) {
        vscode.window.showErrorMessage(
          `sqldoc ${command} failed: ${err.message}. Is sqldoc installed (pip install sqldoc) and on PATH?`
        );
      }
    }
  );
}

async function documentDatabase() {
  const conn = getConfig();
  return runReport('doc', 'Documentation', conn.documentArgs);
}

async function scanPII() {
  return runReport('scan', 'PII scan', ['--yes']);
}

async function healthCheck() {
  return runReport('health', 'Health check');
}

// View Documentation: open an existing documentation.html in the workspace if
// present; otherwise generate one.
async function viewDocumentation() {
  const candidate = path.join(workspaceRoot(), 'documentation.html');
  if (fs.existsSync(candidate)) {
    showReport('sqldoc — Documentation', candidate);
    return;
  }
  vscode.window.showInformationMessage('sqldoc: no documentation.html found — generating one…');
  return documentDatabase();
}

function activate(context) {
  const reg = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  reg('sqldoc.documentDatabase', documentDatabase);
  reg('sqldoc.scanPII', scanPII);
  reg('sqldoc.healthCheck', healthCheck);
  reg('sqldoc.viewDocumentation', viewDocumentation);
}

function deactivate() {}

module.exports = { activate, deactivate };
