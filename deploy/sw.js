// Injected by the /sw.js route from the server's VAPID_PUBLIC_KEY (empty if
// unconfigured). Used as a fallback to re-subscribe when the browser rotates
// the push endpoint but doesn't hand us the old applicationServerKey.
const VAPID_PUBLIC_KEY = "__VAPID_PUBLIC_KEY__";

function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = self.atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) {
        output[i] = raw.charCodeAt(i);
    }
    return output;
}

self.addEventListener("install", function () {
    self.skipWaiting();
});

self.addEventListener("activate", function (event) {
    event.waitUntil(self.clients.claim());
});

function retryDelay(milliseconds) {
    return new Promise(function (resolve) {
        setTimeout(resolve, milliseconds);
    });
}

async function migrateSubscription(oldEndpoint, newSubscription) {
    if (!oldEndpoint || !newSubscription) return false;

    let lastError = null;
    for (let attempt = 0; attempt < 3; attempt++) {
        try {
            const response = await fetch("/api/push/resubscribe", {
                method: "POST",
                credentials: "omit",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    old_endpoint: oldEndpoint,
                    subscription: newSubscription.toJSON(),
                }),
            });
            if (response.ok) return true;
            lastError = new Error("Subscription migration failed: " + response.status);
            if (response.status < 500 && response.status !== 429) break;
        } catch (error) {
            lastError = error;
        }
        if (attempt < 2) await retryDelay(500 * Math.pow(2, attempt));
    }
    console.warn("Push subscription migration will be repaired on next login", lastError);
    return false;
}

// Chrome/FCM periodically rotate the push endpoint. Without this handler the
// old endpoint silently dies, the server prunes it on the next 410, and the
// member's notifications stay off until they manually re-enable — the "隔一陣
//自己熄" symptom. Re-subscribe and tell the server to migrate the DB row from
// the old endpoint to the new one (keying on the old endpoint, no auth needed).
self.addEventListener("pushsubscriptionchange", function (event) {
    event.waitUntil((async function () {
        const oldSub = event.oldSubscription || null;
        const oldEndpoint = oldSub ? oldSub.endpoint : null;

        let newSub = event.newSubscription || null;
        if (!newSub) {
            let appKey = null;
            if (oldSub && oldSub.options && oldSub.options.applicationServerKey) {
                appKey = oldSub.options.applicationServerKey;  // ArrayBuffer, accepted as-is
            } else if (VAPID_PUBLIC_KEY) {
                appKey = urlBase64ToUint8Array(VAPID_PUBLIC_KEY);
            }
            if (!appKey) return;
            try {
                newSub = await self.registration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: appKey,
                });
            } catch (error) {
                return;  // best effort; nothing more we can do in the SW
            }
        }
        if (!newSub) return;

        await migrateSubscription(oldEndpoint, newSub);
    })());
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
        tag: data.tag || undefined,
        renotify: Boolean(data.tag),
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
