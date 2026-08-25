const { rcedit } = require("rcedit");

const [exe, icon] = process.argv.slice(2);
if (!exe || !icon) {
  console.error("usage: node patch-exe-icon.cjs <exe> <icon>");
  process.exit(1);
}

rcedit(exe, { icon })
  .then(() => {
    console.log("patched", exe);
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
