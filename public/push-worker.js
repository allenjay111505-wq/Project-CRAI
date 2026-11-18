self.addEventListener('push', event => {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (error) {
      payload = { body: event.data.text() };
    }
  }
  const title = payload.title || 'C.R.A.I.';
  const options = {
    body: payload.body || 'You have an update.',
    icon: '/default-avatar.svg',
    badge: '/default-avatar.svg',
    data: { url: payload.url || '/' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data.url || '/', self.location.origin).href;
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      const existing = windowClients.find(client => client.url === targetUrl);
      if (existing) return existing.focus();
      return clients.openWindow(targetUrl);
    })
  );
});
