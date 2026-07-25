const fs = require('fs');
const path = require('path');

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

fs.rmSync(distDir, { recursive: true, force: true });
fs.cpSync(publicDir, clientDir, { recursive: true });
fs.mkdirSync(serverDir, { recursive: true });
fs.copyFileSync(path.join(root, 'worker', 'index.js'), path.join(serverDir, 'index.js'));
