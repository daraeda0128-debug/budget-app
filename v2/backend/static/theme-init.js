(() => {
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const preference = () => localStorage.getItem('wallet-theme') || 'system';

  function apply() {
    const selected = preference();
    const dark = selected === 'dark' || (selected === 'system' && media.matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const color = dark ? '#141916' : '#f6f7f5';
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = color;
    const button = document.getElementById('theme-toggle');
    if (button) {
      button.textContent = dark ? '☀' : '☾';
      button.title = dark ? '라이트 모드로 전환' : '다크 모드로 전환';
      button.setAttribute('aria-label', dark ? '라이트 모드 사용' : '다크 모드 사용');
      button.setAttribute('aria-pressed', String(dark));
    }
  }

  media.addEventListener('change', () => {
    if (preference() === 'system') apply();
  });

  window.walletTheme = {
    apply,
    toggle() {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      localStorage.setItem('wallet-theme', next);
      apply();
    },
  };

  apply();
})();
