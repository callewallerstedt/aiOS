const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const publicDir = path.join(root, 'public');

fs.rmSync(publicDir, { recursive: true, force: true });
fs.mkdirSync(publicDir, { recursive: true });

for (const name of ['index.html', 'phone.css', 'phone.js', 'manifest.webmanifest', 'backend.json']) {
  fs.copyFileSync(path.join(root, name), path.join(publicDir, name));
}

fs.cpSync(path.join(root, 'icons'), path.join(publicDir, 'icons'), { recursive: true });

const extraLogo = path.resolve(root, '..', 'assets', 'aios-logo.png');
if (fs.existsSync(extraLogo)) fs.copyFileSync(extraLogo, path.join(publicDir, 'icons', 'aios-logo.png'));
