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

for (const name of ['index.html', 'phone.css', 'phone.js', 'manifest.webmanifest', 'sw.js', 'og-aios-remote.png']) {
  fs.copyFileSync(path.join(root, name), path.join(publicDir, name));
}

fs.cpSync(path.join(root, 'icons'), path.join(publicDir, 'icons'), { recursive: true });

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
