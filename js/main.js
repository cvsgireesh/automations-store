const PRODUCT = Object.freeze({
  slug: 'hermes-hybrid-operator-kit',
  price: 12,
  checkoutUrl: '',
  status: 'pending'
});

const isGumroadProductUrl = (value) => {
  try {
    if (typeof value !== 'string' || value !== value.trim()) return false;
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    const isGumroadHost = hostname === 'gumroad.com' || hostname.endsWith('.gumroad.com');
    const productPath = /^\/l\/([A-Za-z0-9][A-Za-z0-9_-]*)$/.test(url.pathname);
    return url.protocol === 'https:'
      && !url.username
      && !url.password
      && !url.port
      && !url.search
      && !url.hash
      && isGumroadHost
      && productPath;
  } catch {
    return false;
  }
};

const checkoutIsOpen = PRODUCT.status === 'ready' && isGumroadProductUrl(PRODUCT.checkoutUrl);

document.querySelectorAll('[data-checkout-cta]').forEach((cta) => {
  if (checkoutIsOpen) {
    cta.href = PRODUCT.checkoutUrl;
    cta.textContent = `Buy for $${PRODUCT.price}`;
  } else {
    cta.href = 'order.html';
    cta.textContent = 'Checkout setup status';
  }
});

document.querySelectorAll('[data-checkout-message]').forEach((message) => {
  message.textContent = checkoutIsOpen
    ? `Checkout status: open for $${PRODUCT.price}.`
    : 'Checkout status: pending.';
});

const navToggle = document.querySelector('.nav-toggle');
const navigation = document.querySelector('.site-nav');

if (navToggle && navigation) {
  navToggle.addEventListener('click', () => {
    const expanded = navToggle.getAttribute('aria-expanded') === 'true';
    navToggle.setAttribute('aria-expanded', String(!expanded));
    navigation.classList.toggle('is-open', !expanded);
  });
}
