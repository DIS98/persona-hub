(function () {
  var root = document.documentElement;
  var stored = null;
  try { stored = localStorage.getItem('theme'); } catch (e) {}
  var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  root.setAttribute('data-theme', stored || (dark ? 'dark' : 'light'));
  var btn = document.getElementById('themeToggle');
  if (btn) {
    btn.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (e) {}
      btn.textContent = next === 'dark' ? '☀' : '☾';
      btn.setAttribute('aria-label', next === 'dark' ? '切换到浅色' : '切换到深色');
    });
    btn.textContent = root.getAttribute('data-theme') === 'dark' ? '☀' : '☾';
  }
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { threshold: 0.08 });
    document.querySelectorAll('.reveal').forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
  }
})();
