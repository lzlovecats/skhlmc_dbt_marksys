self.addEventListener("install", function () {
    self.skipWaiting();
});

self.addEventListener("activate", function (event) {
    event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
    let data = {};

    if (event.data) {
        try {
            data = event.data.json();
        } catch (error) {
            data = { body: event.data.text() };
        }
    }

    const title = data.title || "聖呂中辯";
    const options = {
        body: data.body || "",
        icon: "/app-icon-192.png",
        badge: "/app-icon-192.png",
        data: {
            url: data.url || "/",
        },
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
    event.notification.close();

    const rawTargetUrl = event.notification.data && event.notification.data.url
        ? event.notification.data.url
        : "/";
    const targetUrl = new URL(rawTargetUrl, self.location.origin).href;

    event.waitUntil((async function () {
        const windowClients = await self.clients.matchAll({
            type: "window",
            includeUncontrolled: true,
        });

        for (const client of windowClients) {
            if ("focus" in client) {
                client.navigate(targetUrl);
                return client.focus();
            }
        }

        if (self.clients.openWindow) {
            return self.clients.openWindow(targetUrl);
        }
    })());
});
