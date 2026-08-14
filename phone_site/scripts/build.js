const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');

const root = path.resolve(__dirname, '..');
const publicDir = path.join(root, 'public');
const distDir = path.join(root, 'dist');
const clientDir = path.join(distDir, 'client');
const serverDir = path.join(distDir, 'server');

fs.rmSync(publicDir, { recursive: true, force: true });
fs.mkdirSync(publicDir, { recursive: true });

// The Director client. The old aiOS Remote pages (phone.*, coding.*) are still
// in the repo but are no longer deployed — Director replaces them.
for (const name of [
  'index.html',
  'director.css',
  'director.js',
  'inline-markdown.js',
  'manifest.webmanifest',
  'sw.js',
  'og-aios-remote.png',
]) {
  fs.copyFileSync(path.join(root, name), path.join(publicDir, name));
}

// CODE transcript renderer. Prefer the live aiOS copy when this checkout
// has it; otherwise use the vendored files in ./code so a Vercel build
// (which only sees phone_site/) still ships them.
const codePublic = path.join(publicDir, 'code');
fs.mkdirSync(codePublic, { recursive: true });
const aiosWeb = path.resolve(root, '..', 'aios_ui', 'web');
const localCode = path.join(root, 'code');
for (const [from, to] of [
  ['js/transcript.js', 'transcript.js'],
  ['js/markdown.js', 'markdown.js'],
  ['css/code.css', 'code.css'],
  ['css/code-beautiful.css', 'code-beautiful.css'],
]) {
  const aiosPath = path.join(aiosWeb, from);
  const localPath = path.join(localCode, to);
  const src = fs.existsSync(aiosPath) ? aiosPath : localPath;
  if (!fs.existsSync(src)) {
    throw new Error(`missing CODE asset ${to} (looked in ${aiosPath} and ${localPath})`);
  }
  fs.copyFileSync(src, path.join(codePublic, to));
}

fs.cpSync(path.join(root, 'icons'), path.join(publicDir, 'icons'), { recursive: true });
const fontsDir = path.join(root, 'fonts');
if (fs.existsSync(fontsDir)) {
  fs.cpSync(fontsDir, path.join(publicDir, 'fonts'), { recursive: true });
}

const extraLogo = path.resolve(root, '..', 'assets', 'aios-logo.png');
if (fs.existsSync(extraLogo)) fs.copyFileSync(extraLogo, path.join(publicDir, 'icons', 'aios-logo.png'));

function gitValue(args, fallback = '') {
  try {
    return childProcess.execFileSync('git', args, {
      cwd: path.resolve(root, '..'),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return fallback;
  }
}

const commit = process.env.GITHUB_SHA || gitValue(['rev-parse', 'HEAD']);
const committedAt = gitValue(['show', '-s', '--format=%cI', commit || 'HEAD']);
fs.writeFileSync(path.join(publicDir, 'version.json'), JSON.stringify({
  commit,
  committed_at: committedAt,
  built_at: new Date().toISOString(),
}, null, 2));

fs.rmSync(distDir, { recursive: true, force: true });
fs.cpSync(publicDir, clientDir, { recursive: true });
fs.mkdirSync(serverDir, { recursive: true });
fs.copyFileSync(path.join(root, 'worker', 'index.js'), path.join(serverDir, 'index.js'));
