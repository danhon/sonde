// Tailwind 3.4.17, the version the Play CDN served when sonde moved off it
// (sonde#6). No theme changes: the pages were designed against the defaults.
// Content covers every place a class name can be written, including the
// classes sonde.js sets at runtime.
module.exports = {
  content: [
    "./sonde/web/templates/**/*.html",
    "./sonde/web/static/**/*.js",
    "./sonde/**/*.py",
  ],
};
